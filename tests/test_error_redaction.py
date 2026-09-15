from pathlib import Path

from threads_operator import operator_cli
from threads_operator.safe_errors import redact_error

TOKEN_KEY = "THREADS_ACCESS_" "TOKEN"
SERVICE_KEY = "SUPABASE_SERVICE_" "ROLE_KEY"


def _account(home: Path) -> None:
    accounts = home / "accounts"
    accounts.mkdir(parents=True, exist_ok=True)
    (accounts / "brand_a.env").write_text(
        TOKEN_KEY + "=very-secret-token\n"
        "THREADS_USER_ID=user-a\n"
        "SUPABASE_URL=https://example.supabase.co\n"
        + SERVICE_KEY + "=very-secret-service-key\n"
    )


def test_redact_error_hides_known_values_and_access_token_query():
    text = (
        "request failed https://graph.threads.net/v1.0/me?access_token="
        "very-secret-token&fields=id using very-secret-service-key"
    )
    safe = redact_error(text, ["very-secret-token", "very-secret-service-key"])
    assert "very-secret-token" not in safe
    assert "very-secret-service-key" not in safe
    assert "access_token=[REDACTED]" in safe


def test_unified_cli_never_prints_selected_account_secrets_on_failure(
    tmp_path, monkeypatch, capsys
):
    _account(tmp_path)

    def fail_collect(*args, **kwargs):
        raise RuntimeError(
            "upstream error access_token=very-secret-token; "
            "service=very-secret-service-key"
        )

    monkeypatch.setattr(operator_cli, "collect_once", fail_collect)

    code = operator_cli.main(
        ["insights", "--account", "brand_a"],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )

    output = capsys.readouterr().err
    assert code == 1
    assert "very-secret-token" not in output
    assert "very-secret-service-key" not in output
    assert "[REDACTED]" in output
