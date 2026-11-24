import asyncio
import logging
import os
import time
from urllib.parse import urlparse

import aiohttp
import discord
import regex
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from ap_client import APError, APManager, HintInfo, SlotConnection
from storage import Store

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

store = Store(os.getenv("DATA_FILE", "data.json"))
MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)
NO_MENTIONS = discord.AllowedMentions.none()
ADMIN_ROLE = os.getenv("ADMIN_ROLE", "ArchipelagoAdmin")
CONSOLE_COMMANDS = ["help", "players", "status", "release", "collect", "remaining", "missing", "checked",
                    "alias", "getitem", "countdown", "options", "hint", "hint_location", "admin"]


class ArchipelagoBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # pour les commandes "!hint"
        super().__init__(command_prefix="!", intents=intents)
        self.ap = APManager(self.on_hint, self.on_log)
        self.log_queues: dict[int, list[str]] = {}
        self.seen_hints: set[tuple] = set()
        self.forum_lock = asyncio.Lock()  # évite de créer deux posts pour le même slot

    async def setup_hook(self):
        # Reconnecte tous les joueurs déjà liés
        for gid in store.guild_ids():
            if store.guild(gid)["host"]:
                for _, entry in store.entries(gid):
                    self.ap_connect(gid, entry)
        self.keepalive_task = asyncio.create_task(self.keepalive_loop())
        self.log_task = asyncio.create_task(self.log_loop())
        guild = os.getenv("GUILD_ID")
        if guild:  # sync instantané pour un serveur de test
            obj = discord.Object(int(guild))
            self.tree.copy_global_to(guild=obj)
            try:
                await self.tree.sync(guild=obj)
            except discord.Forbidden:
                log.error(
                    "Impossible d'enregistrer les commandes sur le serveur %s (Missing Access) : "
                    "le bot n'y est pas invité avec le scope `applications.commands`, ou GUILD_ID est faux. "
                    "Réinvite le bot via OAuth2 > URL Generator (scopes bot + applications.commands).", guild)
        else:
            await self.tree.sync()

    async def keepalive_loop(self):
        """Surveille la room (archipelago.gg l'arrête après 2 h sans location cochée) et la relance si besoin."""
        await self.wait_until_ready()
        state: dict[int, dict] = {}
        headers = {"User-Agent": "archipelago-discord-bot"}  # sans User-Agent de navigateur : réponse allégée
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as http:
            while True:
                for gid in store.guild_ids():
                    if store.room_url_of(gid):
                        try:
                            await self.keepalive_tick(http, gid, state.setdefault(gid, {}))
                        except Exception:  # noqa: BLE001
                            log.exception("keepalive")
                await asyncio.sleep(60)

    async def keepalive_tick(self, http, gid: int, st: dict):
        conns = [c for (g, _), c in self.ap.conns.items() if g == gid and c.alive]
        if conns and not any(c.ready.is_set() for c in conns):
            return  # premières connexions en cours : on ne sait pas encore
        up = any(c.connected.is_set() for c in conns) if conns else None  # None : aucun slot lié, on ne sait pas
        if up:
            if st.pop("down", False):
                await self.notify(gid, "✅ La room Archipelago est de nouveau en ligne.")
            return
        if up is False and not st.get("down"):
            st["down"] = True
            await self.notify(gid, "⚠️ La room Archipelago ne répond plus (arrêtée pour inactivité ?). "
                                   "Je tente de la relancer…")
        # room arrêtée : visiter sa page la relance. Sans slot lié on ne peut pas savoir, donc on visite plus rarement.
        interval = 120 if up is False else 600
        if time.monotonic() - st.get("last_ping", float("-inf")) >= interval:
            st["last_ping"] = time.monotonic()
            await ping_room(http, store.room_url_of(gid))

    async def on_log(self, gid: int, conn: SlotConnection, pkt: dict, text: str):
        """Message du serveur Archipelago à relayer dans le salon des logs."""
        channel_id, items_mode = store.log_config(gid)
        if not channel_id:
            return
        ptype = pkt.get("type")
        if ptype in ("Join", "Part") and "TextOnly" in pkt.get("tags", []):
            return  # connexions de ce bot (et autres clients texte) : bruit à chaque reconnexion
        if ptype == "ItemSend":
            important = pkt.get("item", {}).get("flags", 0) & 0b111  # progression, utile ou piège
            if items_mode == "none" or (items_mode == "important" and not important):
                return
        # toutes les connexions reçoivent les mêmes messages : une seule (la première, par nom) les relaie
        live = [c for (g, _), c in self.ap.conns.items() if g == gid and c.connected.is_set()]
        if not live or conn is not min(live, key=lambda c: c.slot):
            return
        queue = self.log_queues.setdefault(gid, [])
        queue.append(f"{LOG_ICONS.get(ptype, '▫️')} {discord.utils.escape_markdown(text)}")
        if len(queue) > 2000:  # Discord limite le débit : on abandonne les plus anciens en cas de rafale
            del queue[:len(queue) - 2000]
            queue[0] = "… des messages ont été omis (trop de messages d'un coup)"

    async def log_loop(self):
        """Envoie les logs par paquets : Discord limite à ~5 messages / 5 s par salon."""
        await self.wait_until_ready()
        while True:
            await asyncio.sleep(3)
            for gid in list(self.log_queues):
                try:
                    await self.flush_logs(gid)
                except Exception:  # noqa: BLE001 - une erreur ne doit pas arrêter définitivement les logs
                    log.exception("Erreur d'envoi des logs")
                    self.log_queues[gid].clear()

    async def flush_logs(self, gid: int):
        queue = self.log_queues[gid]
        channel_id, _ = store.log_config(gid)
        channel = self.get_channel(channel_id) if channel_id else None
        if channel is None and queue:
            log.warning("Salon des logs %s introuvable (supprimé ? le bot n'a pas la permission de le voir ?)",
                        channel_id)
        if not queue or channel is None:
            queue.clear()
            return
        for _ in range(3):  # au plus 3 messages par tick, le reste attend
            chunk, size = [], 0
            while queue and size + len(queue[0]) + 1 <= 1900:
                line = queue.pop(0)[:1900]
                chunk.append(line)
                size += len(line) + 1
            if not chunk:
                return
            try:
                await channel.send("\n".join(chunk), allowed_mentions=NO_MENTIONS)
            except discord.DiscordException:
                log.exception("Impossible d'écrire dans le salon des logs")
                queue.clear()
                return

    async def notify(self, gid: int, text: str):
        channel_id = store.guild(gid)["channel"]
        channel = self.get_channel(channel_id) if channel_id else None
        if channel:
            await channel.send(text, allowed_mentions=NO_MENTIONS)

    def ap_connect(self, gid: int, entry: dict) -> SlotConnection:
        g = store.guild(gid)
        return self.ap.ensure(gid, g["host"], entry["slot"], entry["password"] or g["password"])

    def get_conn(self, gid: int, entry: dict) -> SlotConnection:
        return self.ap.get(gid, entry["slot"]) or self.ap_connect(gid, entry)

    def mention(self, gid: int, slot: str) -> str:
        uid = store.owner_of(gid, slot)
        return f"<@{uid}>" if uid else f"**{slot}**"

    async def on_hint(self, gid: int, conn: SlotConnection, h: HintInfo):
        key = (gid, h.receiver, h.finder, h.item, h.location)
        if h.found:
            return await self.mark_found(gid, h, key)
        if key in self.seen_hints:  # plusieurs connexions reçoivent le même hint
            return
        self.seen_hints.add(key)
        if store.get_hint_messages(gid, key[1:]):
            # déjà posté (avant un redémarrage du bot par exemple) : `!hint` sur un hint existant le renvoie
            # ("Hint was previously used, no points deducted.") mais il ne faut pas le reposter
            return
        if store.forum_of(gid):
            # forum configuré : les hints vont là et nulle part ailleurs, même si le forum est introuvable
            try:
                sent = await self.post_in_forum(gid, h)
            except discord.DiscordException:
                log.exception("Impossible de poster le hint dans le forum (supprimé ? permissions ?). "
                              "Hint non posté : refais /apforum ou corrige les permissions.")
                self.seen_hints.discard(key)  # sera retenté si le hint est renvoyé une fois le forum réparé
                return
        else:
            channel_id = store.guild(gid)["channel"]
            channel = self.get_channel(channel_id) if channel_id else None
            if channel is None:
                return
            msg = await channel.send(self.hint_text(gid, h), allowed_mentions=MENTIONS)
            sent = [(channel.id, msg.id)]  # (salon ou post, message), pour pouvoir barrer le hint plus tard
        store.set_hint_messages(gid, key[1:], sent)

    async def mark_found(self, gid: int, h: HintInfo, key: tuple):
        """Le hint a été validé (la location est cochée) : on barre son message."""
        # le serveur renvoie tous les hints validés à chaque connexion : on ignore ceux déjà traités
        if ("found", *key) in self.seen_hints or store.is_hint_found(gid, key[1:]):
            return
        self.seen_hints.add(("found", *key))
        ok = True
        # hint créé avant le suivi, ou jamais posté : liste vide, rien à barrer
        for channel_id, message_id in store.get_hint_messages(gid, key[1:]):
            try:
                channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                if isinstance(channel, discord.Thread) and channel.archived:
                    await channel.edit(archived=False)
                await channel.get_partial_message(message_id).edit(
                    content=self.hint_text(gid, h), allowed_mentions=NO_MENTIONS)
            except discord.NotFound:  # message supprimé à la main : rien à barrer
                pass
            except discord.DiscordException:
                ok = False
                log.exception("Impossible de barrer le hint")
        if ok:
            store.mark_hint_found(gid, key[1:])

    def hint_text(self, gid: int, h: HintInfo) -> str:
        text = (f"{self.mention(gid, h.receiver)} a besoin de **{h.item}** "
                f"chez {self.mention(gid, h.finder)} dans **{h.location}**")
        return f"✅ ~~{text}~~" if h.found else f"💡 {text}"

    def hint_reply(self, gid: int, h: HintInfo, my_slot: str) -> str:
        """Texte du hint, suivi d'un lien vers le message précis du forum (dans le post du joueur si possible)."""
        if h.found:  # `!hint` sur un item dont la location est déjà cochée
            text = (f"✅ **Objet déjà trouvé** : **{h.item}** de {self.mention(gid, h.receiver)} "
                    f"était à **{h.location}** chez {self.mention(gid, h.finder)}.")
        else:
            text = self.hint_text(gid, h)
        refs = store.get_hint_messages(gid, (h.receiver, h.finder, h.item, h.location)) if store.forum_of(gid) else []
        if not refs:
            return text
        mine = store.thread_of(gid, my_slot)
        channel_id, message_id = next((r for r in refs if r[0] == mine), refs[0])
        return f"{text}\n📌 [Voir ce hint dans le forum](https://discord.com/channels/{gid}/{channel_id}/{message_id})"

    async def post_in_forum(self, gid: int, h: HintInfo) -> list[tuple[int, int]]:
        """Poste le hint dans le post de chaque joueur concerné (receveur et possesseur) ; renvoie les messages créés.

        Seul le premier message ping : le second reprend le même texte sans notification."""
        forum = self.get_channel(store.forum_of(gid))
        if not isinstance(forum, discord.ForumChannel):
            raise discord.DiscordException("forum introuvable")
        text = self.hint_text(gid, h)
        refs = []
        async with self.forum_lock:
            for slot in dict.fromkeys((h.receiver, h.finder)):  # sans doublon si le slot se hint lui-même
                mentions = MENTIONS if not refs else NO_MENTIONS
                try:
                    refs.append(await self._post_in_thread(forum, gid, slot, text, mentions))
                except discord.DiscordException:
                    if not refs:
                        raise
                    log.exception("Impossible de poster dans le post de %s", slot)
        return refs

    async def _post_in_thread(self, forum: discord.ForumChannel, gid: int, slot: str, text: str,
                              mentions: discord.AllowedMentions) -> tuple[int, int]:
        thread = None
        thread_id = store.thread_of(gid, slot)
        if thread_id:
            try:
                thread = self.get_channel(thread_id) or await self.fetch_channel(thread_id)
            except discord.NotFound:  # post supprimé à la main : on le recrée
                store.set_thread(gid, slot, None)
        if thread is None:
            created = await forum.create_thread(name=slot[:100], content=text, allowed_mentions=mentions)
            store.set_thread(gid, slot, created.thread.id)
            return created.thread.id, created.message.id
        if thread.archived:
            await thread.edit(archived=False)
        msg = await thread.send(text, allowed_mentions=mentions)
        return thread.id, msg.id


