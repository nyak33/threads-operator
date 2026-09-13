"""Unit tests for text_normalizer – deterministic hashing & dedup."""
from __future__ import annotations

import hashlib

from threads_operator.text_normalizer import normalize_snippet, snippet_hash, fallback_fingerprint


def test_normalize_basics():
    assert normalize_snippet("Hello...") == "hello"
    assert normalize_snippet("  hello   world  ") == "hello world"
    assert normalize_snippet("") == ""
    assert normalize_snippet(None) == ""
    assert normalize_snippet("HeLLo WoRLd") == "hello world"


def test_snippet_hash_deterministic():
    assert snippet_hash("same text") == snippet_hash("same text")
    assert snippet_hash("text a") != snippet_hash("text b")
    normalized = normalize_snippet("Same Text...")
    assert snippet_hash("Same Text...") == hashlib.sha256(normalized.encode()).hexdigest()
    assert len(snippet_hash("")) == 64


def test_fallback_fingerprint_deterministic_and_sensitive():
    base = ("2026-09-12T10:00:00+00:00", "Kadang jadi ayah ni kelakar...", "ridhuanazmi", 3)
    assert fallback_fingerprint(*base) == fallback_fingerprint(*base)
    assert fallback_fingerprint(*base) != fallback_fingerprint(base[0], base[1], base[2], 4)
    assert fallback_fingerprint(*base) != fallback_fingerprint(base[0], base[1], "other", base[3])
    assert len(fallback_fingerprint(*base)) == 32
