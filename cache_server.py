#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# ///
"""cache-server: sessions, replay and a store, over HTTP.

Deployment 2 of `docs/design.md`. The server decides; the client transfers.

    POST /sessions                      open a session   -> {id, ...}
    GET  /sessions/{id}                 session + events
    GET  /sessions?repo=&limit=         recent sessions
    POST /sessions/{id}/restore         plan a restore    -> {hit, layers[], signature}
    POST /sessions/{id}/publish         upload layer bytes -> {kind, seq, digest}
    GET  /blobs/{digest}                the bytes a restore named, digest-checked
    POST /sessions/{id}/close           seal it           -> {status}
    POST /sessions/{id}/replay          re-derive it      -> {identical, steps[]}
    GET  /health                        liveness + counts

Every session operation appends to `sqlite.db`; nothing is updated in place.
`restore` returns a *decision* and the layer digests -- the following `GET
/blobs/...` is what moves bytes, so a client can decide not to download.

Errors are HTTP codes with a JSON body: 400 malformed input or an unusable
digest, 404 unknown session/blob, 409 an operation on a sealed session, 500 an
unexpected failure. A cache server that returns 500 for "no cache" would be a
worse cache than no cache at all, so a miss is a 200 with `"hit": false`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import urllib.parse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cachelib.chain import plan_push, plan_restore
from cachelib.manifest import Manifest, ManifestError
from cachelib.retention import sweep_all
from cachelib.sessions import (
    Session,
    SessionLog,
    publish_signature,
    record_publish,
    record_restore,
    replay,
    restore_signature,
)
from cachelib.store import FileStore, digest_of

ROUTES = {
    "sessions": re.compile(r"^/sessions$"),
    "session": re.compile(r"^/sessions/(?P<id>[0-9a-f]{32})$"),
    "restore": re.compile(r"^/sessions/(?P<id>[0-9a-f]{32})/restore$"),
    "publish": re.compile(r"^/sessions/(?P<id>[0-9a-f]{32})/publish$"),
    "close": re.compile(r"^/sessions/(?P<id>[0-9a-f]{32})/close$"),
    "replay": re.compile(r"^/sessions/(?P<id>[0-9a-f]{32})/replay$"),
    "blob": re.compile(r"^/blobs/(?P<digest>sha256:[0-9a-f]{64})$"),
    "sweep": re.compile(r"^/sweep$"),
    "health": re.compile(r"^/health$"),
}

MAX_PUBLISH_BYTES = int(os.environ.get("CACHECTL_MAX_PUBLISH_BYTES", 8 << 30))


class Conflict(ValueError):
    """Well-formed request, forbidden by the session's state (409, not 400)."""


class State:
    """Everything a request needs, injected rather than imported as globals."""

    def __init__(self, store_root: Path, db_path: Path) -> None:
        self.store_root = store_root
        self.db_path = db_path

    def log(self) -> SessionLog:
        return SessionLog(self.db_path)

    def store(self) -> FileStore:
        return FileStore(self.store_root)


def session_json(session: Session) -> dict:
    return {
        "id": session.id,
        "repo": session.key.repo,
        "family": session.key.family,
        "platform": session.key.platform,
        "identity": session.identity,
        "ref": session.ref,
        "sha": session.sha,
        "run_id": session.run_id,
        "job": session.job,
        "started_at": session.started_at,
        "status": session.status,
    }


