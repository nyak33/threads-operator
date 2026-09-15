"""Unit tests for Activity payload parsing."""
from __future__ import annotations

from threads_operator.activity_parser import parse_notification_node, parse_activity_payload, extract_notification_nodes


def _make_node(extra_content="Example post", extra_context="Followed from your post",
               extra_title=None, icon_name="follow", tuuid="test-1", timestamp=1726138200):
    title = "{ridhuanazmi|71001583809|1|user?id=71001583809|none} followed you"
    return {
        "story_type": "follow",
        "args": {
            "tuuid": tuuid,
            "timestamp": timestamp,
            "extra": {
                "title": title if extra_title is None else extra_title,
                "context": extra_context,
                "content": extra_content,
                "icon_name": icon_name,
                "media_dict": None,
            },
        },
    }


def test_extract_nodes_nested():
    payload = {"data": {"notifications": {"edges": [{"node": _make_node()}]}}}
    assert len(extract_notification_nodes(payload)) == 1


def test_basic_follow_attribution():
    evt = parse_notification_node(_make_node())
    assert evt["notification_id"] == "test-1"
    assert evt["visible_username"] == "ridhuanazmi"
    assert evt["follow_count"] == 1
    assert evt["attribution_possible"] is True


def test_grouped_others_count():
    evt = parse_notification_node(_make_node(extra_title="{alice|111|1|x|none} and 20 others"))
    assert evt["grouped_others_count"] == 20
    assert evt["follow_count"] == 21


def test_plain_follow_without_post_source_filtered_out():
    assert parse_notification_node(_make_node(extra_content="", extra_context="Followed you")) is None


def test_requested_filtered_out():
    assert parse_notification_node(_make_node(extra_context="Requested your follow")) is None


def test_non_follow_filtered_out():
    assert parse_notification_node(_make_node(icon_name="repost")) is None


def test_duplicate_notification_id_deduped():
    edges = [{"node": _make_node(tuuid="dup")}, {"node": _make_node(tuuid="dup")}, {"node": _make_node(tuuid="other")}]
    result = parse_activity_payload({"data": {"notifications": {"edges": edges}}})
    ids = [e["notification_id"] for e in result]
    assert ids.count("dup") == 1
    assert "other" in ids


def test_repeated_text_different_ids_preserved():
    edges = [{"node": _make_node(tuuid=f"a{i}", extra_content="Hadith text v1")} for i in range(3)]
    result = parse_activity_payload({"data": {"notifications": {"edges": edges}}})
    assert len(result) == 3
