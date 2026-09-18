from threads_operator.publisher import publish_next


class FakeStore:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    def peek_due_post(self, table, campaign_code=None, now=None):
        self.calls.append(("peek", table, campaign_code))
        return self.row

    def claim_due_post(self, table, campaign_code=None, now=None):
        self.calls.append(("claim", table, campaign_code))
        return self.row

    def mark_post_main_published(self, table, row_id, post_id):
        self.calls.append(("main", table, row_id, post_id))

    def mark_post_reply_progress(self, table, row_id, reply_ids):
        self.calls.append(("reply-progress", table, row_id, list(reply_ids)))

    def mark_post_posted(self, table, row_id, reply_ids, posted_at=None):
        self.calls.append(("posted", table, row_id, reply_ids))

    def mark_post_failed(self, table, row_id, error):
        self.calls.append(("failed", table, row_id, error))


class FakeAPI:
    def __init__(self, ids=None, fail_at=None):
        self.ids = list(ids or [])
        self.fail_at = fail_at
        self.calls = []

    def publish_text(self, text, reply_to_id=None):
        self.calls.append((text, reply_to_id))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise RuntimeError("publish exploded")
        return self.ids.pop(0)


def row(replies=None):
    return {
        "id": 7,
        "main_post_text": "main post",
        "reply_texts": replies or [],
    }


def test_dry_run_peeks_without_claiming_or_publishing():
    store = FakeStore(row())
    api = FakeAPI(["post-1"])

    result = publish_next(api, store, "queue", campaign_code="RANDOM", dry_run=True)

    assert result == {"status": "dry-run", "queue_id": 7}
    assert store.calls == [("peek", "queue", "RANDOM")]
    assert api.calls == []


def test_no_due_row_returns_idle():
    store = FakeStore(None)
    api = FakeAPI()

    assert publish_next(api, store, "queue") == {"status": "idle"}
    assert api.calls == []


def test_main_only_publish_claims_before_api_and_marks_posted():
    store = FakeStore(row())
    api = FakeAPI(["post-1"])

    result = publish_next(api, store, "queue")

    assert result == {
        "status": "posted",
        "queue_id": 7,
        "main_post_id": "post-1",
        "reply_ids": [],
    }
    assert store.calls[0][0] == "claim"
    assert api.calls == [("main post", None)]
    assert store.calls[1] == ("main", "queue", 7, "post-1")
    assert store.calls[2] == ("posted", "queue", 7, [])


def test_reply_chain_uses_previous_post_as_parent_and_persists_progress():
    store = FakeStore(row(["reply one", "reply two"]))
    api = FakeAPI(["post-1", "reply-1", "reply-2"])

    result = publish_next(api, store, "queue")

    assert api.calls == [
        ("main post", None),
        ("reply one", "post-1"),
        ("reply two", "reply-1"),
    ]
    assert result["reply_ids"] == ["reply-1", "reply-2"]
    progress = [call for call in store.calls if call[0] == "reply-progress"]
    assert progress == [
        ("reply-progress", "queue", 7, ["reply-1"]),
        ("reply-progress", "queue", 7, ["reply-1", "reply-2"]),
    ]


def test_failure_after_main_publish_retains_main_id_and_marks_failed():
    store = FakeStore(row(["reply one"]))
    api = FakeAPI(["post-1"], fail_at=2)

    result = publish_next(api, store, "queue")

    assert result["status"] == "failed"
    assert result["queue_id"] == 7
    assert result["main_post_id"] == "post-1"
    assert "publish exploded" in result["error"]
    assert ("main", "queue", 7, "post-1") in store.calls
    failed = [call for call in store.calls if call[0] == "failed"]
    assert len(failed) == 1
    assert "publish exploded" in failed[0][3]


def test_failure_after_one_reply_keeps_successful_reply_id_persisted():
    store = FakeStore(row(["reply one", "reply two"]))
    api = FakeAPI(["post-1", "reply-1"], fail_at=3)

    result = publish_next(api, store, "queue")

    assert result["status"] == "failed"
    assert result["main_post_id"] == "post-1"
    assert result["reply_ids"] == ["reply-1"]
    assert ("reply-progress", "queue", 7, ["reply-1"]) in store.calls
    assert store.calls.index(("reply-progress", "queue", 7, ["reply-1"])) < next(
        index for index, call in enumerate(store.calls) if call[0] == "failed"
    )


def test_resume_after_main_publish_does_not_duplicate_main():
    resumed = row(["reply one"])
    resumed["threads_main_post_id"] = "post-1"
    resumed["threads_reply_ids"] = []
    store = FakeStore(resumed)
    api = FakeAPI(["reply-1"])

    result = publish_next(api, store, "queue")

    assert result["status"] == "posted"
    assert result["main_post_id"] == "post-1"
    assert result["reply_ids"] == ["reply-1"]
    assert api.calls == [("reply one", "post-1")]
    assert not any(call[0] == "main" for call in store.calls)


def test_resume_after_one_reply_continues_from_last_reply():
    resumed = row(["reply one", "reply two"])
    resumed["threads_main_post_id"] = "post-1"
    resumed["threads_reply_ids"] = ["reply-1"]
    store = FakeStore(resumed)
    api = FakeAPI(["reply-2"])

    result = publish_next(api, store, "queue")

    assert result["status"] == "posted"
    assert result["reply_ids"] == ["reply-1", "reply-2"]
    assert api.calls == [("reply two", "reply-1")]
    assert ("reply-progress", "queue", 7, ["reply-1", "reply-2"]) in store.calls


def test_persisted_reply_ids_without_main_id_fail_closed():
    inconsistent = row(["reply one"])
    inconsistent["threads_reply_ids"] = ["reply-1"]
    store = FakeStore(inconsistent)
    api = FakeAPI(["unused"])

    result = publish_next(api, store, "queue")

    assert result["status"] == "failed"
    assert "without threads_main_post_id" in result["error"]
    assert api.calls == []
