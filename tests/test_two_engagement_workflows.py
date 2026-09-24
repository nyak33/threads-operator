"""Tests for the two engagement workflows (A: trend->own post, B: own-post replies).

Store-level tests use a stubbed httpx.Client against a fake PostgREST backend so
no live Supabase is touched. LLM-dependent paths inject a fake provider so no
network or credentials are needed.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from threads_operator import content_generation, own_replies, trend_engagement
from threads_operator.supabase_store import SupabaseStore, TREND_STATUSES


# --------------------------------------------------------------------------
# Fake PostgREST backend
# --------------------------------------------------------------------------

class FakePostgREST:
    """Minimal in-memory PostgREST for the tables these workflows touch."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            "threads_trend_candidates": [],
            "threads_own_reply_engagement": [],
            "threads_publish_queue": [],
        }
        self._ids: dict[str, int] = {}

    # -- request entrypoint -------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        table = request.url.path.rsplit("/", 1)[-1]
        rows = self.tables.setdefault(table, [])
        params = dict(request.url.params)
        if request.method == "GET":
            return self._select(rows, params)
        if request.method == "POST":
            body = json.loads(request.content.decode() or "{}")
            return self._insert(table, rows, body)
        if request.method == "PATCH":
            body = json.loads(request.content.decode() or "{}")
            return self._update(rows, params, body)
        return httpx.Response(405, json={"error": "method"})

    # -- operations ---------------------------------------------------------
    def _matches(self, row: dict[str, Any], params: dict[str, str]) -> bool:
        for key, raw in params.items():
            if key in {"select", "order", "limit"}:
                continue
            if raw.startswith("eq."):
                expected: Any = raw[3:]
                actual = row.get(key)
                if str(actual) != expected and actual != self._coerce(expected):
                    return False
            elif raw.startswith("in.("):
                options = raw[4:-1].split(",")
                if str(row.get(key)) not in options:
                    return False
            elif raw.startswith("lt."):
                cutoff = raw[3:]
                actual = row.get(key)
                if actual is None:
                    return False
                actual_dt = datetime.fromisoformat(str(actual).replace("Z", "+00:00"))
                cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
                if actual_dt >= cutoff_dt:
                    return False
            elif raw.startswith("is.null"):
                if row.get(key) is not None:
                    return False
            elif raw == "not.is.null":
                if row.get(key) is None:
                    return False
        return True

    @staticmethod
    def _coerce(value: str) -> Any:
        try:
            return int(value)
        except ValueError:
            return value

    def _select(self, rows: list[dict[str, Any]], params: dict[str, str]) -> httpx.Response:
        matched = [dict(r) for r in rows if self._matches(r, params)]
        limit = params.get("limit")
        if limit and limit.isdigit():
            matched = matched[: int(limit)]
        return httpx.Response(200, json=matched)

    def _insert(self, table: str, rows: list[dict[str, Any]], body: dict[str, Any]) -> httpx.Response:
        next_id = self._ids.get(table, 0) + 1
        self._ids[table] = next_id
        row = {"id": next_id, **body}
        # unique (account_key, reply_id) for own replies
        if table == "threads_own_reply_engagement":
            for existing in rows:
                if (
                    existing.get("account_key") == row.get("account_key")
                    and existing.get("reply_id") == row.get("reply_id")
                ):
                    return httpx.Response(409, json={"code": "23505"})
        rows.append(row)
        return httpx.Response(201, json=[dict(row)])

    def _update(
        self, rows: list[dict[str, Any]], params: dict[str, str], body: dict[str, Any]
    ) -> httpx.Response:
        updated: list[dict[str, Any]] = []
        for row in rows:
            if self._matches(row, params):
                row.update(body)
                updated.append(dict(row))
        return httpx.Response(200, json=updated)


@pytest.fixture()
def backend() -> FakePostgREST:
    return FakePostgREST()


@pytest.fixture()
def store(backend: FakePostgREST) -> SupabaseStore:
    transport = httpx.MockTransport(backend.handle)
    client = httpx.Client(transport=transport, base_url="http://test")
    return SupabaseStore("http://test", "service-key", client=client, account_key="syaqir")


def _seed_candidate(store: SupabaseStore, backend: FakePostgREST, **over: Any) -> dict[str, Any]:
    row = {
        "id": backend._ids.get("threads_trend_candidates", 0) + 1,
        "target_account_id": "syaqir",
        "source_platform": "threads",
        "source_post_id": None,
        "source_username": "najmilatifnasrudin",
        "source_permalink": "https://www.threads.com/@najmilatifnasrudin/post/ABC123",
        "source_text": "Aku dah 10 tahun handle marketing SME Malaysia...",
        "status": "discovered",
        "raw_metadata": {"candidate_roles": ["external_trend"]},
        "topic": None,
        "trend_score": None,
        "adaptation_angle": None,
        "why_it_works": None,
        "used_in_queue_id": None,
    }
    row.update(over)
    backend._ids["threads_trend_candidates"] = row["id"]
    backend.tables["threads_trend_candidates"].append(row)
    return row


