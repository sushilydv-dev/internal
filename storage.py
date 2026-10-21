"""
SQLite persistence: job descriptions, screening runs, per-resume results, and
an append-only audit log of every step (including every WorkDrive deletion
decision). The database is a single file next to the app (screener.db).

Why SQLite: the app runs as one process, the data is small, and it needs to
survive restarts - the old in-memory CANDIDATE_STORE lost everything.
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import config

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_descriptions (
    role_folder_id  TEXT PRIMARY KEY,
    role_name       TEXT NOT NULL,
    jd_text         TEXT NOT NULL DEFAULT '',
    pass_threshold  INTEGER NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id        TEXT NOT NULL,
    folder_path      TEXT NOT NULL,
    role_folder_id   TEXT NOT NULL,
    role_name        TEXT NOT NULL,
    jd_snapshot      TEXT NOT NULL,
    pass_threshold   INTEGER NOT NULL,
    status           TEXT NOT NULL,          -- running | cancelling | cancelled | completed | failed
    total            INTEGER NOT NULL DEFAULT 0,
    processed        INTEGER NOT NULL DEFAULT 0,
    passed           INTEGER NOT NULL DEFAULT 0,
    rejected         INTEGER NOT NULL DEFAULT 0,
    failed           INTEGER NOT NULL DEFAULT 0,
    deleted          INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT
);

CREATE TABLE IF NOT EXISTS screenings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL REFERENCES runs(id),
    file_id             TEXT NOT NULL,
    filename            TEXT NOT NULL,
    folder_id           TEXT NOT NULL,
    file_size           INTEGER,
    stage               TEXT NOT NULL,       -- queued | downloading | scoring | pushing | deleting | done
    outcome             TEXT,                -- passed | rejected | failed
    candidate_name      TEXT,
    email               TEXT,
    overall_score       REAL,
    ai_score            REAL,
    skill_score         REAL,
    recommendation      TEXT,
    ai_json             TEXT,
    recruit_status      TEXT NOT NULL DEFAULT 'not_pushed',  -- not_pushed | saved | failed
    recruit_record_id   TEXT,
    recruit_error       TEXT,
    dropped_fields      TEXT,                -- JSON list of fields Recruit refused
    workdrive_action    TEXT NOT NULL DEFAULT 'kept',  -- kept | deleted | delete_blocked | delete_failed
    workdrive_reason    TEXT,
    error               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_screenings_run  ON screenings(run_id);
CREATE INDEX IF NOT EXISTS idx_screenings_file ON screenings(file_id);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER,
    screening_id  INTEGER,
    level         TEXT NOT NULL,             -- info | warn | error | delete
    message       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);

CREATE TABLE IF NOT EXISTS fetched_emails (
    message_id  TEXT PRIMARY KEY,
    position    TEXT,
    candidate   TEXT,
    file_name   TEXT,
    file_id     TEXT,
    fetched_at  TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _conn():
    with _lock:
        conn = sqlite3.connect(config.DATABASE_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


# Columns added after the first release - applied to existing databases.
_MIGRATIONS = {
    "screenings": {
        "content_hash": "TEXT",
        "extraction": "TEXT",                       # quick | full
        "prompt_tokens": "INTEGER NOT NULL DEFAULT 0",
        "completion_tokens": "INTEGER NOT NULL DEFAULT 0",
        "cached_tokens": "INTEGER NOT NULL DEFAULT 0",
        "reused": "INTEGER NOT NULL DEFAULT 0",     # 1 = analysis reused from an identical file
        "recruit_action": "TEXT",                   # insert = new candidate | update = existing one
        "duration_ms": "INTEGER",                   # wall time for this resume, download -> done
    },
}


def init_db() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)
        for table, columns in _MIGRATIONS.items():
            existing = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in existing:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_screenings_hash ON screenings(content_hash)")


def _row(r):
    return dict(r) if r else None


# ---------------------------------------------------------------- JDs

def get_jd(role_folder_id: str):
    with _conn() as c:
        return _row(c.execute(
            "SELECT * FROM job_descriptions WHERE role_folder_id = ?", (role_folder_id,)
        ).fetchone())


def list_jds() -> dict:
    with _conn() as c:
        return {r["role_folder_id"]: dict(r) for r in c.execute("SELECT * FROM job_descriptions")}


def save_jd(role_folder_id: str, role_name: str, jd_text: str, pass_threshold: int) -> dict:
    with _conn() as c:
        c.execute(
            """INSERT INTO job_descriptions (role_folder_id, role_name, jd_text, pass_threshold, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(role_folder_id) DO UPDATE SET
                 role_name = excluded.role_name, jd_text = excluded.jd_text,
                 pass_threshold = excluded.pass_threshold, updated_at = excluded.updated_at""",
            (role_folder_id, role_name, jd_text, pass_threshold, now()),
        )
    return get_jd(role_folder_id)


# ---------------------------------------------------------------- runs

def create_run(folder_id, folder_path, role_folder_id, role_name, jd_text, threshold) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO runs (folder_id, folder_path, role_folder_id, role_name, jd_snapshot,
                                 pass_threshold, status, started_at)
               VALUES (?, ?, ?, ?, ?, ?, 'running', ?)""",
            (folder_id, folder_path, role_folder_id, role_name, jd_text, threshold, now()),
        )
        return cur.lastrowid


