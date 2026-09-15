from __future__ import annotations

from threads_operator import activity_cli


def test_main_dispatches_insights_with_two_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(activity_cli, "_load_env", lambda *a, **k: {})
    monkeypatch.setattr(activity_cli, "_setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(activity_cli, "cmd_insights", lambda env, args: calls.append((env, args)) or 0)
    assert activity_cli.main(["insights"]) == 0
    assert len(calls) == 1


def test_activity_follow_cli_passes_persist_false_and_account_key_for_dry_run(monkeypatch):
    seen = {}

    class DummyStore:
        def __init__(self, *a, **k):
            seen["store_kwargs"] = k

    monkeypatch.setattr(activity_cli, "SupabaseStore", DummyStore)
    monkeypatch.setattr(activity_cli, "collect_activity_follows", lambda **kwargs: seen.update(kwargs) or {
        "success": True, "total_events": 0, "new_events": 0,
        "high": 0, "medium": 0, "low": 0, "unknown": 0,
    })
    args = activity_cli._build_parser().parse_args(["activity-follow", "--dry-run"])
    env = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "secret",
        "THREADS_ACCOUNT": "legacy_account",
    }
    rc = activity_cli.cmd_activity_follow(env, args, activity_cli.pathlib.Path("/tmp/profile"))
    assert rc == 0
    assert seen["persist"] is False
    assert seen["store_kwargs"]["account_key"] == "legacy_account"


def test_insights_uses_default_threads_api_base_url_when_env_omits_it(monkeypatch):
    seen = {}

    class DummyAPI:
        user_id = "u"
        def __init__(self, token, user_id, base_url=None):
            seen["base_url"] = base_url

    class DummyStore:
        def __init__(self, *a, **k):
            pass

    import sys, types
    import threads_operator.threads_api as threads_api
    fake_collector = types.ModuleType("threads_operator.collector")
    fake_collector.collect_once = lambda *a, **k: {}
    monkeypatch.setitem(sys.modules, "threads_operator.collector", fake_collector)
    monkeypatch.setattr(threads_api, "ThreadsAPI", DummyAPI, raising=False)
    monkeypatch.setattr(activity_cli, "SupabaseStore", DummyStore)
    args = activity_cli._build_parser().parse_args(["insights"])
    env = {
        "THREADS_ACCESS_TOKEN": "token",
        "THREADS_USER_ID": "user",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "secret",
    }
    assert activity_cli.cmd_insights(env, args) == 0
    assert seen["base_url"] == "https://graph.threads.net/v1.0"
