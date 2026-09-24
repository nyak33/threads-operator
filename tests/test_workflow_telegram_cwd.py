"""Regression tests for _BaseWorkflowDispatcher cwd resolution.

Production bug: the gateway instantiates TrendEngagementTelegramBridge /
OwnReplyTelegramBridge / TrendBacklogTelegramBridge without an explicit cwd,
and _BaseWorkflowDispatcher used to store it verbatim (None). The subprocess
then ran the relative DEFAULT_CLI (.venv/bin/threads-operator) from the Hermes
gateway working directory and died with FileNotFoundError on every Approve /
Edit / Reject / Skip / Use tap.

Covers:
1.  dispatcher with cwd=None resolves the repo root automatically
2.  relative DEFAULT_CLI resolves to an existing binary under the resolved cwd
3.  explicitly supplied cwd is preserved
4.  Approve callback runs the CLI subprocess with cwd=repo root (no FileNotFoundError)
5.  Edit/Reject/Skip callbacks use the same resolved cwd
6.  own-reply dispatcher resolves the same cwd
7.  backlog dispatcher resolves the same cwd
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from threads_operator import workflow_telegram
from threads_operator.workflow_telegram import (
    OwnReplyDispatcher,
    TrendBacklogDispatcher,
    TrendEngagementDispatcher,
    _BaseWorkflowDispatcher,
)

REPO_ROOT = Path(workflow_telegram.__file__).resolve().parents[2]


def _make(cls, **kwargs) -> _BaseWorkflowDispatcher:
    async def _send(*args: Any, **kwargs: Any) -> None:
        return None

    kwargs.setdefault("account", "syaqir")
    kwargs.setdefault("send", _send)
    return cls(**kwargs)


# --------------------------------------------------------------------------
# 1. cwd=None resolves repo root automatically
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cls", [TrendEngagementDispatcher, OwnReplyDispatcher, TrendBacklogDispatcher])
def test_cwd_none_resolves_repo_root(cls):
    disp = _make(cls)
    assert disp.cwd is not None
    assert (Path(disp.cwd) / "pyproject.toml").exists()
    assert Path(disp.cwd) == REPO_ROOT


# --------------------------------------------------------------------------
# 2. relative DEFAULT_CLI resolves to an existing binary under resolved cwd
# --------------------------------------------------------------------------

def test_default_cli_binary_exists_under_resolved_cwd():
    disp = _make(TrendEngagementDispatcher)
    argv0 = disp.cli_argv[0]
    assert not os.path.isabs(argv0)
    assert (Path(disp.cwd) / argv0).exists(), (
        f"CLI binary {argv0} missing under {disp.cwd} — callbacks would raise "
        "FileNotFoundError"
    )


# --------------------------------------------------------------------------
# 3. explicitly supplied cwd is preserved
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cls", [TrendEngagementDispatcher, OwnReplyDispatcher, TrendBacklogDispatcher])
def test_explicit_cwd_preserved(cls, tmp_path):
    disp = _make(cls, cwd=str(tmp_path))
    assert disp.cwd == str(tmp_path)


# --------------------------------------------------------------------------
# 4. Approve callback: subprocess spawned with cwd=repo root, no FileNotFoundError
# --------------------------------------------------------------------------

def _run_callback(disp, data: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_run_cli(cli_argv, group, cli_args, *, cwd, timeout=300.0):
        captured["cli_argv"] = cli_argv
        captured["group"] = group
        captured["cli_args"] = cli_args
        captured["cwd"] = cwd
        return 0, {"ok": True, "queue_id": 55, "topic": "digital marketing"}

    async def answer(*, text: str) -> None:
        captured["answer"] = text

    with patch.object(workflow_telegram, "_run_cli", fake_run_cli):
        handled = asyncio.run(
            disp.handle_callback(
                data=data, chat_id="79553451", user_id="79553451",
                message_id="5108", answer=answer,
            )
        )
    captured["handled"] = handled
    return captured


def test_trendeng_approve_callback_uses_resolved_cwd():
    disp = _make(TrendEngagementDispatcher)
    out = _run_callback(disp, "trendeng:approve:144")
    assert out["handled"] is True
    assert out["cwd"] == str(REPO_ROOT)
    assert out["cli_argv"][0] == ".venv/bin/threads-operator"
    assert (Path(out["cwd"]) / out["cli_argv"][0]).exists()
    assert out["group"] == "trend-engagement"
    assert out["cli_args"][:2] == ["approve", "--account"]


# --------------------------------------------------------------------------
# 5. Edit/Reject/Skip callbacks use the same resolved cwd
# --------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["reject", "skip"])
def test_trendeng_decision_callbacks_use_resolved_cwd(action):
    disp = _make(TrendEngagementDispatcher)
    out = _run_callback(disp, f"trendeng:{action}:144")
    assert out["handled"] is True
    assert out["cwd"] == str(REPO_ROOT)
    assert out["cli_args"][0] == action


def test_trendeng_edit_callback_starts_edit_session(tmp_path):
    async def _send(*args: Any, **kwargs: Any) -> None:
        return None

    async def answer(*, text: str) -> None:
        return None

    disp = TrendEngagementDispatcher(
        account="syaqir", send=_send,
        edit_store=__import__("threads_operator.engagement_sessions", fromlist=["PendingEditStore"]).PendingEditStore(tmp_path / "sessions.json"),
    )
    handled = asyncio.run(
        disp.handle_callback(
            data="trendeng:edit:144", chat_id="79553451", user_id="79553451",
            message_id="5108", answer=answer,
        )
    )
    assert handled is True
    assert disp.cwd == str(REPO_ROOT)
    # edit session recorded so the next text message is treated as the new draft
    session = disp.store.find_for_sender(chat_id="79553451", user_id="79553451")
    assert session is not None


# --------------------------------------------------------------------------
# 6. own-reply callback still works with resolved cwd
# --------------------------------------------------------------------------

def test_ownreply_approve_callback_uses_resolved_cwd():
    disp = _make(OwnReplyDispatcher)
    out = _run_callback(disp, "ownreply:approve:77")
    assert out["handled"] is True
    assert out["cwd"] == str(REPO_ROOT)
    assert out["group"] == "own-replies"
    assert out["cli_args"][0] == "approve"


# --------------------------------------------------------------------------
# 7. backlog callback still works with resolved cwd
# --------------------------------------------------------------------------

def test_backlog_use_callback_uses_resolved_cwd():
    disp = _make(TrendBacklogDispatcher)
    out = _run_callback(disp, "backlog:use:88")
    assert out["handled"] is True
    assert out["cwd"] == str(REPO_ROOT)
    assert out["group"] == "trend-engagement"
    assert out["cli_args"][0] == "use"


# --------------------------------------------------------------------------
# 8. Missing CLI binary under explicit cwd -> clear warning, no silent failure
# --------------------------------------------------------------------------

def test_missing_cli_binary_logs_diagnostic(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="threads_operator.workflow_telegram"):
        _make(TrendEngagementDispatcher, cwd=str(tmp_path))
    assert any("does not exist" in r.message for r in caplog.records)
