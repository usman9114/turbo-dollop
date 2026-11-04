"""Record / replay of OpenRouter traffic as JSON fixtures (offline evals and tests)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx2 as httpx


def request_hash(body: dict) -> str:
    canon = {k: v for k, v in body.items() if k != "seed"}
    return hashlib.sha1(json.dumps(canon, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _is_chat(request: httpx.Request) -> bool:
    return request.method == "POST" and request.url.path.endswith("/chat/completions")


class RecordingTransport(httpx.BaseTransport):
    """Wraps a real transport; writes DIR/<sha1(request body)>.json for every chat completion."""

    def __init__(self, inner: httpx.BaseTransport, directory: str | Path):
        self.inner, self.dir = inner, Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        resp = self.inner.handle_request(request)
        if _is_chat(request):
            resp.read()
            body = json.loads(request.content)
            try:
                payload = resp.json()
            except Exception:
                payload = {"raw": resp.text}
            keep = {k: v for k, v in resp.headers.items() if k.lower() in ("retry-after",) or k.lower().startswith("x-ratelimit")}
            (self.dir / f"{request_hash(body)}.json").write_text(
                json.dumps({"request": body, "status": resp.status_code, "headers": keep, "response": payload},
                           ensure_ascii=False, indent=1), encoding="utf-8")
        return resp

    def close(self) -> None:
        self.inner.close()


class ReplayTransport(httpx.BaseTransport):
    """Answers chat completions from recorded fixtures; anything else (or a miss) is a 404, never a network call."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.misses: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not _is_chat(request):
            return httpx.Response(404, json={"error": {"message": "replay: not recorded"}})
        h = request_hash(json.loads(request.content))
        path = self.dir / f"{h}.json"
        if not path.exists():
            self.misses.append(h)
            return httpx.Response(404, json={"error": {"message": f"replay: no fixture {h}"}})
        rec = json.loads(path.read_text(encoding="utf-8"))
        return httpx.Response(rec["status"], json=rec["response"], headers=rec.get("headers") or {})
