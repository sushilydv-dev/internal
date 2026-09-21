"""
Zoho WorkDrive client: browse the resume folder tree, download resumes, and
permanently delete a resume ONLY after it passes strict safety checks.

Folder layout:  ROOT / <role> / <date> / resume files
Everything is confined to config.WORKDRIVE_ROOT_FOLDER_ID: browsing a folder
outside it is refused, and the delete guard re-verifies ancestry live.

Docs referenced:
  List folder:  GET   {WORKDRIVE_API_BASE}/files/{folder_id}/files
  File info:    GET   {WORKDRIVE_API_BASE}/files/{file_id}
  Download:     GET   {WORKDRIVE_API_BASE}/download/{file_id}
  Trash:        PATCH {WORKDRIVE_API_BASE}/files/{file_id}   status "51"
  Delete:       PATCH {WORKDRIVE_API_BASE}/files/{file_id}   status "61" (from trash)

attributes.download_url is deliberately NOT used for downloads: it targets the
browser shard (download-accl.zoho.com), which rejects OAuth tokens with
401 INVALID_OAUTHSCOPE.
"""
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import config
from zoho_auth import auth_header


class WorkDriveError(Exception):
    pass


class OutsideRootError(WorkDriveError):
    pass


PAGE_LIMIT = 100
STATUS_ACTIVE = 1
STATUS_TRASHED = 51
_JSONAPI = "application/vnd.api+json"


def _is_resume(name: str) -> bool:
    return os.path.splitext(name or "")[1].lower() in config.SUPPORTED_RESUME_EXTENSIONS


def _get(url: str, **kwargs) -> requests.Response:
    try:
        return requests.get(url, headers=auth_header(), timeout=kwargs.pop("timeout", 30), **kwargs)
    except requests.exceptions.RequestException as e:
        raise WorkDriveError(f"Could not reach WorkDrive API ({url}): {e}")


# ---------------------------------------------------------------- reading

def get_file(file_id: str) -> dict:
    """Live metadata for one file or folder: {id, name, is_folder, parent_id,
    status, size, extn}. Raises WorkDriveError if it can't be read."""
    if not file_id:
        raise WorkDriveError("Missing file id.")
    resp = _get(f"{config.WORKDRIVE_API_BASE}/files/{file_id}")
    if resp.status_code != 200:
        raise WorkDriveError(f"Could not read WorkDrive item {file_id}: HTTP {resp.status_code} {resp.text[:200]}")
    try:
        data = resp.json()["data"]
        a = data["attributes"]
    except (ValueError, KeyError, TypeError):
        raise WorkDriveError(f"Unexpected metadata response for {file_id}.")
    return {
        "id": data.get("id"),
        "name": a.get("name", ""),
        "is_folder": bool(a.get("is_folder")) or a.get("type") == "folder",
        "parent_id": a.get("parent_id"),
        "status": int(a["status"]) if str(a.get("status", "")).isdigit() else a.get("status"),
        "size": (a.get("storage_info") or {}).get("size_in_bytes"),
        "extn": a.get("extn", ""),
        "modified": a.get("modified_time_in_millisecond"),
    }


def list_children(folder_id: str) -> dict:
    """{"folders": [...], "files": [...]} - files are resumes only."""
    url = f"{config.WORKDRIVE_API_BASE}/files/{folder_id}/files"
    folders, files = [], []
    # page[offset] is a 0-indexed RECORD offset, not a page number.
    offset = 0
    while True:
        resp = _get(url, params={"page[limit]": PAGE_LIMIT, "page[offset]": offset})
        if resp.status_code != 200:
            raise WorkDriveError(f"Could not list folder '{folder_id}': HTTP {resp.status_code} {resp.text[:200]}")
        try:
            items = resp.json().get("data", [])
        except ValueError:
            raise WorkDriveError(f"WorkDrive returned a non-JSON response (HTTP {resp.status_code}).")
        if not items:
            break
        for item in items:
            a = item.get("attributes", {})
            name = a.get("name", "")
            if a.get("is_folder") or a.get("type") == "folder":
                folders.append({"id": item["id"], "name": name,
                                "modified": a.get("modified_time_in_millisecond")})
            elif _is_resume(name):
                files.append({
                    "id": item["id"],
                    "name": name,
                    "size": (a.get("storage_info") or {}).get("size_in_bytes"),
                    "extn": a.get("extn", ""),
                    "modified": a.get("modified_time_in_millisecond"),
                    "uploaded_by": (a.get("created_by") or ""),
                })
        if len(items) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT

    folders.sort(key=lambda f: f.get("modified") or 0, reverse=True)
    files.sort(key=lambda f: f["name"].lower())
    return {"folders": folders, "files": files}