bot = ArchipelagoBot()


async def ping_room(http: aiohttp.ClientSession, url: str) -> bool:
    """Visite la page de la room : si elle est arrêtée, WebHost la relance."""
    try:
        async with http.get(url) as resp:
            log.info("Visite de la page de la room : HTTP %s", resp.status)
            return resp.status < 400
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("Impossible de visiter la page de la room : %s", e)
        return False


LOG_ICONS = {"ItemSend": "📦", "ItemCheat": "🎁", "Join": "🟢", "Part": "🔴", "Goal": "🏆", "Chat": "💬",
             "ServerChat": "📢", "Release": "🔓", "Collect": "📥", "Countdown": "⏳"}


class Out:
    """Même interface pour répondre à un slash command (Interaction) ou à un message `!xxx` (Context)."""

    def __init__(self, source):
        self.src = source
        self.slash = isinstance(source, discord.Interaction)
        self.user = source.user if self.slash else source.author
        self.gid = source.guild_id if self.slash else source.guild.id

    async def defer(self, ephemeral: bool = False):
        if self.slash:
            await self.src.response.defer(ephemeral=ephemeral)
        else:
            await self.src.defer()

    async def send(self, text: str, ephemeral: bool = False):
        if self.slash:
            await self.src.followup.send(text, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS)
        else:
            await self.src.reply(text, allowed_mentions=NO_MENTIONS)

    async def fail(self, text: str):
        """Message d'erreur privé quand c'est possible."""
        if self.slash and not self.src.response.is_done():
            await self.src.response.send_message(text, ephemeral=True)
        else:
            await self.send(text, ephemeral=True)


