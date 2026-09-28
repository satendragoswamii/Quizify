"""SQLite storage for the admin panel.

Uses the standard library only — no ORM, no extra dependency. The schema is applied
idempotently on first use and versioned so future changes can migrate in place.
"""

import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from .. import config

log = logging.getLogger("quizify.admin.db")

SCHEMA_VERSION = 8

_lock = threading.RLock()
_local = threading.local()
_db_path: Optional[str] = None


def default_path() -> str:
    configured = config.get("QUIZ_DB_PATH")
    if configured:
        return configured
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "quizify.db")


def set_path(path: str) -> None:
    """Point the store at a different file (used by tests)."""
    global _db_path
    with _lock:
        _db_path = path
        close()


def path() -> str:
    return _db_path or default_path()


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch the database into WAL mode, tolerating a race to get there.

    Changing the journal mode needs a brief exclusive lock, and SQLite does not always
    apply the busy handler to it. Several WSGI workers booting at once therefore race,
    and the losers used to raise "database is locked" during import — which killed the
    worker and took the whole server down with it.

    Losing the race is harmless: the journal mode is a property of the file, so whoever
    wins sets it for everyone. Retry briefly, then carry on either way.
    """
    for attempt in range(3):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            time.sleep(0.1 * (attempt + 1))

    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    except Exception:  # pragma: no cover - defensive
        mode = "unknown"
    if str(mode).lower() != "wal":
        log.warning("Could not switch the database to WAL mode (currently %s). "
                    "Concurrent writes will be slower but still correct.", mode)


def connect() -> sqlite3.Connection:
    """One connection per thread; Flask serves requests across a thread pool."""
    conn = getattr(_local, "conn", None)
    same_process = getattr(_local, "pid", None) == os.getpid()

    if conn is not None and same_process and getattr(_local, "path", None) == path():
        return conn

    if conn is not None:
        if same_process:
            conn.close()
        else:
            # Inherited across a fork (a preloading WSGI server): parent and child
            # would otherwise write through one descriptor, which corrupts the file.
            # Drop the reference without closing, since the descriptor is the
            # parent's to manage, and open a fresh connection below.
            conn = None

    directory = os.path.dirname(os.path.abspath(path()))
    if directory:
        os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(path(), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # busy_timeout comes first so the statements below have something to wait on.
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA foreign_keys=ON")
    _enable_wal(conn)
    _local.conn = conn
    _local.path = path()
    _local.pid = os.getpid()
    return conn


def close() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# --------------------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    version     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    name            TEXT    NOT NULL DEFAULT '',
    password_hash   TEXT    NOT NULL,
    role            TEXT    NOT NULL DEFAULT 'user',
    is_active       INTEGER NOT NULL DEFAULT 1,
    daily_quota     INTEGER NOT NULL DEFAULT 0,   -- 0 = unlimited
    notes           TEXT    NOT NULL DEFAULT '',
    batch           TEXT    NOT NULL DEFAULT '',   -- students: batch/class name
    roll_no         TEXT    NOT NULL DEFAULT '',   -- students: roll number
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_login_at   TEXT,
    failed_logins   INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by  INTEGER REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS providers (
    name        TEXT PRIMARY KEY,
    enabled     INTEGER NOT NULL DEFAULT 1,
    api_key     TEXT    NOT NULL DEFAULT '',
    model       TEXT    NOT NULL DEFAULT '',
    base_url    TEXT    NOT NULL DEFAULT '',
    priority    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    source_name     TEXT    NOT NULL DEFAULT '',
    source_format   TEXT    NOT NULL DEFAULT '',
    subject         TEXT    NOT NULL DEFAULT '',
    topic           TEXT    NOT NULL DEFAULT '',
    output_format   TEXT    NOT NULL DEFAULT '',
    engine          TEXT    NOT NULL DEFAULT '',
    questions       INTEGER NOT NULL DEFAULT 0,
    answered        INTEGER NOT NULL DEFAULT 0,
    avg_confidence  REAL    NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'ok',
    error           TEXT    NOT NULL DEFAULT '',
    ip              TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    actor_email TEXT    NOT NULL DEFAULT '',
    action      TEXT    NOT NULL,
    target      TEXT    NOT NULL DEFAULT '',
    detail      TEXT    NOT NULL DEFAULT '',
    ip          TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit(created_at DESC);

-- Corrections a user made in the preview, kept only when they opt in. These are the
-- app's accuracy feedback loop: `fingerprint` is the normalised question stem, so a
-- later parse of the same question can recover the answer the user supplied, and the
-- closest entries are offered to the AI backend as worked examples.
CREATE TABLE IF NOT EXISTS corrections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    fingerprint   TEXT    NOT NULL,
    question      TEXT    NOT NULL DEFAULT '',
    subject       TEXT    NOT NULL DEFAULT '',
    topic         TEXT    NOT NULL DEFAULT '',
    source_format TEXT    NOT NULL DEFAULT '',
    engine        TEXT    NOT NULL DEFAULT '',
    fields        TEXT    NOT NULL DEFAULT '',
    before_json   TEXT    NOT NULL DEFAULT '',
    after_json    TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_corrections_key ON corrections(fingerprint);
CREATE INDEX IF NOT EXISTS idx_corrections_created ON corrections(created_at DESC);

-- The question bank: one row per distinct question ever downloaded, keyed by the same
-- normalised stem the deduplicator uses. This is what lets the converter say "you have
-- used this question before" and what the admin panel exports by subject or topic.
CREATE TABLE IF NOT EXISTS questions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint    TEXT    NOT NULL,
    question       TEXT    NOT NULL,
    qtype          TEXT    NOT NULL DEFAULT '',
    options_json   TEXT    NOT NULL DEFAULT '[]',
    answer         TEXT    NOT NULL DEFAULT '',
    answer_labels  TEXT    NOT NULL DEFAULT '',
    answer_text    TEXT    NOT NULL DEFAULT '',
    explanation    TEXT    NOT NULL DEFAULT '',
    subject        TEXT    NOT NULL DEFAULT '',
    topic          TEXT    NOT NULL DEFAULT '',
    source_name    TEXT    NOT NULL DEFAULT '',
    source_format  TEXT    NOT NULL DEFAULT '',
    engine         TEXT    NOT NULL DEFAULT '',
    confidence     REAL    NOT NULL DEFAULT 0,
    user_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    times_used     INTEGER NOT NULL DEFAULT 1,
    first_used_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    last_used_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_key ON questions(fingerprint);
CREATE INDEX IF NOT EXISTS idx_questions_subject ON questions(subject);
CREATE INDEX IF NOT EXISTS idx_questions_topic ON questions(topic);
CREATE INDEX IF NOT EXISTS idx_questions_used ON questions(last_used_at DESC);

-- One row per time a banked question turned up in a download, so the converter can
-- report *when* and *in which file* a repeat was used before, not merely how often.
CREATE TABLE IF NOT EXISTS question_uses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id  INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    source_name  TEXT    NOT NULL DEFAULT '',
    subject      TEXT    NOT NULL DEFAULT '',
    topic        TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_question_uses_q ON question_uses(question_id, created_at DESC);

-- Per-user overrides of the features and limits in `settings`. A key absent here means
-- the user simply follows the global value, so an install that never touches this table
-- behaves exactly as it did before.
CREATE TABLE IF NOT EXISTS user_settings (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key        TEXT    NOT NULL,
    value      TEXT    NOT NULL DEFAULT '',
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, key)
);

-- An assigned quiz that can be taken online like an exam. `token` is the public,
-- unguessable id used in the share link. `questions_json` is a frozen snapshot of
-- the questions at assignment time, so editing the bank later never changes a live
-- exam. Settings (timer, shuffle, reveal answers) are stored inline.
CREATE TABLE IF NOT EXISTS exams (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    token          TEXT    NOT NULL UNIQUE,
    title          TEXT    NOT NULL DEFAULT '',
    subject        TEXT    NOT NULL DEFAULT '',
    topic          TEXT    NOT NULL DEFAULT '',
    questions_json TEXT    NOT NULL DEFAULT '[]',
    question_count INTEGER NOT NULL DEFAULT 0,
    total_points   REAL    NOT NULL DEFAULT 0,
    time_limit_min INTEGER NOT NULL DEFAULT 0,   -- 0 = no limit
    shuffle_q      INTEGER NOT NULL DEFAULT 0,
    shuffle_opts   INTEGER NOT NULL DEFAULT 0,
    reveal_answers INTEGER NOT NULL DEFAULT 1,
    allow_pdf      INTEGER NOT NULL DEFAULT 1,    -- takers may download a result PDF
    available_from TEXT    NOT NULL DEFAULT '',   -- ISO datetime, '' = open immediately
    available_until TEXT   NOT NULL DEFAULT '',   -- ISO datetime, '' = no deadline
    instructions   TEXT    NOT NULL DEFAULT '',   -- shown to takers before the exam
    proctored      INTEGER NOT NULL DEFAULT 0,    -- require camera + capture snapshots
    proctor_interval INTEGER NOT NULL DEFAULT 20, -- seconds between webcam snapshots
    is_open        INTEGER NOT NULL DEFAULT 1,    -- 0 = closed, no new attempts
    created_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Webcam snapshots captured during a proctored attempt. Stored as data URIs so the
-- feature needs no filesystem or object store; pruned when the exam is deleted via
-- the attempt cascade.
CREATE TABLE IF NOT EXISTS exam_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id  INTEGER NOT NULL REFERENCES exam_attempts(id) ON DELETE CASCADE,
    exam_id     INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    image       TEXT    NOT NULL DEFAULT '',      -- data:image/jpeg;base64,...
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_snapshots_attempt ON exam_snapshots(attempt_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exams_created ON exams(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exams_creator ON exams(created_by);

-- One row per person who takes an exam. `answers_json` is what they submitted,
-- `detail_json` is the per-question graded breakdown. `needs_review` flags an
-- attempt that contains ungradable (e.g. short-answer) responses.
CREATE TABLE IF NOT EXISTS exam_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id       INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- enrolled student, if any
    taker_name    TEXT    NOT NULL DEFAULT '',
    taker_email   TEXT    NOT NULL DEFAULT '',
    answers_json  TEXT    NOT NULL DEFAULT '{}',
    detail_json   TEXT    NOT NULL DEFAULT '[]',
    score         REAL    NOT NULL DEFAULT 0,
    total_points  REAL    NOT NULL DEFAULT 0,
    correct_count INTEGER NOT NULL DEFAULT 0,
    graded_count  INTEGER NOT NULL DEFAULT 0,
    percent       REAL    NOT NULL DEFAULT 0,
    needs_review  INTEGER NOT NULL DEFAULT 0,
    duration_sec  INTEGER NOT NULL DEFAULT 0,
    ip            TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_attempts_exam ON exam_attempts(exam_id, created_at DESC);

-- Links an enrolled student (a user with role 'student') to an exam they may take.
-- One row per (exam, student). Assigning by batch simply creates a row per student.
CREATE TABLE IF NOT EXISTS exam_assignments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id      INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assigned_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (exam_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_assignments_user ON exam_assignments(user_id);
CREATE INDEX IF NOT EXISTS idx_assignments_exam ON exam_assignments(exam_id);
"""


