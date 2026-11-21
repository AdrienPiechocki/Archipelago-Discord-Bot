"""Client Archipelago minimal (TextOnly) : une connexion par slot lié à un joueur Discord."""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import websockets

log = logging.getLogger("ap")


class APError(Exception):
    """Erreur fatale (refus de connexion, serveur injoignable...) : inutile de réessayer en boucle."""


@dataclass
class HintInfo:
    receiver: str  # nom du slot qui reçoit l'item
    finder: str  # nom du slot dans lequel se trouve l'item
    item: str
    location: str
    found: bool


class DataPackage:
    """Noms d'items / locations par jeu, partagé par toutes les connexions d'un même serveur."""

    def __init__(self):
        self.items: dict[str, dict[int, str]] = {}
        self.locations: dict[str, dict[int, str]] = {}

    def has(self, game: str) -> bool:
        return game in self.items

    def update(self, games: dict):
        for game, d in games.items():
            self.items[game] = {v: k for k, v in d.get("item_name_to_id", {}).items()}
            self.locations[game] = {v: k for k, v in d.get("location_name_to_id", {}).items()}

    def item_names(self, game: Optional[str]) -> list[str]:
        return sorted(set(self.items.get(game, {}).values()), key=str.lower)

    def location_names(self, game: Optional[str]) -> list[str]:
        return sorted(set(self.locations.get(game, {}).values()), key=str.lower)

    def item(self, game: Optional[str], i: int) -> str:
        return self.items.get(game, {}).get(i, f"Item {i}")

    def location(self, game: Optional[str], i: int) -> str:
        return self.locations.get(game, {}).get(i, f"Location {i}")


HintCallback = Callable[["SlotConnection", HintInfo], Awaitable[None]]
LogCallback = Callable[["SlotConnection", dict, str], Awaitable[None]]  # (connexion, paquet PrintJSON, texte)
# Types de messages du serveur relayés comme "logs" (les hints ont leur propre circuit)
LOG_TYPES = {"ItemSend", "ItemCheat", "Join", "Part", "Goal", "Chat", "ServerChat", "Release", "Collect", "Countdown"}


