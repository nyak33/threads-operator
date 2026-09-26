"""Tests for threads_operator.telegram_gateway — the loopback card-send client.

The client reads API_SERVER_KEY from the Hermes-owned ~/.hermes/.env (same uid, mode 600)
and POSTs a card to the gateway's loopback endpoint. It must fail closed (raise
TelegramGatewayError) on any auth/connection/Telegram error so callers leave the
opportunity retryable, and it must never log the key.
"""

import json
import os
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from threads_operator import telegram_gateway


def _write_env(tmp_path, values: dict) -> str:
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    return str(env_path)


class TestReadHermesEnvValue:
    def test_reads_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_env(tmp_path, {"API_SERVER_KEY": "sk-abc", "OTHER": "x"})
        assert telegram_gateway._read_hermes_env_value("API_SERVER_KEY") == "sk-abc"

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert telegram_gateway._read_hermes_env_value("API_SERVER_KEY") == ""

    def test_missing_key_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_env(tmp_path, {"OTHER": "x"})
        assert telegram_gateway._read_hermes_env_value("API_SERVER_KEY") == ""

    def test_ignores_comments_and_blanks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / ".env").write_text("# comment\n\nAPI_SERVER_KEY=sk-real\n# API_SERVER_KEY=fake\n")
        assert telegram_gateway._read_hermes_env_value("API_SERVER_KEY") == "sk-real"


class TestAvailability:
    def test_available_when_key_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_env(tmp_path, {"API_SERVER_KEY": "sk-abc"})
        assert telegram_gateway.gateway_card_send_available() is True

    def test_unavailable_without_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_env(tmp_path, {})
        assert telegram_gateway.gateway_card_send_available() is False


class TestSendTelegramCard:
    def test_raises_without_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_env(tmp_path, {})
        with pytest.raises(telegram_gateway.TelegramGatewayError, match="API_SERVER_KEY"):
            telegram_gateway.send_telegram_card(chat_id="1", text="hi")

    def test_success_returns_message_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_env(tmp_path, {"API_SERVER_KEY": "sk-abc"})
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({"ok": True, "message_id": "555"}).encode()
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            mid = telegram_gateway.send_telegram_card(
                chat_id="79553451",
                text="card",
                inline_keyboard=[[{"text": "Approve", "callback_data": "dmopp:approve:2"}]],
            )
        assert mid == "555"
        # Auth header carried the key; payload carried chat/text/keyboard.
        req = mock_open.call_args.args[0]
        assert req.get_header("Authorization") == "Bearer sk-abc"
        body = json.loads(req.data.decode())
        assert body["chat_id"] == "79553451"
        assert body["inline_keyboard"][0][0]["callback_data"] == "dmopp:approve:2"

    def test_http_error_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_env(tmp_path, {"API_SERVER_KEY": "sk-abc"})
        err = urllib.error.HTTPError(
            url="u", code=401, msg="unauth", hdrs=None,
            fp=MagicMock(read=lambda: json.dumps({"error": {"message": "bad key"}}).encode()),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(telegram_gateway.TelegramGatewayError, match="HTTP 401"):
                telegram_gateway.send_telegram_card(chat_id="1", text="hi")

    def test_unreachable_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_env(tmp_path, {"API_SERVER_KEY": "sk-abc"})
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with pytest.raises(telegram_gateway.TelegramGatewayError, match="unreachable"):
                telegram_gateway.send_telegram_card(chat_id="1", text="hi")

    def test_missing_message_id_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_env(tmp_path, {"API_SERVER_KEY": "sk-abc"})
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({"ok": True, "message_id": ""}).encode()
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=fake_resp):
            with pytest.raises(telegram_gateway.TelegramGatewayError, match="no message_id"):
                telegram_gateway.send_telegram_card(chat_id="1", text="hi")
