"""RED tests: deterministic Relay/__bbox preloader extractor for trend enrichment.

Behavior contract (spec sections 3-5):
  document text + expected shortcode -> structured factual evidence {code, pk}
  Fail closed on: absent shortcode, OG-only, login/challenge, unparsable
  payload, conflicting structured matches, mismatch. No guessing, no
  unrelated-feed fallback, no DOM-selector dependence.
"""

import pytest

from threads_operator.trend_enrich_extract import (
    TrendChallengeError,
    TrendEnrichError,
    expected_shortcode,
    extract_post_evidence,
)

SHORTCODE = "DdGAw6kFfPA"
OTHER_SHORTCODE = "AbCdEf12345"


def _bbox(result_json: str) -> str:
    return '{"__bbox":{"status":"success","message":"","exception":null,' f'"result":{result_json}}}'


def _post(pk: str, code: str, text: str = "kita ni belakang kira") -> str:
    import json as _json

    return _json.dumps(
        {
            "pk": pk,
            "code": code,
            "text": text,
            "user": {"id": "555", "username": "syaqir_sharani"},
        }
    )


def _feed_document(root_post_json: str, extra_edges: str = "") -> str:
    # {"data":{"feedData":{"edges":[{"node":{"text_post_app_thread":<post>}} ...]}}}
    edges = '{"node":{"text_post_app_thread":' + root_post_json + "}}"
    if extra_edges:
        edges = edges + "," + extra_edges
    return _bbox('{"data":{"feedData":{"edges":[' + edges + "]}}}")


# ---------------------------------------------------------------- permalink


def test_expected_shortcode_from_canonical_permalink():
    assert (
        expected_shortcode(
            f"https://www.threads.com/@syaqir_sharani/post/{SHORTCODE}"
        )
        == SHORTCODE
    )


def test_expected_shortcode_rejects_non_permalink():
    with pytest.raises(ValueError):
        expected_shortcode("https://www.threads.com/@syaqir_sharani")


# ---------------------------------------------------------------- happy path


def test_extracts_pk_for_exact_shortcode_from_feed_edges():
    document = _feed_document(_post("1788651234567890", SHORTCODE))

    evidence = extract_post_evidence(document, SHORTCODE)

    assert evidence["code"] == SHORTCODE
    assert evidence["pk"] == "1788651234567890"


def test_finds_post_nested_in_thread_items():
    root = (
        '{"pk":"1788000000000001","code":"' + SHORTCODE + '","text":"root",'
        '"thread_items":[{"post":{"pk":"1788000000000001","code":"' + SHORTCODE + '"}},'
        '{"post":{"pk":"1788999999999999","code":"' + OTHER_SHORTCODE + '"}}]}'
    )
    document = _feed_document(root)

    evidence = extract_post_evidence(document, SHORTCODE)

    assert evidence["pk"] == "1788000000000001"


def test_ignores_unrelated_posts_and_picks_exact_match():
    unrelated = _post("1788111111111111", OTHER_SHORTCODE)
    target = _post("1788222222222222", SHORTCODE)
    document = (
        _feed_document(unrelated, extra_edges="") + "\n" + _feed_document(target)
    )

    evidence = extract_post_evidence(document, SHORTCODE)

    assert evidence["pk"] == "1788222222222222"


def test_repeated_same_pk_is_not_a_conflict():
    dup = _post("1788333333333333", SHORTCODE)
    document = _feed_document(dup) + "\n" + _feed_document(dup)

    evidence = extract_post_evidence(document, SHORTCODE)

    assert evidence["pk"] == "1788333333333333"


# ---------------------------------------------------------------- fail-closed


def test_missing_shortcode_fails_closed():
    document = _feed_document(_post("1788444444444444", OTHER_SHORTCODE))

    with pytest.raises(TrendEnrichError):
        extract_post_evidence(document, SHORTCODE)


def test_conflicting_pk_for_same_code_fails_closed():
    first = _post("1788555555555555", SHORTCODE)
    second = _post("1788666666666666", SHORTCODE)
    document = _feed_document(first) + "\n" + _feed_document(second)

    with pytest.raises(TrendEnrichError):
        extract_post_evidence(document, SHORTCODE)


def test_match_without_pk_fails_closed():
    document = _feed_document('{"code":"' + SHORTCODE + '","text":"no pk"}')

    with pytest.raises(TrendEnrichError):
        extract_post_evidence(document, SHORTCODE)


def test_og_only_document_fails_closed():
    document = (
        "<html><head>"
        f'<meta property="og:title" content="Syaqir on Threads: &quot;kita ni&quot;">'
        f'<meta property="og:url" content="https://www.threads.com/@syaqir_sharani/post/{SHORTCODE}">'
        "</head><body></body></html>"
    )

    with pytest.raises(TrendEnrichError):
        extract_post_evidence(document, SHORTCODE)


def test_unparsable_result_object_fails_closed():
    document = '{"__bbox":{"result":{"data":{"feedData":{"edges":[{"node":{"text_post_app_thread":{"pk":"1","code":"' + SHORTCODE + '"}'
    with pytest.raises(TrendEnrichError):
        extract_post_evidence(document, SHORTCODE)


def test_empty_document_fails_closed():
    with pytest.raises(TrendEnrichError):
        extract_post_evidence("", SHORTCODE)


def test_login_challenge_document_raises_challenge_error():
    document = (
        "<html><head><title>Login</title></head><body>"
        '<form action="/login/?next=%2F">Enter code</form>'
        "</body></html>"
    )

    with pytest.raises(TrendChallengeError):
        extract_post_evidence(document, SHORTCODE)


def test_checkpoint_document_raises_challenge_error():
    document = (
        "<html><body>Check your recent activity for a security checkpoint"
        "</body></html>"
    )

    with pytest.raises(TrendChallengeError):
        extract_post_evidence(document, SHORTCODE)


def test_blank_shortcode_argument_rejected():
    document = _feed_document(_post("1788777777777777", SHORTCODE))

    with pytest.raises(ValueError):
        extract_post_evidence(document, "")