class SlotConnection:
    def __init__(self, host: str, slot: str, password: Optional[str], datapackage: DataPackage,
                 on_hint: HintCallback, on_log: Optional[LogCallback] = None):
        self.host = host
        self.slot = slot
        self.password = password or None
        self.dp = datapackage
        self.on_hint = on_hint
        self.on_log = on_log

        self.ws = None
        self.ready = asyncio.Event()  # posé dès qu'on sait si la connexion marche ou non
        self.connected = asyncio.Event()
        self.error: Optional[str] = None
        self.team = 0
        self.my_slot: Optional[int] = None
        self._hints_key: Optional[str] = None
        self._hinted_items: set[int] = set()  # items (de ce slot) pour lesquels un hint existe
        self._hinted_locations: set[int] = set()  # locations (de ce slot) pour lesquelles un hint existe
        self._received: list[int] = []  # ids des items reçus par ce slot, dans l'ordre
        self.slot_names: dict[int, str] = {}
        self.slot_aliases: dict[int, str] = {}
        self.slot_game: dict[int, str] = {}
        self._version = {"major": 0, "minor": 6, "build": 0}
        self._capture: Optional[list[str]] = None
        self._hint_capture: Optional[list[HintInfo]] = None
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None

    # --- cycle de vie -------------------------------------------------

    @property
    def alive(self) -> bool:
        """Faux si la connexion a abandonné définitivement (mauvais nom de slot, mauvais mot de passe...)."""
        return self._task is not None and not self._task.done()

    def start(self):
        self._task = asyncio.create_task(self._run(), name=f"ap-{self.slot}")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.ws:
            await self.ws.close()

    async def wait_ready(self, timeout: float = 15):
        try:
            await asyncio.wait_for(self.ready.wait(), timeout)
        except asyncio.TimeoutError:
            raise APError("Le serveur ne répond pas.")
        if self.error:
            raise APError(self.error)

    # --- connexion ----------------------------------------------------

    async def _open(self):
        if self.host.startswith(("ws://", "wss://")):
            urls = [self.host]
        else:  # comme le client officiel : wss d'abord, puis ws
            urls = [f"wss://{self.host}", f"ws://{self.host}"]
        last: Exception = APError("connexion impossible")
        for url in urls:
            try:
                return await websockets.connect(url, max_size=2**25, open_timeout=10)
            except Exception as e:  # noqa: BLE001
                last = e
        raise last

    async def _run(self):
        backoff = 2
        while True:
            try:
                await self._session()
                backoff = 2
            except asyncio.CancelledError:
                raise
            except APError as e:  # refus définitif (mauvais nom, mauvais mot de passe...)
                self.error = str(e)
                self.ready.set()
                log.warning("[%s] %s", self.slot, e)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] connexion perdue : %s", self.slot, e)
                if not self.ready.is_set():
                    self.error = f"Impossible de joindre {self.host} ({e})"
                    self.ready.set()
            self.connected.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

    async def _session(self):
        async with await self._open() as ws:
            self.ws = ws
            async for raw in ws:
                for pkt in json.loads(raw):
                    await self._handle(pkt)

    async def _send(self, *packets: dict):
        await self.ws.send(json.dumps(list(packets)))

    async def _connect_packet(self):
        await self._send({
            "cmd": "Connect", "password": self.password, "name": self.slot,
            "version": {**self._version, "class": "Version"},
            "tags": ["TextOnly"], "items_handling": 0b111, "uuid": uuid.uuid4().hex,
            "game": "", "slot_data": False,
        })

    # --- paquets ------------------------------------------------------

    async def _handle(self, pkt: dict):
        cmd = pkt.get("cmd")
        if cmd == "RoomInfo":
            self._version = {k: pkt["version"][k] for k in ("major", "minor", "build")}
            missing = [g for g in pkt.get("games", []) if not self.dp.has(g)]
            if missing:
                await self._send({"cmd": "GetDataPackage", "games": missing})
            else:
                await self._connect_packet()
        elif cmd == "DataPackage":
            self.dp.update(pkt["data"]["games"])
            await self._connect_packet()
        elif cmd == "Connected":
            self.team = pkt["team"]
            self.my_slot = pkt["slot"]
            self._received = []  # le serveur renvoie tout l'inventaire après Connected
            self._hinted_items, self._hinted_locations = set(), set()
            # hints existants (y compris ceux créés hors Discord) + notifications des changements
            self._hints_key = f"_read_hints_{self.team}_{self.my_slot}"
            await self._send({"cmd": "Get", "keys": [self._hints_key]},
                             {"cmd": "SetNotify", "keys": [self._hints_key]})
            for p in pkt["players"]:
                if p["team"] == self.team:
                    self.slot_names[p["slot"]] = p["name"]
                    self.slot_aliases[p["slot"]] = p.get("alias") or p["name"]
            self.slot_game = {int(s): i["game"] for s, i in pkt["slot_info"].items()}
            self.error = None
            self.connected.set()
            self.ready.set()
            log.info("[%s] connecté à %s", self.slot, self.host)
        elif cmd == "ConnectionRefused":
            raise APError("Connexion refusée : " + ", ".join(pkt.get("errors", ["?"])))
        elif cmd == "Retrieved":
            if self._hints_key in pkt.get("keys", {}):
                await self._set_hints(pkt["keys"][self._hints_key])
        elif cmd == "SetReply":
            if pkt.get("key") == self._hints_key:
                await self._set_hints(pkt.get("value"))
        elif cmd == "ReceivedItems":
            if pkt["index"] == 0:
                self._received = [i["item"] for i in pkt["items"]]
            elif pkt["index"] == len(self._received):
                self._received += [i["item"] for i in pkt["items"]]
            else:  # on a raté des paquets : on redemande tout
                await self._send({"cmd": "Sync"})
        elif cmd == "PrintJSON":
            await self._on_print(pkt)

    def _hint_info(self, recv: int, finder: int, item_id: int, location_id: int, found: bool) -> HintInfo:
        return HintInfo(
            receiver=self.slot_names.get(recv, f"Slot {recv}"),
            finder=self.slot_names.get(finder, f"Slot {finder}"),
            item=self.dp.item(self.slot_game.get(recv), item_id),
            location=self.dp.location(self.slot_game.get(finder), location_id),
            found=found,
        )

    async def _set_hints(self, hints):
        """Liste complète des hints de ce slot, envoyée par le serveur à la connexion puis à chaque changement.

        C'est la seule façon d'apprendre qu'un hint est validé : quand une location est cochée, le serveur ne
        renvoie pas de message "Hint" mais met à jour cette liste."""
        items, locations, found = set(), set(), []
        for h in hints or []:
            if isinstance(h, dict):
                recv, finder, loc, item = h["receiving_player"], h["finding_player"], h["location"], h["item"]
                is_found = bool(h.get("found"))
            else:
                recv, finder, loc, item, is_found = h[0], h[1], h[2], h[3], bool(h[4])
            if is_found:
                found.append(self._hint_info(recv, finder, item, loc, True))
            if recv == self.my_slot:  # `!hint <item>` porte sur les items que ce slot reçoit
                items.add(item)
            if finder == self.my_slot:  # `!hint_location <loc>` porte sur les locations de ce slot
                locations.add(loc)
        self._hinted_items, self._hinted_locations = items, locations
        for info in found:
            try:
                await self.on_hint(self, info)
            except Exception:  # noqa: BLE001
                log.exception("on_hint a échoué")

    @property
    def hinted_item_names(self) -> set[str]:
        return {self.dp.item(self.game, i) for i in self._hinted_items}

    @property
    def hinted_location_names(self) -> set[str]:
        return {self.dp.location(self.game, i) for i in self._hinted_locations}

    @property
    def received_item_names(self) -> set[str]:
        """Noms des items déjà reçus par ce slot (au moins une fois)."""
        return {self.dp.item(self.game, i) for i in set(self._received)}

    @property
    def game(self) -> Optional[str]:
        return self.slot_game.get(self.my_slot)

    def render(self, parts: list[dict]) -> str:
        out = []
        for p in parts:
            t, text = p.get("type"), p.get("text", "")
            try:
                if t == "player_id":
                    out.append(self.slot_aliases.get(int(text), text))
                elif t == "item_id":
                    out.append(self.dp.item(self.slot_game.get(p.get("player")), int(text)))
                elif t == "location_id":
                    out.append(self.dp.location(self.slot_game.get(p.get("player")), int(text)))
                else:
                    out.append(text)
            except ValueError:
                out.append(text)
        return "".join(out)

    async def _on_print(self, pkt: dict):
        ptype = pkt.get("type")
        if self.on_log and ptype in LOG_TYPES:
            try:
                await self.on_log(self, pkt, self.render(pkt["data"]))
            except Exception:  # noqa: BLE001
                log.exception("on_log a échoué")
        # Sorties de commandes ; Release/Collect/Countdown sont diffusées à tous mais sont aussi la réponse
        if ptype in ("CommandResult", "Countdown", "Release", "Collect") and self._capture is not None:
            self._capture.append(self.render(pkt["data"]))
        elif ptype == "Hint":
            item = pkt["item"]
            recv, finder = pkt["receiving"], item["player"]
            info = self._hint_info(recv, finder, item["item"], item["location"], bool(pkt.get("found")))
            if self._hint_capture is not None:  # y compris les hints d'items déjà trouvés (found=True)
                self._hint_capture.append(info)
            try:
                await self.on_hint(self, info)
            except Exception:  # noqa: BLE001
                log.exception("on_hint a échoué")

    # --- API ----------------------------------------------------------

    async def say(self, text: str, wait: float = 2.0) -> tuple[list[str], list[HintInfo]]:
        """Envoie `text` comme si le joueur le tapait dans la console.

        Renvoie (réponses texte du serveur, hints créés pendant ce temps)."""
        await self.wait_ready()
        if not self.connected.is_set():
            raise APError("Connexion au serveur Archipelago perdue, reconnexion en cours...")
        async with self._lock:
            self._capture, self._hint_capture = [], []
            try:
                await self._send({"cmd": "Say", "text": text})
                await asyncio.sleep(wait)
                return self._capture, self._hint_capture
            finally:
                self._capture = self._hint_capture = None


