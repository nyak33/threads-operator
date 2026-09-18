import os
import stat
import time

from threads_operator.engagement_sessions import PendingEditStore


def test_create_find_and_remove_roundtrip(tmp_path):
    store = PendingEditStore(tmp_path / "sessions.json")
    session = store.create(
        chat_id="79553451",
        user_id="79553451",
        engagement_id=1,
        account_key="syaqir",
        card_message_id="3425",
        now=1000.0,
    )
    assert session["engagement_id"] == 1
    assert session["expires_at"] == 1000.0 + store.ttl_seconds

    found = store.find_for_sender(chat_id="79553451", user_id="79553451", now=1001.0)
    assert found is not None and found["token"] == session["token"]

    store.remove(session["token"])
    assert store.find_for_sender(chat_id="79553451", user_id="79553451", now=1002.0) is None


def test_sessions_survive_reload(tmp_path):
    path = tmp_path / "sessions.json"
    first = PendingEditStore(path)
    created = first.create(
        chat_id="1", user_id="2", engagement_id=7, account_key="syaqir", now=1000.0
    )
    second = PendingEditStore(path)
    found = second.find_by_token(created["token"], now=1001.0)
    assert found is not None and found["engagement_id"] == 7


def test_file_permissions_are_owner_only(tmp_path):
    path = tmp_path / "sessions.json"
    store = PendingEditStore(path)
    store.create(chat_id="1", user_id="2", engagement_id=1, account_key="syaqir")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_expired_sessions_are_not_returned(tmp_path):
    store = PendingEditStore(tmp_path / "sessions.json", ttl_seconds=60)
    store.create(chat_id="1", user_id="2", engagement_id=1, account_key="syaqir", now=1000.0)
    assert store.find_for_sender(chat_id="1", user_id="2", now=1000.0 + 61.0) is None
    assert store.sessions(now=1000.0 + 61.0) == []


def test_wrong_chat_or_user_cannot_see_session(tmp_path):
    store = PendingEditStore(tmp_path / "sessions.json")
    store.create(chat_id="111", user_id="222", engagement_id=1, account_key="syaqir", now=1000.0)
    assert store.find_for_sender(chat_id="999", user_id="222", now=1001.0) is None
    assert store.find_for_sender(chat_id="111", user_id="999", now=1001.0) is None
    assert store.find_for_sender(chat_id="111", user_id="222", now=1001.0) is not None


def test_create_replaces_existing_session_for_same_key(tmp_path):
    store = PendingEditStore(tmp_path / "sessions.json")
    first = store.create(chat_id="1", user_id="2", engagement_id=1, account_key="syaqir", now=1000.0)
    second = store.create(chat_id="1", user_id="2", engagement_id=1, account_key="syaqir", now=1010.0)
    assert first["token"] != second["token"]
    sessions = store.sessions(now=1011.0)
    assert len(sessions) == 1 and sessions[0]["token"] == second["token"]


def test_corrupt_file_fails_closed(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("not json", encoding="utf-8")
    store = PendingEditStore(path)
    assert store.sessions() == []
    assert store.find_for_sender(chat_id="1", user_id="2") is None