class Handler(BaseHTTPRequestHandler):
    state: State  # set by the factory
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt: str, *args: object) -> None:
        if os.environ.get("CACHECTL_VERBOSE"):
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def _send(
        self,
        code: int,
        payload: dict | None = None,
        body: bytes | None = None,
        content_type: str = "application/json",
    ) -> None:
        data = body if body is not None else json.dumps(payload or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: int, message: str) -> None:
        self._send(code, {"error": message})

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_PUBLISH_BYTES:
            raise ValueError(f"body of {length} bytes is outside the accepted range")
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict:
        raw = self._body()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"body is not JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise ValueError("body must be a JSON object")
        return parsed

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def _route(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        for name, pattern in ROUTES.items():
            if (match := pattern.match(parsed.path)) is None:
                continue
            handler = getattr(self, f"{method.lower()}_{name}", None)
            if handler is None:
                self._error(405, f"{method} is not allowed on {parsed.path}")
                return
            try:
                handler(match, urllib.parse.parse_qs(parsed.query))
            except KeyError as error:
                self._error(404, str(error).strip("'\""))
            except ManifestError as error:
                self._error(400, str(error))
            except Conflict as error:
                self._error(409, str(error))
            except ValueError as error:
                self._error(400, str(error))
            except FileNotFoundError as error:
                self._error(404, str(error))
            except Exception as error:  # noqa: BLE001 - the last line of defence
                self._error(500, f"{type(error).__name__}: {error}")
            return
        self._error(404, f"no route for {parsed.path}")

    # -- operations --------------------------------------------------------
    def get_health(self, _match: re.Match, _query: dict) -> None:
        with self.state.log() as log:
            sessions = log.sessions(limit=1)
        store = self.state.store()
        self._send(
            200,
            {
                "ok": True,
                "store": str(self.state.store_root),
                "db": str(self.state.db_path),
                "keys": len(store.keys()),
                "has_sessions": bool(sessions),
            },
        )

    def post_sessions(self, _match: re.Match, _query: dict) -> None:
        body = self._json_body()
        for field in ("repo", "family", "platform", "identity"):
            if not body.get(field):
                raise ValueError(f"{field} is required")
        from cachelib.manifest import Key

        key = Key(body["repo"], body["family"], body["platform"])
        with self.state.log() as log:
            session = log.open(
                key,
                body["identity"],
                ref=body.get("ref", ""),
                sha=body.get("sha", ""),
                run_id=body.get("run_id", ""),
                job=body.get("job", ""),
            )
            payload = session_json(session)
        payload["events"] = []
        self._send(201, payload)

    def get_sessions(self, _match: re.Match, query: dict) -> None:
        repo = (query.get("repo") or [None])[0]
        limit = int((query.get("limit") or ["50"])[0])
        with self.state.log() as log:
            sessions = log.sessions(repo=repo, limit=limit)
        self._send(200, {"sessions": [session_json(s) for s in sessions]})

    def get_session(self, match: re.Match, _query: dict) -> None:
        session_id = match["id"]
        with self.state.log() as log:
            session = log.get(session_id)
            if session is None:
                raise KeyError(f"no session {session_id}")
            payload = session_json(session)
            payload["events"] = log.events(session_id)
        self._send(200, payload)

    def post_restore(self, match: re.Match, _query: dict) -> None:
        session_id = match["id"]
        with self.state.log() as log:
            session = self._open_session(log, session_id)
            plan = plan_restore(self.state.store().entries(session.key), session.identity)
            record_restore(log, session_id, plan)
            layers = [
                {
                    "seq": layer.seq,
                    "digest": layer.digest,
                    "bytes": layer.bytes,
                    "kind": layer.kind,
                }
                for layer in plan.layers
            ]
            signature = restore_signature(plan)
        self._send(
            200,
            {
                "hit": plan.hit,
                "reason": plan.reason,
                "layers": layers,
                "bytes": plan.missing_bytes(),
                "signature": signature,
            },
        )

    def post_publish(self, match: re.Match, _query: dict) -> None:
        session_id = match["id"]
        with self.state.log() as log:
            session = self._open_session(log, session_id)
            body = self._body()
            if not body:
                raise ValueError("publish needs the layer bytes as the request body")
            store = self.state.store()
            plan = plan_push(store.entries(session.key), session.key, session.identity)
            with tempfile.TemporaryDirectory() as tmp:
                staged = Path(tmp) / "layer"
                staged.write_bytes(body)
                manifest = Manifest(
                    key=session.key,
                    seq=plan.seq,
                    identity=session.identity,
                    kind=plan.kind,
                    parent=plan.parent.digest if plan.parent else None,
                    digest=digest_of(staged),
                    bytes=staged.stat().st_size,
                    created_at=session.started_at,
                    ref=session.ref,
                    source_sha=session.sha,
                    run_id=session.run_id,
                )
                store.put_blob(manifest, staged)
            store.put_manifest(manifest)
            record_publish(log, session_id, plan, manifest)
        self._send(
            201,
            {
                "kind": manifest.kind,
                "seq": manifest.seq,
                "digest": manifest.digest,
                "bytes": manifest.bytes,
                "signature": publish_signature(plan),
            },
        )

    def post_sweep(self, _match: re.Match, _query: dict) -> None:
        """Bound the store: the scheduled sweep, on demand.

        Defaults to a dry run on purpose -- deletion is an administrative decision
        and a caller that wants it has to say so. A sweep never blocks a publish.
        """
        body = self._json_body()
        keep = int(body.get("keep", 2))
        if keep < 1:
            # Validate the request, not the store: a bad keep is a bad keep even
            # when there is nothing to sweep.
            raise ValueError("keep must be at least 1")
        max_bytes = body.get("max_bytes")
        protect = set(body.get("protect") or [])
        dry_run = bool(body.get("dry_run", True))
        reports = sweep_all(
            self.state.store(), keep=keep, max_bytes=max_bytes, protect=protect, dry_run=dry_run
        )
        self._send(200, {"dry_run": dry_run, "keys": len(reports), "reports": reports})

    def get_blob(self, match: re.Match, _query: dict) -> None:
        digest = match["digest"]
        store = self.state.store()
        path = store.root / "blobs" / digest.replace(":", "/", 1)
        if not path.exists():
            raise FileNotFoundError(f"blob {digest} is not in the store")
        self._send(200, body=path.read_bytes(), content_type="application/octet-stream")

    def post_close(self, match: re.Match, _query: dict) -> None:
        session_id = match["id"]
        body = self._json_body()
        outcome = body.get("outcome", "closed")
        with self.state.log() as log:
            self._open_session(log, session_id, allow_sealed=True)
            log.seal(session_id, outcome=outcome, detail=body.get("detail", ""))
            session = log.get(session_id)
        self._send(200, {"id": session_id, "status": session.status, "outcome": outcome})

    def post_replay(self, match: re.Match, _query: dict) -> None:
        session_id = match["id"]
        body = self._json_body()
        dry_run = bool(body.get("dry_run", True))
        with self.state.log() as log:
            if log.get(session_id) is None:
                raise KeyError(f"no session {session_id}")
            report = replay(log, self.state.store(), session_id, dry_run=dry_run)
        self._send(
            200,
            {
                "session": session_id,
                "dry_run": dry_run,
                "identical": report.identical,
                "steps": [asdict(step) for step in report.steps],
            },
        )

    # -- helpers -----------------------------------------------------------
    def _open_session(self, log: SessionLog, session_id: str, allow_sealed: bool = False):
        session = log.get(session_id)
        if session is None:
            raise KeyError(f"no session {session_id}")
        if session.closed and not allow_sealed:
            raise Conflict(f"session {session_id} is {session.status}; close is the last word")
        return session


def make_server(state: State, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"state": state})
    return ThreadingHTTPServer((host, port), handler)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--store", default=".cachectl", help="store root (file backend)")
    parser.add_argument("--db", default="sqlite.db", help="session log")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    state = State(Path(args.store), Path(args.db))
    server = make_server(state, args.host, args.port)
    host, port = server.server_address[:2]
    print(
        f"cache-server listening on http://{host}:{port} "
        f"(store={state.store_root}, db={state.db_path})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
