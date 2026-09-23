"""
Resume Screener - Flask app.

Hiring team flow:
  1. Browse ROOT / role / date folders in WorkDrive.
  2. Each role has an editable job description + pass threshold.
  3. "Screen resumes" on a folder runs the pipeline in the background
     (see pipeline.py): score every resume, push passed candidates to
     Zoho Recruit, then delete them from WorkDrive after verification.
  4. Every run, result and deletion is kept in the audit log (screener.db).

Run:  PORT=5001 python3 app.py
"""
import os
import traceback

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

import ai_scorer
import config
import pipeline
import storage
import zoho_auth
import zoho_recruit
import zoho_workdrive
from zoho_auth import ZohoAuthError
from zoho_workdrive import OutsideRootError, WorkDriveError

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.config["PROPAGATE_EXCEPTIONS"] = False
# 50 resumes x 10 MB each, plus multipart overhead.
app.config["MAX_CONTENT_LENGTH"] = 520 * 1024 * 1024

storage.init_db()
_interrupted = storage.mark_interrupted_runs()
if _interrupted:
    print(f"Marked {_interrupted} interrupted run(s) from a previous server session as failed.")

REQUIRED_SCOPES = {
    "WorkDrive.files.READ": "Browse folders and download resumes",
    "WorkDrive.files.CREATE": "Create folders and upload resumes",
    "WorkDrive.files.UPDATE": "Move passed resumes to trash",
    "WorkDrive.files.DELETE": "Permanently delete passed resumes",
    "ZohoRecruit.modules.ALL": "Create candidates and attach resumes",
    "ZohoRecruit.settings.READ": "Read Recruit field types for exact mapping",
}


def _error(message: str, status: int):
    return jsonify({"error": message}), status


@app.errorhandler(Exception)
def handle_uncaught(e):
    if isinstance(e, HTTPException):
        # Normal HTTP errors (404 for /favicon.ico etc.) keep their own status.
        if request.path.startswith("/api/"):
            return _error(e.description or e.name, e.code or 500)
        return e
    if request.path.startswith("/api/"):
        traceback.print_exc()
        if isinstance(e, OutsideRootError):
            return _error(str(e), 403)
        if isinstance(e, (WorkDriveError, ZohoAuthError)):
            return _error(str(e), 502)
        if isinstance(e, pipeline.PipelineError):
            return _error(str(e), 400)
        return _error(f"Unexpected server error: {e}", 500)
    raise e


