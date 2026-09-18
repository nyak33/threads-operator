"""RED tests: `trend enrich` CLI subcommand.

trend enrich --account <acct> --url <permalink> --dry-run  (zero store use)
trend enrich --account <acct> --id <n>                     (live: read row, patch pk)
- requires THREADS_BROWSER_PROFILE (like activity-follow)
- dry-run: browser read + extraction only; NEVER constructs a SupabaseStore
- output is sanitized JSON, never leaks secrets
"""

import json

import pytest

from threads_operator import operator_cli

TOKEN_KEY = "THREADS_ACCESS_" "TOKEN"
SERVICE_KEY = "SUPABASE_SERVICE_" "ROLE_KEY"
SHORTCODE = "DdGAw6kFfPA"
PERMALINK = f"https://www.threads.com/@syaqir_sharani/post/{SHORTCODE}"


def write_account(home, name, *, profile=True):
    accounts = home / "accounts"
    accounts.mkdir(parents=True, exist_ok=True)
    text = (
        TOKEN_KEY + "=token-" + name + "\n"
        "THREADS_USER_ID=user-" + name + "\n"
        "SUPABASE_URL=https://" + name + ".supabase.co\n"
        + SERVICE_KEY + "=service-" + name + "\n"
    )
    if profile:
        text += "THREADS_BROWSER_PROFILE=" + str(home / "profile") + "\n"
    (accounts / f"{name}.env").write_text(text)


def _feed_doc(pk="1788000000000001", code=SHORTCODE):
    post = json.dumps({"pk": pk, "code": code, "text": "kita ni"})
    return (
        '{"__bbox":{"status":"success","result":{"data":{"feedData":{"edges":'
        '[{"node":{"text_post_app_thread":' + post + "}}]}}}}}"
    )


@pytest.fixture
def stub_reader(monkeypatch):
    from threads_operator import trend_enrich

    def fake_read(profile_dir, permalink, *, settle_seconds=3.0):
        return _feed_doc()

    monkeypatch.setattr(trend_enrich, "read_post_document_sync", fake_read)
    return fake_read


def test_trend_enrich_dry_run_never_constructs_store(tmp_path, capsys, monkeypatch, stub_reader):
    write_account(tmp_path, "syaqir")
    monkeypatch.setattr(
        operator_cli,
        "SupabaseStore",
        lambda *a, **k: pytest.fail("dry-run must not construct a store"),
    )

    code = operator_cli.main(
        [
            "trend", "enrich",
            "--account", "syaqir",
            "--url", PERMALINK,
            "--dry-run",
        ],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["writes"] == 0
    assert payload["evidence"]["source_post_id"] == "1788000000000001"
    assert payload["expected_shortcode"] == SHORTCODE


def test_trend_enrich_dry_run_output_sanitized(tmp_path, capsys, monkeypatch, stub_reader):
    write_account(tmp_path, "syaqir")
    monkeypatch.setattr(
        operator_cli, "SupabaseStore", lambda *a, **k: pytest.fail("no store")
    )
    code = operator_cli.main(
        [
            "trend", "enrich",
            "--account", "syaqir",
            "--url", PERMALINK,
            "--dry-run",
        ],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "token-syaqir" not in out
    assert "service-syaqir" not in out


def test_trend_enrich_requires_browser_profile(tmp_path, capsys, monkeypatch):
    write_account(tmp_path, "syaqir", profile=False)
    monkeypatch.setattr(
        operator_cli, "SupabaseStore", lambda *a, **k: pytest.fail("no store")
    )
    code = operator_cli.main(
        [
            "trend", "enrich",
            "--account", "syaqir",
            "--url", PERMALINK,
            "--dry-run",
        ],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )
    assert code != 0
    err = capsys.readouterr().err
    # AccountConfigError prints a redacted message to stderr and exits 2
    assert "THREADS_BROWSER_PROFILE" in err


def test_trend_enrich_live_patches_store(monkeypatch, tmp_path, capsys):
    """Non-dry-run routes through the store's allowlisted update method."""
    from threads_operator import trend_enrich

    def fake_read(profile_dir, permalink, *, settle_seconds=3.0):
        return _feed_doc()

    monkeypatch.setattr(trend_enrich, "read_post_document_sync", fake_read)
    write_account(tmp_path, "syaqir")

    candidate = {
        "id": 1,
        "target_account_id": "syaqir",
        "source_permalink": PERMALINK,
        "status": "discovered",
        "source_post_id": None,
    }
    patches = []

    class FakeStore:
        def __init__(self, *a, **k):
            pass

        def get_trend_candidate(self, cid):
            return candidate

        def update_trend_candidate_evidence(self, *, candidate_id, evidence, enrichment=None):
            patches.append((candidate_id, evidence))
            return {**candidate, **evidence}

    monkeypatch.setattr(operator_cli, "SupabaseStore", FakeStore)

    code = operator_cli.main(
        ["trend", "enrich", "--account", "syaqir", "--id", "1"],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )

    assert code == 0
    assert patches[0][0] == 1
    assert patches[0][1]["source_post_id"] == "1788000000000001"
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["updated"] is True
    assert payload["dry_run"] is False
