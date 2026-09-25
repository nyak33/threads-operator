"""Watchdog DM-send pass tests (deterministic; CLI + Telegram mocked)."""
import importlib.util
import json
import pathlib

import pytest

WATCHDOG = pathlib.Path("/home/admin/threads-operator/scripts/own_replies_watchdog_5m.py")
spec = importlib.util.spec_from_file_location("own_replies_watchdog_5m", WATCHDOG)
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)


def _patch_run_cli(monkeypatch, script):
    """script: list of (args_prefix_tuple, (code, payload)) matched in order."""
    calls = []

    def run_cli(args, timeout=240):
        calls.append(args)
        for prefix, resp in script:
            if tuple(args[: len(prefix)]) == prefix:
                return resp
        return (0, {"ok": True})
    monkeypatch.setattr(wd, "run_cli", run_cli)
    return calls


def test_send_pass_no_approved_no_send(monkeypatch):
    calls = _patch_run_cli(monkeypatch, [
        (("dm", "send-queue"), (0, {"ok": True, "approved": []})),
    ])
    out = wd.run_dm_send_pass()
    assert out["dm_send_attempted"] == 0
    assert not any(c[:2] == ["dm", "send"] for c in calls)


def test_send_pass_sends_and_notifies(monkeypatch):
    sent_tg = []
    monkeypatch.setattr(wd, "_tg_send", lambda text: sent_tg.append(text))
    _patch_run_cli(monkeypatch, [
        (("dm", "send-queue"), (0, {"ok": True, "approved": [{"id": 42}]})),
        (("dm", "send"), (0, {"ok": True, "id": 42, "target_username": "hanisah",
                              "confirmation_ref": "t1", "status": "sent"})),
    ])
    out = wd.run_dm_send_pass()
    assert out["dm_sent"] == 1
    assert any("SENT" in t for t in sent_tg)


def test_send_pass_failed_notifies(monkeypatch):
    sent_tg = []
    monkeypatch.setattr(wd, "_tg_send", lambda text: sent_tg.append(text))
    _patch_run_cli(monkeypatch, [
        (("dm", "send-queue"), (0, {"ok": True, "approved": [{"id": 42}]})),
        (("dm", "send"), (1, {"ok": False, "id": 42, "target_username": "hanisah",
                              "failure_category": "recipient_not_found"})),
    ])
    out = wd.run_dm_send_pass()
    assert out["dm_failed"] == 1
    assert any("FAILED" in t for t in sent_tg)


def test_send_pass_uncertain_stops_and_notifies(monkeypatch):
    sent_tg = []
    monkeypatch.setattr(wd, "_tg_send", lambda text: sent_tg.append(text))
    _patch_run_cli(monkeypatch, [
        (("dm", "send-queue"), (0, {"ok": True, "approved": [{"id": 42}]})),
        (("dm", "send"), (1, {"ok": False, "id": 42, "target_username": "hanisah",
                              "failure_category": "send_uncertain", "uncertain": True})),
    ])
    out = wd.run_dm_send_pass()
    assert out["dm_uncertain"] == 1
    assert any("UNCERTAIN" in t and "auto-resend STOPPED" in t for t in sent_tg)


def test_send_pass_idempotent_when_queue_empty_on_retry(monkeypatch):
    """A retry that finds the queue already drained sends nothing (idempotent)."""
    calls = _patch_run_cli(monkeypatch, [
        (("dm", "send-queue"), (0, {"ok": True, "approved": []})),
    ])
    out1 = wd.run_dm_send_pass()
    out2 = wd.run_dm_send_pass()
    assert out1["dm_send_attempted"] == 0 and out2["dm_send_attempted"] == 0
    assert not any(c[:2] == ["dm", "send"] for c in calls)
