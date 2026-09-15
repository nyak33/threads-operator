"""Minimal read-only Chrome DevTools Protocol client for the Activity collector.

Connects to the ALREADY-RUNNING persistent headless Chromium that hosts the
authenticated Threads profile (port read from DevToolsActivePort inside the
profile dir). Never launches or kills browsers, never reads cookies, and
creates only a short-lived private tab that is closed afterwards.

Raw websocket CDP (no Playwright) keeps the deployed venv light.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Callable

import httpx
import websockets

DEFAULT_TIMEOUT = 60.0


class BrowserCDPError(RuntimeError):
    """Raised when the CDP browser is unreachable or a command fails."""


def devtools_port(profile_dir: pathlib.Path) -> int:
    """Read the live CDP port from the browser profile's DevToolsActivePort."""
    marker = pathlib.Path(profile_dir) / "DevToolsActivePort"
    if not marker.exists():
        raise BrowserCDPError(
            f"DevToolsActivePort not found in {profile_dir} — persistent "
            "Threads browser is not running")
    try:
        return int(marker.read_text().splitlines()[0].strip())
    except (ValueError, IndexError) as exc:
        raise BrowserCDPError(f"unparsable DevToolsActivePort: {exc}") from exc


class CDPConnection:
    """Websocket to the browser endpoint; routes events to session routers."""

    def __init__(self, ws):
        self._ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: list[Callable[[str, dict, str | None], None]] = []
        self._reader = asyncio.ensure_future(self._read_loop())

    @classmethod
    async def connect(cls, profile_dir: pathlib.Path) -> "CDPConnection":
        port = devtools_port(pathlib.Path(profile_dir))
        url = f"http://127.0.0.1:{port}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{url}/json/version")
                resp.raise_for_status()
                info = resp.json()
        except Exception as exc:  # noqa: BLE001 - surfaced as CDP failure
            raise BrowserCDPError(f"CDP endpoint {url} unreachable") from exc
        ws_url = info.get("webSocketDebuggerUrl")
        if not ws_url:
            raise BrowserCDPError("no webSocketDebuggerUrl in /json/version")
        ws = await websockets.connect(ws_url, max_size=None, close_timeout=5)
        return cls(ws)

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(BrowserCDPError(
                                f"CDP error: {msg['error'].get('message')}"))
                        else:
                            fut.set_result(msg.get("result", {}))
                else:
                    for handler in list(self._handlers):
                        try:
                            handler(msg.get("method", ""),
                                    msg.get("params", {}),
                                    msg.get("sessionId"))
                        except Exception:  # noqa: BLE001 - handlers must not kill loop
                            pass
        except Exception:  # noqa: BLE001 - socket closed
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(BrowserCDPError("CDP connection closed"))
            self._pending.clear()

    async def send(self, method: str, params: dict | None = None,
                   session_id: str | None = None,
                   timeout: float = DEFAULT_TIMEOUT) -> dict:
        self._id += 1
        rid = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        frame: dict = {"id": rid, "method": method, "params": params or {}}
        if session_id:
            frame["sessionId"] = session_id
        await self._ws.send(json.dumps(frame))
        return await asyncio.wait_for(fut, timeout)

    def add_handler(self, handler) -> None:
        self._handlers.append(handler)

    async def aclose(self) -> None:
        self._reader.cancel()
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass


class PageSession:
    """A CDP session attached to one tab; records network responses."""

    def __init__(self, conn: CDPConnection, target_id: str,
                 session_id: str):
        self.conn = conn
        self.target_id = target_id
        self.session_id = session_id
        self.finished: list[dict] = []          # {requestId, url}
        self.responses: dict[str, dict] = {}    # requestId -> response info
        self.requests: dict[str, dict] = {}     # requestId -> {url, method, postData}
        conn.add_handler(self._handle)

    def _handle(self, method: str, params: dict,
                session: str | None) -> None:
        if session != self.session_id:
            return
        if method == "Network.requestWillBeSent":
            info = params.get("request", {})
            self.requests[str(params.get("requestId", ""))] = {
                "url": info.get("url"), "method": info.get("method"),
                "postData": info.get("postData"),
                "type": params.get("type"),
            }
        elif method == "Network.responseReceived":
            info = params.get("response", {})
            self.responses[str(params.get("requestId", ""))] = {
                "status": info.get("status"), "url": info.get("url"),
                "mimeType": info.get("mimeType"),
                "type": params.get("type"),
            }
        elif method == "Network.loadingFinished":
            self.finished.append({"requestId": params.get("requestId")})

    async def call(self, method: str, params: dict | None = None,
                   timeout: float = DEFAULT_TIMEOUT) -> dict:
        return await self.conn.send(method, params, self.session_id, timeout)

    async def response_body(self, request_id: str) -> tuple[str, bool]:
        """Return (text, base64_flag) for a finished request."""
        res = await self.call("Network.getResponseBody",
                              {"requestId": request_id})
        return res.get("body", ""), res.get("base64Encoded", False)


class ActivityBrowser:
    """Async context manager: attach, open ONE private tab, close on exit."""

    def __init__(self, profile_dir: pathlib.Path):
        self.profile_dir = pathlib.Path(profile_dir)
        self.conn: CDPConnection | None = None
        self.session: PageSession | None = None
        self._target_id: str | None = None

    async def __aenter__(self) -> "ActivityBrowser":
        self.conn = await CDPConnection.connect(self.profile_dir)
        res = await self.conn.send("Target.createTarget",
                                   {"url": "about:blank"})
        self._target_id = res["targetId"]
        attach = await self.conn.send("Target.attachToTarget",
                                      {"targetId": self._target_id,
                                       "flatten": True})
        self.session = PageSession(self.conn, self._target_id,
                                   attach["sessionId"])
        return self

    async def aclose(self) -> None:
        if self.conn is not None:
            if self._target_id:
                try:
                    await self.conn.send("Target.closeTarget",
                                         {"targetId": self._target_id})
                except BrowserCDPError:
                    pass
            await self.conn.aclose()
            self.conn = None

    async def __aexit__(self, *exc) -> bool:
        await self.aclose()
        return False
