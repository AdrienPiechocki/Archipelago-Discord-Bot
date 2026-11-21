"""Stockage JSON : config serveur par guilde + liens joueur Discord <-> slots Archipelago.

Un joueur Discord peut avoir plusieurs slots ; l'un d'eux est le slot "actif" (celui utilisé par défaut).
Format : links[user_id] = {"active": "Alice", "slots": [{"slot": "Alice", "password": None}, ...]}
"""
import json
import os
from pathlib import Path
from typing import Iterator, Optional


class Store:
    def __init__(self, path: str = "data.json"):
        self.path = Path(path)
        self.data: dict = {"guilds": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        self._migrate()

    def _migrate(self):
        """Ancien format : links[user_id] = {"slot": ..., "password": ...}."""
        for g in self.data["guilds"].values():
            for uid, v in list(g["links"].items()):
                if "slots" not in v:
                    g["links"][uid] = {"active": v["slot"],
                                       "slots": [{"slot": v["slot"], "password": v.get("password")}]}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.chmod(tmp, 0o600)  # contient des mots de passe
        tmp.replace(self.path)

    def guild(self, gid: int) -> dict:
        return self.data["guilds"].setdefault(
            str(gid), {"host": None, "password": None, "channel": None, "links": {}})

    def guild_ids(self) -> list[int]:
        return [int(g) for g in self.data["guilds"]]

    def set_server(self, gid: int, host: str, password: Optional[str], channel: Optional[int]):
        self.guild(gid).update(host=host, password=password, channel=channel)
        self._save()

    def room_url_of(self, gid: int) -> Optional[str]:
        return self.guild(gid).get("room_url")

    def set_room_url(self, gid: int, url: Optional[str]):
        self.guild(gid)["room_url"] = url
        self._save()

    def log_config(self, gid: int) -> tuple[Optional[int], str]:
        """(salon des logs ou None, mode d'affichage des envois d'items : all / important / none)"""
        g = self.guild(gid)
        return g.get("log_channel"), g.get("log_items", "important")

    def set_log_config(self, gid: int, channel_id: Optional[int], items: str):
        g = self.guild(gid)
        g["log_channel"], g["log_items"] = channel_id, items
        self._save()

    # --- forum des hints ---------------------------------------------

    def forum_of(self, gid: int) -> Optional[int]:
        return self.guild(gid).get("forum")

    def set_forum(self, gid: int, forum_id: Optional[int]):
        g = self.guild(gid)
        if g.get("forum") != forum_id:
            g["threads"] = {}  # les posts appartiennent à l'ancien forum
        g["forum"] = forum_id
        self._save()

    def thread_of(self, gid: int, slot: str) -> Optional[int]:
        return self.guild(gid).get("threads", {}).get(slot)

    def set_thread(self, gid: int, slot: str, thread_id: Optional[int]):
        threads = self.guild(gid).setdefault("threads", {})
        if thread_id is None:
            threads.pop(slot, None)
        else:
            threads[slot] = thread_id
        self._save()

    # --- messages de hints (pour barrer ceux qui sont validés) -----------

    @staticmethod
    def _hint_key(receiver: str, finder: str, item: str, location: str) -> str:
        return "\x1f".join((receiver, finder, item, location))

    def set_hint_messages(self, gid: int, hint: tuple, refs: list[tuple[int, int]]):
        """refs : [(id du post/salon, id du message), ...] — un message par joueur concerné."""
        self.guild(gid).setdefault("hint_messages", {})[self._hint_key(*hint)] = [list(r) for r in refs]
        self._save()

    def is_hint_found(self, gid: int, hint: tuple) -> bool:
        return self._hint_key(*hint) in self.guild(gid).get("hint_found", [])

    def mark_hint_found(self, gid: int, hint: tuple):
        found = self.guild(gid).setdefault("hint_found", [])
        found.append(self._hint_key(*hint))
        self._save()

    def get_hint_messages(self, gid: int, hint: tuple) -> list[tuple[int, int]]:
        refs = self.guild(gid).get("hint_messages", {}).get(self._hint_key(*hint), [])
        if refs and isinstance(refs[0], int):  # ancien format : un seul [salon, message]
            refs = [refs]
        return [tuple(r) for r in refs]

    # --- liens --------------------------------------------------------

    def entries(self, gid: int) -> Iterator[tuple[int, dict]]:
        """Tous les (user_id, {"slot", "password"}) de la guilde."""
        for uid, link in self.guild(gid)["links"].items():
            for entry in link["slots"]:
                yield int(uid), entry

    def slots_of(self, gid: int, user_id: int) -> list[dict]:
        return self.guild(gid)["links"].get(str(user_id), {}).get("slots", [])

    def active_slot(self, gid: int, user_id: int) -> Optional[str]:
        return self.guild(gid)["links"].get(str(user_id), {}).get("active")

    def get_slot(self, gid: int, user_id: int, slot: Optional[str] = None) -> Optional[dict]:
        """Le slot demandé, ou le slot actif si `slot` est None."""
        slot = slot or self.active_slot(gid, user_id)
        return next((e for e in self.slots_of(gid, user_id) if e["slot"] == slot), None)

    def link(self, gid: int, user_id: int, slot: str, password: Optional[str]):
        link = self.guild(gid)["links"].setdefault(str(user_id), {"active": slot, "slots": []})
        link["slots"] = [e for e in link["slots"] if e["slot"] != slot] + [{"slot": slot, "password": password}]
        link["active"] = slot  # le dernier slot lié devient le slot actif
        self._save()

    def unlink(self, gid: int, user_id: int, slot: str) -> bool:
        links = self.guild(gid)["links"]
        link = links.get(str(user_id))
        if not link or not any(e["slot"] == slot for e in link["slots"]):
            return False
        link["slots"] = [e for e in link["slots"] if e["slot"] != slot]
        if not link["slots"]:
            del links[str(user_id)]
        elif link["active"] == slot:
            link["active"] = link["slots"][0]["slot"]
        self._save()
        return True

    def set_active(self, gid: int, user_id: int, slot: str) -> bool:
        link = self.guild(gid)["links"].get(str(user_id))
        if not link or not any(e["slot"] == slot for e in link["slots"]):
            return False
        link["active"] = slot
        self._save()
        return True

    def owner_of(self, gid: int, slot: str) -> Optional[int]:
        return next((uid for uid, e in self.entries(gid) if e["slot"] == slot), None)