# --- administration (slash uniquement : ça manipule des mots de passe) ---------------------

def archipelago_admin():
    """Réserve une commande aux membres ayant le rôle ADMIN_ROLE."""
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if isinstance(member, discord.Member) and any(r.name == ADMIN_ROLE for r in member.roles):
            return True
        raise app_commands.CheckFailure(f"Cette commande est réservée au rôle @{ADMIN_ROLE}.")
    return app_commands.check(predicate)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await Out(interaction).fail(f"⛔ {error}")
    else:
        log.error("Erreur de commande", exc_info=error)


@bot.tree.command(name="apserver", description="Configure le serveur Archipelago de ce Discord")
@app_commands.describe(host="ex : archipelago.gg:38281", password="Mot de passe de la room (optionnel)",
                       channel="Salon où poster les hints (défaut : ce salon)")
@app_commands.guild_only()
@archipelago_admin()
async def apserver(interaction: discord.Interaction, host: str, password: str | None = None,
                   channel: discord.TextChannel | None = None):
    gid = interaction.guild_id
    ch = channel or interaction.channel
    store.set_server(gid, host.strip(), password, ch.id)
    for _, entry in store.entries(gid):  # relance les connexions avec la nouvelle config
        bot.ap_connect(gid, entry)
    await interaction.response.send_message(
        f"✅ Serveur : `{host}` — hints postés dans {ch.mention}.", ephemeral=True)