def update_run(run_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))


def bump_run(run_id: int, **increments) -> None:
    cols = ", ".join(f"{k} = {k} + ?" for k in increments)
    with _conn() as c:
        c.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*increments.values(), run_id))


def get_run(run_id: int):
    with _conn() as c:
        return _row(c.execute("SELECT runs.*" + _RUN_TOKENS + " FROM runs WHERE id = ?", (run_id,)).fetchone())


_RUN_TOKENS = """, (SELECT COALESCE(SUM(prompt_tokens), 0) FROM screenings WHERE run_id = runs.id) AS prompt_tokens,
    (SELECT COALESCE(SUM(completion_tokens), 0) FROM screenings WHERE run_id = runs.id) AS completion_tokens,
    (SELECT COALESCE(SUM(cached_tokens), 0) FROM screenings WHERE run_id = runs.id) AS cached_tokens"""


def list_runs(limit: int = 100, folder_id: str | None = None) -> list:
    sql = "SELECT runs.*" + _RUN_TOKENS + " FROM runs"
    args: list = []
    if folder_id:
        sql += " WHERE folder_id = ?"
        args.append(folder_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def active_run_for_folder(folder_id: str):
    with _conn() as c:
        return _row(c.execute(
            "SELECT * FROM runs WHERE folder_id = ? AND status IN ('running', 'cancelling') ORDER BY id DESC LIMIT 1",
            (folder_id,),
        ).fetchone())


def request_cancel_run(run_id: int) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE runs SET status = 'cancelling' WHERE id = ? AND status = 'running'",
            (run_id,),
        )
        return cur.rowcount > 0


def mark_interrupted_runs() -> int:
    """Runs left 'running' by a previous process can never finish - the thread
    died with it. Close them out so the folder isn't locked forever."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE runs SET status = 'failed', error = 'Interrupted: the server restarted mid-run.', "
            "finished_at = ? WHERE status = 'running'",
            (now(),),
        )
        return cur.rowcount


# ---------------------------------------------------------------- screenings

def create_screening(run_id, file_id, filename, folder_id) -> int:
    with _conn() as c:
        ts = now()
        cur = c.execute(
            """INSERT INTO screenings (run_id, file_id, filename, folder_id, stage, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
            (run_id, file_id, filename, folder_id, ts, ts),
        )
        return cur.lastrowid


