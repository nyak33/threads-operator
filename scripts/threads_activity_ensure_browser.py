#!/usr/bin/env python3
"""Deterministic ensure-browser for the syaqir Threads Activity collector.

Behavior contract (no LLM, no automated login):
  A. A Chromium main process owns the profile AND CDP answers -> reuse.
  B. Reuse path also verifies the Threads session is still authenticated.
  C. No valid browser -> remove stale runtime-only artifacts (only when no
     process owns the profile), launch headless Chromium with a LOOPBACK-only
     fixed CDP port, wait for DevToolsActivePort + /json/version, verify auth.
  D. Not authenticated -> exit 4 f"Browser login required for account: {ACCOUNT}".
     Never attempts password/OTP/CAPTCHA entry.
  - Never runs two Chromium instances against the same profile: >1 main
    process on the profile is a hard failure (exit 5).

Exit codes: 0 ready (reused or launched), 4 login required, 5 failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

import httpx
import websockets

ACCOUNT = os.environ.get("THREADS_ACCOUNT", "syaqir")
_PROFILE_ROOT = pathlib.Path(os.environ.get(
    "THREADS_BROWSER_PROFILE_ROOT",
    str(pathlib.Path.home() / ".threads-operator" / "browser-profiles")))
PROFILE = pathlib.Path(os.environ.get(
    "THREADS_ACTIVITY_PROFILE", str(_PROFILE_ROOT / ACCOUNT)))
CDP_PORT = int(os.environ.get("THREADS_ACTIVITY_CDP_PORT", "43399"))


def _find_chrome() -> str:
    """Locate the Playwright/agent-browser Chromium without hardcoding a home path."""
    override = os.environ.get("THREADS_ACTIVITY_CHROME")
    if override:
        return override
    bases = []
    pw = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if pw:
        bases.append(pathlib.Path(pw))
    bases.append(pathlib.Path.home() / ".cache" / "ms-playwright")
    candidates: list[pathlib.Path] = []
    for base in bases:
        if not base.is_dir():
            continue
        for d in sorted(base.glob("chromium-*"), reverse=True):
            candidates.append(d / "chrome-linux64" / "chrome")
            candidates.append(d / "chrome-linux" / "chrome")
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    raise SystemExit(f"no chromium found under {bases}; set THREADS_ACTIVITY_CHROME")


CHROME = _find_chrome()
AUTH_URL = "https://www.threads.com/"
RUNTIME_ARTIFACTS = ("SingletonLock", "SingletonSocket",
                     "SingletonCookie", "DevToolsActivePort")
CHALLENGE_MARKERS = ("checkpoint", "login", "two-factor", "confirmation_code",
                     "captcha", "suspicious", "unsupported_browser")
MARKER = PROFILE / "activity-browser.log"


def log(msg: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        with MARKER.open("a") as fh:
            fh.write(f"{stamp} {msg}\n")
    except OSError:
        pass


def main_procs() -> list[int]:
    """PIDs of Chromium MAIN processes (no --type= child) bound to PROFILE."""
    pids: list[int] = []
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode(errors="replace")
        except OSError:
            continue
        if "chrome-linux64/chrome" not in cmdline:
            continue
        if f"--user-data-dir={PROFILE}" not in cmdline:
            continue
        if "--type=" in cmdline:  # renderer/gpu/zygote child, not the main proc
            continue
        pids.append(int(entry.name))
    return pids


def cdp_base() -> str:
    return f"http://127.0.0.1:{CDP_PORT}"


def read_port_file() -> int | None:
    try:
        return int((PROFILE / "DevToolsActivePort").read_text().splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


def cdp_alive() -> bool:
    # Headless Chrome does not always write DevToolsActivePort when an
    # explicit --remote-debugging-port is used; fall back to the fixed port.
    port = read_port_file()
    if port is None:
        port = CDP_PORT
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=5)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def write_port_file() -> None:
    """Materialise DevToolsActivePort for the collector.

    Headless Chrome with an explicit --remote-debugging-port may not write
    this file, but browser_cdp.py in threads-operator reads it from the
    profile dir. Only call once the fixed LOOPBACK port is proven live.
    """
    target = PROFILE / "DevToolsActivePort"
    try:
        if read_port_file() != CDP_PORT:
            target.write_text(f"{CDP_PORT}\nDevToolsActivePort written by ensure-browser\n")
            log(f"wrote {target} -> {CDP_PORT}")
    except OSError as exc:  # noqa: BLE001
        log(f"WARN could not write DevToolsActivePort: {exc}")


def kill_browser(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not [p for p in pids if pathlib.Path(f"/proc/{p}").exists()]:
            break
        time.sleep(0.5)
    agent_db = pathlib.Path.home() / ".agent-browser"
    # Best-effort: drop any agent-browser daemon holding the old session so a
    # later manual --session syaqir command relaunches against this profile.
    subprocess.run(["pkill", "-f", "agent-browser-linux-x64"], check=False)
    time.sleep(1)


def clean_runtime_artifacts() -> None:
    for name in RUNTIME_ARTIFACTS:
        target = PROFILE / name
        if target.is_symlink() or target.exists():
            try:
                target.unlink()
                log(f"removed stale runtime artifact {name}")
            except OSError as exc:
                log(f"WARN could not remove {name}: {exc}")


def launch() -> None:
    args = [
        CHROME,
        f"--user-data-dir={PROFILE}",
        f"--remote-debugging-port={CDP_PORT}",
        "--remote-debugging-address=127.0.0.1",
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--disable-popup-blocking",
        "--noerrdialogs",
        "--metrics-recording-only",
        "--password-store=basic",
        "--use-mock-keychain",
        "--window-size=1280,720",
        "--ozone-platform=headless",
        "--ozone-override-screen-size=800,600",
        "--use-angle=swiftshader-webgl",
        "--enable-features=NetworkService,NetworkServiceInProcess",
        "about:blank",
    ]
    env = dict(os.environ)
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH",
                      str(pathlib.Path.home() / ".cache" / "ms-playwright"))
    log(f"launching chromium cdp={CDP_PORT}")
    subprocess.Popen(args, start_new_session=True, env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def auth_state() -> tuple[str, str]:
    """Open one temp tab at AUTH_URL via CDP, return (final_url, verdict).

    verdict in {"authenticated", "challenge"}. Tab is closed afterwards.
    Read-only: navigation only, never clicks/types.
    """
    base = cdp_base()
    # Headless Chrome only accepts the full browser ws GUID from /json/version.
    version = httpx.get(f"{base}/json/version", timeout=5).json()
    ws_url = version["webSocketDebuggerUrl"]
    async with websockets.connect(ws_url, max_size=None) as ws:
        rid = 0

        async def call(method, params=None, timeout=30):
            nonlocal rid
            rid += 1
            await ws.send(json.dumps({"id": rid, "method": method,
                                      "params": params or {}}))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                msg = json.loads(await asyncio.wait_for(
                    ws.recv(), max(0.1, deadline - loop.time())))
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise RuntimeError(msg["error"].get("message", "cdp"))
                    return msg.get("result", {})

        target = await call("Target.createTarget", {"url": AUTH_URL})
        target_id = target["targetId"]
        final_url = ""
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                await asyncio.sleep(1.5)
                targets = await call("Target.getTargets")
                for t in targets.get("targetInfos", []):
                    if t.get("targetId") == target_id:
                        final_url = t.get("url", "")
                        if final_url and "threads" in final_url:
                            deadline = 0
                            break
        finally:
            try:
                await call("Target.closeTarget", {"targetId": target_id},
                           timeout=10)
            except Exception:  # noqa: BLE001
                pass
    low = final_url.lower()
    if any(m in low for m in CHALLENGE_MARKERS):
        return final_url, "challenge"
    if "threads" not in low or not final_url:
        return final_url, "challenge"
    return final_url, "authenticated"


def check_auth() -> str:
    for attempt in range(2):
        try:
            url, verdict = asyncio.run(auth_state())
            log(f"auth probe -> {verdict} url={url}")
            return verdict
        except Exception as exc:  # noqa: BLE001
            log(f"auth probe attempt {attempt + 1} error: {type(exc).__name__}: {exc}")
            time.sleep(2)
    return "error"


def ensure_port_file_matches() -> bool:
    """Confirm our fixed CDP_PORT answers (port file may be absent in headless)."""
    for _ in range(40):
        port = read_port_file()
        if port is not None and port != CDP_PORT:
            time.sleep(0.5)
            continue
        try:
            r = httpx.get(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=5)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    procs = main_procs()
    if len(procs) > 1:
        log(f"DUPLICATE chromium instances on profile: {procs}")
        print(f"DUPLICATE_BROWSER profile={PROFILE} pids={procs}",
              file=sys.stderr)
        return 5
    if len(procs) == 1:
        if cdp_alive():
            verdict = check_auth()
            if verdict == "authenticated":
                write_port_file()
                log(f"reusing browser pid={procs[0]} port={read_port_file()}")
                print("reused")
                return 0
            if verdict == "challenge":
                print(f"Browser login required for account: {ACCOUNT}",
                      file=sys.stderr)
                return 4
            # CDP ws flaked — treat process as unhealthy and recycle.
        log(f"browser pid={procs[0]} present but CDP/auth unhealthy -> recycle")
        kill_browser(procs)
    elif read_port_file() is not None:
        log("DevToolsActivePort present but no owning process -> stale")
    # Fresh launch path: only here do we touch runtime artifacts, and only
    # after proving no process owns the profile (procs == 0 above).
    if main_procs():
        log("race: process appeared on profile, aborting launch")
        return 5
    clean_runtime_artifacts()
    launch()
    if not ensure_port_file_matches():
        log("launch failed: DevToolsActivePort/CDP never became healthy")
        print(f"Browser failed to launch for account: {ACCOUNT}", file=sys.stderr)
        return 5
    verdict = check_auth()
    if verdict != "authenticated":
        print(f"Browser login required for account: {ACCOUNT}", file=sys.stderr)
        return 4
    write_port_file()
    log("launched fresh browser, authenticated")
    print("launched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
