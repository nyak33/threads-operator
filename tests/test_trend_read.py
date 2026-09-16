"""TDD: account-scoped read-only trend candidate access (store + CLI)."""
import json
from io import StringIO
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from threads_operator import operator_cli
from threads_operator.supabase_store import SupabaseStore

TABLE = "threads_trend_candidates"
TOKEN_KEY = "THREADS_ACCESS_" "TOKEN"
SERVICE_KEY = "SUPABASE_SERVICE_" "ROLE_KEY"


def _row(**over):
    row = {
        "id": 1,
        "target_account_id": "syaqir",
        "source_platform": "threads",
        "source_post_id": None,
        "source_username": "nakocah",
        "source_permalink": "https://www.threads.com/@nakocah/post/DTA7rB_kyq9",
        "source_text": "text",
        "published_at": None,
        "discovered_at": "2026-09-16T01:00:00+00:00",
        "last_checked_at": None,
        "views": None,
        "likes": None,
        "replies": None,
        "reposts": None,
        "quotes": None,
        "status": "discovered",
        "raw_metadata": {"manual": True},
    }
    row.update(over)
    return row


def _mock_store(handler, account_key="syaqir"):
    return SupabaseStore(
        "https://example.supabase.co",
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        account_key=account_key,
    )


def _recording_handler(rows, calls):
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=rows)

    return handle


def _query(request):
    return {k: request.url.params.get_list(k) for k in request.url.params}


# ---------------------------------------------------------------- store level

def test_list_is_get_only_and_scoped_to_account():
    calls = []
    store = _mock_store(_recording_handler([_row()], calls))
    rows = store.list_trend_candidates()
    assert [c.method for c in calls] == ["GET"]
    assert calls[0].url.path == f"/rest/v1/{TABLE}"
    q = _query(calls[0])
    assert q["target_account_id"] == ["eq.syaqir"]
    assert q["order"] == ["discovered_at.desc"]
    assert q["limit"] == ["20"]
    assert rows and rows[0]["id"] == 1


def test_list_hard_scopes_even_if_caller_lies():
    # Identity is self.account_key only; no parameter can override it.
    store = _mock_store(_recording_handler([], []))
    with pytest.raises(TypeError):
        store.list_trend_candidates(target_account_id="other")


def test_list_limit_and_status_filter():
    calls = []
    store = _mock_store(_recording_handler([], calls))
    store.list_trend_candidates(limit=5, status="reviewed")
    q = _query(calls[0])
    assert q["limit"] == ["5"]
    assert q["status"] == ["eq.reviewed"]


def test_list_rejects_invalid_limit():
    store = _mock_store(_recording_handler([], []))
    for bad in (0, -1, 101, 1000, "20", 2.5):
        with pytest.raises(ValueError):
            store.list_trend_candidates(limit=bad)


def test_list_rejects_invalid_status():
    store = _mock_store(_recording_handler([], []))
    for bad in ("trending", "pending", "DISCOVERED"):
        with pytest.raises(ValueError):
            store.list_trend_candidates(status=bad)


def test_get_scoped_by_id_and_account():
    calls = []
    store = _mock_store(_recording_handler([_row()], calls))
    row = store.get_trend_candidate(1)
    assert [c.method for c in calls] == ["GET"]
    q = _query(calls[0])
    assert q["id"] == ["eq.1"]
    assert q["target_account_id"] == ["eq.syaqir"]
    assert row["id"] == 1


def test_get_other_account_row_reads_as_missing():
    # Server-side filter already excludes it; emulate a leaky server response
    # too — a row with foreign target_account_id must read as missing.
    calls = []
    store = _mock_store(_recording_handler([_row(target_account_id="other")], calls))
    assert store.get_trend_candidate(7) is None


def test_get_missing_returns_none_clean():
    calls = []
    store = _mock_store(_recording_handler([], calls))
    assert store.get_trend_candidate(999) is None


def test_get_rejects_bad_id():
    store = _mock_store(_recording_handler([], []))
    for bad in (0, -3, "1", 3.5, True):
        with pytest.raises(ValueError):
            store.get_trend_candidate(bad)


# ---------------------------------------------------------------- CLI level

def write_account(home: Path, name: str) -> None:
    accounts = home / "accounts"
    accounts.mkdir(parents=True, exist_ok=True)
    (accounts / f"{name}.env").write_text(
        TOKEN_KEY + "=token-" + name + "\n"
        "THREADS_USER_ID=user-" + name + "\n"
        "SUPABASE_URL=https://" + name + ".supabase.co\n"
        + SERVICE_KEY + "=service-" + name + "\n"
    )