def init() -> None:
    """Create the schema if absent and record its version."""
    with _lock:
        conn = connect()
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT version FROM schema_info WHERE id = 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_info (id, version) VALUES (1, ?)",
                         (SCHEMA_VERSION,))
        else:
            _migrate(conn, int(row["version"]))
        conn.commit()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str,
                           decl: str) -> None:
    """Add ``column`` to ``table`` only if it is not already present.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``, so this checks PRAGMA table_info
    first. Safe to call repeatedly.
    """
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Apply forward migrations. Each step is idempotent and additive."""
    version = from_version
    # v2 added the `corrections` table. No step is needed here because init() replays the
    # whole CREATE TABLE IF NOT EXISTS script on every start, so an existing database
    # picks the table up on its own; this only records that it happened.
    if version < 2:
        version = 2
    # v3 added `questions` and `question_uses`, v4 added `user_settings`. All arrive the
    # same way, so these steps only record that the tables are expected to be present.
    if version < 3:
        version = 3
    if version < 4:
        version = 4
    # v5 added `exams` and `exam_attempts` for the online-exam feature. Like the
    # tables above they arrive through the replayed CREATE TABLE IF NOT EXISTS script,
    # so this step only records that they are expected to be present.
    if version < 5:
        version = 5
    # v6 added exam scheduling + PDF columns. The exams table already existed at v5,
    # so CREATE TABLE IF NOT EXISTS will not add columns to it — these need real
    # ALTER statements. Each is guarded so re-running on a fresh DB (already correct)
    # does not error.
    if version < 6:
        _add_column_if_missing(conn, "exams", "allow_pdf", "INTEGER NOT NULL DEFAULT 1")
        _add_column_if_missing(conn, "exams", "available_from", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "exams", "available_until", "TEXT NOT NULL DEFAULT ''")
        version = 6
    # v7 added exam instructions + proctoring columns and the exam_snapshots table.
    if version < 7:
        _add_column_if_missing(conn, "exams", "instructions", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "exams", "proctored", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "exams", "proctor_interval", "INTEGER NOT NULL DEFAULT 20")
        version = 7
    # v8 added student enrollment: batch/roll on users, a link from an attempt to the
    # student who made it, and the exam_assignments table (arrives via CREATE above).
    if version < 8:
        _add_column_if_missing(conn, "users", "batch", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "users", "roll_no", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "exam_attempts", "user_id", "INTEGER")
        version = 8
    # Future schema changes go here:
    #   if version < 9:
    #       conn.execute("ALTER TABLE users ADD COLUMN ...")
    #       version = 9
    if version != from_version:
        conn.execute("UPDATE schema_info SET version = ? WHERE id = 1", (version,))


# --------------------------------------------------------------------------------------
# Query helpers
# --------------------------------------------------------------------------------------

def query(sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
    return connect().execute(sql, tuple(params)).fetchall()


def one(sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
    return connect().execute(sql, tuple(params)).fetchone()


def scalar(sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
    row = one(sql, params)
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """Run a write and return lastrowid for an INSERT, else the number of rows changed.

    The statement kind has to decide this. ``cursor.lastrowid`` keeps the id from the
    last INSERT on the connection, so a plain ``lastrowid or rowcount`` would report
    that stale id for every UPDATE and DELETE — making "removed N rows" wrong and
    truthiness checks pass on deletes that matched nothing.
    """
    with _lock:
        conn = connect()
        cursor = conn.execute(sql, tuple(params))
        conn.commit()
        if sql.lstrip()[:6].upper() == "INSERT":
            return cursor.lastrowid or cursor.rowcount
        return cursor.rowcount


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]
