"""Async Ollama client with multi-server routing, fallback, and retry.

Ollama exposes a very simple HTTP API:

* ``POST /api/chat``   — multi-turn chat, streams JSON lines.
* ``POST /api/generate`` — single-turn text completion, streams too.
* ``GET  /api/tags``   — list installed models.
* ``GET  /``           — liveness (returns "Ollama is running").

This module wraps those endpoints, adds a per-server semaphore so we don't
overload any single GPU, and transparently falls back to the next candidate
in the role's list when the primary server is unreachable / erroring out.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import AsyncRetrying, RetryError, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import AppConfig, ModelPick, RoleConfig, get_config

log = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    pass


@dataclass
class ServerHandle:
    name: str
    url: str
    semaphore: asyncio.Semaphore
    client: httpx.AsyncClient
    last_error: str = ""
    online: bool = True
    models: list[str] = field(default_factory=list)


class OllamaRouter:
    def __init__(self, cfg: AppConfig | None = None) -> None:
        self.cfg = cfg or get_config()
        self._servers: dict[str, ServerHandle] = {}
        for name, srv in self.cfg.servers.items():
            self._servers[name] = ServerHandle(
                name=name,
                url=srv.url.rstrip("/"),
                semaphore=asyncio.Semaphore(srv.concurrency),
                client=httpx.AsyncClient(
                    base_url=srv.url.rstrip("/"),
                    timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=30.0),
                ),
            )

    async def close(self) -> None:
        for s in self._servers.values():
            await s.client.aclose()

    async def probe_all(self) -> list[dict[str, Any]]:
        async def _probe(handle: ServerHandle) -> dict[str, Any]:
            try:
                r = await handle.client.get("/api/tags", timeout=8.0)
                r.raise_for_status()
                models = [m["name"] for m in r.json().get("models", [])]
                handle.online = True
                handle.models = models
                handle.last_error = ""
                return {
                    "name": handle.name,
                    "url": handle.url,
                    "online": True,
                    "models": models,
                    "error": "",
                }
            except Exception as exc:  # pragma: no cover - network
                handle.online = False
                handle.last_error = str(exc)
                return {
                    "name": handle.name,
                    "url": handle.url,
                    "online": False,
                    "models": [],
                    "error": str(exc),
                }

        return await asyncio.gather(*(_probe(h) for h in self._servers.values()))

    def role(self, name: str) -> RoleConfig:
        if name not in self.cfg.roles:
            raise KeyError(f"Unknown role '{name}'")
        return self.cfg.roles[name]

    async def chat(
        self,
        role: str,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        stream_callback: Any | None = None,
    ) -> str:
        """Full (non-streaming from caller POV) chat completion.

        Internally we stream from Ollama so we can push partial tokens to the
        UI via ``stream_callback(partial_text, delta)``.
        """
        rc = self.role(role)
        last_exc: Exception | None = None
        for pick in rc.candidates():
            handle = self._servers.get(pick.server)
            if handle is None:
                last_exc = OllamaError(f"server '{pick.server}' not configured")
                continue
            try:
                return await self._chat_on(
                    handle,
                    pick,
                    messages,
                    json_mode=json_mode,
                    temperature=temperature if temperature is not None else rc.temperature,
                    num_ctx=num_ctx if num_ctx is not None else rc.num_ctx,
                    num_predict=num_predict if num_predict is not None else rc.num_predict,
                    idle_timeout=rc.idle_timeout_seconds,
                    call_timeout=rc.call_timeout_seconds,
                    stream_callback=stream_callback,
                )
            except Exception as exc:
                log.warning("role=%s server=%s model=%s failed: %s", role, pick.server, pick.model, exc)
                handle.last_error = str(exc)
                handle.online = False
                last_exc = exc
                continue
        raise OllamaError(
            f"All candidates exhausted for role '{role}': {last_exc!s}"
        ) from last_exc

    async def _chat_on(
        self,
        handle: ServerHandle,
        pick: ModelPick,
        messages: list[dict[str, str]],
        *,
        json_mode: bool,
        temperature: float,
        num_ctx: int,
        num_predict: int,
        idle_timeout: float,
        call_timeout: float,
        stream_callback: Any | None,
    ) -> str:
        body: dict[str, Any] = {
            "model": pick.model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx,
                "num_predict": num_predict,
            },
            "keep_alive": "30m",
        }
        if json_mode:
            body["format"] = "json"

        async def _attempt() -> str:
            async with handle.semaphore:
                acc: list[str] = []
                async with handle.client.stream("POST", "/api/chat", json=body) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                    resp.raise_for_status()
                    iterator = resp.aiter_lines().__aiter__()
                    while True:
                        # Watchdog: refuse to wait more than ``idle_timeout``
                        # between two tokens. A stuck / paged-out model on
                        # a busy GPU typically drops to <0.1 tok/s or stops
                        # entirely; this lets us fall back to the next
                        # candidate instead of hanging silently.
                        try:
                            line = await asyncio.wait_for(
                                iterator.__anext__(), timeout=idle_timeout
                            )
                        except StopAsyncIteration:
                            break
                        except TimeoutError as exc:
                            raise OllamaError(
                                f"idle timeout ({idle_timeout:.0f}s) on "
                                f"{handle.name}/{pick.model}: model produced "
                                f"no token for too long"
                            ) from exc
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if "error" in data:
                            raise OllamaError(data["error"])
                        msg = data.get("message") or {}
                        delta = msg.get("content") or ""
                        if delta:
                            acc.append(delta)
                            if stream_callback is not None:
                                try:
                                    res = stream_callback(delta)
                                    if asyncio.iscoroutine(res):
                                        await res
                                except Exception:
                                    log.exception("stream_callback failed")
                        if data.get("done"):
                            break
                return "".join(acc)

        async def _attempt_with_wall_timeout() -> str:
            if call_timeout and call_timeout > 0:
                try:
                    return await asyncio.wait_for(_attempt(), timeout=call_timeout)
                except TimeoutError as exc:
                    raise OllamaError(
                        f"call timeout ({call_timeout:.0f}s) on "
                        f"{handle.name}/{pick.model}"
                    ) from exc
            return await _attempt()

        try:
            async for attempt in AsyncRetrying(
                reraise=True,
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1.5, min=1, max=10),
                retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            ):
                with attempt:
                    return await _attempt_with_wall_timeout()
        except RetryError as exc:
            raise OllamaError(f"retries exhausted on {handle.name}/{pick.model}") from exc
        raise OllamaError("unreachable")  # pragma: no cover

    async def generate(
        self,
        role: str,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        stream_callback: Any | None = None,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return await self.chat(role, messages, json_mode=json_mode, stream_callback=stream_callback)


_router: OllamaRouter | None = None


def get_router() -> OllamaRouter:
    global _router
    if _router is None:
        _router = OllamaRouter()
    return _router


async def shutdown_router() -> None:
    global _router
    if _router is not None:
        await _router.close()
        _router = None


__all__ = ["OllamaError", "OllamaRouter", "get_router", "shutdown_router"]


# Small helper for orchestrator streaming.
async def stream_lines(stream: AsyncIterator[str]) -> str:  # pragma: no cover - helper
    buf: list[str] = []
    async for chunk in stream:
        buf.append(chunk)
    return "".join(buf)
