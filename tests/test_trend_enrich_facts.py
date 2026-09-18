"""RED tests (v2): broad factual evidence extraction from the exact post node.

Spec sections 4-7: the extractor must return ONLY observed factual fields
allowlisted for the evidence UPDATE:
  source_post_id, source_username, source_text, published_at,
  likes, replies, reposts, quotes, media_type, self_thread_length
Counters present as 0 are real observations and must be kept.
Absent fields must be absent from the result (never null) so a later
payload omission can never erase previously known facts.
view_count noise must NEVER surface as views.
Sensitive relay tokens (__token, organic_tracking_token, logging_info_token)
must never appear anywhere in the returned evidence.
published_at must be a UTC ISO-8601 string converted from taken_at.
"""

import pytest

from threads_operator.trend_enrich_extract import (
    TrendEnrichError,
    extract_post_facts,
)

SHORTCODE = "DdGAw6kFfPA"
OTHER_SHORTCODE = "AbCdEf12345"

TOKEN_SENTINELS = ("tok-secret-1", "org-secret-2", "log-secret-3")


def _bbox(result_json: str) -> str:
    return (
        '{"__bbox":{"status":"success","message":"","exception":null,'
        f'"result":{result_json}}}'
    )
def _thread_doc(post_json: str, *, length: int = 1) -> str:
    thread = (
        '{"id":"thread-1","thread_type":"self","thread_items":['
        + ",".join('{"post":' + post_json + "}" for _ in range(length))
        + "]}"
    )
    return _bbox(
        '{"data":{"feedData":{"edges":[{"node":{"text_post_app_thread":'
        + thread
        + "}}]}}}"
    )


def _post(**overrides) -> str:
    import json as _json

    node = {
        "pk": "1788651234567890",
        "code": SHORTCODE,
        "taken_at": 1757350000,
        "text": "kita ni belakang kira",
        "user": {"pk": "68701187603", "username": "syaqir_sharani"},
        "like_count": 9,
        "text_post_app_info": {
            "direct_reply_count": 1,
            "repost_count": 1,
            "quote_count": 0,
        },
        "media_type": 19,
    }
    node.update(overrides)
    return _json.dumps(node)


# ------------------------------------------------------------- fact mapping

def test_full_fact_extraction():
    facts = extract_post_facts(_thread_doc(_post()), SHORTCODE)

    assert facts["source_post_id"] == "1788651234567890"
    assert facts["source_username"] == "syaqir_sharani"
    assert facts["source_text"] == "kita ni belakang kira"
    assert facts["published_at"] == "2025-09-08T16:46:40+00:00"
    assert facts["likes"] == 9
    assert facts["replies"] == 1
    assert facts["reposts"] == 1
    assert facts["quotes"] == 0
    assert facts["media_type"] == 19
    assert facts["self_thread_length"] == 1


def test_taken_at_converts_to_utc_timestamp():
    facts = extract_post_facts(_thread_doc(_post(taken_at=0)), SHORTCODE)
    assert facts["published_at"] == "1970-01-01T00:00:00+00:00"


def test_taken_at_string_number_is_accepted():
    facts = extract_post_facts(_thread_doc(_post(taken_at="1757350000")), SHORTCODE)
    assert facts["published_at"] == "2025-09-08T16:46:40+00:00"


def test_zero_counters_are_kept():
    facts = extract_post_facts(
        _thread_doc(
            _post(
                like_count=0,
                text_post_app_info={
                    "direct_reply_count": 0,
                    "repost_count": 0,
                    "quote_count": 0,
                },
            )
        ),
        SHORTCODE,
    )
    assert facts["likes"] == 0
    assert facts["replies"] == 0
    assert facts["reposts"] == 0
    assert facts["quotes"] == 0


def test_absent_fields_are_absent_not_nulled():
    """A payload missing counters/user/text must not emit null entries."""
    node = {"pk": "1788000000000001", "code": SHORTCODE}
    document = (
        '{"__bbox":{"status":"success","result":{"data":{"feedData":{"edges":'
        '[{"node":{"text_post_app_thread":' + _json_dumps(node) + "}}]}}}}}"
    )
    facts = extract_post_facts(document, SHORTCODE)
    assert facts == {"source_post_id": "1788000000000001"}
    for key in (
        "source_username",
        "source_text",
        "published_at",
        "likes",
        "replies",
        "reposts",
        "quotes",
        "media_type",
        "self_thread_length",
        "views",
    ):
        assert key not in facts


def _json_dumps(obj) -> str:
    import json

    return json.dumps(obj)


