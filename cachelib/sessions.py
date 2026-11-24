"""Sessions: one build's cache lifecycle, logged append-only in sqlite.

The database is a *history*, not the cache. Store manifests stay authoritative;
drop the file and the log can be rebuilt by replaying sessions against the store.
That distinction is what keeps the interesting properties:

* **Append-only.** Every operation is one INSERT. A close, a failure, a re-close
  is another event. Nothing is ever UPDATEd, so a recorded session cannot be
  quietly rewritten and replay means what it says.
* **Single writer, WAL.** Concurrent readers never block on a publish.
* **Derived state.** `status` is computed from the events, not maintained in a
  column that could disagree with them.

Replay re-executes a session's recorded operations against a store and reports,
per step, whether the decision is still the one that was taken. A dry run only
computes; a live run also reads the blobs it would apply, which proves the bytes
are still there. Publishing is never re-executed: replaying a publish against a
live store would duplicate entries, so a live replay skips it and says so.
"""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .chain import PushPlan, RestorePlan, plan_push, plan_restore
from .manifest import Key, Manifest
from .store import FileStore

OPEN = "open"
CLOSED = "closed"
FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id          TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    family      TEXT NOT NULL,
    platform    TEXT NOT NULL,
    identity    TEXT NOT NULL,
    ref         TEXT NOT NULL DEFAULT '',
    sha         TEXT NOT NULL DEFAULT '',
    run_id      TEXT NOT NULL DEFAULT '',
    job         TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event (
    session_id  TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    op          TEXT NOT NULL,
    seq         INTEGER,
    digest      TEXT,
    bytes       INTEGER,
    outcome     TEXT NOT NULL DEFAULT 'ok',
    detail      TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL,
    PRIMARY KEY (session_id, ordinal)
);
CREATE INDEX IF NOT EXISTS event_session ON event (session_id, ordinal);
"""

# Operations the logger understands. Anything else is stored and reported, but
# replay marks it as not replayable rather than guessing.
RESTORE = "restore"
PUBLISH = "publish"
CLOSE = "close"
FAIL = "fail"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def restore_signature(plan: RestorePlan) -> str:
    """What a restore decided, in one comparable string."""
    if not plan.hit:
        return f"miss:{plan.reason}"
    return "|".join(f"s{layer.seq:06d}:{layer.digest}" for layer in plan.layers)


def publish_signature(plan: PushPlan) -> str:
    """What a publish decided, in one comparable string."""
    parent = plan.parent.digest if plan.parent else "-"
    return f"{plan.kind} s{plan.seq:06d} parent={parent}"


@dataclass(frozen=True)
class Session:
    id: str
    key: Key
    identity: str
    ref: str
    sha: str
    run_id: str
    job: str
    started_at: str
    status: str

    @property
    def closed(self) -> bool:
        return self.status in (CLOSED, FAILED)


class SessionLog:
    """`sqlite.db`: sessions and their events, append-only."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")

    # -- lifecycle ---------------------------------------------------------
    def close_connection(self) -> None:
        self._db.close()

    def __enter__(self) -> SessionLog:
        return self

    def __exit__(self, *_: object) -> None:
        self.close_connection()

    # -- writes (INSERT only) ---------------------------------------------
    def open(
        self,
        key: Key,
        identity: str,
        ref: str = "",
        sha: str = "",
        run_id: str = "",
        job: str = "",
    ) -> Session:
        session_id = uuid.uuid4().hex
        started = now()
        self._db.execute(
            "INSERT INTO session (id, repo, family, platform, identity, ref, sha, "
            "run_id, job, started_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                key.repo,
                key.family,
                key.platform,
                identity,
                ref,
                sha,
                run_id,
                job,
                started,
            ),
        )
        return Session(session_id, key, identity, ref, sha, run_id, job, started, OPEN)

    def append(
        self,
        session_id: str,
        op: str,
        *,
        seq: int | None = None,
        digest: str | None = None,
        size: int | None = None,
        outcome: str = "ok",
        detail: str = "",
    ) -> int:
        """Append one event; returns its ordinal. Never updates an earlier row."""
        ordinal = self._next_ordinal(session_id)
        self._db.execute(
            "INSERT INTO event (session_id, ordinal, op, seq, digest, bytes, outcome, "
            "detail, at) VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, ordinal, op, seq, digest, size, outcome, detail, now()),
        )
        return ordinal

    def seal(self, session_id: str, outcome: str = CLOSED, detail: str = "") -> int:
        """Close a session. Sealing twice appends a second event and changes nothing."""
        session = self.get(session_id)
        if session is None:
            raise KeyError(f"no session {session_id}")
        op = FAIL if outcome == FAILED else CLOSE
        return self.append(session_id, op, outcome=outcome, detail=detail)

    # -- reads -------------------------------------------------------------
    def _next_ordinal(self, session_id: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(ordinal), 0) AS top FROM event WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["top"]) + 1

    def events(self, session_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT ordinal, op, seq, digest, bytes, outcome, detail, at FROM event "
            "WHERE session_id = ? ORDER BY ordinal",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get(self, session_id: str) -> Session | None:
        row = self._db.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        return Session(
            row["id"],
            Key(row["repo"], row["family"], row["platform"]),
            row["identity"],
            row["ref"],
            row["sha"],
            row["run_id"],
            row["job"],
            row["started_at"],
            self._status(session_id),
        )

    def sessions(self, repo: str | None = None, limit: int = 100) -> list[Session]:
        if repo is None:
            rows = self._db.execute(
                "SELECT id FROM session ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT id FROM session WHERE repo = ? ORDER BY started_at DESC LIMIT ?",
                (repo, limit),
            ).fetchall()
        return [s for s in (self.get(row["id"]) for row in rows) if s is not None]

    def _status(self, session_id: str) -> str:
        row = self._db.execute(
            "SELECT op FROM event WHERE session_id = ? AND op IN (?, ?) "
            "ORDER BY ordinal DESC LIMIT 1",
            (session_id, CLOSE, FAIL),
        ).fetchone()
        if row is None:
            return OPEN
        return CLOSED if row["op"] == CLOSE else FAILED


# -- replay ----------------------------------------------------------------


@dataclass(frozen=True)
class ReplayStep:
    ordinal: int
    op: str
    recorded: str
    replayed: str
    matches: bool
    note: str


@dataclass(frozen=True)
class ReplayReport:
    session_id: str
    dry_run: bool
    steps: list[ReplayStep]

    @property
    def identical(self) -> bool:
        return all(step.matches for step in self.steps)

    @property
    def divergent(self) -> list[ReplayStep]:
        return [step for step in self.steps if not step.matches]


def replay(
    log: SessionLog, store: FileStore, session_id: str, dry_run: bool = True
) -> ReplayReport:
    """Re-derive a recorded session's decisions against the store as it is now.

    Every restore/publish event carries the decision it took (`detail`). Replay
    recomputes that decision from current store state and compares. A live run
    (dry_run=False) additionally fetches the layers a restore would apply, which
    is the only way to know the bytes still exist; publishes are never
    re-executed, because that would duplicate live entries.
    """
    session = log.get(session_id)
    if session is None:
        raise KeyError(f"no session {session_id}")
    entries = store.entries(session.key)
    by_seq = {m.seq: m for m in entries}
    # The store as the session saw it: entries published before the session's
    # own first publish, plus whatever the session published as we walk. Without
    # this, replaying a session that started empty and then published would
    # "diverge" on its own opening miss.
    recorded_publishes = [
        e["seq"] for e in log.events(session_id) if e["op"] == PUBLISH and e["seq"] is not None
    ]
    first = min(recorded_publishes) if recorded_publishes else None
    view = [m for m in entries if first is None or m.seq < first]
    steps: list[ReplayStep] = []
    for event in log.events(session_id):
        op = event["op"]
        recorded = event["detail"]
        if op == RESTORE:
            plan = plan_restore(view, session.identity)
            replayed = restore_signature(plan)
            note = "recomputed"
            if not dry_run and plan.hit:
                with tempfile.TemporaryDirectory() as tmp:
                    for index, layer in enumerate(plan.layers):
                        store.get_blob(layer, Path(tmp) / f"layer-{index}")
                note = f"recomputed and read {len(plan.layers)} layer(s)"
        elif op == PUBLISH:
            plan = plan_push(view, session.key, session.identity)
            replayed = publish_signature(plan)
            note = "recomputed" if dry_run else "recomputed (publish not re-executed)"
            published = by_seq.get(event["seq"] or -1)
            if published is not None:
                view.append(published)
            else:
                note += "; the entry this session published is no longer in the store"
        else:
            steps.append(
                ReplayStep(event["ordinal"], op, recorded, "", True, "not replayable by design")
            )
            continue
        steps.append(
            ReplayStep(event["ordinal"], op, recorded, replayed, recorded == replayed, note)
        )
    return ReplayReport(session_id, dry_run, steps)


def record_restore(log: SessionLog, session_id: str, plan: RestorePlan) -> int:
    """Log what a restore decided, so replay has something to re-derive."""
    if not plan.hit:
        return log.append(session_id, RESTORE, outcome="miss", detail=restore_signature(plan))
    tip = plan.tip
    return log.append(
        session_id,
        RESTORE,
        seq=tip.seq if tip else None,
        digest=tip.digest if tip else None,
        detail=restore_signature(plan),
    )


def record_publish(
    log: SessionLog, session_id: str, plan: PushPlan, manifest: Manifest | None = None
) -> int:
    return log.append(
        session_id,
        PUBLISH,
        seq=plan.seq,
        digest=manifest.digest if manifest else None,
        size=manifest.bytes if manifest else None,
        detail=publish_signature(plan),
    )


__all__ = [
    "CLOSED",
    "FAILED",
    "OPEN",
    "PUBLISH",
    "RESTORE",
    "ReplayReport",
    "ReplayStep",
    "Session",
    "SessionLog",
    "publish_signature",
    "record_publish",
    "record_restore",
    "replay",
    "restore_signature",
]
