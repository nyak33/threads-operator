import json
from pathlib import Path

import pytest

from threads_operator import operator_cli

TOKEN_KEY = "THREADS_ACCESS_" "TOKEN"
SERVICE_KEY = "SUPABASE_SERVICE_" "ROLE_KEY"
NOW = "2026-09-15T04:00:00+00:00"


def write_account(home: Path, name: str, extra: str = "") -> None:
    accounts = home / "accounts"
    accounts.mkdir(parents=True, exist_ok=True)
    (accounts / f"{name}.env").write_text(
        TOKEN_KEY + "=token-" + name + "\n"
        "THREADS_USER_ID=user-" + name + "\n"
        "SUPABASE_URL=https://" + name + ".supabase.co\n"
        + SERVICE_KEY + "=service-" + name + "\n"
        + extra
    )


def test_accounts_list_prints_sorted_account_keys(tmp_path, capsys):
    write_account(tmp_path, "zeta")
    write_account(tmp_path, "alpha")

    code = operator_cli.main(
        ["accounts", "list"],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )

    assert code == 0
    assert capsys.readouterr().out.strip().splitlines() == ["alpha", "zeta"]


def test_doctor_reports_configuration_without_printing_secrets(tmp_path, capsys):
    write_account(tmp_path, "brand_a")

    code = operator_cli.main(
        ["doctor", "--account", "brand_a"],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert code == 0
    assert payload["ok"] is True
    assert payload["account"] == "brand_a"
    assert "token-brand_a" not in output
    assert "service-brand_a" not in output


def test_publish_live_is_blocked_by_safe_defaults(tmp_path, capsys, monkeypatch):
    write_account(tmp_path, "brand_a")
    monkeypatch.setattr(
        operator_cli,
        "publish_next",
        lambda *args, **kwargs: pytest.fail("publisher must not run when disabled"),
    )

    code = operator_cli.main(
        ["publish", "--account", "brand_a"],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )

    assert code == 3
    assert "disabled" in capsys.readouterr().err.lower()


def test_insights_uses_selected_account_credentials(tmp_path, monkeypatch, capsys):
    write_account(tmp_path, "brand_a")
    seen = {}

    class FakeAPI:
        def __init__(self, access_token, user_id, base_url):
            seen["api"] = (access_token, user_id, base_url)

    class FakeStore:
        def __init__(self, base_url, service_role_key, account_key=None):
            seen["store"] = (base_url, service_role_key, account_key)

    def fake_collect(api, store, account_sample_minutes):
        seen["sample"] = account_sample_minutes
        return {"account_snapshot": True, "posts_seen": 0}

    monkeypatch.setattr(operator_cli, "ThreadsAPI", FakeAPI)
    monkeypatch.setattr(operator_cli, "SupabaseStore", FakeStore)
    monkeypatch.setattr(operator_cli, "collect_once", fake_collect)

    code = operator_cli.main(
        ["insights", "--account", "brand_a"],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )

    assert code == 0
    assert seen["api"][0:2] == ("token-brand_a", "user-brand_a")
    assert seen["store"] == (
        "https://brand_a.supabase.co",
        "service-brand_a",
        "brand_a",
    )
    assert seen["sample"] == 15
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_activity_uses_selected_api_owned_posts_for_matching(tmp_path, monkeypatch, capsys):
    profile = tmp_path / "profiles" / "brand_a"
    profile.mkdir(parents=True)
    write_account(
        tmp_path,
        "brand_a",
        "THREADS_BROWSER_PROFILE=" + str(profile) + "\n"
        "ACTIVITY_FOLLOW_COLLECTOR_ENABLED=true\n",
    )
    seen = {}

    class FakeAPI:
        def __init__(self, access_token, user_id, base_url):
            pass

        def list_posts(self):
            return [{"id": "post-1", "text": "hello", "timestamp": "now", "permalink": "url"}]

    class FakeStore:
        def __init__(self, base_url, service_role_key, account_key=None):
            seen["account_key"] = account_key

    def fake_activity(**kwargs):
        seen["own_posts"] = kwargs["own_posts"]
        seen["persist"] = kwargs["persist"]
        return {"success": True, "total_events": 0, "new_events": 0}

    monkeypatch.setattr(operator_cli, "ThreadsAPI", FakeAPI)
    monkeypatch.setattr(operator_cli, "SupabaseStore", FakeStore)
    monkeypatch.setattr(operator_cli, "collect_activity_follows", fake_activity)

    code = operator_cli.main(
        ["activity-follow", "--account", "brand_a", "--dry-run"],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )

    assert code == 0
    assert seen["account_key"] == "brand_a"
    assert seen["own_posts"][0]["id"] == "post-1"
    assert seen["persist"] is False
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_publish_dry_run_is_allowed_when_live_posting_is_disabled(tmp_path, monkeypatch, capsys):
    write_account(tmp_path, "brand_a")
    seen = {}

    class FakeAPI:
        def __init__(self, access_token, user_id, base_url):
            pass

    class FakeStore:
        def __init__(self, base_url, service_role_key, account_key=None):
            seen["account"] = account_key

    def fake_publish(api, store, table, campaign_code=None, dry_run=False):
        seen["args"] = (table, campaign_code, dry_run)
        return {"status": "idle"}

    monkeypatch.setattr(operator_cli, "ThreadsAPI", FakeAPI)
    monkeypatch.setattr(operator_cli, "SupabaseStore", FakeStore)
    monkeypatch.setattr(operator_cli, "publish_next", fake_publish)

    code = operator_cli.main(
        ["publish", "--account", "brand_a", "--dry-run"],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )

    assert code == 0
    assert seen["account"] == "brand_a"
    assert seen["args"] == ("threads_publish_queue", None, True)
    assert json.loads(capsys.readouterr().out)["status"] == "idle"


def test_enqueue_draft_uses_selected_account_without_constructing_threads_api(
    tmp_path, monkeypatch, capsys
):
    write_account(tmp_path, "brand_a")
    seen = {}

    class FakeStore:
        def __init__(self, base_url, service_role_key, account_key=None):
            seen["store"] = (base_url, service_role_key, account_key)

        def enqueue_draft(
            self,
            table,
            main_post_text,
            reply_texts=None,
            campaign_code=None,
            scheduled_at=None,
        ):
            seen["enqueue"] = (
                table,
                main_post_text,
                reply_texts,
                campaign_code,
                scheduled_at,
            )
            return {"id": 9, "status": "draft"}

    def fail_api(*args, **kwargs):
        pytest.fail("enqueue-draft must not construct ThreadsAPI or publish")

    monkeypatch.setattr(operator_cli, "SupabaseStore", FakeStore)
    monkeypatch.setattr(operator_cli, "ThreadsAPI", fail_api)

    code = operator_cli.main(
        [
            "enqueue-draft",
            "--account",
            "brand_a",
            "--text",
            "generated by Hermes",
            "--reply",
            "reply one",
            "--reply",
            "reply two",
            "--campaign-code",
            "HERMES_GENERATED",
            "--scheduled-at",
            NOW,
        ],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert seen["store"] == (
        "https://brand_a.supabase.co",
        "service-brand_a",
        "brand_a",
    )
    assert seen["enqueue"] == (
        "threads_publish_queue",
        "generated by Hermes",
        ["reply one", "reply two"],
        "HERMES_GENERATED",
        NOW,
    )
    assert payload == {"ok": True, "account": "brand_a", "id": 9, "status": "draft"}
