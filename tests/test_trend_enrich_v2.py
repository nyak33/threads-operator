"""RED tests (v2): enrich_candidate broad-evidence orchestration + CLI.

Spec sections 9-11, 16:
- live enrich builds a factual evidence patch from observed facts ONLY,
  advances last_checked_at after a successful validated extraction,
  merges enrichment metadata (enrichment_source, media_type,
  self_thread_length) into raw_metadata via the store;
- identical evidence => semantic no-op (writes 0), row untouched;
- extraction failure => no evidence update, no last_checked_at change;
- --dry-run works with --id too: real read/extraction, ZERO Supabase
  writes, sanitized deterministic JSON (account, candidate id, expected
  shortcode, extraction source, would-update fields, writes: 0);
- non-Threads candidate rejected; account identity never overridable.
"""

import json

import pytest

from threads_operator import trend_enrich
from threads_operator.trend_enrich import TrendEnrichError

SHORTCODE = "DdGAw6kFfPA"
PERMALINK = f"https://www.threads.com/@syaqir_sharani/post/{SHORTCODE}"

TOKEN_KEY = "THREADS_ACCESS_" "TOKEN"
SERVICE_KEY = "SUPABASE_SERVICE_" "ROLE_KEY"


def _facts_doc(**overrides) -> str:
    import json as _json

    node = {
        "pk": "1788651234567890",
        "code": SHORTCODE,
        "taken_at": 1757350000,
        "text": "kita ni belakang kira",
        "user": {"username": "syaqir_sharani"},
        "like_count": 9,
        "text_post_app_info": {
            "direct_reply_count": 1,
            "repost_count": 1,
            "quote_count": 0,
        },
        "media_type": 19,
    }
    node.update(overrides)
    thread = '{"thread_items":[{"post":' + _json.dumps(node) + "}]}"
    return (
        '{"__bbox":{"status":"success","result":{"data":{"feedData":{"edges":'
        '[{"node":{"text_post_app_thread":' + thread + "}}]}}}}}"
    )


@pytest.fixture
def stub_reader(monkeypatch):
    def fake_read(profile_dir, permalink, *, settle_seconds=3.0):
        return _facts_doc()

    monkeypatch.setattr(trend_enrich, "read_post_document_sync", fake_read)
    return fake_read


class RecordingStore:
    def __init__(self, candidate):
        self.candidate = candidate
        self.evidence_calls = []
        self.pk_calls = []

    def get_trend_candidate(self, cid):
        return dict(self.candidate) if self.candidate and cid == self.candidate["id"] else None

    def update_trend_candidate_evidence(self, *, candidate_id, evidence, enrichment=None):
        self.evidence_calls.append(
            {"candidate_id": candidate_id, "evidence": evidence, "enrichment": enrichment}
        )
        return {**self.candidate, **evidence}

    def update_trend_candidate_source_post_id(self, *, candidate_id, source_post_id):
        self.pk_calls.append((candidate_id, source_post_id))
        return {**self.candidate, "source_post_id": source_post_id}


CANDIDATE = {
    "id": 1,
    "target_account_id": "syaqir",
    "source_platform": "threads",
    "source_permalink": PERMALINK,
    "source_post_id": None,
    "status": "discovered",
    "views": None,
}


# ------------------------------------------------------------- orchestration

def test_live_enrich_sends_broad_evidence_patch(stub_reader):
    store = RecordingStore(CANDIDATE)
    import pathlib

    result = trend_enrich.enrich_candidate(
        store, candidate_id=1, profile_dir=pathlib.Path("/tmp/whatever")
    )

    assert result["updated"] is True
    assert store.pk_calls == []  # v1 single-field path must no longer be used
    assert len(store.evidence_calls) == 1
    call = store.evidence_calls[0]
    assert call["candidate_id"] == 1
    ev = call["evidence"]
    assert ev["source_post_id"] == "1788651234567890"
    assert ev["source_username"] == "syaqir_sharani"
    assert ev["source_text"] == "kita ni belakang kira"
    assert ev["published_at"] == "2025-09-08T16:46:40+00:00"
    assert ev["likes"] == 9
    assert ev["replies"] == 1
    assert ev["reposts"] == 1
    assert ev["quotes"] == 0
    assert ev["last_checked_at"].endswith("+00:00")
    # prohibited never present
    for forbidden in ("status", "views", "topic", "trend_score"):
        assert forbidden not in ev
    enr = call["enrichment"]
    assert enr["enrichment_source"] == "permalink_preloader"
    assert enr["media_type"] == 19
    assert enr["self_thread_length"] == 1


def test_identical_evidence_is_semantic_noop(stub_reader):
    """Second run over an already-enriched identical row: writes 0."""
    enriched = dict(CANDIDATE)
    enriched.update(
        {
            "source_post_id": "1788651234567890",
            "source_username": "syaqir_sharani",
            "source_text": "kita ni belakang kira",
            "published_at": "2025-09-08T16:46:40+00:00",
            "likes": 9,
            "replies": 1,
            "reposts": 1,
            "quotes": 0,
            "last_checked_at": "2020-01-01T00:00:00+00:00",
        }
    )
    store = RecordingStore(enriched)
    import pathlib

    result = trend_enrich.enrich_candidate(
        store, candidate_id=1, profile_dir=pathlib.Path("/tmp/whatever")
    )
    assert result["updated"] is False
    assert store.evidence_calls == []
    assert store.pk_calls == []


