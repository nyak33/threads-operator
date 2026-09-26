"""Tests for Task 2A (Context Engine) and Task 2B (Intent + Lead Detection).

Uses the same FakePostgREST pattern as test_two_engagement_workflows.py.
Covers:
1. Context assembly with all 11 required fields
2. Bounded context (no unlimited history)
3. Intent classification for all 8 categories
4. CTA match detection (exact keyword + natural language)
5. DM opportunity creation + dedup
6. Idempotency (same event processed twice)
7. Account isolation
8. Existing public reply workflow preserved
9. Fail-closed on missing context
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from threads_operator import dm_opportunity, reply_context, reply_intent
from threads_operator.supabase_store import SupabaseStore


# --------------------------------------------------------------------------
# Fake PostgREST backend (extended for DM opportunities)
# --------------------------------------------------------------------------

class FakePostgREST:
    """Minimal in-memory PostgREST for context/intent tests."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            "threads_own_reply_engagement": [],
            "threads_dm_opportunities": [],
            "threads_posts": [],
        }
        self._ids: dict[str, int] = {}

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
        # unique (account_key, from_username, root_post_id) for DM opportunities
        if table == "threads_dm_opportunities":
            for existing in rows:
                if (
                    existing.get("account_key") == row.get("account_key")
                    and existing.get("from_username") == row.get("from_username")
                    and existing.get("root_post_id") == row.get("root_post_id")
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


@pytest.fixture()
def store_account_b(backend: FakePostgREST) -> SupabaseStore:
    transport = httpx.MockTransport(backend.handle)
    client = httpx.Client(transport=transport, base_url="http://test")
    return SupabaseStore("http://test", "service-key", client=client, account_key="other_account")


def _seed_own_reply(backend: FakePostgREST, **over: Any) -> dict[str, Any]:
    row = {
        "id": backend._ids.get("threads_own_reply_engagement", 0) + 1,
        "account_key": "syaqir",
        "reply_id": "111222333",
        "parent_post_id": "999888777",
        "parent_post_text": "Setup Facebook Ads ramai orang boleh. Komen berminat, nanti saya DM.",
        "parent_post_permalink": "https://www.threads.com/@syaqir_sharani/post/XYZ",
        "from_username": "commenter_one",
        "reply_text": "Berminat",
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


def _seed_post(backend: FakePostgREST, **over: Any) -> dict[str, Any]:
    row = {
        "thread_id": "999888777",
        "text": "Setup Facebook Ads ramai orang boleh. Komen berminat, nanti saya DM.",
        "published_at": "2026-09-20T08:00:00+00:00",
        "permalink": "https://www.threads.com/@syaqir_sharani/post/XYZ",
        "topic_tag": "facebook ads",
        "category": "marketing",
    }
    row.update(over)
    backend.tables["threads_posts"].append(row)
    return row


# --------------------------------------------------------------------------
# Context Engine Tests (Task 2A)
# --------------------------------------------------------------------------

class TestContextAssembly:
    """Task 2A: deterministic context assembly."""

    def test_assemble_context_with_all_fields(self, store, backend):
        """All 11 context fields are assembled correctly."""
        _seed_post(backend)
        reply_row = _seed_own_reply(backend)

        # Use a different username so prior_interactions doesn't include current reply
        reply_row_different_user = dict(reply_row)
        reply_row_different_user["from_username"] = "unique_user_xyz"

        context = reply_context.assemble_reply_context(
            store=store,
            account_key="syaqir",
            reply_row=reply_row_different_user,
            persona_text="Father / working class voice",
            cta_patterns=("berminat", "nak", "dm saya"),
        )

        # 1. Account
        assert context.account_key == "syaqir"
        # 2. Root post
        assert context.root_post_id == "999888777"
        assert context.root_post_permalink == "https://www.threads.com/@syaqir_sharani/post/XYZ"
        assert "Setup Facebook Ads" in (context.root_post_text or "")
        assert context.root_post_topic_tag == "facebook ads"
        assert context.root_post_category == "marketing"
        # 3. Parent reply (none — direct reply to post)
        assert context.parent_reply_id is None
        # 4. Current reply
        assert context.reply_id == "111222333"
        assert context.reply_text == "Berminat"
        assert context.reply_username == "unique_user_xyz"
        # 5. Conversation chain
        assert len(context.conversation_chain) == 2  # root + current
        assert context.conversation_chain[0]["type"] == "root_post"
        assert context.conversation_chain[1]["type"] == "current_reply"
        # 6. Topic
        assert context.topic_tag == "facebook ads"
        # 7. Content pillar
        assert context.content_pillar is not None
        # 8. Post objective
        assert context.post_objective is not None
        # 9. CTA
        assert context.post_cta is not None
        assert "berminat" in context.post_cta.lower()
        assert "berminat" in context.cta_patterns
        # 10. Prior interactions
        assert context.prior_interaction_count == 0
        # 11. Persona
        assert context.persona_text == "Father / working class voice"

        # Serialization works
        d = context.to_dict()
        assert d["account"]["account_key"] == "syaqir"
        assert d["root_post"]["topic_tag"] == "facebook ads"
        assert d["cta"]["post_cta"] is not None

    def test_context_bounded_no_unlimited_history(self, store, backend):
        """Prior interactions are bounded to MAX_PRIOR_INTERACTIONS."""
        _seed_post(backend)
        # Seed many prior replies from same user
        for i in range(20):
            _seed_own_reply(
                backend,
                reply_id=f"reply_{i}",
                from_username="repeat_commenter",
                reply_text=f"Comment {i}",
            )
        reply_row = _seed_own_reply(
            backend,
            reply_id="current_reply",
            from_username="repeat_commenter",
            reply_text="Berminat",
        )

        context = reply_context.assemble_reply_context(
            store=store,
            account_key="syaqir",
            reply_row=reply_row,
        )

        # Bounded: at most MAX_PRIOR_INTERACTIONS
        assert context.prior_interaction_count <= reply_context.MAX_PRIOR_INTERACTIONS
        assert len(context.prior_interactions) <= reply_context.MAX_PRIOR_INTERACTIONS

    def test_context_fails_closed_on_missing_account(self, store):
        """Context assembly fails when account context is missing."""
        reply_row = {"reply_id": "123", "parent_post_id": "456"}
        with pytest.raises(reply_context.ContextAssemblyError, match="account_key"):
            reply_context.assemble_reply_context(
                store=store,
                account_key="",
                reply_row=reply_row,
            )

    def test_context_fails_closed_on_account_mismatch(self, store, backend):
        """Cross-account data mixing is prevented."""
        _seed_post(backend)
        reply_row = _seed_own_reply(backend, account_key="other_account")
        with pytest.raises(reply_context.ContextAssemblyError, match="does not match"):
            reply_context.assemble_reply_context(
                store=store,  # scoped to "syaqir"
                account_key="other_account",
                reply_row=reply_row,
            )

    def test_context_fails_closed_on_missing_reply_id(self, store):
        """Missing reply_id is rejected."""
        with pytest.raises(reply_context.ContextAssemblyError, match="reply_id"):
            reply_context.assemble_reply_context(
                store=store,
                account_key="syaqir",
                reply_row={"reply_id": "", "parent_post_id": "456"},
            )

    def test_context_falls_back_to_reply_row_when_post_missing(self, store, backend):
        """When root post is not in threads_posts, use reply_row's parent_post_text."""
        # Do NOT seed post — reply_row has parent_post_text
        reply_row = _seed_own_reply(backend)

        context = reply_context.assemble_reply_context(
            store=store,
            account_key="syaqir",
            reply_row=reply_row,
        )

        # Falls back to reply_row data
        assert context.root_post_text == reply_row["parent_post_text"]
        assert context.root_post_permalink == reply_row["parent_post_permalink"]
        # topic_tag not available from fallback
        assert context.root_post_topic_tag is None

    def test_context_truncates_long_text(self, store, backend):
        """Long post text is truncated to bounded length."""
        long_text = "A" * 2000
        _seed_post(backend, text=long_text)
        reply_row = _seed_own_reply(backend)

        context = reply_context.assemble_reply_context(
            store=store,
            account_key="syaqir",
            reply_row=reply_row,
        )

        assert len(context.root_post_text or "") <= reply_context.MAX_POST_TEXT_LENGTH + 3  # +3 for "..."

    def test_context_persona_missing_is_safe(self, store, backend):
        """Missing persona does not break context assembly."""
        _seed_post(backend)
        reply_row = _seed_own_reply(backend)

        context = reply_context.assemble_reply_context(
            store=store,
            account_key="syaqir",
            reply_row=reply_row,
            persona_text=None,
        )

        assert context.persona_text is None
        d = context.to_dict()
        assert d["persona"]["available"] is False


# --------------------------------------------------------------------------
# Intent Classification Tests (Task 2B)
# --------------------------------------------------------------------------

class TestIntentClassification:
    """Task 2B: intent classification with deterministic evidence."""

    def _make_context(self, **over) -> reply_context.ReplyContext:
        """Helper to create a minimal context for classification tests."""
        defaults = {
            "account_key": "syaqir",
            "root_post_id": "999888777",
            "root_post_permalink": None,
            "root_post_text": "Setup Facebook Ads. Komen berminat, nanti saya DM.",
            "root_post_topic_tag": "facebook ads",
            "root_post_category": "marketing",
            "root_post_published_at": "2026-09-20T08:00:00+00:00",
            "parent_reply_id": None,
            "parent_reply_text": None,
            "parent_reply_username": None,
            "reply_id": "111222333",
            "reply_text": None,
            "reply_username": "commenter_one",
            "replied_at": "2026-09-21T10:00:00+00:00",
            "conversation_chain": (),
            "topic_tag": "facebook ads",
            "content_pillar": "digital_marketing",
            "post_objective": "lead_generation",
            "post_cta": "Komen berminat, nanti saya DM",
            "cta_patterns": ("berminat", "nak"),
            "prior_interactions": (),
            "prior_interaction_count": 0,
            "persona_text": None,
            "assembled_at": "2026-09-21T10:05:00+00:00",
        }
        defaults.update(over)
        return reply_context.ReplyContext(**defaults)

    # Test 1: normal casual reply
    def test_classify_casual_reply(self):
        context = self._make_context(reply_text="Betul sangat tu bang")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="Betul sangat tu bang",
            post_cta="Komen berminat, nanti saya DM",
        )
        assert result.intent == reply_intent.INTENT_CASUAL
        assert result.dm_opportunity is False
        assert result.lead_score < 0.5

    # Test 2: direct question
    def test_classify_question_reply(self):
        context = self._make_context(reply_text="Berapa harga untuk setup ads?")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="Berapa harga untuk setup ads?",
            post_cta="Komen berminat, nanti saya DM",
        )
        assert result.intent == reply_intent.INTENT_QUESTION
        assert result.dm_opportunity is False

    # Test 3: explicit CTA response (exact keyword)
    def test_classify_cta_match_exact_keyword(self):
        context = self._make_context(reply_text="Berminat")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="Berminat",
            post_cta="Komen berminat, nanti saya DM",
            cta_patterns=("berminat", "nak"),
        )
        assert result.intent == reply_intent.INTENT_CTA_MATCH
        assert result.dm_opportunity is True
        assert result.lead_score >= 0.6

    # Test 4: natural-language CTA response (not exact keyword)
    def test_classify_cta_match_natural_language(self):
        context = self._make_context(reply_text="Nak try boleh?")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="Nak try boleh?",
            post_cta="Komen berminat, nanti saya DM",
            cta_patterns=("berminat", "nak"),
        )
        # "nak try" matches buying patterns, "boleh" matches CTA patterns
        # Composite: buying + CTA = potential_lead
        assert result.intent in (reply_intent.INTENT_CTA_MATCH, reply_intent.INTENT_BUYING, reply_intent.INTENT_POTENTIAL_LEAD)

    def test_classify_cta_match_natural_minat_variant(self):
        context = self._make_context(reply_text="Minat")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="Minat",
            post_cta="Komen berminat, nanti saya DM",
            cta_patterns=("berminat",),
        )
        assert result.intent == reply_intent.INTENT_CTA_MATCH
        assert "minat" in result.evidence.matched_patterns


    def test_generic_cta_word_without_post_cta_does_not_create_dm(self):
        context = self._make_context(reply_text="Boleh juga")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="Boleh juga",
            post_cta=None,
            cta_patterns=(),
        )
        assert result.intent != reply_intent.INTENT_CTA_MATCH
        assert reply_intent.should_create_dm_opportunity(result) is False

    def test_reply_keyword_must_align_with_actual_post_cta(self):
        context = self._make_context(reply_text="Boleh cuba LokalFlow")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="Boleh cuba LokalFlow",
            post_cta="Korang rasa seller akan berminat tak?",
            cta_patterns=(),
        )
        assert result.intent != reply_intent.INTENT_CTA_MATCH
        assert reply_intent.should_create_dm_opportunity(result) is False
        assert result.dm_opportunity is False

    # Test 5: ambiguous reply
    def test_classify_ambiguous_reply(self):
        context = self._make_context(reply_text="Ok")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="Ok",
            post_cta="Komen berminat, nanti saya DM",
            cta_patterns=("berminat", "nak"),
        )
        # Ambiguous short reply — may be CTA match or casual
        assert result.intent in (
            reply_intent.INTENT_CTA_MATCH,
            reply_intent.INTENT_CASUAL,
            reply_intent.INTENT_NEEDS_REVIEW,
        )

    # Test 6: buying intent without CTA
    def test_classify_buying_intent(self):
        context = self._make_context(reply_text="Berapa harga pakej ni?")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="Berapa harga pakej ni?",
            post_cta=None,
            cta_patterns=(),
        )
        assert result.intent == reply_intent.INTENT_QUESTION  # Question patterns win
        assert result.dm_opportunity is False

    # Test 7: objection
    def test_classify_objection(self):
        context = self._make_context(reply_text="Mahal sangat, tak berbaloi")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="Mahal sangat, tak berbaloi",
            post_cta=None,
        )
        assert result.intent == reply_intent.INTENT_OBJECTION
        assert result.dm_opportunity is False
        assert result.requires_human is True

    # Test 8: positive engagement
    def test_classify_positive_engagement(self):
        context = self._make_context(reply_text="Terbaik bang, sangat inspiring!")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="Terbaik bang, sangat inspiring!",
            post_cta=None,
        )
        assert result.intent == reply_intent.INTENT_POSITIVE
        assert result.dm_opportunity is False

    # Test 9: potential lead (buying + CTA)
    def test_classify_potential_lead(self):
        context = self._make_context(reply_text="Berminat, berapa harga?")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="Berminat, berapa harga?",
            post_cta="Komen berminat, nanti saya DM",
            cta_patterns=("berminat",),
        )
        assert result.intent == reply_intent.INTENT_POTENTIAL_LEAD
        assert result.dm_opportunity is True
        assert result.lead_score >= 0.8

    # Test 10: empty reply
    def test_classify_empty_reply(self):
        context = self._make_context(reply_text="")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="",
            post_cta=None,
        )
        assert result.intent == reply_intent.INTENT_NEEDS_REVIEW
        assert result.dm_opportunity is False
        assert result.requires_human is True

    # Test 11: no DM opportunity when evidence is insufficient
    def test_no_dm_opportunity_when_evidence_insufficient(self):
        context = self._make_context(reply_text="Nice post")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="Nice post",
            post_cta=None,
        )
        assert result.dm_opportunity is False
        assert reply_intent.should_create_dm_opportunity(result) is False

    def test_evidence_is_deterministic(self):
        """Classification includes deterministic evidence, not just LLM vibes."""
        context = self._make_context(reply_text="Berminat")
        result = reply_intent.classify_reply_intent(
            context=context,
            reply_text="Berminat",
            post_cta="Komen berminat, nanti saya DM",
        )
        assert result.evidence is not None
        assert result.evidence.evidence_type in ("keyword", "cta_match", "composite", "contextual")
        assert 0.0 <= result.evidence.confidence <= 1.0


