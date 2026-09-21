"""
Screening pipeline for one WorkDrive folder, run in a background thread.

Per resume:
  download -> extract text + hyperlinks -> compact text
    identical file already analysed under the same JD -> reuse it (0 tokens)
    else one AI call: basic information + scores
      score <  threshold : rejected -> file stays in WorkDrive
      score >= threshold : passed   -> upsert to Recruit
                                    -> attach resume -> VERIFY in Recruit
                                    -> guarded permanent delete from WorkDrive

The WorkDrive delete happens only after Recruit is confirmed to hold the
resume. Any failure along the way leaves the file in WorkDrive and records
exactly why in the audit log.
"""
import hashlib
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import ai_scorer
import config
import recruit_mapping
import resume_parser
import storage
import zoho_recruit
import zoho_workdrive
from zoho_recruit import RecruitError
from zoho_workdrive import WorkDriveError

MAX_PARALLEL_RESUMES = 3

_start_lock = threading.Lock()


class PipelineError(Exception):
    pass


def start_run(folder_id: str, rescreen: bool = False, file_ids: list[str] | None = None) -> int:
    """Validates the folder and JD, creates the run, and starts it in the
    background. Returns run_id. Raises PipelineError for user-fixable issues."""
    path = zoho_workdrive.resolve_path(folder_id)
    if len(path) < 2:
        raise PipelineError("Open a role folder (e.g. Full Stack) or one of its date folders to screen resumes.")
    role = path[1]
    folder_path = " / ".join(p["name"] for p in path)

    jd = storage.get_jd(role["id"])
    if not jd or not jd["jd_text"].strip():
        raise PipelineError(f"No job description saved for '{role['name']}'. Add one before screening.")

    with _start_lock:
        active = storage.active_run_for_folder(folder_id)
        if active:
            raise PipelineError(f"A screening run (#{active['id']}) is already in progress for this folder.")
        run_id = storage.create_run(
            folder_id, folder_path, role["id"], role["name"], jd["jd_text"], jd["pass_threshold"]
        )

    scope = f"{len(file_ids)} selected resume(s)" if file_ids else ("all resumes, re-screening" if rescreen else "all resumes")
    storage.log("info", f"Run started on '{folder_path}' for {scope} (pass threshold {jd['pass_threshold']}).", run_id)
    thread = threading.Thread(target=_run, args=(run_id, folder_id, rescreen, file_ids), daemon=True, name=f"run-{run_id}")
    thread.start()
    return run_id


def _human_duration(started: float) -> str:
    seconds = time.monotonic() - started
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def _run(run_id: int, folder_id: str, rescreen: bool, file_ids: list[str] | None = None) -> None:
    run = storage.get_run(run_id)
    run_started = time.monotonic()
    try:
        files = zoho_workdrive.list_children(folder_id)["files"]
        if file_ids:
            # Only ids that are really resumes in THIS folder - anything else is ignored.
            wanted = set(file_ids)
            unknown = wanted - {f["id"] for f in files}
            if unknown:
                storage.log("warn", f"{len(unknown)} selected file(s) are no longer in this folder and were skipped.", run_id)
            files = [f for f in files if f["id"] in wanted]
        else:
            files = _skip_already_screened(run, files, rescreen)
        storage.update_run(run_id, total=len(files))
        if not files:
            storage.log("info", "No resumes to screen in this folder.", run_id)

        jobs = [(storage.create_screening(run_id, f["id"], f["name"], folder_id), f) for f in files]
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_RESUMES) as pool:
            for (sid, f) in jobs:
                pool.submit(_safe_process, run, sid, f)

        final = storage.get_run(run_id)
        storage.update_run(run_id, status="completed", finished_at=storage.now())
        storage.log("info", f"Run finished in {_human_duration(run_started)}: {final['passed']} passed, "
                            f"{final['rejected']} rejected, {final['failed']} failed, "
                            f"{final['deleted']} deleted from WorkDrive.", run_id)
    except Exception as e:
        traceback.print_exc()
        storage.update_run(run_id, status="failed", error=str(e), finished_at=storage.now())
        storage.log("error", f"Run aborted: {e}", run_id)