_FOLDER_CACHE_TTL = 300
_folder_cache: dict[str, tuple[float, dict]] = {}
_folder_cache_lock = threading.Lock()


def _folder_meta_cached(item_id: str) -> dict:
    """get_file with a short cache, for NAVIGATION ONLY (breadcrumbs). Folder
    names/parents rarely change and each live lookup costs 1-2s. The delete
    guard never uses this - it always re-reads live metadata."""
    now = time.time()
    with _folder_cache_lock:
        hit = _folder_cache.get(item_id)
        if hit and now - hit[0] < _FOLDER_CACHE_TTL:
            return hit[1]
    meta = get_file(item_id)
    with _folder_cache_lock:
        _folder_cache[item_id] = (now, meta)
    return meta


def resolve_path(folder_id: str) -> list[dict]:
    """Breadcrumb from ROOT down to folder_id: [{id, name}, ...]. Raises
    OutsideRootError if the folder is not inside the configured root."""
    root = config.WORKDRIVE_ROOT_FOLDER_ID
    chain, current = [], folder_id
    for depth in range(config.MAX_FOLDER_DEPTH + 1):
        meta = _folder_meta_cached(current)
        if not meta["is_folder"]:
            if depth == 0:
                raise WorkDriveError(f"'{meta['name']}' is a file, not a folder.")
            # Walked above every folder (e.g. into the workspace library)
            # without meeting ROOT.
            break
        chain.append({"id": meta["id"], "name": meta["name"]})
        if current == root:
            chain.reverse()
            return chain
        if not meta["parent_id"]:
            break
        current = meta["parent_id"]
    raise OutsideRootError("That folder is not inside the configured resumes root folder.")


def browse(folder_id: str | None) -> dict:
    """Everything the UI needs to render one folder level."""
    folder_id = folder_id or config.WORKDRIVE_ROOT_FOLDER_ID
    with ThreadPoolExecutor(max_workers=2) as pool:
        path_future = pool.submit(resolve_path, folder_id)
        children_future = pool.submit(list_children, folder_id)
        path = path_future.result()  # raises OutsideRootError before anything is returned
        children = children_future.result()

    # Per-subfolder counts, fetched in parallel so a role with many date
    # folders still loads quickly.
    def count(f):
        try:
            c = list_children(f["id"])
            return {**f, "folder_count": len(c["folders"]), "resume_count": len(c["files"])}
        except WorkDriveError as e:
            return {**f, "folder_count": None, "resume_count": None, "error": str(e)}

    with ThreadPoolExecutor(max_workers=8) as pool:
        subfolders = list(pool.map(count, children["folders"]))

    return {
        "folder": path[-1],
        "path": path,
        "depth": len(path) - 1,  # 0 = root, 1 = role, 2 = date
        "role": path[1] if len(path) > 1 else None,
        "subfolders": subfolders,
        "files": children["files"],
    }