@app.get("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------- status

@app.get("/api/status")
def api_status():
    status = {
        "root_folder_id": config.WORKDRIVE_ROOT_FOLDER_ID,
        "default_threshold": config.DEFAULT_PASS_THRESHOLD,
        "delete_enabled": config.WORKDRIVE_DELETE_ENABLED,
        "openai_configured": bool(config.OPENAI_API_KEY),
        "model": config.OPENAI_MODEL,
        "zoho_connected": False,
        "scopes": [],
        "recruit_schema": None,
        "warnings": [],
    }
    try:
        granted = zoho_auth.granted_scopes()
        status["zoho_connected"] = True
    except ZohoAuthError as e:
        status["warnings"].append(f"Zoho connection failed: {e}")
        return jsonify(status)

    for scope, purpose in REQUIRED_SCOPES.items():
        status["scopes"].append({"scope": scope, "purpose": purpose, "granted": scope in granted})

    missing = [s["scope"] for s in status["scopes"] if not s["granted"]]
    if any(s.startswith("WorkDrive.files.") and s != "WorkDrive.files.READ" for s in missing) \
            and config.WORKDRIVE_DELETE_ENABLED:
        status["warnings"].append(
            "Passed resumes can't be deleted from WorkDrive yet: the Zoho token is missing delete "
            "permission. Candidates still reach Recruit, and files stay in WorkDrive until the token "
            "is regenerated."
        )

    status["recruit_schema"] = zoho_recruit.schema_status()
    if not status["openai_configured"]:
        status["warnings"].append("OPENAI_API_KEY is not set - resumes cannot be scored.")
    return jsonify(status)


# ---------------------------------------------------------------- browsing

@app.get("/api/browse")
def api_browse():
    data = zoho_workdrive.browse(request.args.get("folder_id") or None)

    jds = storage.list_jds()
    for sub in data["subfolders"]:
        if data["depth"] == 0:
            jd = jds.get(sub["id"])
            sub["has_jd"] = bool(jd and jd["jd_text"].strip())
        last = storage.list_runs(limit=1, folder_id=sub["id"])
        sub["last_run"] = last[0] if last else None

    latest = storage.latest_screening_by_file([f["id"] for f in data["files"]])
    for f in data["files"]:
        f["last_screening"] = latest.get(f["id"])

    if data["role"]:
        data["jd"] = storage.get_jd(data["role"]["id"])
    data["runs"] = storage.list_runs(limit=10, folder_id=data["folder"]["id"])
    active = storage.active_run_for_folder(data["folder"]["id"])
    data["active_run_id"] = active["id"] if active else None
    return jsonify(data)


# ---------------------------------------------------------------- folders & uploads

@app.post("/api/folders")
def api_create_folder():
    body = request.get_json(force=True, silent=True) or {}
    parent_id = (body.get("parent_id") or "").strip() or config.WORKDRIVE_ROOT_FOLDER_ID
    folder = zoho_workdrive.create_folder(parent_id, body.get("name") or "")
    storage.log("info", f"Folder '{folder['name']}' created from the app.")
    return jsonify(folder), 201


@app.post("/api/folders/<folder_id>/upload")
def api_upload(folder_id):
    files = request.files.getlist("files")
    if not files:
        return _error("Choose at least one resume to upload.", 400)
    if len(files) > 50:
        return _error("Upload at most 50 resumes at a time.", 400)
    results = []
    for f in files:
        name = f.filename or "resume"
        try:
            uploaded = zoho_workdrive.upload_resume(folder_id, name, f.read())
            results.append({"name": uploaded["name"], "id": uploaded["id"], "ok": True})
        except WorkDriveError as e:
            results.append({"name": name, "ok": False, "error": str(e)})
    ok = [r for r in results if r["ok"]]
    if ok:
        storage.log("info", f"Uploaded {len(ok)} resume(s) from the app: {', '.join(r['name'] for r in ok)}.")
    return jsonify({"results": results, "uploaded": len(ok), "failed": len(results) - len(ok)})


# ---------------------------------------------------------------- JDs

@app.get("/api/roles/<role_folder_id>/jd")
def api_get_jd(role_folder_id):
    return jsonify(storage.get_jd(role_folder_id) or {})


@app.put("/api/roles/<role_folder_id>/jd")
def api_save_jd(role_folder_id):
    body = request.get_json(force=True, silent=True) or {}
    path = zoho_workdrive.resolve_path(role_folder_id)
    if len(path) != 2:
        return _error("Job descriptions belong to role folders (the first level inside the root).", 400)

    jd_text = (body.get("jd_text") or "").strip()
    if len(jd_text) < 30:
        return _error("The job description is too short to screen against - add the role's requirements.", 400)
    try:
        threshold = int(body.get("pass_threshold", config.DEFAULT_PASS_THRESHOLD))
    except (TypeError, ValueError):
        return _error("Pass threshold must be a whole number.", 400)
    if not 0 <= threshold <= 100:
        return _error("Pass threshold must be between 0 and 100.", 400)

    previous = storage.get_jd(role_folder_id)
    saved = storage.save_jd(role_folder_id, path[1]["name"], jd_text, threshold)
    change = "created" if not previous else (
        "updated" if (previous["jd_text"] != jd_text or previous["pass_threshold"] != threshold) else "saved (no change)"
    )
    storage.log("info", f"Job description for '{path[1]['name']}' {change} (threshold {threshold}).")
    return jsonify(saved)


# ---------------------------------------------------------------- runs

@app.post("/api/runs")
def api_start_run():
    body = request.get_json(force=True, silent=True) or {}
    folder_id = (body.get("folder_id") or "").strip()
    if not folder_id:
        return _error("folder_id is required.", 400)
    if not config.OPENAI_API_KEY:
        return _error("OPENAI_API_KEY is not set - resumes cannot be scored.", 400)
    file_ids = body.get("file_ids")
    if file_ids is not None:
        if not isinstance(file_ids, list) or not all(isinstance(x, str) and x.isalnum() for x in file_ids):
            return _error("file_ids must be a list of WorkDrive file ids.", 400)
        if not file_ids:
            return _error("Select at least one resume to screen.", 400)
        if len(file_ids) > 500:
            return _error("Select at most 500 resumes per run.", 400)
    run_id = pipeline.start_run(folder_id, rescreen=bool(body.get("rescreen")), file_ids=file_ids or None)
    return jsonify({"run_id": run_id}), 202


@app.get("/api/runs")
def api_list_runs():
    return jsonify({"runs": storage.list_runs(limit=int(request.args.get("limit", 100)))})


@app.get("/api/runs/<int:run_id>")
def api_get_run(run_id):
    run = storage.get_run(run_id)
    if not run:
        return _error("Run not found.", 404)
    run.pop("jd_snapshot", None)
    _with_cost(run)
    return jsonify({
        "run": run,
        "screenings": storage.screenings_for_run(run_id),
        "events": storage.events(run_id=run_id, after_id=int(request.args.get("after_event", 0))),
    })


@app.delete("/api/runs/<int:run_id>")
def api_cancel_run(run_id):
    run = storage.get_run(run_id)
    if not run:
        return _error("Run not found.", 404)
    if run["status"] == "cancelling":
        return jsonify({"status": "cancelling"})
    if run["status"] != "running" or not storage.request_cancel_run(run_id):
        return _error("This screening is no longer running.", 409)
    storage.log("warn", "Screening stop requested by the user.", run_id)
    return jsonify({"status": "cancelling"})


@app.get("/api/runs/<int:run_id>/jd")
def api_run_jd(run_id):
    run = storage.get_run(run_id)
    if not run:
        return _error("Run not found.", 404)
    return jsonify({"jd_text": run["jd_snapshot"], "pass_threshold": run["pass_threshold"]})


@app.get("/api/screenings/<int:screening_id>")
def api_get_screening(screening_id):
    s = storage.get_screening(screening_id)
    if not s:
        return _error("Screening not found.", 404)
    return jsonify(s)


def _with_cost(block: dict) -> dict:
    block["tokens_total"] = (block.get("prompt_tokens") or 0) + (block.get("completion_tokens") or 0)
    block["estimated_cost_usd"] = round(ai_scorer.estimate_cost(
        block.get("prompt_tokens") or 0, block.get("completion_tokens") or 0, block.get("cached_tokens") or 0), 4)
    return block


@app.get("/api/dashboard")
def api_dashboard():
    data = storage.dashboard()
    _with_cost(data["tokens"])
    analysed = data["tokens"].get("analysed") or 0
    data["tokens"]["avg_tokens_per_resume"] = round(data["tokens"]["tokens_total"] / analysed) if analysed else 0
    data["recent_runs"] = [_with_cost(r) for r in storage.list_runs(limit=6)]
    return jsonify(data)


@app.get("/api/candidates")
def api_candidates():
    outcome = request.args.get("outcome") or None
    if outcome and outcome not in ("passed", "rejected", "failed"):
        return _error("outcome must be passed, rejected or failed.", 400)
    recruit = request.args.get("recruit") or None
    if recruit and recruit not in storage._RECRUIT_FILTERS:
        return _error("recruit must be saved, not_saved or failed.", 400)
    rows = storage.candidates(outcome=outcome, role_folder_id=request.args.get("role") or None,
                              query=(request.args.get("q") or "").strip() or None, recruit=recruit)
    roles = sorted({(r["role_folder_id"], r["role_name"]) for r in storage.candidates()}, key=lambda x: x[1].lower())
    return jsonify({"candidates": rows, "roles": [{"id": i, "name": n} for i, n in roles]})


@app.get("/api/events")
def api_events():
    return jsonify({"events": storage.events(limit=int(request.args.get("limit", 300)))})


if __name__ == "__main__":
    # Render binds to a dynamic port and expects the server to listen on 0.0.0.0,
    # not localhost/127.0.0.1.
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0").lower() in {"1", "true", "yes", "on"}
    # use_reloader=False: the reloader runs the app twice, and a restart would
    # kill an in-progress screening run mid-way.
    app.run(host=host, debug=debug, port=port, use_reloader=False, threaded=True)