def run_cli(tmp_path, monkeypatch, rows, args, capsys):
    calls = []

    def factory(*a, account_key=None, **kw):
        return _mock_store(_recording_handler(rows, calls), account_key=account_key)

    monkeypatch.setattr(operator_cli, "SupabaseStore", factory)
    code = operator_cli.main(args, process_env={"THREADS_OPERATOR_HOME": str(tmp_path)})
    out = capsys.readouterr()
    return code, out.out, out.err, calls


def test_cli_list_outputs_deterministic_json(tmp_path, monkeypatch, capsys):
    write_account(tmp_path, "syaqir")
    rows = [
        _row(id=2, discovered_at="2026-09-16T02:00:00+00:00"),
        _row(id=1),
    ]
    code, out, err, calls = run_cli(
        tmp_path, monkeypatch, rows, ["trend", "list", "--account", "syaqir"], capsys
    )
    assert code == 0, err
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["account"] == "syaqir"
    assert payload["writes"] == 0
    assert [c.method for c in calls] == ["GET"]
    ids = [c["id"] for c in payload["candidates"]]
    assert ids == [2, 1]  # newest discovered_at first, server order preserved
    cand = payload["candidates"][0]
    assert cand["target_account_id"] == "syaqir"
    assert cand["status"] == "discovered"
    assert cand["source_permalink"].startswith("https://www.threads.com/")
    for f in (
        "id", "target_account_id", "source_platform", "source_post_id",
        "source_username", "source_permalink", "source_text", "published_at",
        "discovered_at", "last_checked_at", "views", "likes", "replies",
        "reposts", "quotes", "status", "raw_metadata",
    ):
        assert f in cand, f


def test_cli_list_limit_and_status_flags(tmp_path, monkeypatch, capsys):
    write_account(tmp_path, "syaqir")
    code, out, err, calls = run_cli(
        tmp_path,
        monkeypatch,
        [],
        ["trend", "list", "--account", "syaqir", "--limit", "5", "--status", "discovered"],
        capsys,
    )
    assert code == 0, err
    q = _query(calls[0])
    assert q["limit"] == ["5"]
    assert q["status"] == ["eq.discovered"]


def test_cli_list_rejects_invalid_args(tmp_path, monkeypatch, capsys):
    write_account(tmp_path, "syaqir")
    for bad in (
        ["trend", "list", "--account", "syaqir", "--limit", "0"],
        ["trend", "list", "--account", "syaqir", "--limit", "101"],
        ["trend", "list", "--account", "syaqir", "--status", "trending"],
    ):
        code, out, err, calls = run_cli(tmp_path, monkeypatch, [], bad, capsys)
        assert code != 0, bad
        assert out == "", bad


def test_cli_show_returns_row(tmp_path, monkeypatch, capsys):
    write_account(tmp_path, "syaqir")
    code, out, err, calls = run_cli(
        tmp_path,
        monkeypatch,
        [_row()],
        ["trend", "show", "--account", "syaqir", "--id", "1"],
        capsys,
    )
    assert code == 0, err
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["found"] is True
    assert payload["candidate"]["id"] == 1
    assert payload["candidate"]["target_account_id"] == "syaqir"
    assert [c.method for c in calls] == ["GET"]


def test_cli_show_missing_is_clean_not_found(tmp_path, monkeypatch, capsys):
    write_account(tmp_path, "syaqir")
    code, out, err, calls = run_cli(
        tmp_path,
        monkeypatch,
        [],
        ["trend", "show", "--account", "syaqir", "--id", "999"],
        capsys,
    )
    assert code == 0, err
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["found"] is False
    assert payload["candidate"] is None


def test_cli_show_other_account_never_leaks(tmp_path, monkeypatch, capsys):
    write_account(tmp_path, "syaqir")
    code, out, err, calls = run_cli(
        tmp_path,
        monkeypatch,
        [_row(target_account_id="other")],
        ["trend", "show", "--account", "syaqir", "--id", "1"],
        capsys,
    )
    assert code == 0, err
    payload = json.loads(out)
    assert payload["found"] is False
    assert "other" not in out


def test_cli_read_output_contains_no_secrets(tmp_path, monkeypatch, capsys):
    write_account(tmp_path, "syaqir")
    code, out, err, calls = run_cli(
        tmp_path, monkeypatch, [_row()], ["trend", "list", "--account", "syaqir"], capsys
    )
    assert code == 0, err
    combined = out + err
    assert "service-syaqir" not in combined
    assert "token-syaqir" not in combined
    assert "user-syaqir" not in combined
    assert SERVICE_KEY not in combined
    assert TOKEN_KEY not in combined