def download_file(file_id: str) -> bytes:
    if not file_id:
        raise WorkDriveError("This file has no id from Zoho - cannot download it.")
    url = f"{config.WORKDRIVE_API_BASE}/download/{file_id}"
    resp = _get(url, timeout=90)
    if resp.status_code != 200:
        raise WorkDriveError(f"Could not download file: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.content


# ---------------------------------------------------------------- deleting

def verify_safe_to_delete(file_id: str, *, expected_folder_id: str, expected_name: str,
                          expected_size: int, allowed_statuses=(STATUS_ACTIVE,)) -> tuple[bool, str]:
    """Live re-check that file_id is exactly the resume this run processed and
    sits inside the resumes root. Returns (ok, reason). Any error = not safe."""
    root = config.WORKDRIVE_ROOT_FOLDER_ID
    try:
        if not file_id or file_id == root or file_id == expected_folder_id:
            return False, "refusing to delete a folder id"
        if expected_folder_id == root:
            return False, "refusing to delete files directly in the root folder"

        meta = get_file(file_id)
        if meta["is_folder"]:
            return False, "item is a folder, not a file"
        if not _is_resume(meta["name"]):
            return False, f"'{meta['name']}' is not a .pdf/.docx resume"
        if meta["name"] != expected_name:
            return False, f"name changed ('{expected_name}' -> '{meta['name']}')"
        if meta["size"] is None or int(meta["size"]) != int(expected_size):
            return False, f"size changed ({expected_size} -> {meta['size']} bytes)"
        if meta["status"] not in allowed_statuses:
            return False, f"unexpected WorkDrive status {meta['status']}"
        if meta["parent_id"] != expected_folder_id:
            return False, "file was moved out of the screened folder"

        current = meta["parent_id"]
        for _ in range(config.MAX_FOLDER_DEPTH):
            if current == root:
                return True, "verified inside resumes root"
            parent = get_file(current)
            if not parent["is_folder"] or not parent["parent_id"]:
                break
            current = parent["parent_id"]
        return False, "file is not inside the resumes root folder"
    except WorkDriveError as e:
        return False, f"could not verify: {e}"


def _set_status(file_id: str, status: int) -> requests.Response:
    url = f"{config.WORKDRIVE_API_BASE}/files/{file_id}"
    body = {"data": {"attributes": {"status": str(status)}, "type": "files"}}
    try:
        return requests.patch(
            url,
            headers={**auth_header(), "Content-Type": _JSONAPI, "Accept": _JSONAPI},
            json=body,
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        raise WorkDriveError(f"Could not reach WorkDrive API ({url}): {e}")


def delete_permanently(file_id: str, *, expected_folder_id: str, expected_name: str,
                       expected_size: int) -> None:
    """Trash then permanently delete, re-verifying before each step. Raises
    WorkDriveError (file untouched, or at worst recoverable in trash) on any
    failed check or API error."""
    ok, reason = verify_safe_to_delete(
        file_id, expected_folder_id=expected_folder_id,
        expected_name=expected_name, expected_size=expected_size,
    )
    if not ok:
        raise WorkDriveError(f"Delete blocked: {reason}")

    resp = _set_status(file_id, STATUS_TRASHED)
    if resp.status_code not in (200, 204):
        raise WorkDriveError(f"Move to trash failed: HTTP {resp.status_code} {resp.text[:300]}")

    # In trash the parent_id is kept, so the same checks still apply.
    ok, reason = verify_safe_to_delete(
        file_id, expected_folder_id=expected_folder_id, expected_name=expected_name,
        expected_size=expected_size, allowed_statuses=(STATUS_TRASHED,),
    )
    if not ok:
        raise WorkDriveError(f"File is in WorkDrive trash, but permanent delete was blocked: {reason}")

    resp = _set_status(file_id, 61)
    if resp.status_code not in (200, 204):
        raise WorkDriveError(
            f"File is in WorkDrive trash, but permanent delete failed: HTTP {resp.status_code} {resp.text[:300]}"
        )


# ---------------------------------------------------------------- creating

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean_folder_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "")).strip()
    if not name:
        raise WorkDriveError("Folder name can't be empty.")
    if len(name) > 100:
        raise WorkDriveError("Folder name must be 100 characters or fewer.")
    if _BAD_NAME_CHARS.search(name) or name in (".", ".."):
        raise WorkDriveError('Folder name can\'t contain / \\ : * ? " < > |')
    return name


