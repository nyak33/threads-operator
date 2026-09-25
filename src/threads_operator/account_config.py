"""Account-scoped configuration for portable multi-account deployments.

Account secrets deliberately come only from the selected account file. Process
environment is used for operator-wide selectors (home/account/logging), not as a
fallback for another account's credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from collections.abc import Mapping

DEFAULT_HOME = Path("~/.threads-operator")
ACCOUNT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

REQUIRED_ACCOUNT_VALUES = (
    "THREADS_ACCESS_TOKEN",
    "THREADS_USER_ID",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
)

DEFAULT_ACCOUNT_VALUES: dict[str, str] = {
    "THREADS_API_BASE_URL": "https://graph.threads.net/v1.0",
    "THREADS_ACCOUNT_SAMPLE_MINUTES": "15",
    "ACTIVITY_FOLLOW_COLLECTOR_ENABLED": "false",
    "THREADS_POSTING_ENABLED": "false",
    "THREADS_EXECUTION_MODE": "approval_required",
    "THREADS_QUEUE_TABLE": "threads_publish_queue",
    "THREADS_QUEUE_CAMPAIGN_CODE": "",
    # Optional: the account's real Threads username (e.g. "syaqir_sharani").
    # Used by the DM sender to verify the browser is logged into the exact
    # account. Distinct from the account_key label (env-file name).
    "THREADS_USERNAME": "",
}

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}


class AccountConfigError(ValueError):
    """Raised when an account selector or account environment file is invalid."""


@dataclass(frozen=True)
class AccountConfig:
    name: str
    home: Path
    env_path: Path
    values: dict[str, str]

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)

    def require(self, key: str) -> str:
        value = self.values.get(key, "").strip()
        if not value:
            raise AccountConfigError(f"Missing required account setting: {key}")
        return value

    def get_int(self, key: str) -> int:
        raw = self.require(key)
        try:
            value = int(raw)
        except ValueError as exc:
            raise AccountConfigError(f"{key} must be a positive integer") from exc
        if value <= 0:
            raise AccountConfigError(f"{key} must be a positive integer")
        return value

    def get_bool(self, key: str) -> bool:
        raw = (self.get(key, "") or "").strip().lower()
        if raw in _TRUE_VALUES:
            return True
        if raw in _FALSE_VALUES:
            return False
        raise AccountConfigError(f"{key} must be true or false")


def _validate_account_key(name: str) -> str:
    if not isinstance(name, str) or not ACCOUNT_KEY_RE.fullmatch(name):
        raise AccountConfigError(
            "Invalid account key; use letters, digits, '-' or '_' and start with a letter or digit"
        )
    return name


def resolve_home(process_env: Mapping[str, str] | None = None) -> Path:
    env = process_env if process_env is not None else os.environ
    raw = env.get("THREADS_OPERATOR_HOME") or str(DEFAULT_HOME)
    return Path(raw).expanduser().resolve()


def resolve_account_name(
    explicit: str | None,
    process_env: Mapping[str, str] | None = None,
) -> str:
    env = process_env if process_env is not None else os.environ
    name = explicit or env.get("THREADS_ACCOUNT")
    if not name:
        raise AccountConfigError(
            "Account is required; pass --account <key> or set THREADS_ACCOUNT"
        )
    return _validate_account_key(name)


def _strip_optional_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise AccountConfigError(f"Invalid env line {line_number} in {path.name}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise AccountConfigError(f"Invalid env key on line {line_number} in {path.name}")
        values[key] = _strip_optional_quotes(value.strip())
    return values


def load_account_config(
    name: str,
    process_env: Mapping[str, str] | None = None,
) -> AccountConfig:
    account_name = _validate_account_key(name)
    home = resolve_home(process_env)
    env_path = home / "accounts" / f"{account_name}.env"
    if not env_path.is_file():
        raise AccountConfigError(f"Account config not found: {env_path}")

    file_values = _read_env_file(env_path)
    missing = [key for key in REQUIRED_ACCOUNT_VALUES if not file_values.get(key, "").strip()]
    if missing:
        raise AccountConfigError(
            "Missing required account settings: " + ", ".join(missing)
        )

    values = {**DEFAULT_ACCOUNT_VALUES, **file_values}
    config = AccountConfig(
        name=account_name,
        home=home,
        env_path=env_path,
        values=values,
    )
    config.get_int("THREADS_ACCOUNT_SAMPLE_MINUTES")
    config.get_bool("ACTIVITY_FOLLOW_COLLECTOR_ENABLED")
    config.get_bool("THREADS_POSTING_ENABLED")
    return config


def list_accounts(process_env: Mapping[str, str] | None = None) -> list[str]:
    accounts_dir = resolve_home(process_env) / "accounts"
    if not accounts_dir.is_dir():
        return []
    names: list[str] = []
    for path in accounts_dir.glob("*.env"):
        name = path.stem
        if ACCOUNT_KEY_RE.fullmatch(name):
            names.append(name)
    return sorted(names)
