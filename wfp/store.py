"""SQLite persistence. All state changes for ONE step (state + history + audit) are ONE transaction.

Tables: requests (current state, pinned config snapshot, optimistic-lock `version`),
        history  (every status/stage change), audit (explainable decision trail).
UNIQUE(workflow, idem_key) makes duplicate suppression safe even under concurrent submits.
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from .errors import ConflictError, NotFound

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(
  id TEXT PRIMARY KEY, workflow TEXT NOT NULL, workflow_version INTEGER NOT NULL,
  idem_key TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL,
  current_stage TEXT NOT NULL, payload TEXT NOT NULL, context TEXT NOT NULL,
  config TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(workflow, idem_key));
CREATE TABLE IF NOT EXISTS history(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, ts TEXT NOT NULL,
  from_status TEXT, to_status TEXT NOT NULL, stage TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, seq INTEGER NOT NULL,
  ts TEXT NOT NULL, stage TEXT, kind TEXT NOT NULL, rule_id TEXT, message TEXT NOT NULL, detail TEXT NOT NULL);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path=":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript(SCHEMA)
        self.fault = None  # test hook: callable invoked inside the transaction to simulate a failed write

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    # ---------- writes ----------
    def _add_audit(self, rid, entries):
        seq = self.conn.execute("SELECT COALESCE(MAX(seq),0) FROM audit WHERE request_id=?", (rid,)).fetchone()[0]
        for e in entries:
            seq += 1
            self.conn.execute(
                "INSERT INTO audit(request_id,seq,ts,stage,kind,rule_id,message,detail) VALUES(?,?,?,?,?,?,?,?)",
                (rid, seq, now(), e.get("stage"), e["kind"], e.get("rule_id"), e["message"],
                 json.dumps(e.get("detail", {}))))

    def create_request(self, rid, cfg, key, phash, payload):
        """Insert new request. Returns (row, created). created=False => duplicate key already existed."""
        ts = now()
        try:
            with self.tx():
                self.conn.execute(
                    "INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, cfg["name"], cfg["version"], key, phash, "RECEIVED", cfg["start"],
                     json.dumps(payload), json.dumps({"input": payload, "ext": {}}),
                     json.dumps(cfg), 1, ts, ts))
                self.conn.execute(
                    "INSERT INTO history(request_id,ts,from_status,to_status,stage,note) VALUES(?,?,?,?,?,?)",
                    (rid, ts, None, "RECEIVED", cfg["start"], "request accepted"))
                self._add_audit(rid, [{"kind": "SYSTEM", "stage": cfg["start"],
                                       "message": f"Request received; pinned to workflow '{cfg['name']}' v{cfg['version']}",
                                       "detail": {"idempotency_key": key, "payload_hash": phash}}])
            return self.get(rid), True
        except sqlite3.IntegrityError:
            return self.get_by_key(cfg["name"], key), False

    def commit_step(self, rid, version, status, stage, ctx, history, audits):
        """Atomically: optimistic-lock update + history rows + audit rows."""
        with self.tx():
            cur = self.conn.execute(
                "UPDATE requests SET status=?, current_stage=?, context=?, version=version+1, updated_at=? "
                "WHERE id=? AND version=?", (status, stage, json.dumps(ctx), now(), rid, version))
            if cur.rowcount == 0:
                raise ConflictError(f"request {rid} was modified concurrently")
            for h in history:
                self.conn.execute(
                    "INSERT INTO history(request_id,ts,from_status,to_status,stage,note) VALUES(?,?,?,?,?,?)",
                    (rid, now(), h["from"], h["to"], h["stage"], h["note"]))
            self._add_audit(rid, audits)
            if self.fault:
                self.fault()  # may raise StoreError -> whole step rolls back
        return self.get(rid)

    # ---------- reads ----------
    @staticmethod
    def _row(r):
        d = dict(r)
        for k in ("payload", "context", "config"):
            d[k] = json.loads(d[k])
        return d

    def get(self, rid):
        with self.lock:
            r = self.conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r:
            raise NotFound(f"request '{rid}' not found")
        return self._row(r)

    def get_by_key(self, workflow, key):
        with self.lock:
            r = self.conn.execute("SELECT * FROM requests WHERE workflow=? AND idem_key=?", (workflow, key)).fetchone()
        return self._row(r) if r else None

    def history(self, rid):
        with self.lock:
            rows = self.conn.execute("SELECT ts,from_status,to_status,stage,note FROM history WHERE request_id=? ORDER BY id", (rid,)).fetchall()
        return [dict(r) for r in rows]

    def audit(self, rid):
        with self.lock:
            rows = self.conn.execute("SELECT seq,ts,stage,kind,rule_id,message,detail FROM audit WHERE request_id=? ORDER BY seq", (rid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out

    def count_requests(self):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
