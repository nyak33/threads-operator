"""Command-line entry point for historical Threads Insights collection."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping

from .collector import collect_once
from .supabase_store import SupabaseStore
from .threads_api import DEFAULT_BASE_URL, ThreadsAPI

REQUIRED_ENV = (
    "THREADS_ACCESS_TOKEN",
    "THREADS_USER_ID",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
)


class ConfigError(ValueError):
    pass


def load_settings(env: Mapping[str, str]) -> dict[str, object]:
    missing = [name for name in REQUIRED_ENV if not env.get(name)]
    if missing:
        raise ConfigError("Missing required environment variables: " + ", ".join(missing))
    raw_interval = env.get("THREADS_ACCOUNT_SAMPLE_MINUTES", "15")
    try:
        account_sample_minutes = int(raw_interval)
    except (TypeError, ValueError) as exc:
        raise ConfigError("THREADS_ACCOUNT_SAMPLE_MINUTES must be a positive integer") from exc
    if account_sample_minutes <= 0:
        raise ConfigError("THREADS_ACCOUNT_SAMPLE_MINUTES must be a positive integer")
    return {
        "threads_access_token": env["THREADS_ACCESS_TOKEN"],
        "threads_user_id": env["THREADS_USER_ID"],
        "threads_base_url": env.get("THREADS_API_BASE_URL", DEFAULT_BASE_URL),
        "supabase_url": env["SUPABASE_URL"],
        "supabase_service_role_key": env["SUPABASE_SERVICE_ROLE_KEY"],
        "account_sample_minutes": account_sample_minutes,
    }


def main() -> int:
    try:
        settings = load_settings(os.environ)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    api = ThreadsAPI(
        str(settings["threads_access_token"]),
        str(settings["threads_user_id"]),
        base_url=str(settings["threads_base_url"]),
    )
    store = SupabaseStore(
        str(settings["supabase_url"]),
        str(settings["supabase_service_role_key"]),
    )
    try:
        summary = collect_once(
            api,
            store,
            account_sample_minutes=int(settings["account_sample_minutes"]),
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, **summary}, separators=(",", ":")))
    return 0
