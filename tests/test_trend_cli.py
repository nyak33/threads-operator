"""Tests for the `trend add` manual ingress CLI (dry-run = zero writes)."""

import json
from pathlib import Path

import pytest

from threads_operator import operator_cli

TOKEN_KEY = "THREADS_ACCESS_" "TOKEN"
SERVICE_KEY = "SUPABASE_SERVICE_" "ROLE_KEY"
URL = "https://www.threads.com/@someone/post/ABC123?xid=Un2o#reply"


def write_account(home: Path, name: str) -> None:
    accounts = home / "accounts"
    accounts.mkdir(parents=True, exist_ok=True)
    (accounts / f"{name}.env").write_text(
        TOKEN_KEY + "=token-" + name + "\n"
        "THREADS_USER_ID=user-" + name + "\n"
        "SUPABASE_URL=https://" + name + ".supabase.co\n"
        + SERVICE_KEY + "=service-" + name + "\n"
    )


def test_trend_add_dry_run_prints_sanitized_candidate_and_never_writes(
    tmp_path, capsys, monkeypatch
):
    write_account(tmp_path, "brand_a")
    monkeypatch.setattr(
        operator_cli,
        "SupabaseStore",
        lambda *a, **k: pytest.fail("dry-run must not construct a store"),
    )

    code = operator_cli.main(
        ["trend", "add", "--account", "brand_a", "--url", URL, "--dry-run"],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )

    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["candidate"]["target_account_id"] == "brand_a"
    assert payload["candidate"]["source_permalink"] == (
        "https://www.threads.com/@someone/post/ABC123"
    )
    assert payload["candidate"]["source_username"] == "someone"
    assert payload["candidate"]["raw_metadata"]["manual"] is True
    assert payload["candidate"]["raw_metadata"]["discovery_method"] == (
        "manual_url"
    )
    assert "source_post_id" not in payload["candidate"]
    assert "service-brand_a" not in out
    assert "token-brand_a" not in out


def test_trend_add_rejects_non_threads_url_without_writes(
    tmp_path, capsys, monkeypatch
):
    write_account(tmp_path, "brand_a")
    monkeypatch.setattr(
        operator_cli,
        "SupabaseStore",
        lambda *a, **k: pytest.fail("invalid URL must not reach the store"),
    )

    code = operator_cli.main(
        [
            "trend", "add", "--account", "brand_a",
            "--url", "https://threads.com.evil.example/@x/post/Y",
            "--dry-run",
        ],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )

    # Invalid URL must fail closed (non-zero rc, sanitized stderr, no writes).
    assert code != 0
    err = capsys.readouterr().err
    assert "threads.com.evil.example" in err
    assert "service-brand_a" not in err
    assert "token-brand_a" not in err


def test_trend_add_real_run_uses_selected_account_store(
    tmp_path, capsys, monkeypatch
):
    write_account(tmp_path, "brand_a")
    seen = {}

    class FakeStore:
        def __init__(self, *args, account_key=None, **kwargs):
            seen["account_key"] = account_key

        def insert_trend_candidate(self, url, **kwargs):
            seen["url"] = url
            seen["kwargs"] = kwargs
            return {"status": "inserted", "id": 1, "permalink": url}

    monkeypatch.setattr(operator_cli, "SupabaseStore", FakeStore)

    code = operator_cli.main(
        ["trend", "add", "--account", "brand_a", "--url", URL],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "inserted"
    assert seen["account_key"] == "brand_a"
    assert seen["url"] == URL


def test_trend_add_no_target_account_id_flag_exists(tmp_path):
    parser_env = {"THREADS_OPERATOR_HOME": str(tmp_path)}
    with pytest.raises(SystemExit):
        operator_cli.main(
            ["trend", "add", "--account", "brand_a", "--url", URL,
             "--target-account-id", "victim"],
            process_env=parser_env,
        )