def _seed_own_reply(backend: FakePostgREST, **over: Any) -> dict[str, Any]:
    row = {
        "id": backend._ids.get("threads_own_reply_engagement", 0) + 1,
        "account_key": "syaqir",
        "reply_id": "111222333",
        "parent_post_id": "999888777",
        "parent_post_text": "Setup Facebook Ads ramai orang boleh.",
        "parent_post_permalink": "https://www.threads.com/@syaqir_sharani/post/XYZ",
        "from_username": "commenter_one",
        "reply_text": "Betul sangat tu bang",
        "reply_permalink": None,
        "replied_at": "2026-09-21T10:00:00+00:00",
        "proposed_text": None,
        "status": "discovered",
        "attempt_count": 0,
    }
    row.update(over)
    backend._ids["threads_own_reply_engagement"] = row["id"]
    backend.tables["threads_own_reply_engagement"].append(row)
    return row


# --------------------------------------------------------------------------
# Workflow A statuses / migration alignment
# --------------------------------------------------------------------------

def test_extended_trend_statuses_present():
    for status in (
        "discovered", "drafted", "pending_approval", "approved",
        "queued", "rejected", "skipped", "posted", "failed",
    ):
        assert status in TREND_STATUSES


# --------------------------------------------------------------------------
# Trend relevance filtering
# --------------------------------------------------------------------------

class FakeLLM:
    """Patches content_generation with scripted verdicts/drafts."""

    def __init__(self, verdict: dict[str, Any], draft: str = "draft post") -> None:
        self.verdict = verdict
        self.draft = draft

    def __enter__(self):
        self._p1 = patch.object(
            content_generation, "generate_json", return_value=dict(self.verdict)
        )
        self._p2 = patch.object(
            content_generation, "generate_text", return_value=self.draft
        )
        self._p1.start()
        self._p2.start()
        return self

    def __exit__(self, *exc):
        self._p1.stop()
        self._p2.stop()


def test_relevance_filter_rejects_unrelated():
    verdict = {"score": 0.1, "topic": "gossip", "reason": "tak relevan",
               "angle": "", "relevant": False}
    with FakeLLM(verdict):
        out = trend_engagement.score_candidate(
            {"source_text": "gosip artis terkini", "source_username": "x"},
            persona_text="persona",
            threshold=0.6,
        )
    assert out["relevant"] is False
    assert out["score"] == pytest.approx(0.1)


def test_relevance_filter_accepts_on_theme():
    verdict = {"score": 0.85, "topic": "digital marketing",
               "reason": "SME marketing angle ngam dengan account",
               "angle": "cerita pengalaman handle client F&B", "relevant": True}
    with FakeLLM(verdict):
        out = trend_engagement.score_candidate(
            {"source_text": "10 tahun handle marketing SME", "source_username": "najmi"},
            persona_text="persona",
            threshold=0.6,
        )
    assert out["relevant"] is True
    assert out["topic"] == "digital marketing"


def test_draft_respects_char_limit():
    long_draft = "x" * 600
    with patch.object(
        content_generation, "generate_text", side_effect=[long_draft, "pendek cukup"]
    ) as gen:
        draft = trend_engagement.generate_original_draft(
            {"source_text": "ref"}, {"angle": "a", "reason": "r"}, persona_text="p"
        )
    assert draft == "pendek cukup"
    assert gen.call_count == 2  # overage triggered one compression retry


def test_draft_fails_loud_when_still_over():
    with patch.object(
        content_generation, "generate_text", return_value="y" * 900
    ):
        with pytest.raises(ValueError, match="over 500"):
            trend_engagement.generate_original_draft(
                {"source_text": "ref"}, {"angle": "a", "reason": "r"}, persona_text="p"
            )


# --------------------------------------------------------------------------
# Trend dedup + state transitions (store level)
# --------------------------------------------------------------------------

def test_trend_transition_cas(store, backend):
    row = _seed_candidate(store, backend)
    moved = store.transition_trend_candidate(
        candidate_id=row["id"], from_status="discovered", to_status="pending_approval"
    )
    assert moved is not None and moved["status"] == "pending_approval"
    # second transition from the OLD state must not fire
    again = store.transition_trend_candidate(
        candidate_id=row["id"], from_status="discovered", to_status="pending_approval"
    )
    assert again is None