@bot.tree.command(name="aproom", description="Relance automatiquement la room si elle s'arrête (sans URL : désactive)")
@app_commands.describe(url="Page de la room, ex : https://archipelago.gg/room/xxxxxxxx")
@app_commands.guild_only()
@archipelago_admin()
async def aproom(interaction: discord.Interaction, url: str | None = None):
    if url:
        p = urlparse(url.strip())
        if p.scheme not in ("http", "https") or not p.netloc or "/room/" not in p.path:
            return await interaction.response.send_message(
                "❌ L'URL doit être la page de la room, ex : `https://archipelago.gg/room/xxxxxxxx`.", ephemeral=True)
        url = url.strip()
    store.set_room_url(interaction.guild_id, url or None)
    msg = ("✅ Si la room s'arrête, le bot la relancera et préviendra dans le salon." if url
           else "Relance automatique désactivée.")
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="aplogs", description="Relaie les logs du serveur Archipelago dans un salon (sans salon : désactive)")
@app_commands.describe(channel="Salon où poster les logs", items="Quels envois d'items afficher (défaut : importants)")
@app_commands.choices(items=[
    app_commands.Choice(name="Importants seulement (progression, utiles, pièges)", value="important"),
    app_commands.Choice(name="Tous les items", value="all"),
    app_commands.Choice(name="Aucun item", value="none")])
@app_commands.guild_only()
@archipelago_admin()
async def aplogs(interaction: discord.Interaction, channel: discord.TextChannel | None = None,
                 items: str = "important"):
    gid = interaction.guild_id
    if channel:  # message de test : détecte tout de suite un salon où le bot ne peut pas écrire
        try:
            await channel.send("✅ Les logs du serveur Archipelago seront postés ici.", allowed_mentions=NO_MENTIONS)
        except discord.DiscordException as e:
            return await interaction.response.send_message(
                f"❌ Je ne peux pas écrire dans {channel.mention} ({e}). Donne au bot les permissions "
                "« Voir le salon » et « Envoyer des messages » dans ce salon.", ephemeral=True)
    store.set_log_config(gid, channel.id if channel else None, items)
    if not channel:
        return await interaction.response.send_message("Logs désactivés.", ephemeral=True)
    msg = f"✅ Les logs du serveur seront postés dans {channel.mention}."
    if not any(c.connected.is_set() for (g, _), c in bot.ap.conns.items() if g == gid):
        msg += ("\n⚠️ Aucun slot lié n'est connecté au serveur Archipelago pour l'instant : le bot lit le serveur "
                "via ces connexions (`/link`), rien ne sera relayé tant qu'aucune n'est établie.")
    msg += "\nLes logs n'apparaissent que pour les événements qui suivent (nouvel envoi d'item, connexion…)."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="apforum", description="Poste les hints dans un forum, un post par slot (sans forum : désactive)")
