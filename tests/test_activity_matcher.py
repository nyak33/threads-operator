"""Unit tests for activity_matcher – deterministic matching with confidence levels."""
from __future__ import annotations

from threads_operator.activity_matcher import eligible_candidates, match_event


def _post(thread_id="post-A", text="Hello world this is my post",
          published_at="2026-09-10T10:00:00+00:00",
          permalink="/@syaqir_sharani/post/Abc123"):
    return {"thread_id": thread_id, "text": text,
            "published_at": published_at, "permalink": permalink}


def _event(source_snippet_raw="Hello world this is my post",
           notification_datetime="2026-09-11T10:00:00+00:00"):
    return {"source_snippet_raw": source_snippet_raw,
            "notification_datetime": notification_datetime}


def _posts_unique():
    return [_post("post-A", text="Unique post A"), _post("post-B", text="Unique post B")]


def _posts_duplicate():
    return [
        _post("post-X", text="Hadith daily lesson v1", published_at="2026-09-08T10:00:00+00:00"),
        _post("post-Y", text="Hadith daily lesson v1", published_at="2026-09-09T10:00:00+00:00"),
    ]


class TestEligibleCandidates:
    def test_prefix_match(self):
        result = eligible_candidates(_event(source_snippet_raw="Hello"), [_post("p", text="Hello world this is a long post")])
        assert len(result) == 1 and result[0]["thread_id"] == "p"

    def test_no_match(self):
        assert eligible_candidates(_event(source_snippet_raw="different"), [_post("p", text="Hello")]) == []

    def test_reject_future_post(self):
        evt = _event(notification_datetime="2026-09-10T12:00:00+00:00")
        posts = [_post("f", text="Future post", published_at="2026-09-15T10:00:00+00:00")]
        assert eligible_candidates(evt, posts) == []

    def test_empty_snippet(self):
        assert eligible_candidates({"source_snippet_raw": ""}, []) == []


class TestMatchConfidence:
    def test_unique_candidate_high(self):
        m = match_event(_event(source_snippet_raw="Unique post A"), _posts_unique())
        assert m["match_confidence"] == "high"
        assert m["matched_post_id"] == "post-A"

    def test_no_candidate_unknown(self):
        m = match_event(_event(source_snippet_raw="ghost"), _posts_unique())
        assert m["match_confidence"] == "unknown"
        assert m["matched_post_id"] is None

    def test_repeated_text_nearest_prior_medium(self):
        m = match_event(_event(source_snippet_raw="Hadith daily lesson v1"), _posts_duplicate())
        assert m["match_confidence"] == "medium"
        assert m["matched_post_id"] == "post-Y"
        assert m["match_method"] == "nearest_prior_inference"

    def test_same_publish_instant_low(self):
        same = "2026-09-08T10:00:00+00:00"
        posts = [_post("t1", text="Twins", published_at=same), _post("t2", text="Twins", published_at=same)]
        m = match_event(_event(source_snippet_raw="Twins"), posts)
        assert m["match_confidence"] == "low"
        assert m["matched_post_id"] is None

    def test_missing_timestamp_low(self):
        posts = [_post("t1", text="Twins"), _post("t2", text="Twins")]
        for p in posts:
            p.pop("published_at", None)
        m = match_event(_event(source_snippet_raw="Twins"), posts)
        assert m["match_confidence"] == "low"

    def test_future_candidate_unknown(self):
        posts = [_post("future", text="Future post", published_at="2026-09-15T10:00:00+00:00")]
        m = match_event(_event(source_snippet_raw="Future post"), posts)
        assert m["match_confidence"] == "unknown"

    def test_deterministic(self):
        evt = _event(source_snippet_raw="Unique post A")
        assert match_event(evt, _posts_unique()) == match_event(evt, _posts_unique())
