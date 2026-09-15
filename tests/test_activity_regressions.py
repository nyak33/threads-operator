from __future__ import annotations

from pathlib import Path
import httpx

from threads_operator.activity_collector import collect_activity_follows
from threads_operator.activity_parser import parse_notification_node
from threads_operator.supabase_store import SupabaseStore


def _payload(context="Followed from your post", content="Hello world", tuuid="n1", title="{alice|0|1|user?id=1|none}"):
    return {
        "data": {
            "notifications": {
                "edges": [
                    {
                        "node": {
                            "story_type": "follow",
                            "args": {
                                "tuuid": tuuid,
                                "timestamp": 1789215000,
                                "extra": {
                                    "context": context,
                                    "content": content,
                                    "title": title,
                                    "icon_name": "follow",
                                },
                            },
                        }
                    }
                ]
            }
        }
    }


class FakeStore:
    def __init__(self):
        self.insert_calls = 0
        self.summary_calls = 0

    def read_activity_follows_sync(self, profile_dir, settle_seconds=3.0):
        return {"payload": _payload()}

    def list_posts(self):
        return [{
            "thread_id": "p1",
            "text": "Hello world",
            "published_at": "2026-09-12T10:00:00+00:00",
            "permalink": "https://www.threads.com/@me/post/abc",
        }]

    def insert_activity_events(self, events):
        self.insert_calls += 1
        raise AssertionError("dry run must not write activity events")

    def upsert_activity_follows_summary(self, *args, **kwargs):
        self.summary_calls += 1
        raise AssertionError("dry run must not write summaries")


def test_dry_run_never_writes_to_store():
    store = FakeStore()
    result = collect_activity_follows(store, Path("/tmp/profile"), persist=False)
    assert result["success"] is True
    assert result["total_events"] == 1
    assert result["high"] == 1
    assert store.insert_calls == 0
    assert store.summary_calls == 0


def test_plain_follow_without_post_attribution_is_ignored():
    node = _payload(context="Followed you", content="")["data"]["notifications"]["edges"][0]["node"]
    assert parse_notification_node(node) is None


def test_supabase_store_lists_existing_threads_posts_table():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["select"] = request.url.params.get("select")
        return httpx.Response(200, json=[])

    store = SupabaseStore(
        "https://example.supabase.co",
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert store.list_posts() == []
    assert seen["path"] == "/rest/v1/threads_posts"
    assert seen["select"] == "thread_id,text,published_at,permalink"


def test_migration_uses_existing_posts_and_per_post_summary_view():
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "003_activity_follow_events.sql").read_text().lower()
    assert "threads_known_posts" not in sql
    assert "create" in sql and "threads_follows_from_post_summary" in sql
    assert "follows_from_post_activity" in sql
    assert "group by" in sql and "matched_post_id" in sql
    assert "enable row level security" in sql


def test_confidence_totals_count_grouped_follows_not_notification_rows():
    class GroupStore(FakeStore):
        def read_activity_follows_sync(self, profile_dir, settle_seconds=3.0):
            return {"payload": _payload(title="{alice|0|1|user?id=1|none} and 20 others")}

    result = collect_activity_follows(GroupStore(), Path("/tmp/profile"), persist=False)
    assert result["total_events"] == 1
    assert result["total_follows"] == 21
    assert result["high"] == 21


def test_activity_storage_is_append_only_and_dedupes_by_fingerprint():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, dict(request.url.params)))
        if request.method == "GET":
            return httpx.Response(200, json=[{"fallback_fingerprint": "fp-old"}])
        if request.method == "POST":
            body = request.read().decode()
            assert "fp-new" in body
            assert "fp-old" not in body
            assert "on_conflict" not in request.url.params
            assert "\"upserted\"" not in body
            return httpx.Response(201, json=[])
        raise AssertionError(request.method)

    store = SupabaseStore(
        "https://example.supabase.co", "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    events = [
        {"notification_id": "n1", "fallback_fingerprint": "fp-old"},
        {"notification_id": "n2", "fallback_fingerprint": "fp-new"},
    ]
    assert store.insert_activity_events(events) == 1
    assert events[0]["upserted"] is False
    assert events[1]["upserted"] is True
