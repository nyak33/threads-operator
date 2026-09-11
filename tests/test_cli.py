import pytest

from threads_operator.cli import ConfigError, load_settings


def test_load_settings_requires_all_credentials():
    with pytest.raises(ConfigError) as exc:
        load_settings({"THREADS_ACCESS_TOKEN": "token"})
    message = str(exc.value)
    assert "THREADS_USER_ID" in message
    assert "SUPABASE_URL" in message
    assert "SUPABASE_SERVICE_ROLE_KEY" in message


def test_load_settings_uses_safe_defaults():
    settings = load_settings(
        {
            "THREADS_ACCESS_TOKEN": "token",
            "THREADS_USER_ID": "user",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "key",
        }
    )
    assert settings["threads_base_url"] == "https://graph.threads.net/v1.0"
    assert settings["account_sample_minutes"] == 15


def test_load_settings_rejects_invalid_sample_interval():
    env = {
        "THREADS_ACCESS_TOKEN": "token",
        "THREADS_USER_ID": "user",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "key",
        "THREADS_ACCOUNT_SAMPLE_MINUTES": "zero",
    }
    with pytest.raises(ConfigError):
        load_settings(env)
