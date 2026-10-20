import os
import sqlite3
import time
import json
from .config import CORE_IDS
from .security import canonical


class State:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS watch(core TEXT PRIMARY KEY, enabled INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, core TEXT NOT NULL, sha TEXT NOT NULL,
            payload TEXT NOT NULL, notes TEXT NOT NULL, status TEXT NOT NULL,
            message_id TEXT, run_id TEXT, run_url TEXT, notified INTEGER NOT NULL DEFAULT 0,
            created INTEGER NOT NULL, result TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT '', reported_stage TEXT NOT NULL DEFAULT '',
            stage_pct INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS release_summaries(core TEXT PRIMARY KEY, revision TEXT NOT NULL, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, added_at INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS thread_issues(
            thread_id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            issue_url TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
          );
          CREATE INDEX IF NOT EXISTS job_core_sha ON jobs(core,sha,status);
          CREATE INDEX IF NOT EXISTS job_status ON jobs(status,created);
        """)
        if "result" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN result TEXT NOT NULL DEFAULT ''")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        for name in ("stage", "reported_stage"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        if "stage_pct" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN stage_pct INTEGER NOT NULL DEFAULT 0")
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO watch VALUES (?, 1)", [(x,) for x in CORE_IDS])
            self.db.execute("UPDATE jobs SET status='uncertain' WHERE status='dispatching'")

    def admins(self):
        return {int(r["user_id"]) for r in self.db.execute("SELECT user_id FROM admins")}

    def add_admin(self, user_id):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO admins(user_id, added_at) VALUES (?, ?)", (int(user_id), int(time.time())))

    def remove_admin(self, user_id):
        with self.db:
            self.db.execute("DELETE FROM admins WHERE user_id = ?", (int(user_id),))

    def is_admin(self, user_id, owner_id):
        try:
            uid = int(user_id)
            return uid == int(owner_id) or uid in self.admins()
        except (ValueError, TypeError):
            return False

    def get_thread_issue(self, thread_id):
        try:
            row = self.db.execute("SELECT * FROM thread_issues WHERE thread_id=?", (int(thread_id),)).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def save_thread_issue(self, thread_id, repo, issue_number, issue_url):
        now = int(time.time())
        existing = self.get_thread_issue(thread_id)
        created_at = existing["created_at"] if existing else now
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO thread_issues(thread_id, repo, issue_number, issue_url, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (int(thread_id), str(repo), int(issue_number), str(issue_url), created_at, now)
            )

    def watches(self):
        return {r["core"]: bool(r["enabled"]) for r in self.db.execute("SELECT * FROM watch")}

    def set_watch(self, core, enabled):
        if core not in CORE_IDS:
            raise ValueError("Unknown core")
        with self.db:
            self.db.execute("UPDATE watch SET enabled=? WHERE core=?", (int(enabled), core))
            if not enabled:
                self.db.execute("UPDATE jobs SET status='rejected' WHERE core=? AND status='pending'", (core,))

    def insert(self, payload, notes):
        with self.db:
            result = self.db.execute(
                "INSERT OR IGNORE INTO jobs(id,core,sha,payload,notes,status,created) VALUES(?,?,?,?,?,'pending',?)",
                (payload["id"], payload["core"], payload["sha"], canonical(payload), notes, int(time.time())))
        return result.rowcount == 1

    def get(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def seen(self, core, sha):
        return self.latest_for(core, sha) is not None

    def latest_for(self, core, sha):
        row = self.db.execute("SELECT * FROM jobs WHERE core=? AND sha=? AND status NOT IN ('stale','expired') ORDER BY created DESC, rowid DESC LIMIT 1", (core, sha)).fetchone()
        return dict(row) if row else None

    def jobs(self, *statuses):
        placeholders = ",".join("?" for _ in statuses)
        rows = self.db.execute(f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created DESC LIMIT 200", statuses)
        return list(reversed([dict(r) for r in rows]))

    def unnotified(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM jobs WHERE status IN ('published','failed','cancelled') AND notified=0 ORDER BY created LIMIT 50")]

    def transition(self, job_id, previous, following):
        with self.db:
            result = self.db.execute("UPDATE jobs SET status=? WHERE id=? AND status=?", (following, job_id, previous))
        return result.rowcount == 1

    def update(self, job_id, **fields):
        if not fields or not set(fields) <= {"message_id", "run_id", "run_url", "notified", "result", "notes", "stage", "reported_stage", "stage_pct"}:
            raise ValueError("Invalid job fields")
        with self.db:
            self.db.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), job_id))

    def release_summary(self, core, revision=None):
        row = self.db.execute("SELECT revision, data FROM release_summaries WHERE core=?", (core,)).fetchone()
        if row and (revision is None or row["revision"] == revision):
            return json.loads(row["data"])
        return None

    def cache_release(self, core, revision, data):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO release_summaries VALUES (?,?,?)", (core, revision, json.dumps(data)))

    def expire(self):
        for job in self.jobs("pending"):
            if json.loads(job["payload"])["expires"] <= time.time():
                self.transition(job["id"], "pending", "expired")

    def close(self):
        self.db.close()
