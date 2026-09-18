"""Tests for URL normalization + identity forms covering BOTH permalink shapes:
A. threads.com shortcode permalinks (existing behavior — must not regress)
B. threads.net numeric (fbid) Activity permalinks (new)

Live compatibility probe findings this encodes:
- https://www.threads.net/@user/post/<numeric> is a 301 host-only rewrite to
  https://www.threads.com/@user/post/<numeric> (numeric slug intact), so the
  numeric form is a legitimate canonical Threads post URL and is normalized
  to the SAME www.threads.com host with the identifier preserved verbatim.
- Shortcodes and numeric ids are DIFFERENT identity spaces: a numeric code
  never appears as a structured web doc 'code', and no local numeric->
  shortcode conversion exists. Bridging requires the official Graph API
  (proven separately) and is NOT part of URL normalization.
"""

import pytest

from threads_operator.trend_urls import (
    identity_forms,
    normalize_threads_post_url,
    permalinks_reference_same_post,
)

NUM = "17909304312530892"


# ---------------------------------------------------------------- URLS 1-8
def test_threads_com_shortcode_still_accepted():
    canon, user = normalize_threads_post_url(
        "https://www.threads.com/@syaqir_sharani/post/DdGAw6kFfPA")
    assert canon == "https://www.threads.com/@syaqir_sharani/post/DdGAw6kFfPA"
    assert user == "syaqir_sharani"


def test_threads_net_numeric_accepted():
    canon, user = normalize_threads_post_url(
        f"https://threads.net/@syaqir_sharani/post/{NUM}")
    # host unified to www.threads.com (proven 301 host-only redirect),
    # numeric slug preserved VERBATIM.
    assert canon == f"https://www.threads.com/@syaqir_sharani/post/{NUM}"
    assert user == "syaqir_sharani"


def test_www_threads_net_numeric_accepted():
    canon, _ = normalize_threads_post_url(
        f"https://www.threads.net/@syaqir_sharani/post/{NUM}")
    assert canon == f"https://www.threads.com/@syaqir_sharani/post/{NUM}"


def test_malformed_numeric_path_rejected():
    for bad in (
        "https://www.threads.net/@syaqir_sharani/post/12ab34",   # mixed alnum, too short
        "https://www.threads.net/@syaqir_sharani/post/",          # missing id
        "https://www.threads.net/@syaqir_sharani/post/0xdeadbeef",
        "https://www.threads.net/syaqir_sharani/post/17909304312530892",  # no @
    ):
        with pytest.raises(ValueError):
            normalize_threads_post_url(bad)


def test_host_suffix_attack_rejected_on_threads_net():
    for bad in (
        "https://threads.net.evil.example/@foo/post/17909304312530892",
        "https://www.threads.net.evil.example/@foo/post/ABC123def",
    ):
        with pytest.raises(ValueError):
            normalize_threads_post_url(bad)


def test_query_fragment_stripped_numeric_url():
    canon, _ = normalize_threads_post_url(
        f"https://www.threads.net/@foo/post/{NUM}?xid=1&utm_source=share#r2")
    assert canon == f"https://www.threads.com/@foo/post/{NUM}"


def test_shortcode_case_preserved():
    canon, _ = normalize_threads_post_url("https://www.threads.com/@FOO/post/AbC123xYz")
    assert canon == "https://www.threads.com/@foo/post/AbC123xYz"
    assert identity_forms(canon)["shortcode"] == "AbC123xYz"


def test_numeric_identifier_preserved_verbatim():
    long_num = "1790930431253089200"
    canon, _ = normalize_threads_post_url(
        f"https://www.threads.net/@foo/post/{long_num}")
    assert canon.endswith(f"/post/{long_num}")
    forms = identity_forms(canon)
    assert forms["kind"] == "numeric"
    assert forms["numeric_id"] == long_num
    assert forms["username"] == "foo"
    assert "shortcode" not in forms  # nothing fabricated


# ------------------------------------------------------------ identity 9-13
def test_identity_forms_shortcode():
    forms = identity_forms("https://www.threads.com/@foo/post/DdGAw6kFfPA")
    assert forms["kind"] == "shortcode"
    assert forms["shortcode"] == "DdGAw6kFfPA"
    assert "numeric_id" not in forms


def test_threads_net_and_threads_com_numeric_same_identity():
    a = f"https://www.threads.net/@Foo/post/{NUM}"
    b = f"https://www.threads.com/@foo/post/{NUM}"
    assert permalinks_reference_same_post(a, b) is True


def test_shortcode_vs_numeric_never_claimed_same_locally():
    # No local conversion exists; different identity spaces => not same.
    assert permalinks_reference_same_post(
        f"https://www.threads.net/@foo/post/{NUM}",
        "https://www.threads.com/@foo/post/DdGAw6kFfPA") is False


def test_different_numeric_never_same():
    assert permalinks_reference_same_post(
        f"https://www.threads.net/@foo/post/{NUM}",
        f"https://www.threads.net/@foo/post/{NUM}1") is False


def test_shortcode_exact_match_case_sensitive():
    a = "https://www.threads.com/@foo/post/AbC123xYz"
    assert permalinks_reference_same_post(a, a) is True
    assert permalinks_reference_same_post(
        a, "https://www.threads.com/@foo/post/abc123xyz") is False


def test_invalid_input_rejected_by_identity_helpers():
    with pytest.raises(ValueError):
        identity_forms("https://example.com/@foo/post/ABC123def")
    with pytest.raises(ValueError):
        permalinks_reference_same_post(
            "javascript:alert(1)", "https://www.threads.com/@f/post/ABC123def")
