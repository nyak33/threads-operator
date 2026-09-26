"""Loopback Telegram card delivery through the Hermes gateway.

Threads Operator jobs run as Hermes cron ``no_agent`` scripts. Hermes deliberately strips
``TELEGRAM_BOT_TOKEN`` (Tier-1 always-strip) from every spawned subprocess, so these scripts can
never call the Telegram Bot API directly. The gateway owns the token and the connected Telegram
adapter; this module asks the gateway to send a card on our behalf over a loopback-only HTTP
endpoint (``POST /api/operator/telegram-card``).

Secrets never enter this repo or the process env:
  * The gateway reads ``TELEGRAM_BOT_TOKEN`` from ``~/.hermes/.env`` (Hermes-owned, mode 600).
  * We read ``API_SERVER_KEY`` from the same Hermes-owned ``~/.hermes/.env`` at call time
    (same-uid file read; nothing is exported, printed, or committed). If it is absent we fail
    closed and the caller leaves the opportunity retryable.

Fail closed: any error raises ``TelegramGatewayError``; callers catch it, release the
``approval_sent_at`` claim, and leave the opportunity ``awaiting_approval`` for the next run.
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Any

_DEFAULT_ENDPOINT = "http://127.0.0.1:8642/api/operator/telegram-card"


class TelegramGatewayError(RuntimeError):
    """Raised when the gateway loopback card send fails (caller must leave the row retryable)."""


def _hermes_env_path() -> str:
    hermes_home = os.environ.get("HERMES_HOME", "").strip() or os.path.expanduser("~/.hermes")
    return os.path.join(hermes_home, ".env")


def _read_hermes_env_value(key: str) -> str:
    """Read one key from the Hermes-owned .env (mode 600, same uid). Never logs the value."""
    path = _hermes_env_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def gateway_card_send_available() -> bool:
    """True when the loopback card-send path is configured (API_SERVER_KEY present in Hermes env)."""
    return bool(_read_hermes_env_value("API_SERVER_KEY"))


def send_telegram_card(
    *,
    chat_id: str,
    text: str,
    inline_keyboard: list[list[dict[str, Any]]] | None = None,
    endpoint: str | None = None,
    timeout: float = 20.0,
) -> str:
    """Send a Telegram card via the Hermes gateway loopback endpoint; return the message id.

    ``inline_keyboard`` is a list of rows, each row a list of ``{"text", "callback_data"}`` dicts.
    Raises ``TelegramGatewayError`` on any failure (auth, connection, Telegram rejection).
    """
    api_key = _read_hermes_env_value("API_SERVER_KEY")
    if not api_key:
        raise TelegramGatewayError("API_SERVER_KEY not present in Hermes .env")
    url = (endpoint or os.environ.get("OPERATOR_CARD_ENDPOINT", "")).strip() or _DEFAULT_ENDPOINT
    payload: dict[str, Any] = {"chat_id": str(chat_id), "text": text}
    if inline_keyboard is not None:
        payload["inline_keyboard"] = inline_keyboard
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001 - best-effort detail only
            detail = ""
        raise TelegramGatewayError(f"gateway returned HTTP {exc.code}: {detail or 'no detail'}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise TelegramGatewayError(f"gateway loopback unreachable: {type(exc).__name__}") from exc
    if not body.get("ok"):
        raise TelegramGatewayError("gateway did not confirm send")
    message_id = str(body.get("message_id") or "").strip()
    if not message_id:
        raise TelegramGatewayError("gateway returned no message_id")
    return message_id
