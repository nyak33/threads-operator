from datetime import datetime, timezone

from threads_operator.collector import collect_once


NOW = datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc)


class FakeAPI:
    user_id = "user-123"

    def __init__(self, posts, post_metrics=None, account_metrics=None, fail_posts=None):
        self.posts = posts
        self.post_metrics = post_metrics or {}
        self.account_metrics = account_metrics or {"followers_count": 100, "views": 500}
        self.fail_posts = set(fail_posts or [])
        self.post_calls = []
        self.account_calls = 0

    def list_posts(self, limit=100):
        return self.posts

    def get_post_insights(self, post_id):
        self.post_calls.append(post_id)
        if post_id in self.fail_posts:
            raise RuntimeError("boom")
        return self.post_metrics.get(post_id, {})

    def get_account_insights(self):
        self.account_calls += 1
        return self.account_metrics


class FakeStore:
    def __init__(self, latest_posts=None, latest_account=None):
        self.latest_posts = latest_posts or {}
        self.latest_account = latest_account
        self.account_rows = []
        self.post_rows = []
        self.batch_calls = []

    def latest_post_snapshots(self, captured_since):
        self.batch_calls.append(captured_since)
        return self.latest_posts

    def latest_account_snapshot(self, account_id):
        return self.latest_account

    def insert_account_snapshot(self, payload):
        self.account_rows.append(payload)

    def insert_post_snapshot(self, payload):
        self.post_rows.append(payload)


def post(post_id="p1", published="2026-09-09T08:30:00+00:00"):
    return {"id": post_id, "timestamp": published, "text": "hello"}


def test_fresh_post_without_history_is_sampled():
    api = FakeAPI([post()], {"p1": {"views": 120, "likes": 4, "shares": None}})
    store = FakeStore()

    result = collect_once(api, store, NOW)

    assert result["posts_sampled"] == 1
    assert result["posts_skipped"] == 0
    assert result["posts_all_null"] == 0
    assert store.post_rows[0]["post_id"] == "p1"
    assert store.post_rows[0]["account_id"] == "user-123"
    assert store.post_rows[0]["post_age_minutes"] == 30.0
    assert store.post_rows[0]["shares"] is None
    assert len(store.account_rows) == 1


def test_post_sampled_too_recently_is_skipped():
    api = FakeAPI([post()])
    store = FakeStore(
        latest_posts={"p1": {"captured_at": "2026-09-09T08:58:00+00:00", "views": 1}}
    )

    result = collect_once(api, store, NOW)

    assert result["posts_sampled"] == 0
    assert result["posts_skipped"] == 1
    assert api.post_calls == []


def test_one_post_failure_does_not_block_other_post():
    api = FakeAPI(
        [post("bad"), post("good")],
        post_metrics={"good": {"views": 50}},
        fail_posts={"bad"},
    )
    store = FakeStore()

    result = collect_once(api, store, NOW)

    assert result["posts_sampled"] == 1
    assert result["post_failures"] == [{"post_id": "bad", "error": "RuntimeError: boom"}]
    assert [row["post_id"] for row in store.post_rows] == ["good"]


def test_account_snapshot_respects_default_15_minute_interval():
    api = FakeAPI([post()], {"p1": {"views": 1}})
    store = FakeStore(latest_account={"captured_at": "2026-09-09T08:50:00+00:00"})

    result = collect_once(api, store, NOW)

    assert result["account_snapshot"] == "skipped"
    assert api.account_calls == 0
    assert store.account_rows == []


def test_account_snapshot_due_after_15_minutes():
    api = FakeAPI([])
    store = FakeStore(latest_account={"captured_at": "2026-09-09T08:45:00+00:00"})

    result = collect_once(api, store, NOW)

    assert result["account_snapshot"] == "stored"
    assert store.account_rows[0]["followers_count"] == 100


def test_freshness_is_loaded_once_for_multiple_posts():
    api = FakeAPI([post("p1"), post("p2")])
    store = FakeStore(
        latest_posts={
            "p1": {"captured_at": "2026-09-09T08:58:00+00:00", "views": 1},
            "p2": {"captured_at": "2026-09-09T08:58:00+00:00", "views": 1},
        }
    )

    result = collect_once(api, store, NOW)

    assert result["posts_skipped"] == 2
    assert len(store.batch_calls) == 1
    assert api.post_calls == []


def test_all_null_metrics_are_not_stored_or_failed():
    api = FakeAPI([post()], {"p1": {}})
    store = FakeStore()

    result = collect_once(api, store, NOW)

    assert result["posts_all_null"] == 1
    assert result["posts_sampled"] == 0
    assert result["post_failures"] == []
    assert store.post_rows == []


def test_partial_null_metrics_are_stored_without_zero_filling():
    api = FakeAPI([post()], {"p1": {"views": 10, "likes": None}})
    store = FakeStore()

    result = collect_once(api, store, NOW)

    assert result["posts_sampled"] == 1
    assert result["posts_all_null"] == 0
    assert store.post_rows[0]["views"] == 10
    assert store.post_rows[0]["likes"] is None


def test_zero_metrics_are_usable_and_stored():
    api = FakeAPI([post()], {"p1": {"views": 0}})
    store = FakeStore()

    result = collect_once(api, store, NOW)

    assert result["posts_sampled"] == 1
    assert result["posts_all_null"] == 0
    assert store.post_rows[0]["views"] == 0


def test_all_null_warning_goes_to_stderr(capsys):
    api = FakeAPI([post()], {"p1": {}})
    store = FakeStore()

    collect_once(api, store, NOW)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "all-NULL insights response for post p1" in captured.err