def test_trend_workflow_a_update_merges_raw_metadata(store, backend):
    row = _seed_candidate(store, backend)
    updated = store.update_trend_candidate_workflow_a(
        candidate_id=row["id"],
        fields={"status": "pending_approval",
                "raw_metadata": {"workflow_a": {"draft_text": "draf asli"}}},
    )
    assert updated is not None
    raw = updated["raw_metadata"]
    assert raw["candidate_roles"] == ["external_trend"]  # provenance preserved
    assert raw["workflow_a"]["draft_text"] == "draf asli"


def test_trend_approve_to_enqueue_idempotent(store, backend):
    row = _seed_candidate(store, backend, status="pending_approval",
                          raw_metadata={"workflow_a": {"draft_text": "draf siap"}},
                          topic="digital marketing")
    # emulate the CLI approve: CAS -> enqueue -> mark queued
    moved = store.transition_trend_candidate(
        candidate_id=row["id"], from_status="pending_approval", to_status="approved"
    )
    assert moved is not None
    q = store.enqueue_draft(
        "threads_publish_queue", "draf siap", reply_texts=[], topic="digital marketing"
    )
    store.update_trend_candidate_workflow_a(
        candidate_id=row["id"],
        fields={"status": "queued", "used_in_queue_id": int(q["id"])},
    )
    # double approve: used_in_queue_id set => no second queue row
    current = [r for r in backend.tables["threads_trend_candidates"]
               if r["id"] == row["id"]][0]
    assert current["used_in_queue_id"] == q["id"]
    assert len(backend.tables["threads_publish_queue"]) == 1
    # queue row preserved the topic
    assert backend.tables["threads_publish_queue"][0]["topic"] == "digital marketing"


def test_trend_reject_and_skip_do_not_enqueue(store, backend):
    r1 = _seed_candidate(store, backend, status="pending_approval")
    r2 = _seed_candidate(store, backend, status="pending_approval")
    store.transition_trend_candidate(
        candidate_id=r1["id"], from_status="pending_approval", to_status="rejected")
    store.transition_trend_candidate(
        candidate_id=r2["id"], from_status="pending_approval", to_status="skipped")
    assert len(backend.tables["threads_publish_queue"]) == 0


def test_workflow_a_allowlist_blocks_foreign_fields(store, backend):
    row = _seed_candidate(store, backend)
    with pytest.raises(ValueError, match="allowlist"):
        store.update_trend_candidate_workflow_a(
            candidate_id=row["id"], fields={"views": 99999}
        )


# --------------------------------------------------------------------------
# Workflow B: discovery, dedup, transitions
# --------------------------------------------------------------------------

def test_own_reply_upsert_dedupes(store, backend):
    reply = {"reply_id": "555", "parent_post_id": "777", "reply_text": "mantap"}
    first = store.upsert_own_reply(reply=reply)
    second = store.upsert_own_reply(reply=reply)
    assert first["id"] == second["id"]
    rows = [r for r in backend.tables["threads_own_reply_engagement"]]
    assert len(rows) == 1


def test_own_reply_discovery_skips_own_username(store, backend):
    class FakeAPI:
        def list_direct_replies(self, post_id):
            return [
                {"id": "c1", "text": "reply orang", "username": "stranger"},
                {"id": "c2", "text": "reply sendiri", "username": "syaqir_sharani"},
            ]

    new = own_replies.discover_new_replies(
        api=FakeAPI(),
        store=store,
        recent_posts=[{"id": "p1", "text": "post", "permalink": "u"}],
        own_username="syaqir_sharani",
    )
    assert len(new) == 1
    assert new[0]["from_username"] == "stranger"


def test_own_reply_approval_required_before_posting(store, backend):
    row = _seed_own_reply(backend, status="pending_approval", proposed_text="balas")
    # posting claim from pending_approval must fail — approval transition first
    claimed = store.transition_own_reply(
        row_id=row["id"], from_status="approved", to_status="posting"
    )
    assert claimed is None
    approved = store.transition_own_reply(
        row_id=row["id"], from_status=("discovered", "pending_approval"),
        to_status="approved", fields={"approved_at": "2026-09-21T11:00:00+00:00"},
    )
    assert approved is not None and approved["status"] == "approved"
    claimed = store.transition_own_reply(
        row_id=row["id"], from_status="approved", to_status="posting"
    )
    assert claimed is not None and claimed["status"] == "posting"


