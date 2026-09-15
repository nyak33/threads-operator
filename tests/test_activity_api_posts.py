from threads_operator.activity_matcher import match_event


def test_activity_matcher_accepts_official_threads_api_post_shape():
    event = {
        "source_snippet_raw": "Hello world",
        "notification_datetime": "2026-09-15T10:00:00+00:00",
    }
    posts = [
        {
            "id": "post-1",
            "text": "Hello world from this account",
            "timestamp": "2026-09-15T09:00:00+00:00",
            "permalink": "https://www.threads.com/@example/post/abc",
        }
    ]

    result = match_event(event, posts)

    assert result["matched_post_id"] == "post-1"
    assert result["match_confidence"] == "high"
