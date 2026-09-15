from pathlib import Path

import pytest

from threads_operator.account_config import (
    AccountConfigError,
    list_accounts,
    load_account_config,
    resolve_account_name,
)

TOKEN_KEY = "THREADS_ACCESS_" "TOKEN"
SERVICE_KEY = "SUPABASE_SERVICE_" "ROLE_KEY"


def _write_account(home: Path, name: str, body: str) -> Path:
    accounts = home / "accounts"
    accounts.mkdir(parents=True, exist_ok=True)
    path = accounts / f"{name}.env"
    path.write_text(body)
    return path


def _required_env(extra: str = "") -> str:
    return (
        TOKEN_KEY + "=account-token\n"
        "THREADS_USER_ID=12345\n"
        "SUPABASE_URL=https://example.supabase.co\n"
        + SERVICE_KEY + "=account-service-key\n"
        + extra
    )


def test_resolve_account_name_prefers_explicit_value():
    assert resolve_account_name("brand_a", {"THREADS_ACCOUNT": "brand_b"}) == "brand_a"


def test_resolve_account_name_uses_threads_account_fallback():
    assert resolve_account_name(None, {"THREADS_ACCOUNT": "brand_b"}) == "brand_b"


def test_resolve_account_name_refuses_to_guess():
    with pytest.raises(AccountConfigError, match="--account"):
        resolve_account_name(None, {})


@pytest.mark.parametrize("name", ["../escape", "bad/name", "bad name", "", ".hidden"])
def test_invalid_account_keys_are_rejected(tmp_path, name):
    with pytest.raises(AccountConfigError, match="account key"):
        load_account_config(name, {"THREADS_OPERATOR_HOME": str(tmp_path)})


def test_load_account_config_reads_only_selected_account_file(tmp_path):
    _write_account(tmp_path, "brand_a", _required_env("THREADS_BROWSER_PROFILE=/profiles/a\n"))
    _write_account(
        tmp_path,
        "brand_b",
        _required_env().replace("account-token", "brand-b-token").replace("12345", "67890"),
    )

    cfg = load_account_config("brand_b", {"THREADS_OPERATOR_HOME": str(tmp_path)})

    assert cfg.name == "brand_b"
    assert cfg.get(TOKEN_KEY) == "brand-b-token"
    assert cfg.get("THREADS_USER_ID") == "67890"
    assert cfg.env_path == tmp_path / "accounts" / "brand_b.env"


def test_process_level_account_secrets_do_not_fill_missing_selected_account_values(tmp_path):
    _write_account(
        tmp_path,
        "brand_a",
        "THREADS_USER_ID=12345\n"
        "SUPABASE_URL=https://example.supabase.co\n"
        + SERVICE_KEY + "=account-service-key\n",
    )
    process_env = {
        "THREADS_OPERATOR_HOME": str(tmp_path),
        TOKEN_KEY: "wrong-global-token",
        SERVICE_KEY: "wrong-global-service-key",
    }

    with pytest.raises(AccountConfigError, match=TOKEN_KEY):
        load_account_config("brand_a", process_env)


def test_load_account_config_requires_account_file(tmp_path):
    with pytest.raises(AccountConfigError, match="Account config not found"):
        load_account_config("missing", {"THREADS_OPERATOR_HOME": str(tmp_path)})


def test_load_account_config_supports_safe_defaults_and_typed_values(tmp_path):
    _write_account(tmp_path, "brand_a", _required_env())
    cfg = load_account_config("brand_a", {"THREADS_OPERATOR_HOME": str(tmp_path)})

    assert cfg.get("THREADS_API_BASE_URL") == "https://graph.threads.net/v1.0"
    assert cfg.get_int("THREADS_ACCOUNT_SAMPLE_MINUTES") == 15
    assert cfg.get_bool("ACTIVITY_FOLLOW_COLLECTOR_ENABLED") is False
    assert cfg.get_bool("THREADS_POSTING_ENABLED") is False
    assert cfg.get("THREADS_EXECUTION_MODE") == "approval_required"
    assert cfg.get("THREADS_QUEUE_TABLE") == "threads_publish_queue"


def test_list_accounts_is_sorted_and_ignores_non_env_files(tmp_path):
    _write_account(tmp_path, "zeta", _required_env())
    _write_account(tmp_path, "alpha", _required_env())
    (tmp_path / "accounts" / "README.txt").write_text("ignore")

    assert list_accounts({"THREADS_OPERATOR_HOME": str(tmp_path)}) == ["alpha", "zeta"]
