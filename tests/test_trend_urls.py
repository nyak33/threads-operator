"""Tests for deterministic Threads post-permalink normalization (no browser, no LLM)."""

import pytest

from threads_operator.trend_urls import normalize_threads_post_url


def test_valid_post_url_accepted_and_canonicalized():
    canon, username = normalize_threads_post_url(
        "https://www.threads.com/@syaqir_sharani/post/QVQz1jHkG4r"
    )
    assert canon == "https://www.threads.com/@syaqir_sharani/post/QVQz1jHkG4r"
    assert username == "syaqir_sharani"


def test_bare_host_and_trailing_slash_normalized():
    canon, username = normalize_threads_post_url(
        "https://threads.com/@foo/post/ABC123/"
    )
    assert canon == "https://www.threads.com/@foo/post/ABC123"
    assert username == "foo"


def test_http_scheme_rejected():
    with pytest.raises(ValueError):
        normalize_threads_post_url(
            "http://www.threads.com/@foo/post/ABC123"
        )


def test_non_threads_host_rejected():
    with pytest.raises(ValueError):
        normalize_threads_post_url(
            "https://www.evil.com/@foo/post/ABC123"
        )


def test_misleading_subdomain_prefix_host_rejected():
    with pytest.raises(ValueError):
        normalize_threads_post_url(
            "https://threads.com.evil.example/@foo/post/ABC123"
        )


def test_userinfo_host_spoof_rejected():
    with pytest.raises(ValueError):
        normalize_threads_post_url(
            "https://threads.com@evil.example/@foo/post/ABC123"
        )


def test_tracking_query_and_fragment_stripped():
    canon, username = normalize_threads_post_url(
        "https://www.threads.com/@Foo/post/ABC123"
        "?xid=Un2o&utm_source=share&fbclid=123#reply"
    )
    assert canon == "https://www.threads.com/@foo/post/ABC123"
    assert username == "foo"


def test_case_insensitive_username_dedups_to_same_permalink():
    a, _ = normalize_threads_post_url("https://www.threads.com/@FOO/post/ABC")
    b, _ = normalize_threads_post_url("https://m.threads.com/@foo/post/ABC")
    assert a == b


def test_non_post_paths_rejected():
    with pytest.raises(ValueError):
        normalize_threads_post_url("https://www.threads.com/@foo")
    with pytest.raises(ValueError):
        normalize_threads_post_url("https://www.threads.com/search?q=abc")


def test_missing_post_code_rejected():
    with pytest.raises(ValueError):
        normalize_threads_post_url("https://www.threads.com/@foo/post/")


def test_empty_and_garbage_input_rejected():
    for bad in ("", "not a url", "://", "https://"):
        with pytest.raises(ValueError):
            normalize_threads_post_url(bad)