@app_commands.describe(forum="Le salon forum où créer un post par slot")
@app_commands.guild_only()
@archipelago_admin()
async def apforum(interaction: discord.Interaction, forum: discord.ForumChannel | None = None):
    store.set_forum(interaction.guild_id, forum.id if forum else None)
    msg = (f"✅ Les hints seront postés dans {forum.mention}, un post par slot." if forum
           else "Forum désactivé : les hints reviennent dans le salon classique.")
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="reset", description="Efface toute la config et tous les liens de ce serveur (irréversible)")
@app_commands.describe(confirm="Mets « oui » pour confirmer")
@app_commands.guild_only()
@archipelago_admin()
async def reset(interaction: discord.Interaction, confirm: str = ""):
    if confirm.strip().lower() not in ("oui", "yes"):
        return await interaction.response.send_message(
            "⚠️ Cela efface la config du serveur, tous les liens et le suivi des hints. "
            "Relance `/reset confirm:oui` pour confirmer.", ephemeral=True)
    gid = interaction.guild_id
    for g, slot in [k for k in bot.ap.conns if k[0] == gid]:
        await bot.ap.drop(g, slot)
    store.reset_guild(gid)
    bot.seen_hints = {k for k in bot.seen_hints if k[0] != gid and not (k[0] == "found" and k[1] == gid)}
    bot.log_queues.pop(gid, None)
    await interaction.response.send_message(
        "🗑️ Données du serveur effacées. Refais `/apserver` pour recommencer.", ephemeral=True)


# --- gestion des slots ----------------------------------------------------------------------

async def slot_autocomplete(interaction: discord.Interaction, current: str):
    entries = store.slots_of(interaction.guild_id, interaction.user.id)
    return [app_commands.Choice(name=e["slot"][:100], value=e["slot"])
            for e in entries if current.lower() in e["slot"].lower()][:25]


