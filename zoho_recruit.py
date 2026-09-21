"""
Zoho Recruit client: schema-aware upsert of Candidates, attachment upload
with de-duplication, and read-back verification.

Recruit rejects the WHOLE record if any single field has the wrong type or a
value outside a picklist (e.g. Overall_score is a text field; Current_Job_Title
is a picklist). Two layers keep one bad field from losing a candidate:

1. Field metadata (needs ZohoRecruit.settings.READ): values are coerced to each
   field's real data type, truncated to its max length, matched against picklist
   options, and fields that don't exist / are read-only are dropped up front.
2. Error-driven repair (always on): if Recruit still answers INVALID_DATA for a
   field, the value is coerced to the expected type Recruit names, or the field
   is dropped, and the upsert is retried. Every dropped field is reported back
   so it lands in the audit log - nothing is dropped silently.

Docs referenced:
  Fields:     GET  {RECRUIT_API_BASE}/settings/fields?module=Candidates
  Upsert:     POST {RECRUIT_API_BASE}/Candidates/upsert
  Record:     GET  {RECRUIT_API_BASE}/Candidates/{id}
  Attachment: POST {RECRUIT_API_BASE}/Candidates/{id}/Attachments
  Notes:      GET {RECRUIT_API_BASE}/Candidates/{id}/Notes, POST /Notes, PUT /Notes/{id}
"""
import re
import time
from datetime import datetime

import requests

import config
from zoho_auth import auth_header


class RecruitError(Exception):
    pass


MAX_REPAIR_ATTEMPTS = 30
_SCHEMA_TTL_SECONDS = 600
_schema_cache = {"fields": None, "loaded_at": 0.0, "error": None}

# Types Recruit computes or links itself - never written from resume data.
_UNWRITABLE_TYPES = {"lookup", "ownerlookup", "autonumber", "formula", "userlookup",
                     "subform", "multiselectlookup", "fileupload", "imageupload", "profileimage"}
_NEVER_WRITE = {"Created_By", "Modified_By", "Created_Time", "Updated_On", "Last_Activity_Time",
                "Candidate_ID", "Candidate_Owner", "ZOHO_RECORD_ID", "Assigned_To"}


# ---------------------------------------------------------------- schema

def load_field_schema(force: bool = False):
    """Returns {api_name: field_meta} or None when metadata is unavailable
    (typically: refresh token lacks ZohoRecruit.settings.READ)."""
    fresh = time.time() - _schema_cache["loaded_at"] < _SCHEMA_TTL_SECONDS
    if not force and fresh and (_schema_cache["fields"] is not None or _schema_cache["error"]):
        return _schema_cache["fields"]

    url = f"{config.RECRUIT_API_BASE}/settings/fields"
    try:
        resp = requests.get(url, headers=auth_header(), params={"module": "Candidates"}, timeout=30)
        body = resp.json() if resp.content else {}
    except (requests.exceptions.RequestException, ValueError) as e:
        _schema_cache.update(fields=None, loaded_at=time.time(), error=f"Could not load field metadata: {e}")
        return None

    if resp.status_code != 200 or "fields" not in body:
        _schema_cache.update(
            fields=None, loaded_at=time.time(),
            error=f"Field metadata unavailable (HTTP {resp.status_code}: {str(body)[:160]}). "
                  "Regenerate the refresh token with ZohoRecruit.settings.READ for exact type matching.",
        )
        return None

    fields = {f["api_name"]: f for f in body["fields"] if f.get("api_name")}
    _schema_cache.update(fields=fields, loaded_at=time.time(), error=None)
    return fields


def schema_status() -> dict:
    fields = load_field_schema()
    score_fields = {}
    for key, api in config.RECRUIT_SCORE_FIELDS.items():
        score_fields[key] = {"api_name": api, "exists": None if fields is None else api in fields}
    return {
        "available": fields is not None,
        "field_count": len(fields) if fields else 0,
        "error": _schema_cache["error"],
        "score_fields": score_fields,
    }


def _picklist_options(meta: dict) -> list[str]:
    opts = []
    for v in meta.get("pick_list_values") or []:
        val = v.get("actual_value") or v.get("display_value")
        if val and val != "-None-":
            opts.append(val)
    return opts


def _match_option(value, options: list[str]):
    if isinstance(value, bool):
        candidates = ["Yes", "True"] if value else ["No", "False"]
    else:
        candidates = [str(value)]
    norm = {re.sub(r"[^a-z0-9]", "", o.lower()): o for o in options}
    for cand in candidates:
        key = re.sub(r"[^a-z0-9]", "", cand.lower())
        if key in norm:
            return norm[key]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _match_range(value, options)
    return None


def _match_range(n: float, options: list[str]):
    """Option whose numeric range covers n: '18-25', '25+', '> 30', 'Below 20', '24'."""
    for opt in options:
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", opt)]
        low = opt.lower()
        if len(nums) >= 2 and nums[0] <= n <= nums[1]:
            return opt
        if len(nums) != 1:
            continue
        if re.search(r"<|below|under|less", low):
            if n < nums[0]:
                return opt
        elif re.search(r"\+|above|more|over", low):
            if n >= nums[0]:
                return opt
        elif ">" in low:
            if n > nums[0]:
                return opt
        elif n == nums[0]:
            return opt
    return None