def test_caption_text_used_when_text_absent():
    node = {
        "pk": "1788000000000001",
        "code": SHORTCODE,
        "caption": {"pk": "c1", "text": "hadis maghrib"},
    }
    facts = extract_post_facts(_thread_doc(_json_dumps(node)), SHORTCODE)
    assert facts["source_text"] == "hadis maghrib"


def test_text_wins_over_caption():
    node = {
        "pk": "1788000000000001",
        "code": SHORTCODE,
        "text": "root text",
        "caption": {"text": "caption text"},
    }
    facts = extract_post_facts(_thread_doc(_json_dumps(node)), SHORTCODE)
    assert facts["source_text"] == "root text"


def test_reply_count_fallback_when_direct_reply_absent():
    node = {
        "pk": "1788000000000001",
        "code": SHORTCODE,
        "text_post_app_info": {"reply_count": 7},
    }
    facts = extract_post_facts(_thread_doc(_json_dumps(node)), SHORTCODE)
    assert facts["replies"] == 7
    assert "reposts" not in facts
    assert "quotes" not in facts


def test_view_count_never_becomes_views():
    node = {
        "pk": "1788000000000001",
        "code": SHORTCODE,
        "view_count": 12345,
        "play_count": 999,
        "video_view_count": 888,
    }
    facts = extract_post_facts(_thread_doc(_json_dumps(node)), SHORTCODE)
    assert "views" not in facts
    assert "view_count" not in facts
    assert "play_count" not in facts


def test_self_thread_length_from_thread_items():
    facts = extract_post_facts(_thread_doc(_post(), length=4), SHORTCODE)
    assert facts["self_thread_length"] == 4


def test_no_thread_container_omits_length():
    post = _post()
    document = (
        '{"__bbox":{"status":"success","result":{"data":{"feedData":{"edges":'
        '[{"node":{"text_post_app_thread":{"thread_items":[{"post":' + post + "}]}}}]"
        "}}}}}"
    )
    # thread_items absent at the matched node's own level here is fine;
    # when present we use it. With a bare node and no thread wrapper at all:
    document2 = (
        '{"__bbox":{"status":"success","result":{"data":{"feedData":{"edges":'
        '[{"node":{"text_post_app_thread":' + post + "}}]}}}}}"
    )
    assert extract_post_facts(document2, SHORTCODE)["source_post_id"] == post_pk()
    assert "self_thread_length" not in extract_post_facts(document2, SHORTCODE)


def post_pk() -> str:
    import json

    return json.loads(_post())["pk"]


# ------------------------------------------------------------- validation

def test_negative_counter_fails_closed():
    node = {"pk": "1", "code": SHORTCODE, "like_count": -5}
    with pytest.raises(TrendEnrichError):
        extract_post_facts(_thread_doc(_json_dumps(node)), SHORTCODE)


def test_non_integer_counter_fails_closed():
    node = {"pk": "1", "code": SHORTCODE, "like_count": "lots"}
    with pytest.raises(TrendEnrichError):
        extract_post_facts(_thread_doc(_json_dumps(node)), SHORTCODE)


def test_future_taken_at_fails_closed():
    node = {"pk": "1", "code": SHORTCODE, "taken_at": 99999999999}
    with pytest.raises(TrendEnrichError):
        extract_post_facts(_thread_doc(_json_dumps(node)), SHORTCODE)


def test_blank_username_is_omitted():
    node = {
        "pk": "1788000000000001",
        "code": SHORTCODE,
        "user": {"username": "   "},
    }
    facts = extract_post_facts(_thread_doc(_json_dumps(node)), SHORTCODE)
    assert "source_username" not in facts


def test_no_sensitive_tokens_in_output():
    node = {
        "pk": "1788000000000001",
        "code": SHORTCODE,
        "__token": TOKEN_SENTINELS[0],
        "organic_tracking_token": TOKEN_SENTINELS[1],
        "logging_info_token": TOKEN_SENTINELS[2],
        "user": {"username": "syaqir_sharani"},
    }
    facts = extract_post_facts(_thread_doc(_json_dumps(node)), SHORTCODE)
    import json

    blob = json.dumps(facts)
    for sentinel in TOKEN_SENTINELS:
        assert sentinel not in blob


def test_exact_match_rules_still_apply():
    """Broad facts keep the fail-closed identity contract of v1."""
    other = _post(code=OTHER_SHORTCODE)
    with pytest.raises(TrendEnrichError):
        extract_post_facts(_thread_doc(other), SHORTCODE)

    conflict1 = _post(pk="1111111111111111")
    conflict2 = _post(pk="2222222222222222")
    document = _thread_doc(conflict1) + _thread_doc(conflict2)
    with pytest.raises(TrendEnrichError):
        extract_post_facts(document, SHORTCODE)