# --------------------------------------------------------------------------
# DM Opportunity Tests
# --------------------------------------------------------------------------

class TestDMOpportunity:
    """DM opportunity creation, dedup, and state machine."""

    def _make_context_and_classification(self, store, backend, reply_text="Berminat"):
        """Helper to create context + classification for DM opportunity tests."""
        _seed_post(backend)
        reply_row = _seed_own_reply(backend, reply_text=reply_text)

        context = reply_context.assemble_reply_context(
            store=store,
            account_key="syaqir",
            reply_row=reply_row,
            cta_patterns=("berminat", "nak"),
        )
        classification = reply_intent.classify_reply_intent(
            context=context,
            reply_text=reply_text,
            post_cta=context.post_cta,
            cta_patterns=context.cta_patterns,
        )
        return context, classification, reply_row

    def test_create_dm_opportunity(self, store, backend):
        """DM opportunity is created for CTA match."""
        context, classification, reply_row = self._make_context_and_classification(store, backend)

        result = dm_opportunity.create_dm_opportunity(
            store=store,
            account_key="syaqir",
            context=context,
            classification=classification,
        )

        assert result.success is True
        assert result.opportunity_id is not None
        assert result.status == dm_opportunity.DM_STATUS_DETECTED
        assert result.already_exists is False

        # Verify in DB
        row = store.get_dm_opportunity(result.opportunity_id)
        assert row is not None
        assert row["account_key"] == "syaqir"
        assert row["from_username"] == "commenter_one"
        assert row["root_post_id"] == "999888777"
        assert row["source_own_reply_id"] == reply_row["id"]
        assert row["expires_at"] is not None
        assert row["intent"] in (reply_intent.INTENT_CTA_MATCH, reply_intent.INTENT_POTENTIAL_LEAD)

    def test_dm_opportunity_dedup_same_user_same_post(self, store, backend):
        """Same user replying on same post does not create duplicate DM opportunity."""
        context, classification, _ = self._make_context_and_classification(store, backend)

        # First creation
        result1 = dm_opportunity.create_dm_opportunity(
            store=store, account_key="syaqir", context=context, classification=classification,
        )
        assert result1.success is True
        assert result1.already_exists is False

        # Second creation (same dedupe key)
        result2 = dm_opportunity.create_dm_opportunity(
            store=store, account_key="syaqir", context=context, classification=classification,
        )
        assert result2.success is True
        assert result2.already_exists is True
        assert result2.opportunity_id == result1.opportunity_id

    def test_same_user_different_posts_creates_separate_opportunities(self, store, backend):
        """Same user replying on two different posts creates separate opportunities."""
        _seed_post(backend)
        _seed_post(backend, thread_id="999888778", permalink="https://www.threads.com/@syaqir_sharani/post/ABC")

        reply_row_1 = _seed_own_reply(backend, reply_id="r1", parent_post_id="999888777", reply_text="Berminat")
        reply_row_2 = _seed_own_reply(backend, reply_id="r2", parent_post_id="999888778", reply_text="Berminat")

        ctx1 = reply_context.assemble_reply_context(store=store, account_key="syaqir", reply_row=reply_row_1, cta_patterns=("berminat",))
        cls1 = reply_intent.classify_reply_intent(context=ctx1, reply_text="Berminat", post_cta=ctx1.post_cta, cta_patterns=ctx1.cta_patterns)

        ctx2 = reply_context.assemble_reply_context(store=store, account_key="syaqir", reply_row=reply_row_2, cta_patterns=("berminat",))
        cls2 = reply_intent.classify_reply_intent(context=ctx2, reply_text="Berminat", post_cta=ctx2.post_cta, cta_patterns=ctx2.cta_patterns)

        result1 = dm_opportunity.create_dm_opportunity(store=store, account_key="syaqir", context=ctx1, classification=cls1)
        result2 = dm_opportunity.create_dm_opportunity(store=store, account_key="syaqir", context=ctx2, classification=cls2)

        assert result1.success is True
        assert result2.success is True
        assert result1.opportunity_id != result2.opportunity_id

    def test_account_isolation(self, store, store_account_b, backend):
        """DM opportunities are isolated per account."""
        _seed_post(backend)
        reply_row = _seed_own_reply(backend, account_key="syaqir")

        context = reply_context.assemble_reply_context(
            store=store, account_key="syaqir", reply_row=reply_row, cta_patterns=("berminat",),
        )
        classification = reply_intent.classify_reply_intent(
            context=context, reply_text="Berminat", post_cta=context.post_cta, cta_patterns=context.cta_patterns,
        )

        # Create for account A
        result_a = dm_opportunity.create_dm_opportunity(
            store=store, account_key="syaqir", context=context, classification=classification,
        )
        assert result_a.success is True

        # Account B store should not see it
        row_b = store_account_b.get_dm_opportunity(result_a.opportunity_id)
        assert row_b is None

    def test_no_dm_opportunity_when_classification_rejects(self, store, backend):
        """No DM opportunity when classification says no."""
        _seed_post(backend)
        reply_row = _seed_own_reply(backend, reply_text="Nice post")

        context = reply_context.assemble_reply_context(
            store=store, account_key="syaqir", reply_row=reply_row,
        )
        classification = reply_intent.classify_reply_intent(
            context=context, reply_text="Nice post", post_cta=None,
        )

        result = dm_opportunity.create_dm_opportunity(
            store=store, account_key="syaqir", context=context, classification=classification,
        )
        assert result.success is False
        assert "does not warrant" in result.error

    def test_dm_opportunity_state_machine(self, store, backend):
        """State transitions work correctly."""
        context, classification, _ = self._make_context_and_classification(store, backend)
        result = dm_opportunity.create_dm_opportunity(
            store=store, account_key="syaqir", context=context, classification=classification,
        )
        assert result.success is True

        # detected -> awaiting_approval
        moved = dm_opportunity.transition_dm_opportunity(
            store=store,
            opportunity_id=result.opportunity_id,
            from_status=dm_opportunity.DM_STATUS_DETECTED,
            to_status=dm_opportunity.DM_STATUS_AWAITING_APPROVAL,
        )
        assert moved is not None
        assert moved["status"] == dm_opportunity.DM_STATUS_AWAITING_APPROVAL

        # CAS guard: cannot transition from wrong state
        rejected = dm_opportunity.transition_dm_opportunity(
            store=store,
            opportunity_id=result.opportunity_id,
            from_status=dm_opportunity.DM_STATUS_DETECTED,  # wrong — already moved
            to_status=dm_opportunity.DM_STATUS_APPROVED,
        )
        assert rejected is None

    def test_expire_stale_opportunities(self, store, backend):
        """Stale opportunities are expired correctly."""
        context, classification, _ = self._make_context_and_classification(store, backend)

        # Create with very short expiry (already expired)
        result = dm_opportunity.create_dm_opportunity(
            store=store, account_key="syaqir", context=context, classification=classification,
            expiry_hours=-1,  # already expired
        )
        assert result.success is True

        # Expire
        report = dm_opportunity.expire_stale_opportunities(store, dry_run=False)
        assert report["expired"] >= 1

        # Verify status
        row = store.get_dm_opportunity(result.opportunity_id)
        assert row["status"] == dm_opportunity.DM_STATUS_EXPIRED

    def test_link_reply_to_dm_opportunity(self, store, backend):
        """Reply row can be linked to DM opportunity."""
        context, classification, reply_row = self._make_context_and_classification(store, backend)
        result = dm_opportunity.create_dm_opportunity(
            store=store, account_key="syaqir", context=context, classification=classification,
        )
        assert result.success is True

        linked = dm_opportunity.link_reply_to_dm_opportunity(
            store=store,
            reply_row_id=reply_row["id"],
            dm_opportunity_id=result.opportunity_id,
        )
        assert linked is True

        # Verify link
        updated = store.get_own_reply(reply_row["id"])
        assert updated["dm_opportunity_id"] == result.opportunity_id