def _skip_already_screened(run: dict, files: list, rescreen: bool) -> list:
    """Rejected resumes stay in WorkDrive by design, so every later run would
    re-score (and re-bill) them. Skip ones already rejected under the SAME JD
    and threshold unless a rescreen is requested. Failed and passed-but-kept
    files are always retried - both steps are idempotent."""
    if rescreen or not files:
        return files
    latest = storage.latest_screening_by_file([f["id"] for f in files])
    keep, skipped = [], []
    for f in files:
        prev = latest.get(f["id"])
        if prev and prev["outcome"] == "rejected":
            prev_run = storage.get_run(prev["run_id"])
            if (prev_run and prev_run["jd_snapshot"] == run["jd_snapshot"]
                    and prev_run["pass_threshold"] == run["pass_threshold"]):
                skipped.append(f["name"])
                continue
        keep.append(f)
    if skipped:
        storage.log("info", f"Skipped {len(skipped)} resume(s) already rejected under this JD: "
                            f"{', '.join(skipped)}. Use 'Re-screen all' to score them again.", run["id"])
    return keep


def _safe_process(run: dict, sid: int, f: dict) -> None:
    started = time.monotonic()
    try:
        _process(run, sid, f)
    except Exception as e:
        traceback.print_exc()
        storage.update_screening(sid, stage="done", outcome="failed", error=f"Unexpected error: {e}")
        storage.bump_run(run["id"], processed=1, failed=1)
        storage.log("error", f"{f['name']}: unexpected error: {e}", run["id"], sid)
    finally:
        storage.update_screening(sid, duration_ms=round((time.monotonic() - started) * 1000))


def _fail(run: dict, sid: int, name: str, message: str) -> None:
    storage.update_screening(sid, stage="done", outcome="failed", error=message,
                             workdrive_action="kept", workdrive_reason="not screened successfully")
    storage.bump_run(run["id"], processed=1, failed=1)
    storage.log("error", f"{name}: {message}", run["id"], sid)


