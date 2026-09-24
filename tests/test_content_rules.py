import httpx
import pytest

from threads_operator.content_rules import (
    RULES_LAST_VERIFIED,
    STANDARD_POST_MAX_CHARS,
    TEXT_ATTACHMENT_MAX_CHARS,
    validate_standard_post_text,
    validate_thread_texts,
)
from threads_operator.supabase_store import SupabaseStore


def test_standard_threads_limit_allows_exactly_500_characters():
    validate_standard_post_text("x" * STANDARD_POST_MAX_CHARS)


def test_standard_threads_limit_rejects_501_characters():
    with pytest.raises(ValueError, match=r"501/500 characters"):
        validate_standard_post_text("x" * (STANDARD_POST_MAX_CHARS + 1))


def test_thread_validation_identifies_overlong_reply():
    with pytest.raises(ValueError, match=r"reply 2 exceeds"):
        validate_thread_texts("root", ["ok", "x" * 501])


def test_enqueue_draft_rejects_overlong_reply_before_supabase_write():
    def handler(request):
        raise AssertionError("Supabase must not be called for invalid content")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = SupabaseStore(
        "https://example.supabase.co",
        "secret",
        account_key="acct",
        client=client,
    )

    with pytest.raises(ValueError, match=r"reply 1 exceeds"):
        store.enqueue_draft("threads_publish_queue", "root", ["x" * 501])


def test_documented_separate_text_attachment_limit_and_verification_date():
    assert TEXT_ATTACHMENT_MAX_CHARS == 10_000
    assert RULES_LAST_VERIFIED == "2026-09-24"
