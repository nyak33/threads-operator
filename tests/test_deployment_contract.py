from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_exposes_unified_threads_operator_command():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["threads-operator"] == "threads_operator.operator_cli:main"


def test_bootstrap_script_installs_project_and_creates_runtime_home():
    text = (ROOT / "scripts" / "bootstrap.sh").read_text()
    assert "python3" in text
    assert "3.11" in text
    assert ".venv" in text
    assert "pip install" in text
    assert "THREADS_OPERATOR_HOME" in text
    assert "accounts" in text
    assert "browser-profiles" in text


def test_add_account_script_is_secret_safe_and_refuses_overwrite():
    text = (ROOT / "scripts" / "add_account.sh").read_text()
    assert "umask 077" in text
    assert "accounts" in text
    assert "browser-profiles" in text
    assert "already exists" in text.lower()
    assert "THREADS_POSTING_ENABLED=false" in text
    assert "THREADS_EXECUTION_MODE=approval_required" in text


def test_account_example_uses_safe_defaults_and_placeholders_only():
    text = (ROOT / "accounts" / "example.env").read_text()
    assert "THREADS_ACCESS_TOKEN=replace_me" in text
    assert "SUPABASE_SERVICE_ROLE_KEY=replace_me" in text
    assert "THREADS_POSTING_ENABLED=false" in text
    assert "THREADS_EXECUTION_MODE=approval_required" in text
    assert "THREADS_BROWSER_PROFILE=" in text


def test_runtime_account_files_are_gitignored():
    text = (ROOT / ".gitignore").read_text()
    assert "accounts/*.env" in text
    assert "!accounts/example.env" in text
    assert ".threads-operator" in text


def test_activity_wrappers_route_to_unified_account_aware_cli():
    shell = (ROOT / "scripts" / "collect_activity_follows.sh").read_text()
    python_wrapper = (ROOT / "scripts" / "run_activity.py").read_text()
    module_wrapper = (
        ROOT / "src" / "threads_operator" / "activity_collector_cli.py"
    ).read_text()

    assert "threads-operator activity-follow" in shell
    assert "threads_operator.operator_cli" in python_wrapper
    assert "operator_cli" in module_wrapper


def test_root_operator_docs_exist_and_runbook_covers_portable_flow():
    for name in ("GOAL.md", "RUNBOOK.md", "PROGRESS.md"):
        assert (ROOT / name).is_file()

    runbook = (ROOT / "RUNBOOK.md").read_text().lower()
    assert "git clone" in runbook
    assert "bootstrap" in runbook
    assert "add_account" in runbook or "add-account" in runbook
    assert "doctor" in runbook
    assert "--account" in runbook
    assert "multiple" in runbook or "multi-account" in runbook