def _to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group(0)) if m else None


def _to_date(value):
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def coerce_value(value, data_type: str, meta: dict | None = None):
    """Converts value to what Recruit expects for data_type. Returns
    (new_value, reason) - new_value None means drop the field."""
    meta = meta or {}
    dt = (data_type or "").lower()

    if dt in _UNWRITABLE_TYPES:
        return None, f"type '{dt}' is not writable"

    if dt in ("text", "textarea", "email", "phone", "website", ""):
        if isinstance(value, bool):
            value = "Yes" if value else "No"
        elif isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value if v not in ("", None))
        elif isinstance(value, float) and value.is_integer():
            value = int(value)
        s = str(value).strip()
        if not s:
            return None, "empty after conversion"
        max_len = meta.get("length")
        if isinstance(max_len, int) and max_len > 0 and len(s) > max_len:
            s = s[: max_len - 1].rstrip() + "…"
        return s, None

    if dt in ("integer", "bigint"):
        n = _to_number(value)
        return (int(round(n)), None) if n is not None else (None, f"'{value}' is not a whole number")

    if dt in ("double", "decimal", "currency", "percent"):
        n = _to_number(value)
        return (float(n), None) if n is not None else (None, f"'{value}' is not a number")

    if dt == "boolean":
        if isinstance(value, bool):
            return value, None
        s = str(value).strip().lower()
        if s in ("yes", "true", "y", "1"):
            return True, None
        if s in ("no", "false", "n", "0"):
            return False, None
        return None, f"'{value}' is not yes/no"

    if dt == "date":
        d = _to_date(value)
        return (d, None) if d else (None, f"'{value}' is not a date")

    if dt == "picklist":
        options = _picklist_options(meta)
        if not options:
            return None, "picklist options unknown"
        match = _match_option(value, options)
        return (match, None) if match else (None, f"'{value}' is not one of the picklist options")

    if dt == "multiselectpicklist":
        options = _picklist_options(meta)
        items = value if isinstance(value, (list, tuple)) else [value]
        matched = [m for m in (_match_option(v, options) for v in items) if m]
        return (matched, None) if matched else (None, "no values match the multi-select options")

    return None, f"unsupported type '{dt}'"


def prepare_payload(raw: dict) -> tuple[dict, list[dict]]:
    """Applies field metadata (when available). Returns (payload, dropped)."""
    fields = load_field_schema()
    payload, dropped = {}, []

    for api, value in raw.items():
        if value in ("", None, [], ()):
            continue
        if api in _NEVER_WRITE:
            continue
        if fields is None:
            payload[api] = value  # no metadata: error-driven repair handles types
            continue
        meta = fields.get(api)
        if meta is None:
            dropped.append({"field": api, "reason": "field does not exist in Recruit Candidates"})
            continue
        if meta.get("read_only") or meta.get("field_read_only"):
            dropped.append({"field": api, "reason": "field is read-only"})
            continue
        new_value, reason = coerce_value(value, meta.get("data_type"), meta)
        if new_value is None:
            dropped.append({"field": api, "reason": reason})
            continue
        payload[api] = new_value

    return payload, dropped


# ---------------------------------------------------------------- upsert

def upsert_candidate(raw_payload: dict) -> dict:
    """Creates or updates a candidate matched on RECRUIT_DUPLICATE_FIELD.
    Returns {"record_id", "action", "dropped": [{field, reason}]}."""
    payload, dropped = prepare_payload(raw_payload)

    dup_field = config.RECRUIT_DUPLICATE_FIELD
    if not payload.get(dup_field):
        raise RecruitError(
            f"Cannot save to Recruit: no {dup_field} was extracted from this resume, so there is "
            "no safe way to match an existing candidate."
        )

    url = f"{config.RECRUIT_API_BASE}/Candidates/upsert"
    headers = {**auth_header(), "Content-Type": "application/json"}

    for _ in range(MAX_REPAIR_ATTEMPTS):
        body = {"data": [payload], "duplicate_check_fields": [dup_field], "trigger": ["workflow"]}
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=45)
            result = resp.json()
        except requests.exceptions.RequestException as e:
            raise RecruitError(f"Could not reach Zoho Recruit API ({url}): {e}")
        except ValueError:
            raise RecruitError(f"Zoho Recruit returned a non-JSON response (HTTP {resp.status_code}).")

        try:
            rec = result["data"][0]
        except (KeyError, IndexError, TypeError):
            raise RecruitError(f"Unexpected response from Zoho Recruit: {result}")

        if rec.get("status") == "success":
            return {
                "record_id": rec["details"]["id"],
                "action": rec.get("action", "upsert"),
                "dropped": dropped,
            }

        details = rec.get("details") or {}
        bad_field = details.get("api_name")
        if rec.get("code") != "INVALID_DATA" or not bad_field or bad_field not in payload:
            raise RecruitError(f"Zoho Recruit rejected the record: {rec}")
        if bad_field == dup_field:
            raise RecruitError(f"Zoho Recruit rejected the {dup_field} value: {rec}")

        original = payload[bad_field]
        expected = details.get("expected_data_type")
        repaired = None
        if expected:
            limit = details.get("maximum_length")
            repaired, _ = coerce_value(original, expected, {"length": limit} if limit else None)
        if repaired is not None and repaired != original:
            payload[bad_field] = repaired
        else:
            payload.pop(bad_field)
            reason = f"Recruit rejected value {str(original)[:80]!r}"
            if expected:
                reason += f" (expects {expected})"
            dropped.append({"field": bad_field, "reason": reason})

    raise RecruitError(f"Gave up after {MAX_REPAIR_ATTEMPTS} repair attempts on this record.")


