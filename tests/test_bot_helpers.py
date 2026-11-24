import pytest

import bot


def values(choices):
    return [c.value for c in choices]


def test_best_matches_prefix_before_contains():
    names = ["Big Sword", "Sword", "Swordfish"]
    assert values(bot.best_matches(sorted(names), "sword")) == ["Sword", "Swordfish", "Big Sword"]


def test_best_matches_words_any_order():
    assert values(bot.best_matches(["Fire Rod", "Ice Rod"], "rod fire")) == ["Fire Rod"]


def test_best_matches_deprioritizes_marked():
    marks = {"Aaa": "✅ "}
    res = bot.best_matches(["Aaa", "Aab"], "aa", mark=lambda n: marks.get(n, ""))
    assert values(res) == ["Aab", "Aaa"]
    assert res[1].name == "✅ Aaa"


def test_best_matches_limit_adds_hidden_line():
    names = [f"Item {i:03}" for i in range(40)]
    res = bot.best_matches(names, "item")
    assert len(res) == 25
    assert "16 autres" in res[-1].name
    assert res[-1].value == "item"


def test_best_matches_skips_names_over_100_chars():
    assert bot.best_matches(["x" * 101, "xy"], "x") and values(bot.best_matches(["x" * 101, "xy"], "x")) == ["xy"]


def test_compact_marks():
    assert bot.compact_marks(False, False) == ""
    assert bot.compact_marks(True, False) == f"{bot.RECEIVED} "
    assert bot.compact_marks(True, True) == f"{bot.RECEIVED}{bot.HINTED} "


NAMES = ["Progressive Sword", "Progressive Bow", "Boss Key", "Small Key", "Rupee"]


def test_regex_filter_matches_case_insensitively():
    assert bot.apply_regex_filter(NAMES, r"^progressive|key$") == NAMES[:4]


def test_regex_filter_invalid_and_too_long():
    with pytest.raises(ValueError, match="invalide"):
        bot.apply_regex_filter(NAMES, "(")
    with pytest.raises(ValueError, match="trop longue"):
        bot.apply_regex_filter(NAMES, "a" * 201)


def test_regex_filter_redos_is_stopped():
    with pytest.raises(ValueError, match="trop lente"):
        bot.apply_regex_filter(["a" * 40 + "!"] * 50, r"^(a|aa)+$")


def test_format_columns_order_and_width():
    rows = bot.format_columns(["a", "b", "c", "d", "e"], width=60, max_cols=3)
    # lecture de haut en bas puis de gauche à droite
    assert [r.split() for r in rows] == [["a", "c", "e"], ["b", "d"]]


def test_format_columns_single_column_when_names_are_wide():
    rows = bot.format_columns(["x" * 40, "y" * 40], width=60, max_cols=3)
    assert rows == ["x" * 40, "y" * 40]


class FakeConn:
    def __init__(self, connected=False, error=None, alive=True):
        import asyncio
        self.connected = asyncio.Event()
        if connected:
            self.connected.set()
        self.error, self.alive = error, alive


def test_conn_state():
    assert "non démarrée" in bot.conn_state(None)
    assert "🟢" in bot.conn_state(FakeConn(connected=True))
    assert "Mauvais mot de passe" in bot.conn_state(FakeConn(error="Mauvais mot de passe"))
    assert "en cours" in bot.conn_state(FakeConn())
    assert "abandonnée" in bot.conn_state(FakeConn(alive=False))


def test_format_status_never_shows_password():
    cfg = {"host": "ap.gg:1", "password": "SECRET", "channel": 5, "log_channel": 6, "log_items": "all"}
    text = bot.format_status(cfg, [("Bob", "bob", "🟢 connecté"), ("Alice", "alice", "🔴 x")])
    assert "SECRET" not in text
    assert "`ap.gg:1`" in text and "<#5>" in text and "<#6>" in text and "items : all" in text
    assert text.index("Alice") < text.index("Bob")  # trié par slot
    assert "Slots liés (2)" in text


def test_format_status_empty_config():
    text = bot.format_status({"host": None}, [])
    assert "non configuré" in text and "Aucun slot lié" in text
