import json
import stat

import pytest

from storage import Store

G, U = 1, 100


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "data.json"))


def test_link_sets_active_and_replaces_duplicates(store):
    store.link(G, U, "Alice", None)
    store.link(G, U, "Bob", "pw")
    assert store.active_slot(G, U) == "Bob"
    store.link(G, U, "Alice", "new")
    assert store.active_slot(G, U) == "Alice"
    assert [e["slot"] for e in store.slots_of(G, U)] == ["Bob", "Alice"]
    assert store.get_slot(G, U, "Alice")["password"] == "new"


def test_get_slot_defaults_to_active(store):
    store.link(G, U, "Alice", None)
    assert store.get_slot(G, U)["slot"] == "Alice"
    assert store.get_slot(G, U, "Nobody") is None
    assert store.get_slot(G, 999) is None


def test_unlink_moves_active_and_removes_empty_user(store):
    store.link(G, U, "Alice", None)
    store.link(G, U, "Bob", None)
    assert store.unlink(G, U, "Bob")
    assert store.active_slot(G, U) == "Alice"
    assert store.unlink(G, U, "Alice")
    assert store.slots_of(G, U) == []
    assert not store.unlink(G, U, "Alice")


def test_set_active(store):
    store.link(G, U, "Alice", None)
    store.link(G, U, "Bob", None)
    assert store.set_active(G, U, "Alice")
    assert store.active_slot(G, U) == "Alice"
    assert not store.set_active(G, U, "Nobody")


def test_owner_of_and_entries(store):
    store.link(G, U, "Alice", None)
    store.link(G, 200, "Bob", None)
    assert store.owner_of(G, "Bob") == 200
    assert store.owner_of(G, "Zed") is None
    assert sorted(uid for uid, _ in store.entries(G)) == [U, 200]


def test_persistence_and_permissions(tmp_path):
    path = tmp_path / "data.json"
    s = Store(str(path))
    s.link(G, U, "Alice", "secret")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert Store(str(path)).get_slot(G, U)["password"] == "secret"


def test_migrate_old_link_format(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"guilds": {"1": {"host": "h", "links": {"100": {"slot": "Alice", "password": "pw"}}}}}))
    s = Store(str(path))
    assert s.active_slot(1, 100) == "Alice"
    assert s.get_slot(1, 100)["password"] == "pw"


def test_set_forum_resets_threads_only_when_changed(store):
    store.set_forum(G, 10)
    store.set_thread(G, "Alice", 55)
    store.set_forum(G, 10)
    assert store.thread_of(G, "Alice") == 55
    store.set_forum(G, 11)
    assert store.thread_of(G, "Alice") is None


def test_hint_messages_and_found(store):
    hint = ("Alice", "Bob", "Sword", "Chest")
    store.set_hint_messages(G, hint, [(1, 2), (3, 4)])
    assert store.get_hint_messages(G, hint) == [(1, 2), (3, 4)]
    assert not store.is_hint_found(G, hint)
    store.mark_hint_found(G, hint)
    assert store.is_hint_found(G, hint)


def test_get_hint_messages_old_single_ref_format(store):
    hint = ("A", "B", "I", "L")
    store.guild(G)["hint_messages"] = {Store._hint_key(*hint): [7, 8]}
    assert store.get_hint_messages(G, hint) == [(7, 8)]


def test_reset_guild(store):
    store.set_server(G, "host:1", None, 5)
    store.link(G, U, "Alice", None)
    store.reset_guild(G)
    assert G not in store.guild_ids()
    assert store.guild(G)["host"] is None