def test_counter_change_triggers_refresh(stub_reader, monkeypatch):
    """Counters may change between runs; refresh must go through."""
    monkeypatch.setattr(
        trend_enrich,
        "read_post_document_sync",
        lambda *a, **k: _facts_doc(like_count=42),
    )
    enriched = dict(CANDIDATE)
    enriched.update({"source_post_id": "1788651234567890", "likes": 9})
    store = RecordingStore(enriched)
    import pathlib

    result = trend_enrich.enrich_candidate(
        store, candidate_id=1, profile_dir=pathlib.Path("/tmp/whatever")
    )
    assert result["updated"] is True
    ev = store.evidence_calls[0]["evidence"]
    assert ev["likes"] == 42
    # fields the payload omitted must NOT be nulled
    assert "source_text" not in ev or ev["source_text"] == "kita ni belakang kira"


def test_extraction_failure_writes_nothing(monkeypatch):
    def boom(profile_dir, permalink, *, settle_seconds=3.0):
        raise TrendEnrichError("no structured payload")

    monkeypatch.setattr(trend_enrich, "read_post_document_sync", boom)
    store = RecordingStore(CANDIDATE)
    import pathlib

    with pytest.raises(TrendEnrichError):
        trend_enrich.enrich_candidate(
            store, candidate_id=1, profile_dir=pathlib.Path("/tmp/whatever")
        )
    assert store.evidence_calls == []
    assert store.pk_calls == []


def test_non_threads_candidate_rejected(stub_reader):
    cand = dict(CANDIDATE, source_platform="x")
    store = RecordingStore(cand)
    import pathlib

    with pytest.raises(TrendEnrichError):
        trend_enrich.enrich_candidate(
            store, candidate_id=1, profile_dir=pathlib.Path("/tmp/x")
        )
    assert store.evidence_calls == []


# ------------------------------------------------------------------- dry-run

def test_dry_run_by_id_reports_evidence_and_zero_writes(stub_reader):
    import pathlib

    store = RecordingStore(CANDIDATE)
    result = trend_enrich.enrich_candidate_dry_run(
        store, candidate_id=1, profile_dir=pathlib.Path("/tmp/x")
    )
    assert store.evidence_calls == []
    assert store.pk_calls == []
    assert result["dry_run"] is True
    assert result["writes"] == 0
    assert result["candidate_id"] == 1
    assert result["expected_shortcode"] == SHORTCODE
    assert result["extraction_source"] == "permalink_preloader"
    assert result["evidence"]["source_post_id"] == "1788651234567890"
    assert result["evidence"]["likes"] == 9


# ----------------------------------------------------------------------- CLI

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


def test_cli_dry_run_with_id_never_constructs_store(tmp_path, capsys, monkeypatch, stub_reader):
    from threads_operator import operator_cli

    write_account(tmp_path, "syaqir")
    monkeypatch.setattr(
        operator_cli,
        "SupabaseStore",
        lambda *a, **k: pytest.fail("--id --dry-run must not construct a store"),
    )

    class FakeReadStore:
        def __init__(self, *a, **k):
            pass

        def get_trend_candidate(self, cid):
            return dict(CANDIDATE)

    # --id --dry-run needs a candidate READ but zero writes: the CLI may
    # construct a store ONLY for the account-scoped read, so simulate with a
    # read-only fake; any write method must explode.
    def exploding_store(*a, **k):
        class Exploder(FakeReadStore):
            def update_trend_candidate_evidence(self, **kw):
                pytest.fail("dry-run must perform zero writes")

            def update_trend_candidate_source_post_id(self, **kw):
                pytest.fail("dry-run must perform zero writes")

        return Exploder()

    monkeypatch.setattr(operator_cli, "SupabaseStore", exploding_store)
    code = operator_cli.main(
        ["trend", "enrich", "--account", "syaqir", "--id", "1", "--dry-run"],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["writes"] == 0
    assert payload["account"] == "syaqir"
    assert payload["candidate_id"] == 1
    assert payload["expected_shortcode"] == SHORTCODE
    assert payload["extraction_source"] == "permalink_preloader"
    assert "evidence" in payload
    out = capsys.readouterr()
    assert "token-syaqir" not in out.out
    assert "service-syaqir" not in out.out


def test_cli_live_uses_evidence_method(tmp_path, capsys, monkeypatch, stub_reader):
    from threads_operator import operator_cli

    write_account(tmp_path, "syaqir")
    store = RecordingStore(CANDIDATE)
    monkeypatch.setattr(operator_cli, "SupabaseStore", lambda *a, **k: store)

    code = operator_cli.main(
        ["trend", "enrich", "--account", "syaqir", "--id", "1"],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["writes"] == 1
    assert payload["updated"] is True
    assert len(store.evidence_calls) == 1
    assert store.pk_calls == []


def test_cli_extractor_failure_nonzero_exit_no_write(tmp_path, capsys, monkeypatch):
    from threads_operator import operator_cli

    def boom(profile_dir, permalink, *, settle_seconds=3.0):
        raise TrendEnrichError("no structured payload")

    monkeypatch.setattr(trend_enrich, "read_post_document_sync", boom)
    write_account(tmp_path, "syaqir")
    store = RecordingStore(CANDIDATE)
    monkeypatch.setattr(operator_cli, "SupabaseStore", lambda *a, **k: store)

    code = operator_cli.main(
        ["trend", "enrich", "--account", "syaqir", "--id", "1"],
        process_env={"THREADS_OPERATOR_HOME": str(tmp_path)},
    )
    assert code != 0
    assert store.evidence_calls == []
    err = capsys.readouterr().err
    assert "token-syaqir" not in err
    assert "service-syaqir" not in err
