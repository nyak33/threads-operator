from pathlib import Path


SKILL = Path(__file__).resolve().parents[1] / "skills" / "threads-insights" / "SKILL.md"


def test_threads_insights_skill_has_required_contract():
    text = SKILL.read_text()
    assert text.startswith("---\nname: threads-insights\n")
    assert "description: Use when" in text
    for label in ("measured", "calculated", "inferred", "attributed"):
        assert label in text
    assert "same-age" in text.lower()
    assert "missing" in text.lower() and "zero" in text.lower()
    assert "does not prove" in text.lower()
    assert "Hi, saya datang dari Threads." in text