def create_folder(parent_id: str, name: str) -> dict:
    """Creates a role folder (inside ROOT) or a date folder (inside a role).
    Nothing deeper, so the ROOT / role / date layout stays intact."""
    name = clean_folder_name(name)
    path = resolve_path(parent_id)  # raises OutsideRootError outside the root
    if len(path) > 2:
        raise WorkDriveError("Folders can only be created for roles and their date folders.")
    existing = {f["name"].strip().lower() for f in list_children(parent_id)["folders"]}
    if name.lower() in existing:
        raise WorkDriveError(f"A folder named '{name}' already exists here.")

    url = f"{config.WORKDRIVE_API_BASE}/files"
    body = {"data": {"attributes": {"name": name, "parent_id": parent_id}, "type": "files"}}
    try:
        resp = requests.post(url, headers={**auth_header(), "Content-Type": _JSONAPI, "Accept": _JSONAPI},
                             json=body, timeout=30)
    except requests.exceptions.RequestException as e:
        raise WorkDriveError(f"Could not reach WorkDrive API ({url}): {e}")
    if resp.status_code not in (200, 201):
        raise WorkDriveError(_create_error("Could not create the folder", resp))
    try:
        data = resp.json()["data"]
    except (ValueError, KeyError, TypeError):
        raise WorkDriveError("WorkDrive created the folder but returned an unexpected response.")
    return {"id": data.get("id"), "name": (data.get("attributes") or {}).get("name", name)}


def _looks_like_resume(filename: str, content: bytes) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        return content[:5] == b"%PDF-"
    if ext == ".docx":
        return content[:2] == b"PK"  # .docx is a zip container
    return False


def upload_resume(folder_id: str, filename: str, content: bytes) -> dict:
    """Uploads one .pdf/.docx into a role or date folder (never ROOT).
    Existing files are never overwritten - WorkDrive renames the new copy."""
    filename = os.path.basename((filename or "").strip())
    if not _is_resume(filename):
        raise WorkDriveError(f"'{filename}' is not a .pdf or .docx file.")
    if not content:
        raise WorkDriveError(f"'{filename}' is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise WorkDriveError(f"'{filename}' is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    if not _looks_like_resume(filename, content):
        raise WorkDriveError(f"'{filename}' doesn't look like a real {os.path.splitext(filename)[1]} file.")
    path = resolve_path(folder_id)
    if len(path) < 2:
        raise WorkDriveError("Upload resumes into a role or date folder, not the root folder.")

    url = f"{config.WORKDRIVE_API_BASE}/upload"
    try:
        resp = requests.post(
            url, headers=auth_header(),
            data={"parent_id": folder_id, "filename": filename, "override-name-exist": "false"},
            files={"content": (filename, content)},
            timeout=120,
        )
    except requests.exceptions.RequestException as e:
        raise WorkDriveError(f"Could not reach WorkDrive upload API: {e}")
    if resp.status_code not in (200, 201):
        raise WorkDriveError(_create_error(f"Could not upload '{filename}'", resp))
    try:
        item = resp.json()["data"][0]
        attrs = item.get("attributes") or {}
        file_id = attrs.get("resource_id") or item.get("id")
    except (ValueError, KeyError, IndexError, TypeError):
        file_id, attrs = None, {}
    return {"id": file_id, "name": attrs.get("FileName") or attrs.get("name") or filename}


def _create_error(prefix: str, resp) -> str:
    text = resp.text[:300]
    # WorkDrive reports a missing scope as 401 or as 500 with error F7007.
    if "OAUTH SCOPE" in text.upper() or "INVALID_OAUTHSCOPE" in text.upper() or "F7007" in text:
        return (f"{prefix}: the Zoho token doesn't have WorkDrive.files.CREATE permission. "
                "Regenerate the token with that scope.")
    if resp.status_code == 409:
        return f"{prefix}: an item with that name already exists."
    return f"{prefix}: HTTP {resp.status_code} {text}"