def get_candidate(record_id: str) -> dict:
    url = f"{config.RECRUIT_API_BASE}/Candidates/{record_id}"
    try:
        resp = requests.get(url, headers=auth_header(), timeout=30)
        return resp.json()["data"][0]
    except (requests.exceptions.RequestException, ValueError, KeyError, IndexError, TypeError) as e:
        raise RecruitError(f"Could not read back candidate {record_id}: {e}")


# ---------------------------------------------------------------- notes

def upsert_note(record_id: str, title: str, content: str) -> str:
    """Writes a note on the candidate, replacing the content of an existing
    note with the same title so re-screening doesn't stack duplicates.
    Returns "created" or "updated"."""
    headers = {**auth_header(), "Content-Type": "application/json"}
    list_url = f"{config.RECRUIT_API_BASE}/Candidates/{record_id}/Notes"
    try:
        resp = requests.get(list_url, headers=auth_header(), timeout=30)
        notes = resp.json().get("data", []) if resp.status_code == 200 and resp.content else []
        existing = next((n for n in notes if n.get("Note_Title") == title), None)
        if existing:
            resp = requests.put(f"{config.RECRUIT_API_BASE}/Notes/{existing['id']}", headers=headers,
                                json={"data": [{"Note_Title": title, "Note_Content": content}]}, timeout=30)
        else:
            # Recruit creates notes only on /Notes (the related-list URL answers
            # INVALID_REQUEST_METHOD for POST).
            resp = requests.post(f"{config.RECRUIT_API_BASE}/Notes", headers=headers, timeout=30, json={"data": [{
                "Note_Title": title, "Note_Content": content, "Parent_Id": record_id, "se_module": "Candidates"}]})
        first = resp.json()["data"][0]
    except (requests.exceptions.RequestException, ValueError, KeyError, IndexError, TypeError) as e:
        raise RecruitError(f"Could not write the '{title}' note: {e}")
    if str(first.get("code", "")).upper() != "SUCCESS":
        raise RecruitError(f"Recruit rejected the '{title}' note: {first}")
    return "updated" if existing else "created"


# ---------------------------------------------------------------- attachments

def list_attachments(record_id: str) -> list[dict]:
    url = f"{config.RECRUIT_API_BASE}/Candidates/{record_id}/Attachments"
    try:
        resp = requests.get(url, headers=auth_header(), timeout=30)
    except requests.exceptions.RequestException as e:
        raise RecruitError(f"Could not list attachments for {record_id}: {e}")
    if resp.status_code == 204 or not resp.content:
        return []
    try:
        return resp.json().get("data", []) or []
    except ValueError:
        raise RecruitError(f"Attachment list returned non-JSON (HTTP {resp.status_code}).")


def _size(att: dict):
    try:
        return int(att.get("Size"))
    except (TypeError, ValueError):
        return None


def find_matching_attachment(record_id: str, filename: str, size: int):
    for att in list_attachments(record_id):
        if att.get("File_Name") == filename and _size(att) == size:
            return att
    return None


def attach_resume(record_id: str, file_bytes: bytes, filename: str) -> dict:
    """Uploads the resume unless an identical one (same name + byte size) is
    already attached. Returns {"uploaded": bool, "category": str|None}.

    Recruit quirks handled: attachments_category is mandatory; the "Resume"
    category holds only one file (fall back to "Others"); and a per-file error
    can come back inside an HTTP 202, so the body is checked, not the status.
    """
    if find_matching_attachment(record_id, filename, len(file_bytes)):
        return {"uploaded": False, "category": None}

    url = f"{config.RECRUIT_API_BASE}/Candidates/{record_id}/Attachments"
    last_error = None
    for category in config.RECRUIT_ATTACHMENT_CATEGORIES:
        try:
            resp = requests.post(
                url,
                headers=auth_header(),
                params={"attachments_category": category},
                files={"file": (filename, file_bytes)},
                timeout=90,
            )
        except requests.exceptions.RequestException as e:
            raise RecruitError(f"Could not reach Zoho Recruit API ({url}): {e}")
        try:
            first = resp.json()["data"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            first = {}
        if resp.status_code in (200, 201) and first.get("code") == "SUCCESS":
            return {"uploaded": True, "category": category}
        last_error = f"{resp.status_code} {resp.text.strip()[:300]}"

    raise RecruitError(f"Resume attachment failed: {last_error}")
