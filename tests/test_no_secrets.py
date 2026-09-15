"""Repository safety test: nothing secret-looking may be publishable."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".md", ".py", ".toml", ".yaml", ".yml", ".sql", ".example", ".sh", ".env", ""
}


def publishable_files():
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True)
    return [ROOT / rel for rel in out.stdout.splitlines() if rel.strip()]


def repository_text():
    here = Path(__file__).resolve()
    chunks = []
    for path in publishable_files():
        if not path.is_file():
            continue
        if path.resolve() == here:
            continue
        if path.suffix not in TEXT_SUFFIXES and path.name != ".env.example":
            continue
        try:
            chunks.append(f"\n# {path.relative_to(ROOT)}\n{path.read_text()}")
        except UnicodeDecodeError:
            continue
    return "".join(chunks)


def test_env_example_contains_placeholders_not_real_values():
    text = (ROOT / ".env.example").read_text()
    assert "THREADS_ACCESS_TOKEN=replace_me" in text
    assert "SUPABASE_SERVICE_ROLE_KEY=replace_me" in text


def test_repository_contains_no_obvious_real_credentials_or_phone_numbers():
    text = repository_text()
    assert not re.search(r"THREADS_ACCESS_TOKEN=(?!replace_me)[^\s]+", text)
    assert not re.search(r"SUPABASE_SERVICE_ROLE_KEY=(?!replace_me)[^\s]+", text)
    assert not re.search(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}", text)
    assert not re.search(r"(?<!\d)\+?60\d{9,10}(?!\d)", text)
    assert not re.search(r"wa\.me/\d", text)


def test_real_env_file_is_not_publishable():
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=ROOT, capture_output=True, text=True)
    assert tracked.returncode != 0, ".env must never be tracked by git"
    assert ".env" in (ROOT / ".gitignore").read_text().splitlines()
