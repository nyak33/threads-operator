"""Restart-safe pending-edit sessions for Telebot engagement approval.

A pending edit session records that one Telegram chat+user pressed the Edit
button for one engagement queue row and now owes the replacement reply text.
Only the same chat+user may supply that text; sessions expire after a bounded
TTL and survive process restarts via an atomic JSON file (mode 0600).
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 30 * 60


class PendingEditStore:
    """File-backed edit sessions keyed by (chat_id, user_id, engagement_id)."""

    def __init__(self, path: Path | str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.path = Path(path)
        self.ttl_seconds = int(ttl_seconds)

    def create(
        self,
        *,
        chat_id: str,
        user_id: str,
        engagement_id: int,
        account_key: str,
        card_message_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        sessions = self._load(now)
        session = {
            "token": secrets.token_hex(8),
            "telegram_chat_id": str(chat_id),
            "telegram_user_id": str(user_id),
            "engagement_id": int(engagement_id),
            "account_key": str(account_key),
            "card_message_id": str(card_message_id) if card_message_id is not None else None,
            "created_at": now,
            "expires_at": now + self.ttl_seconds,
        }
        sessions = [
            s
            for s in sessions
            if not (
                s["telegram_chat_id"] == session["telegram_chat_id"]
                and s["telegram_user_id"] == session["telegram_user_id"]
                and s["engagement_id"] == session["engagement_id"]
            )
        ]
        sessions.append(session)
        self._save(sessions)
        return dict(session)

    def find_for_sender(
        self, *, chat_id: str, user_id: str, now: float | None = None
    ) -> dict[str, Any] | None:
        now = time.time() if now is None else now
        sessions = self._load(now)
        for session in sessions:
            if (
                session["telegram_chat_id"] == str(chat_id)
                and session["telegram_user_id"] == str(user_id)
            ):
                return session
        return None

    def find_by_token(self, token: str, *, now: float | None = None) -> dict[str, Any] | None:
        now = time.time() if now is None else now
        for session in self._load(now):
            if session["token"] == token:
                return session
        return None

    def remove(self, token: str) -> None:
        sessions = [s for s in self._load() if s["token"] != token]
        self._save(sessions)

    def sessions(self, *, now: float | None = None) -> list[dict[str, Any]]:
        return self._load(time.time() if now is None else now)

    def _load(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(raw, list):
            return []
        sessions = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            expires_at = item.get("expires_at")
            if not isinstance(expires_at, (int, float)) or expires_at <= now:
                continue
            sessions.append(item)
        return sessions

    def _save(self, sessions: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(sessions, handle)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
            os.chmod(self.path, 0o600)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