def test_own_reply_reject_and_ignore_terminal(store, backend):
    r1 = _seed_own_reply(backend, reply_id="a1", status="pending_approval",
                         proposed_text="x")
    r2 = _seed_own_reply(backend, reply_id="a2", status="pending_approval",
                         proposed_text="y")
    rej = store.transition_own_reply(
        row_id=r1["id"], from_status=("discovered", "pending_approval"),
        to_status="rejected")
    ign = store.transition_own_reply(
        row_id=r2["id"], from_status=("discovered", "pending_approval"),
        to_status="ignored")
    assert rej["status"] == "rejected" and ign["status"] == "ignored"
    # rejected/ignored rows can never re-enter the approval path
    assert store.transition_own_reply(
        row_id=r1["id"], from_status=("discovered", "pending_approval"),
        to_status="approved") is None
    assert store.transition_own_reply(
        row_id=r2["id"], from_status=("discovered", "pending_approval"),
        to_status="approved") is None


def test_own_reply_edit_only_when_pending(store, backend):
    row = _seed_own_reply(backend, status="discovered")
    edited = store.transition_own_reply(
        row_id=row["id"], from_status="pending_approval",
        to_status="pending_approval", fields={"proposed_text": "edited"})
    assert edited is None
    store.transition_own_reply(
        row_id=row["id"], from_status="discovered", to_status="pending_approval",
        fields={"proposed_text": "asal"})
    edited = store.transition_own_reply(
        row_id=row["id"], from_status="pending_approval",
        to_status="pending_approval", fields={"proposed_text": "edited"})
    assert edited is not None and edited["proposed_text"] == "edited"


def test_transient_vs_permanent_classification():
    assert own_replies.is_transient_error("HTTP 503 Service Unavailable") is True
    assert own_replies.is_transient_error("rate limit exceeded") is True
    assert own_replies.is_transient_error("connection reset") is True
    assert own_replies.is_transient_error("OAuthException: Invalid token") is False
    assert own_replies.is_transient_error("permission denied") is False


def test_own_reply_failed_terminal_after_permanent(store, backend):
    row = _seed_own_reply(backend, status="approved", proposed_text="balas",
                          attempt_count=1)
    claimed = store.transition_own_reply(
        row_id=row["id"], from_status="approved", to_status="posting")
    assert claimed is not None
    failed = store.transition_own_reply(
        row_id=row["id"], from_status="posting", to_status="failed",
        fields={"last_error": "OAuthException"})
    assert failed["status"] == "failed"


# --------------------------------------------------------------------------
# Multi-account persona isolation
# --------------------------------------------------------------------------

def test_persona_loading_refuses_cross_account(tmp_path):
    (tmp_path / "syaqir.md").write_text("persona syaqir", encoding="utf-8")
    persona = trend_engagement.load_persona(tmp_path, "syaqir")
    assert persona == "persona syaqir"
    with pytest.raises(trend_engagement.PersonaMissingError):
        trend_engagement.load_persona(tmp_path, "account_lain")


# --------------------------------------------------------------------------
# Provider resolution: no hardcoded provider/model
# --------------------------------------------------------------------------

def test_provider_resolution_env_override(monkeypatch):
    monkeypatch.setenv("THREADS_OPERATOR_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("THREADS_OPERATOR_LLM_API_KEY", "k")
    monkeypatch.setenv("THREADS_OPERATOR_LLM_MODEL", "m1")
    providers = content_generation.resolve_providers()
    assert len(providers) == 1
    assert providers[0].name == "env-override"
    assert providers[0].model == "m1"


def test_provider_resolution_from_hermes_config(tmp_path, monkeypatch):
    monkeypatch.delenv("THREADS_OPERATOR_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("THREADS_OPERATOR_LLM_API_KEY", raising=False)
    monkeypatch.delenv("THREADS_OPERATOR_LLM_MODEL", raising=False)
    monkeypatch.setenv("SOME_GATEWAY_KEY", "secret-key")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "model:\n"
        "  provider: custom\n"
        "  default: kimi-k3\n"
        "  base_url: https://gw.example/v1\n"
        "  api_key: ${SOME_GATEWAY_KEY}\n"
        "fallback_providers:\n"
        "  - provider: custom\n"
        "    model: backup-1\n"
        "    base_url: https://gw2.example/v1\n"
        "    api_key: ${SOME_GATEWAY_KEY}\n",
        encoding="utf-8",
    )
    providers = content_generation.resolve_providers(config_path=cfg)
    names = [p.name for p in providers]
    assert names == ["primary", "fallback-0"]
    assert providers[0].model == "kimi-k3"
    assert providers[0].api_key == "secret-key"
    assert providers[1].model == "backup-1"


def test_generate_text_falls_through_providers():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "bad":
            return httpx.Response(500, json={"error": "down"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hasil"}}]},
        )

    specs = [
        content_generation.LLMProviderSpec("bad", "http://bad/v1", "k", "m"),
        content_generation.LLMProviderSpec("good", "http://good/v1", "k", "m"),
    ]
    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = content_generation.generate_text(
        system_prompt="s", user_prompt="u", providers=specs, client=client
    )
    assert out == "hasil"
    assert calls == ["bad", "good"]