def _process(run: dict, sid: int, f: dict) -> None:
    run_id, name = run["id"], f["name"]
    started = time.monotonic()

    # 1. download
    storage.update_screening(sid, stage="downloading")
    try:
        data = zoho_workdrive.download_file(f["id"])
    except WorkDriveError as e:
        return _fail(run, sid, name, str(e))
    size = len(data)
    storage.update_screening(sid, file_size=size)
    if f.get("size") is not None and int(f["size"]) != size:
        return _fail(run, sid, name, f"Downloaded {size} bytes but WorkDrive lists {f['size']} - file may be changing.")

    # 2. extract
    try:
        parsed = resume_parser.extract(data, name)
    except Exception as e:
        return _fail(run, sid, name, f"Could not read file: {e}")
    if not parsed["text"].strip():
        return _fail(run, sid, name, "No text could be extracted (likely a scanned/image-only PDF).")
    resume_text = ai_scorer.compact_resume_text(resume_parser.build_ai_input(parsed))
    content_hash = hashlib.sha256(data).hexdigest()
    storage.update_screening(sid, content_hash=content_hash)

    # 3. AI: reuse identical file+JD, else one screening call
    storage.update_screening(sid, stage="scoring")
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
    previous = storage.find_reusable_analysis(content_hash, run["jd_snapshot"], ai_scorer.EXTRACTION_VERSION)
    if previous:
        result = previous["ai_json"]
        storage.update_screening(sid, reused=1)
        storage.log("info", f"{name}: identical file already analysed for this JD (screening "
                            f"#{previous['id']}) - reused, 0 tokens.", run_id, sid)
    else:
        try:
            result, usage = ai_scorer.screen_resume(
                resume_text, run["jd_snapshot"], run["role_name"], _picklist_options())
        except Exception as e:
            return _fail(run, sid, name, f"AI scoring failed: {e}")
    result["_source"] = {"filename": name, "file_id": f["id"], "bytes": size, "links_found": parsed["links"]}

    ev = result["evaluation"]
    overall = ev["overall_score"]
    passed = overall >= run["pass_threshold"]
    _save_analysis(sid, result, usage)
    storage.log("info", f"{name}: scored {overall} ({ev['recommendation']}) -> "
                        f"{'PASSED' if passed else 'rejected'} (threshold {run['pass_threshold']}) "
                        f"in {time.monotonic() - started:.1f}s.", run_id, sid)

    if not passed:
        storage.update_screening(sid, stage="done", outcome="rejected", workdrive_action="kept",
                                 workdrive_reason=f"score {overall} below threshold {run['pass_threshold']}")
        storage.bump_run(run_id, processed=1, rejected=1)
        return

    storage.bump_run(run_id, passed=1)

    # 4. Recruit: upsert + attach + verify
    storage.update_screening(sid, stage="pushing", outcome="passed")
    record_id = _push_to_recruit(run, sid, name, result, data)
    if not record_id:
        storage.update_screening(sid, stage="done", workdrive_action="kept",
                                 workdrive_reason="Recruit push not confirmed - file kept")
        storage.bump_run(run_id, processed=1)
        return

    # 5. guarded delete
    if not config.WORKDRIVE_DELETE_ENABLED:
        storage.update_screening(sid, stage="done", workdrive_action="kept",
                                 workdrive_reason="automatic delete is switched off (WORKDRIVE_DELETE_ENABLED)")
        storage.log("warn", f"{name}: kept in WorkDrive - automatic delete is switched off.", run_id, sid)
        storage.bump_run(run_id, processed=1)
        return

    storage.update_screening(sid, stage="deleting")
    try:
        zoho_workdrive.delete_permanently(
            f["id"], expected_folder_id=run["folder_id"], expected_name=name, expected_size=size,
        )
    except WorkDriveError as e:
        blocked = str(e).startswith("Delete blocked")
        storage.update_screening(sid, stage="done",
                                 workdrive_action="delete_blocked" if blocked else "delete_failed",
                                 workdrive_reason=str(e))
        storage.log("warn" if blocked else "error", f"{name}: NOT deleted from WorkDrive - {e}", run_id, sid)
        storage.bump_run(run_id, processed=1)
        return

    storage.update_screening(sid, stage="done", workdrive_action="deleted",
                             workdrive_reason=f"verified in Recruit record {record_id}, then permanently deleted")
    storage.log("delete", f"{name} ({size} bytes, file id {f['id']}) permanently deleted from "
                          f"'{run['folder_path']}' after verification in Recruit record {record_id}.", run_id, sid)
    storage.bump_run(run_id, processed=1, deleted=1)