class APManager:
    """Gère les connexions, indexées par (guild_id, slot)."""

    def __init__(self, on_hint: Callable[[int, SlotConnection, HintInfo], Awaitable[None]],
                 on_log: Optional[Callable[[int, SlotConnection, dict, str], Awaitable[None]]] = None):
        self._on_hint = on_hint
        self._on_log = on_log
        self.conns: dict[tuple[int, str], SlotConnection] = {}
        self.datapackages: dict[str, DataPackage] = {}

    def ensure(self, guild_id: int, host: str, slot: str, password: Optional[str]) -> SlotConnection:
        key = (guild_id, slot)
        old = self.conns.get(key)
        if old and old.host == host and old.password == (password or None) and not old.error:
            return old
        if old:
            asyncio.create_task(old.stop())

        async def cb(conn: SlotConnection, info: HintInfo):
            await self._on_hint(guild_id, conn, info)

        async def log_cb(conn: SlotConnection, pkt: dict, text: str):
            if self._on_log:
                await self._on_log(guild_id, conn, pkt, text)

        conn = SlotConnection(host, slot, password, self.datapackages.setdefault(host, DataPackage()), cb, log_cb)
        self.conns[key] = conn
        conn.start()
        return conn

    def get(self, guild_id: int, slot: str) -> Optional[SlotConnection]:
        return self.conns.get((guild_id, slot))

    async def drop(self, guild_id: int, slot: str):
        conn = self.conns.pop((guild_id, slot), None)
        if conn:
            await conn.stop()