@bot.tree.command(name="link", description="Associe ton compte Discord à un slot Archipelago (tu peux en lier plusieurs)")
@app_commands.describe(slot="Nom exact du slot", password="Mot de passe du slot si différent de celui de la room")
@app_commands.guild_only()
async def link(interaction: discord.Interaction, slot: str, password: str | None = None):
    gid, uid = interaction.guild_id, interaction.user.id
    g = store.guild(gid)
    if not g["host"]:
        return await interaction.response.send_message("Un admin doit d'abord faire `/apserver`.", ephemeral=True)
    owner = store.owner_of(gid, slot)
    if owner and owner != uid:
        return await interaction.response.send_message(f"Ce slot est déjà lié à <@{owner}>.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)

    conn = bot.ap.ensure(gid, g["host"], slot, password or g["password"])
    try:
        await conn.wait_ready()
    except APError as e:
        await bot.ap.drop(gid, slot)
        return await interaction.followup.send(f"❌ {e}", ephemeral=True)
    store.link(gid, uid, slot, password)
    n = len(store.slots_of(gid, uid))
    extra = f" Tu as {n} slots liés, `/slot` pour changer de slot actif." if n > 1 else ""
    await interaction.followup.send(f"✅ Lié au slot **{slot}** (slot actif).{extra}", ephemeral=True)


@bot.tree.command(name="unlink", description="Retire le lien avec un de tes slots (par défaut : le slot actif)")
@app_commands.describe(slot="Slot à délier")
@app_commands.autocomplete(slot=slot_autocomplete)
@app_commands.guild_only()
async def unlink(interaction: discord.Interaction, slot: str | None = None):
    gid, uid = interaction.guild_id, interaction.user.id
    entry = store.get_slot(gid, uid, slot)
    if not entry:
        return await interaction.response.send_message("Slot introuvable parmi tes liens.", ephemeral=True)
    store.unlink(gid, uid, entry["slot"])
    await bot.ap.drop(gid, entry["slot"])
    await interaction.response.send_message(f"Slot **{entry['slot']}** délié.", ephemeral=True)


@bot.tree.command(name="slot", description="Affiche tes slots liés, ou change ton slot actif")
@app_commands.describe(slot="Slot à rendre actif (vide : afficher la liste)")
@app_commands.autocomplete(slot=slot_autocomplete)
@app_commands.guild_only()
async def slot_cmd(interaction: discord.Interaction, slot: str | None = None):
    gid, uid = interaction.guild_id, interaction.user.id
    if slot is None:
        entries = store.slots_of(gid, uid)
        if not entries:
            return await interaction.response.send_message("Aucun slot lié, fais `/link`.", ephemeral=True)
        active = store.active_slot(gid, uid)
        lines = [f"{'⭐' if e['slot'] == active else '▫️'} {discord.utils.escape_markdown(e['slot'])}" for e in entries]
        return await interaction.response.send_message("\n".join(lines), ephemeral=True)
    if not store.set_active(gid, uid, slot):
        return await interaction.response.send_message("Ce slot n'est pas dans tes liens.", ephemeral=True)
    await interaction.response.send_message(f"Slot actif : **{slot}**.", ephemeral=True)


def conn_state(conn: SlotConnection | None) -> str:
    """État lisible d'une connexion de slot."""
    if conn is None:
        return "⚪ non démarrée"
    if conn.connected.is_set():
        return "🟢 connecté"
    if conn.error:
        return f"🔴 {conn.error}"
    return "🟡 connexion en cours" if conn.alive else "🔴 abandonnée"


def format_status(cfg: dict, rows: list[tuple[str, str, str]]) -> str:
    """cfg : config de la guilde ; rows : [(slot, nom du joueur Discord, état), ...]. Aucun mot de passe affiché."""
    def chan(cid):
        return f"<#{cid}>" if cid else "—"
    log_channel = cfg.get("log_channel")
    lines = ["**Configuration**",
             f"Serveur : `{cfg['host']}`" if cfg.get("host") else "Serveur : non configuré (`/apserver`)",
             f"Salon des commandes : {chan(cfg.get('channel'))}",
             f"Forum des hints : {chan(cfg.get('forum'))}",
             f"Logs : {chan(log_channel)}" + (f" (items : {cfg.get('log_items', 'important')})" if log_channel else ""),
             f"Room : {cfg['room_url']}" if cfg.get("room_url") else "Room : relance auto désactivée",
             "", f"**Slots liés ({len(rows)})**"]
    lines += [f"{state} — **{slot}** ({user})" for slot, user, state in sorted(rows)] or ["Aucun slot lié (`/link`)."]
    return "\n".join(lines)


@bot.tree.command(name="status", description="Affiche la configuration du bot et l'état des connexions")
@app_commands.guild_only()
async def status(interaction: discord.Interaction):
    gid = interaction.guild_id
    rows = []
    for uid, entry in store.entries(gid):
        member = interaction.guild.get_member(uid)
        rows.append((entry["slot"], member.display_name if member else str(uid),
                     conn_state(bot.ap.get(gid, entry["slot"]))))
    await interaction.response.send_message(format_status(store.guild(gid), rows)[:2000], ephemeral=True,
                                            allowed_mentions=NO_MENTIONS)


# --- console Archipelago --------------------------------------------------------------------

async def run_console(out: Out, command: str, slot: str | None = None):
    """Envoie `command` à la console Archipelago au nom du joueur (slot actif par défaut)."""
    entry = store.get_slot(out.gid, out.user.id, slot)
    if not entry:
        return await out.fail("Slot introuvable : fais d'abord `/link <ton slot>`." if not slot
                              else f"Tu n'es pas lié au slot **{slot}**.")
    secret = command.lower().startswith("!admin")  # contient un mot de passe : slash + réponse privée seulement
    if secret and not out.slash:
        return await out.fail("⚠️ Utilise `/ap` pour les commandes `!admin` (le mot de passe serait visible ici).")
    conn = bot.get_conn(out.gid, entry)
    await out.defer(ephemeral=secret)
    try:
        lines, hints = await conn.say(command, wait=3.0 if len(command) > 1 else 2.0)
    except APError as e:
        return await out.send(f"❌ {e}", ephemeral=secret)
    # hints créés par la commande (message habituel + lien vers le post du forum), puis réponses du serveur
    text = "\n".join([bot.hint_reply(out.gid, h, entry["slot"]) for h in hints] + lines) or "Commande envoyée."
    for i in range(0, min(len(text), 5 * 1900), 1900):
        await out.send(text[i:i + 1900], ephemeral=secret)


def autocomplete_conn(interaction: discord.Interaction) -> SlotConnection | None:
    """Connexion du slot choisi (ou actif) si elle est prête : sert à proposer les noms du bon jeu."""
    slot = getattr(interaction.namespace, "slot", None)
    entry = store.get_slot(interaction.guild_id, interaction.user.id, slot)
    conn = bot.ap.get(interaction.guild_id, entry["slot"]) if entry else None
    return conn if conn and conn.connected.is_set() else None


RECEIVED, HINTED = "✅", "🔃"


def compact_marks(received: bool, hinted: bool) -> str:
    """Pastilles devant un nom dans l'autocomplétion : ✅ déjà reçu, 🔃 déjà hinté."""
    marks = (RECEIVED if received else "") + (HINTED if hinted else "")
    return marks + " " if marks else ""


def best_matches(names: list[str], current: str, mark=lambda n: "", limit: int = 25) -> list[app_commands.Choice]:
    """Discord n'affiche jamais plus de 25 propositions : on met les plus utiles en premier.

    Ordre : noms qui commencent par la saisie, puis noms qui contiennent tous les mots saisis (dans n'importe
    quel ordre) ; à égalité, ceux qui ne sont ni reçus ni hintés d'abord. Si tout ne tient pas, la dernière
    ligne indique combien de résultats sont cachés."""
    c = current.lower().strip()
    words = c.split()
    ranked = []
    for n in names:
        if len(n) > 100:  # limite Discord sur la valeur d'un choix
            continue
        low = n.lower()
        if low.startswith(c):
            rank = 0
        elif all(w in low for w in words):
            rank = 1
        else:
            continue
        ranked.append((rank, bool(mark(n)), n))
    ranked.sort(key=lambda r: (r[0], r[1]))  # tri stable : l'ordre alphabétique est conservé à égalité
    if len(ranked) <= limit:
        shown, hidden = ranked, 0
    else:
        shown, hidden = ranked[:limit - 1], len(ranked) - (limit - 1)
    choices = [app_commands.Choice(name=(mark(n) + n)[:100], value=n) for _, _, n in shown]
    if hidden:  # choisir cette ligne garde simplement ce qui est déjà tapé
        choices.append(app_commands.Choice(name=f"… et {hidden} autres, continue à taper pour préciser"[:100],
                                           value=current[:100]))
    return choices


async def item_autocomplete(interaction: discord.Interaction, current: str):
    conn = autocomplete_conn(interaction)
    if not conn:
        return []
    received, hinted = conn.received_item_names, conn.hinted_item_names
    return best_matches(conn.dp.item_names(conn.game), current,
                        lambda n: compact_marks(n in received, n in hinted))


async def location_autocomplete(interaction: discord.Interaction, current: str):
    conn = autocomplete_conn(interaction)
    if not conn:
        return []
    hinted = conn.hinted_location_names
    return best_matches(conn.dp.location_names(conn.game), current, lambda n: compact_marks(False, n in hinted))


async def command_autocomplete(interaction: discord.Interaction, current: str):
    c = current.lstrip("!").lower()
    if " " in c:
        return []
    return [app_commands.Choice(name=x, value=x) for x in CONSOLE_COMMANDS if x.startswith(c)][:25]


@bot.tree.command(name="hint", description="Demande un hint pour un item (sans argument : points de hint)")
@app_commands.describe(item="Nom de l'item", slot="Slot à utiliser (défaut : ton slot actif)")
@app_commands.autocomplete(item=item_autocomplete, slot=slot_autocomplete)
@app_commands.guild_only()
async def hint(interaction: discord.Interaction, item: str = "", slot: str | None = None):
    await run_console(Out(interaction), f"!hint {item}".strip(), slot)


@bot.tree.command(name="hint_location", description="Demande un hint pour une location")
@app_commands.describe(location="Nom de la location", slot="Slot à utiliser (défaut : ton slot actif)")
@app_commands.autocomplete(location=location_autocomplete, slot=slot_autocomplete)
@app_commands.guild_only()
async def hint_location(interaction: discord.Interaction, location: str, slot: str | None = None):
    await run_console(Out(interaction), f"!hint_location {location}", slot)


@bot.tree.command(name="ap", description="Envoie n'importe quelle commande à la console Archipelago")
@app_commands.describe(command="ex : players, status, release, collect, remaining, missing, help...",
                       slot="Slot à utiliser (défaut : ton slot actif)")
@app_commands.autocomplete(command=command_autocomplete, slot=slot_autocomplete)
@app_commands.guild_only()
async def ap(interaction: discord.Interaction, command: str, slot: str | None = None):
    await run_console(Out(interaction), command if command.startswith("!") else f"!{command}", slot)


@bot.tree.command(name="items", description="Liste les items possibles dans le jeu d'un de tes slots")
@app_commands.describe(filter="Ne garder que les items contenant ce texte",
                       filter_regex="Ne garder que les items qui matchent cette regex (insensible à la casse), ex : ^Progressive|Key$",
                       slot="Slot à utiliser (défaut : ton slot actif)")
@app_commands.autocomplete(slot=slot_autocomplete)
@app_commands.guild_only()
async def items(interaction: discord.Interaction, filter: str | None = None, filter_regex: str | None = None,
                slot: str | None = None):
    out = Out(interaction)
    entry = store.get_slot(out.gid, out.user.id, slot)
    if not entry:
        return await out.fail("Slot introuvable : fais d'abord `/link <ton slot>`.")
    conn = bot.get_conn(out.gid, entry)
    await out.defer(ephemeral=True)
    try:
        await conn.wait_ready()
    except APError as e:
        return await out.send(f"❌ {e}", ephemeral=True)
    names = conn.dp.item_names(conn.game)
    if filter:
        names = [n for n in names if filter.lower() in n.lower()]
    if filter_regex:
        try:
            names = apply_regex_filter(names, filter_regex)
        except ValueError as e:
            return await out.send(f"❌ {e}", ephemeral=True)
    if not names:
        return await out.send("Aucun item trouvé.", ephemeral=True)

    received, hinted = conn.received_item_names, conn.hinted_item_names
    got = sum(n in received for n in names)
    n_hinted = sum(n in hinted for n in names)
    header = (f"**{conn.game}** — {len(names)} item(s)" + (f" contenant « {filter} »" if filter else "")
              + (f" · regex `{filter_regex[:50]}`" if filter_regex else "") + f" · {got} reçu(s) {RECEIVED} · {n_hinted} hint(s) {HINTED}")
    # deux colonnes de pastilles de largeur fixe pour garder les noms alignés
    rows = format_columns([(RECEIVED if n in received else "  ") + (HINTED if n in hinted else "  ") + " "
                           + n.replace("`", "'") for n in names])
    max_messages = 8
    pages, page, size = [], [], 0
    for row in rows:
        if size + len(row) + 1 > 1800:
            pages.append(page)
            page, size = [], 0
        page.append(row)
        size += len(row) + 1
    pages.append(page)
    truncated = len(pages) > max_messages
    for i, rows_in_page in enumerate(pages[:max_messages]):
        text = "```\n" + "\n".join(rows_in_page) + "\n```"
        if i == 0:
            text = header + "\n" + text
        if truncated and i == max_messages - 1:
            text += "… liste tronquée, affine avec `filter`."
        await out.send(text, ephemeral=True)


def apply_regex_filter(names: list[str], pattern: str) -> list[str]:
    """Filtre par regex. Le module `regex` permet un timeout, pour qu'un motif malveillant (ReDoS) ne bloque pas le bot."""
    if len(pattern) > 200:
        raise ValueError("Regex trop longue (200 caractères max).")
    try:
        pat = regex.compile(pattern, regex.IGNORECASE)
    except regex.error as e:
        raise ValueError(f"Regex invalide : {e}")
    deadline = time.monotonic() + 2
    out = []
    try:
        for n in names:
            if time.monotonic() > deadline:
                raise TimeoutError
            if pat.search(n, timeout=0.05):
                out.append(n)
    except TimeoutError:
        raise ValueError("Regex trop lente à évaluer, simplifie-la.")
    return out


def format_columns(names: list[str], width: int = 60, max_cols: int = 3) -> list[str]:
    """Range les noms en colonnes alignées (ordre alphabétique de haut en bas, puis de gauche à droite)."""
    col_w = max(len(n) for n in names) + 3
    cols = max(1, min(max_cols, width // col_w))
    n_rows = -(-len(names) // cols)  # division entière par excès
    rows = []
    for r in range(n_rows):
        cells = names[r::n_rows][:cols]
        rows.append("".join(c.ljust(col_w) for c in cells).rstrip())
    return rows


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    """Toute commande `!xxx` tapée dans le salon configuré est transmise à la console Archipelago."""
    if isinstance(error, commands.CommandNotFound):
        if ctx.guild and store.guild(ctx.guild.id)["channel"] == ctx.channel.id:
            command = ctx.message.content.strip()
            if command.lower().startswith("!ap "):  # `!ap players` == `!players`
                command = "!" + command[4:].lstrip("! ")
            await run_console(Out(ctx), command)
    else:
        log.error("Erreur de commande", exc_info=error)


if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"])