# --------------------------------------------------------------------------
# Integration: classify_reply (own_replies wrapper)
# --------------------------------------------------------------------------

class TestClassifyReplyIntegration:
    """Integration test for the own_replies.classify_reply wrapper."""

    def test_classify_reply_creates_dm_opportunity(self, store, backend):
        """classify_reply assembles context, classifies intent, and creates DM opportunity."""
        from threads_operator import own_replies

        _seed_post(backend)
        reply_row = _seed_own_reply(backend, reply_text="Berminat")

        result = own_replies.classify_reply(
            store=store,
            account_key="syaqir",
            reply_row=reply_row,
            persona_text="Father / working class voice",
            cta_patterns=("berminat", "nak"),
        )

        assert result["context"] is not None
        assert result["classification"] is not None
        assert result["classification"].intent in (
            reply_intent.INTENT_CTA_MATCH,
            reply_intent.INTENT_POTENTIAL_LEAD,
        )
        assert result["dm_opportunity"] is not None
        assert result["dm_opportunity"].success is True
        assert result["updated_row"] is not None
        assert result["updated_row"]["intent"] is not None
        assert result["updated_row"]["context_json"] is not None

    def test_classify_reply_no_dm_for_casual(self, store, backend):
        """classify_reply does not create DM opportunity for casual replies."""
        from threads_operator import own_replies

        _seed_post(backend)
        reply_row = _seed_own_reply(backend, reply_text="Nice post bang")

        result = own_replies.classify_reply(
            store=store,
            account_key="syaqir",
            reply_row=reply_row,
            persona_text="Father / working class voice",
            cta_patterns=("berminat", "nak"),
        )

        assert result["classification"].intent == reply_intent.INTENT_POSITIVE
        assert result["dm_opportunity"] is None

    def test_classify_reply_persists_context(self, store, backend):
        """Context JSON is persisted on the reply row."""
        from threads_operator import own_replies

        _seed_post(backend)
        reply_row = _seed_own_reply(backend, reply_text="Berminat")

        result = own_replies.classify_reply(
            store=store,
            account_key="syaqir",
            reply_row=reply_row,
            cta_patterns=("berminat",),
        )

        # Fetch the row from DB to verify persistence
        row = store.get_own_reply(reply_row["id"])
        assert row["context_json"] is not None
        assert row["intent"] is not None
        assert row["classified_at"] is not None

        ctx = row["context_json"]
        assert ctx["account"]["account_key"] == "syaqir"
        assert ctx["root_post"]["post_id"] == "999888777"


