"""Client for the cache service: a CI job's whole session, in five calls.

    with CacheClient("http://127.0.0.1:8787") as cache:
        session = cache.open("obs-rust/obs-studio", "app", "linux-x64", identity)
        decision = cache.restore(session)
        if decision.hit:
            for layer in decision.layers:
                cache.download(layer.digest, workdir / f"layer-{layer.seq}")
        ...
        cache.publish(session, layer_bytes)
        cache.close(session)

Everything the client does is a session operation, so the server's log records
it and a later replay can re-derive it. Nothing here decides anything: a
restore that misses is a normal result (`hit is False`), not an exception,
because a probe that raises on a cold cache forces every caller into a
try/except that hides real failures.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

JSON_TYPE = "application/json"


class CacheError(RuntimeError):
    """An HTTP failure from the service, carrying the status and its message."""

    def __init__(self, status: int, message: str, path: str = "") -> None:
        super().__init__(f"{status} {path}: {message}".strip())
        self.status = status
        self.message = message
        self.path = path


@dataclass(frozen=True)
class Layer:
    seq: int
    digest: str
    bytes: int
    kind: str


@dataclass(frozen=True)
class Restore:
    hit: bool
    reason: str
    layers: list[Layer] = field(default_factory=list)
    bytes: int = 0
    signature: str = ""

    @property
    def to_download(self) -> list[Layer]:
        return list(self.layers)


class CacheClient:
    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def __enter__(self) -> CacheClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    # -- transport ---------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = JSON_TYPE,
    ) -> bytes:
        request = urllib.request.Request(self.base_url + path, data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                message = json.loads(raw).get("error", raw.decode(errors="replace"))
            except (json.JSONDecodeError, AttributeError):
                message = raw.decode(errors="replace")
            raise CacheError(error.code, message, path) from error

    def _json(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        return json.loads(self._request(method, path, body) or b"{}")

    # -- session -----------------------------------------------------------
    def health(self) -> dict:
        return self._json("GET", "/health")

    def open(
        self,
        repo: str,
        family: str,
        platform: str,
        identity: str,
        ref: str = "",
        sha: str = "",
        run_id: str = "",
        job: str = "",
    ) -> str:
        payload = self._json(
            "POST",
            "/sessions",
            {
                "repo": repo,
                "family": family,
                "platform": platform,
                "identity": identity,
                "ref": ref,
                "sha": sha,
                "run_id": run_id,
                "job": job,
            },
        )
        return payload["id"]

    def restore(self, session: str) -> Restore:
        payload = self._json("POST", f"/sessions/{session}/restore")
        layers = [
            Layer(layer["seq"], layer["digest"], layer["bytes"], layer["kind"])
            for layer in payload.get("layers", [])
        ]
        return Restore(
            bool(payload.get("hit")),
            payload.get("reason", ""),
            layers,
            int(payload.get("bytes", 0)),
            payload.get("signature", ""),
        )

    def publish(self, session: str, data: bytes) -> dict:
        body = self._request(
            "POST",
            f"/sessions/{session}/publish",
            data,
            content_type="application/octet-stream",
        )
        return json.loads(body or b"{}")

    def download(self, digest: str, destination: str | Path) -> Path:
        data = self._request("GET", f"/blobs/{digest}")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return destination

    def close(self, session: str, outcome: str = "closed", detail: str = "") -> dict:
        return self._json(
            "POST", f"/sessions/{session}/close", {"outcome": outcome, "detail": detail}
        )

    def replay(self, session: str, dry_run: bool = True) -> dict:
        return self._json("POST", f"/sessions/{session}/replay", {"dry_run": dry_run})

    def session(self, session: str) -> dict:
        return self._json("GET", f"/sessions/{session}")

    def sessions(self, repo: str | None = None, limit: int = 50) -> list[dict]:
        query = f"?limit={limit}" + (f"&repo={repo}" if repo else "")
        return self._json("GET", "/sessions" + query).get("sessions", [])


__all__ = ["CacheClient", "CacheError", "Layer", "Restore"]