def _push_to_recruit(run: dict, sid: int, name: str, result: dict, data: bytes):
    """Returns the Recruit record id once the candidate AND the resume
    attachment are confirmed by reading them back, else None."""
    run_id = run["id"]
    try:
        payload = recruit_mapping.build_candidate_payload(
            result, role_name=run["role_name"], folder_path=run["folder_path"])
        outcome = zoho_recruit.upsert_candidate(payload)
        record_id = outcome["record_id"]
        storage.update_screening(sid, recruit_record_id=record_id, recruit_action=outcome["action"],
                                 dropped_fields=outcome["dropped"])
        storage.log("info", f"{name}: Recruit candidate {record_id} ({outcome['action']}), "
                            f"{len(payload) - len(outcome['dropped'])} fields sent.", run_id, sid)
        if outcome["dropped"]:
            detail = "; ".join(f"{d['field']}: {d['reason']}" for d in outcome["dropped"])
            storage.log("warn", f"{name}: {len(outcome['dropped'])} field(s) not saved - {detail}", run_id, sid)

        # AI score + summary as a note: visible on the candidate even while the
        # score fields don't exist in Recruit. Never blocks the push.
        try:
            note = zoho_recruit.upsert_note(record_id, recruit_mapping.AI_NOTE_TITLE, recruit_mapping.ai_note_content(
                result, role_name=run["role_name"], pass_threshold=run["pass_threshold"], filename=name))
            storage.log("info", f"{name}: AI score and summary note {note} in Recruit.", run_id, sid)
        except RecruitError as e:
            storage.log("warn", f"{name}: {e}", run_id, sid)

        attach = zoho_recruit.attach_resume(record_id, data, name)
        storage.log("info", f"{name}: resume " + (
            f"attached (category {attach['category']})." if attach["uploaded"] else "already attached, not re-uploaded."
        ), run_id, sid)

        if not zoho_recruit.find_matching_attachment(record_id, name, len(data)):
            raise RecruitError("Attachment could not be verified in Recruit after upload.")
        saved = zoho_recruit.get_candidate(record_id)
        expected_email = (payload.get(config.RECRUIT_DUPLICATE_FIELD) or "").strip().lower()
        actual_email = str(saved.get(config.RECRUIT_DUPLICATE_FIELD) or "").strip().lower()
        if expected_email != actual_email:
            raise RecruitError(f"Read-back mismatch: record {record_id} has "
                               f"{config.RECRUIT_DUPLICATE_FIELD} '{actual_email}', expected '{expected_email}'.")

        # Recruit silently ignores api_names that don't exist on the module
        # (no error), so detect them from the read-back instead.
        rejected = {d["field"] for d in outcome["dropped"]}
        ignored = sorted(k for k in payload if k not in saved and k not in rejected)
        if ignored:
            all_dropped = outcome["dropped"] + [
                {"field": k, "reason": "field does not exist in Recruit Candidates (ignored)"} for k in ignored
            ]
            storage.update_screening(sid, dropped_fields=all_dropped)
            storage.log("warn", f"{name}: Recruit has no field for {', '.join(ignored)} - "
                                "create these in Recruit to store them.", run_id, sid)
    except RecruitError as e:
        storage.update_screening(sid, recruit_status="failed", recruit_error=str(e))
        storage.log("error", f"{name}: Recruit push failed - {e}", run_id, sid)
        return None
    except Exception as e:
        storage.update_screening(sid, recruit_status="failed", recruit_error=f"Unexpected: {e}")
        storage.log("error", f"{name}: Recruit push failed unexpectedly - {e}", run_id, sid)
        return None

    storage.update_screening(sid, recruit_status="saved", recruit_error=None)
    storage.log("info", f"{name}: verified in Recruit (record + attachment, {len(data)} bytes).", run_id, sid)
    return record_id


def _save_analysis(sid: int, result: dict, usage: dict) -> None:
    ev, basic = result["evaluation"], result["basic_information"]
    storage.update_screening(
        sid, ai_json=result, extraction=result.get("extraction"),
        candidate_name=ai_scorer.full_name(result), email=basic.get("email"),
        overall_score=ev["overall_score"], ai_score=ev["ai_score"], skill_score=ev["skill_score"],
        recommendation=ev["recommendation"], **usage,
    )


def _picklist_options() -> dict[str, list[str]]:
    """Recruit's real options for each dropdown the AI fills, so it picks values
    Recruit accepts. Fields whose metadata can't be read are left out and fall
    back to ai_scorer.DEFAULT_OPTIONS."""
    try:
        fields = zoho_recruit.load_field_schema() or {}
    except Exception:
        return {}
    options = {}
    for key, api in ai_scorer.PICKLIST_FIELDS.items():
        meta = fields.get(api)
        if meta and meta.get("data_type") == "picklist":
            opts = [o for o in zoho_recruit._picklist_options(meta) if o.lower() not in ("option 2",)]
            if opts:
                options[key] = opts
    return options