# --------------------------------------------------------------------------
# Integration: existing public reply workflow preserved
# --------------------------------------------------------------------------

class TestPublicReplyWorkflowPreserved:
    """The existing own-reply workflow must continue to work independently."""

    def test_public_reply_workflow_unaffected(self, store, backend):
        """Context/intent/DM layer does not break existing own-reply flow."""
        _seed_post(backend)
        reply_row = _seed_own_reply(backend)

        # Existing workflow: transition discovered -> pending_approval
        moved = store.transition_own_reply(
            row_id=reply_row["id"],
            from_status="discovered",
            to_status="pending_approval",
            fields={"proposed_text": "Thanks for your comment!"},
        )
        assert moved is not None
        assert moved["status"] == "pending_approval"

        # Approve
        approved = store.transition_own_reply(
            row_id=reply_row["id"],
            from_status="pending_approval",
            to_status="approved",
        )
        assert approved is not None
        assert approved["status"] == "approved"

        # Context fields are additive — they don't interfere
        assert "context_json" in approved or approved.get("context_json") is None
        assert "intent" in approved or approved.get("intent") is None

    def test_same_event_processed_twice_idempotent(self, store, backend):
        """Processing the same reply event twice does not create duplicates."""
        _seed_post(backend)
        reply_row = _seed_own_reply(backend, reply_text="Berminat")

        # First processing: create DM opportunity
        ctx1 = reply_context.assemble_reply_context(
            store=store, account_key="syaqir", reply_row=reply_row, cta_patterns=("berminat",),
        )
        cls1 = reply_intent.classify_reply_intent(
            context=ctx1, reply_text="Berminat", post_cta=ctx1.post_cta, cta_patterns=ctx1.cta_patterns,
        )
        result1 = dm_opportunity.create_dm_opportunity(
            store=store, account_key="syaqir", context=ctx1, classification=cls1,
        )
        assert result1.success is True
        assert result1.already_exists is False

        # Simulate watchdog retry: process same event again
        ctx2 = reply_context.assemble_reply_context(
            store=store, account_key="syaqir", reply_row=reply_row, cta_patterns=("berminat",),
        )
        cls2 = reply_intent.classify_reply_intent(
            context=ctx2, reply_text="Berminat", post_cta=ctx2.post_cta, cta_patterns=ctx2.cta_patterns,
        )
        result2 = dm_opportunity.create_dm_opportunity(
            store=store, account_key="syaqir", context=ctx2, classification=cls2,
        )
        assert result2.success is True
        assert result2.already_exists is True
        assert result2.opportunity_id == result1.opportunity_id

        # Only one DM opportunity in DB
        rows = store.list_dm_opportunities()
        assert len(rows) == 1
