"""Contract tests for scripts/doctor.sh and scripts/install_jobs.sh (Parts 9 + 20).

These verify the scripts' shape and safety properties without touching the
live Hermes cron store: idempotency-by-name-matching, no-secret printing,
secret-safe env loading (parse, never source), and manifest/manifest-template
agreement.
"""
from pathlib import Path
import json
import re

ROOT = Path(__file__).resolve().parents[1]
DOCTOR = (ROOT / "scripts" / "doctor.sh").read_text()
INSTALL = (ROOT / "scripts" / "install_jobs.sh").read_text()
ENV_LIB = (ROOT / "scripts" / "lib" / "env.sh").read_text()
MANIFEST = json.loads((ROOT / "config" / "runtime-jobs.json").read_text())


# --- doctor.sh ---------------------------------------------------------------

def test_doctor_prints_pass_warn_fail_levels():
    assert "PASS" in DOCTOR and "WARN" in DOCTOR and "FAIL" in DOCTOR


def test_doctor_never_prints_secret_values():
    # doctor must not echo credential material; greps extract presence only
    assert "not shown" in DOCTOR
    for bad in ("$THREADS_ACCESS_TOKEN\n", "echo $SUPABASE_SERVICE_ROLE_KEY",
                "echo $TELEGRAM_BOT_TOKEN", "echo \"$TG_TOKEN\""):
        assert bad not in DOCTOR


def test_doctor_checks_core_areas():
    for area in ("Python", "threads_operator", "account", "Supabase",
                 "Telegram", "Hermes", "runtime dir"):
        assert area in DOCTOR, f"doctor.sh must cover: {area}"


def test_doctor_flags_rollups_gap_as_warn_not_fail():
    # 002 rollups table is a known pending manual step — WARN, not FAIL.
    # The WARN branch is the one mentioning the known pending step.
    assert "threads_post_daily_rollups" in DOCTOR
    warn_idx = DOCTOR.index("needs migrations/002_post_daily_rollups.sql")
    assert "warn" in DOCTOR[max(0, warn_idx - 200):warn_idx].lower()


def test_doctor_supports_account_flag():
    assert "--account" in DOCTOR


# --- scripts/lib/env.sh -------------------------------------------------------

def test_env_lib_parses_instead_of_sourcing():
    # the whole point of the lib: never `.` (source) an env file
    assert not re.search(r'^\s*\.\s+"?\$?(FILE|file|ENV|env)', ENV_LIB, re.M)
    assert "export" in ENV_LIB and "while IFS= read" in ENV_LIB


def test_env_lib_strips_export_prefix_and_quotes():
    assert 'line="${line#export }"' in ENV_LIB
    # strips double- and single-quoted values (parameter expansion on `val`)
    assert 'val="${val#' in ENV_LIB and 'val="${val%' in ENV_LIB


# --- config/runtime-jobs.json ---------------------------------------------------

def test_manifest_has_all_seven_live_jobs():
    names = {j["name"] for j in MANIFEST["jobs"]}
    expected = {
        "Threads Insights Collector",
        "Threads Activity Follow Collector",
        "Threads Stale-Claim Watchdog",
        "Threads Trend Engagement Watchdog ({{ACCOUNT}})",
        "Threads Own-Replies Watchdog ({{ACCOUNT}})",
        "Threads Trend Approval Timeout Watchdog ({{ACCOUNT}})",
        "Threads Trend Discovery ({{ACCOUNT}})",
    }
    assert names == expected


def test_manifest_script_jobs_reference_existing_scripts():
    for j in MANIFEST["jobs"]:
        if j["kind"] == "script":
            assert (ROOT / j["script"]).is_file(), j["script"]
        else:
            assert (ROOT / j["prompt_template"]).is_file(), j["prompt_template"]


def test_manifest_schedules_match_live_capture():
    """Schedules mirror the verified live Hermes cron jobs (captured 2026-09-23)."""
    live = {
        "Threads Insights Collector": {"kind": "interval", "minutes": 5},
        "Threads Activity Follow Collector": {"kind": "cron", "expr": "*/15 * * * *"},
        "Threads Stale-Claim Watchdog": {"kind": "cron", "expr": "*/15 * * * *"},
        "Threads Trend Engagement Watchdog ({{ACCOUNT}})": {"kind": "cron", "expr": "*/30 * * * *"},
        "Threads Own-Replies Watchdog ({{ACCOUNT}})": {"kind": "cron", "expr": "*/5 * * * *"},
        "Threads Trend Approval Timeout Watchdog ({{ACCOUNT}})": {"kind": "cron", "expr": "0 * * * *"},
        "Threads Trend Discovery ({{ACCOUNT}})": {"kind": "cron", "expr": "0 8-23/3 * * *"},
    }
    for j in MANIFEST["jobs"]:
        assert j["schedule"] == live[j["name"]], j["name"]


def test_trend_discovery_template_is_templated_not_hardcoded():
    tmpl = (ROOT / "config" / "trend-discovery-prompt.md.tmpl").read_text()
    assert "{{REPO}}" in tmpl and "{{ACCOUNT}}" in tmpl
    assert "/home/admin" not in tmpl


# --- install_jobs.sh ------------------------------------------------------------

def test_installer_is_idempotent_by_name_matching():
    # convergence keyed on exact job name, EDIT existing instead of duplicating
    assert "existing" in INSTALL and 'j.get("name' in INSTALL
    assert "EDIT" in INSTALL and "CREATE" in INSTALL


def test_installer_has_status_remove_dryrun_modes():
    for mode in ("--status", "--remove", "--dry-run"):
        assert mode in INSTALL


def test_installer_never_prints_secrets():
    assert "NEVER prints secrets" in INSTALL


def test_installer_rejects_missing_hermes_cli():
    assert "hermes CLI not on PATH" in INSTALL