def update_screening(screening_id: int, **fields) -> None:
    if "ai_json" in fields and not isinstance(fields["ai_json"], (str, type(None))):
        fields["ai_json"] = json.dumps(fields["ai_json"], ensure_ascii=False)
    if "dropped_fields" in fields and not isinstance(fields["dropped_fields"], (str, type(None))):
        fields["dropped_fields"] = json.dumps(fields["dropped_fields"])
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE screenings SET {cols} WHERE id = ?", (*fields.values(), screening_id))


def _decode_screening(r: dict) -> dict:
    for key in ("ai_json", "dropped_fields"):
        if r.get(key):
            try:
                r[key] = json.loads(r[key])
            except ValueError:
                pass
    return r


def get_screening(screening_id: int):
    with _conn() as c:
        r = _row(c.execute("SELECT * FROM screenings WHERE id = ?", (screening_id,)).fetchone())
    return _decode_screening(r) if r else None


def screenings_for_run(run_id: int, include_json: bool = False) -> list:
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM screenings WHERE run_id = ? ORDER BY id", (run_id,)
        )]
    for r in rows:
        if include_json:
            _decode_screening(r)
        else:
            r.pop("ai_json", None)
            if r.get("dropped_fields"):
                try:
                    r["dropped_fields"] = json.loads(r["dropped_fields"])
                except ValueError:
                    pass
    return rows


def latest_screening_by_file(file_ids: list[str]) -> dict:
    """file_id -> the most recent screening for that file (any run)."""
    if not file_ids:
        return {}
    marks = ",".join("?" * len(file_ids))
    with _conn() as c:
        rows = c.execute(
            f"""SELECT s.* FROM screenings s
                JOIN (SELECT file_id, MAX(id) AS mid FROM screenings
                      WHERE file_id IN ({marks}) GROUP BY file_id) m
                  ON s.id = m.mid""",
            file_ids,
        ).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        d.pop("ai_json", None)
        out[d["file_id"]] = d
    return out


# ---------------------------------------------------------------- events

def log(level: str, message: str, run_id: int | None = None, screening_id: int | None = None) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO events (run_id, screening_id, level, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, screening_id, level, message, now()),
        )
    print(f"[{level.upper():6}] run={run_id} scr={screening_id} {message}", flush=True)


def events(run_id: int | None = None, limit: int = 500, after_id: int = 0) -> list:
    sql = "SELECT * FROM events WHERE id > ?"
    args: list = [after_id]
    if run_id is not None:
        sql += " AND run_id = ?"
        args.append(run_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    rows.reverse()
    return rows


# ---------------------------------------------------------------- token saving

def find_reusable_analysis(content_hash: str, jd_text: str, extraction: str):
    """A previous analysis of a byte-identical file, under the exact same JD,
    produced by the current extraction format."""
    if not content_hash:
        return None
    with _conn() as c:
        r = _row(c.execute(
            """SELECT s.* FROM screenings s JOIN runs r ON r.id = s.run_id
               WHERE s.content_hash = ? AND r.jd_snapshot = ? AND s.extraction = ? AND s.ai_json IS NOT NULL
               ORDER BY s.id DESC LIMIT 1""",
            (content_hash, jd_text, extraction),
        ).fetchone())
    return _decode_screening(r) if r else None


# ---------------------------------------------------------------- dashboard & candidates

_LATEST = """latest AS (
    SELECT s.*, r.role_name, r.role_folder_id, r.folder_path, r.pass_threshold
    FROM screenings s
    JOIN (SELECT file_id, MAX(id) AS mid FROM screenings WHERE stage = 'done' GROUP BY file_id) m
      ON s.id = m.mid
    JOIN runs r ON r.id = s.run_id
)"""

_COUNTS = """COUNT(*) AS screened,
    SUM(outcome = 'passed') AS passed,
    SUM(outcome = 'rejected') AS rejected,
    SUM(outcome = 'failed') AS failed,
    SUM(recruit_status = 'saved') AS in_recruit,
    SUM(workdrive_action = 'deleted') AS deleted,
    ROUND(AVG(overall_score), 1) AS avg_score,
    ROUND(AVG(duration_ms) / 1000.0, 1) AS avg_seconds,
    ROUND(SUM(COALESCE(duration_ms, 0)) / 1000.0, 1) AS total_seconds"""


def dashboard() -> dict:
    """Outcome counts use each file's LATEST screening, so re-screening a
    resume doesn't double count it. Token totals include every AI call."""
    with _conn() as c:
        totals = dict(c.execute(f"WITH {_LATEST} SELECT {_COUNTS} FROM latest").fetchone())
        roles = [dict(r) for r in c.execute(
            f"""WITH {_LATEST} SELECT role_folder_id, role_name, {_COUNTS}, MAX(updated_at) AS last_activity
                FROM latest GROUP BY role_folder_id ORDER BY last_activity DESC""")]
        tokens = dict(c.execute(
            """SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                      COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                      COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                      COALESCE(SUM(reused), 0) AS reused,
                      COALESCE(SUM(extraction = 'full'), 0) AS full_extractions,
                      COALESCE(SUM(extraction IS NOT NULL), 0) AS analysed
               FROM screenings""").fetchone())
        not_in_recruit = [dict(r) for r in c.execute(
            f"""WITH {_LATEST} SELECT id, filename, candidate_name, email, overall_score, recruit_status,
                       recruit_error, role_name, updated_at
                FROM latest WHERE {_RECRUIT_FILTERS['not_saved']} ORDER BY updated_at DESC LIMIT 20""")]
        recent_passed = [dict(r) for r in c.execute(
            f"""WITH {_LATEST} SELECT id, run_id, filename, candidate_name, email, overall_score, recommendation,
                       recruit_status, workdrive_action, role_name, updated_at
                FROM latest WHERE outcome = 'passed' ORDER BY updated_at DESC LIMIT 8""")]
    for block in [totals, *roles]:
        for k in ("screened", "passed", "rejected", "failed", "in_recruit", "deleted"):
            block[k] = block.get(k) or 0
    return {"totals": totals, "roles": roles, "tokens": tokens,
            "recent_passed": recent_passed, "not_in_recruit": not_in_recruit}


# recruit filter -> SQL on the latest screening of each file.
_RECRUIT_FILTERS = {
    "saved": "recruit_status = 'saved'",
    "not_saved": "outcome = 'passed' AND (recruit_status IS NULL OR recruit_status != 'saved')",
    "failed": "recruit_status = 'failed'",
}


def candidates(outcome: str | None = None, role_folder_id: str | None = None,
               query: str | None = None, recruit: str | None = None, limit: int = 500) -> list:
    sql = f"""WITH {_LATEST} SELECT id, run_id, file_id, filename, candidate_name, email, overall_score,
                     ai_score, skill_score, recommendation, outcome, recruit_status, recruit_record_id,
                     workdrive_action, workdrive_reason, extraction, error, role_name, role_folder_id,
                     folder_path, created_at, updated_at, duration_ms, recruit_action, recruit_error,
                     prompt_tokens, completion_tokens, reused
              FROM latest WHERE 1 = 1"""
    args: list = []
    if outcome:
        sql += " AND outcome = ?"
        args.append(outcome)
    if role_folder_id:
        sql += " AND role_folder_id = ?"
        args.append(role_folder_id)
    if recruit in _RECRUIT_FILTERS:
        sql += f" AND ({_RECRUIT_FILTERS[recruit]})"
    if query:
        sql += " AND (LOWER(candidate_name) LIKE ? OR LOWER(email) LIKE ? OR LOWER(filename) LIKE ?)"
        like = f"%{query.lower()}%"
        args += [like, like, like]
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


# ---------------------------------------------------------------- fetched emails

def email_already_fetched(message_id: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM fetched_emails WHERE message_id = ?", (message_id,)).fetchone() is not None


def mark_email_fetched(message_id: str, position: str, candidate: str, file_name: str, file_id: str | None) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO fetched_emails (message_id, position, candidate, file_name, file_id, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, position, candidate, file_name, file_id, now()),
        )
