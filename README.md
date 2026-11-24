# Archipelago Discord Bot

Discord bot to use the console of an [Archipelago](https://archipelago.gg) room without leaving Discord: `/hint` with autocomplete, hints sorted by player in a forum (and struck through once found), pings for the players involved, server logs, and room restart.

## How it works

The bot connects to the Archipelago server as a `TextOnly` client, **one connection per slot linked** to a Discord player (the server assigns `!hint` to the connected slot, hence one connection per player). It reads the server messages, the slot's inventory and its list of hints. It never checks a location.

## Installation

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill it in (see below)
.venv/bin/python bot.py
```

### With Docker

```sh
cp .env.example .env   # fill in DISCORD_TOKEN
docker compose up -d --build
docker compose logs -f
```

`data.json` lives in the `bot-data` volume (it survives rebuilds). To back it up: `docker compose cp bot:/data/data.json .`. Only `DISCORD_TOKEN` is required in `.env`; `DATA_FILE` is already set by the image.

### Creating the Discord bot

1. https://discord.com/developers/applications → **New Application** (the account's email address must be verified).
2. **Bot** tab: **Reset Token** (this is the `DISCORD_TOKEN`) and enable **Message Content Intent** (required for `!hint`).
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`, then open the URL to invite the bot.
4. Useful permissions: view channel, send messages, read message history; in the hints forum: create posts, send messages in posts, and manage posts (to reopen an archived post).

### Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Required. The bot token. |
| `GUILD_ID` | Optional. Your Discord server ID (developer mode → right-click the server → copy ID): slash commands appear instantly instead of taking up to 1 h. Leave empty if the bot serves several servers. |
| `ADMIN_ROLE` | Optional. Name of the role allowed to use admin commands (default: `ArchipelagoAdmin`). The role must exist on the server. |
| `DATA_FILE` | Optional. Storage file (default: `data.json`). |

## Setting up on a Discord server (admins)

Restricted to the `@ArchipelagoAdmin` role.

| Command | Effect |
|---|---|
| `/apserver host:… [password] [channel]` | Server address (e.g. `archipelago.gg:38281`), optional room password, and the channel where the bot posts its messages. **First command to run.** |
| `/apforum [forum]` | Posts hints in a forum, one post per slot. With no argument: disables it. |
| `/aplogs [channel] [items]` | Relays server messages to a channel. `items`: important only (default), all, or none. With no channel: disables it. |
| `/aproom [url]` | Restarts the room if it stops (see below). With no argument: disables it. |
| `/reset [confirm]` | Wipes this server's config, all links and hint tracking (disconnects every slot). Requires `confirm:yes`. Irreversible. |
| `/ap !admin …` | Archipelago server administration commands (private reply). |

## Player commands

**Linking your slots.** Each player runs `/link slot:<exact slot name>`; a player can link **several** slots. The last linked slot becomes the *active slot*, used by default.

| Command | Effect |
|---|---|
| `/link slot [password]` | Links a slot (checks that it can be reached). |
| `/unlink [slot]` | Removes a slot (the active one by default). |
| `/slot [slot]` | With no argument: lists your slots (⭐ = active). With one: switches the active slot. |
| `/status` | Shows the server configuration and the connection state of every linked slot (private reply, no passwords). |
| `/hint [item] [slot]` | Requests a hint. With no item: shows your hint points. |
| `/hint_location location [slot]` | Hint for a location in your world. |
| `/items [filter] [filter_regex] [slot]` | Lists the game's items in columns. `filter`: contained text; `filter_regex`: regular expression (case-insensitive, e.g. `^Progressive\|Key$`). |
| `/ap command [slot]` | Any console command (`players`, `status`, `release`, `collect`, `remaining`, `missing`, `help`…). |

`!xxx` commands typed in the configured channel are also forwarded to the console with your active slot (`!hint Sword`, `!players`, `!release`…). `!admin` commands (which contain a password) only go through `/ap`.

**Badges** in autocomplete and `/items`: ✅ item already received · 🔃 already hinted (also for locations in `/hint_location`). Discord limits autocomplete to 25 suggestions: the most relevant come first (start of the name, then all typed words in any order; items neither received nor hinted before the others) and a last line indicates how many are hidden. `/items` shows them all.

## Hints

When a hint is created, the bot posts it with a ping for the player who receives the item and the one who owns it:

```
💡 @slot1 needs Sword at @slot2 in Chest
```

- **Forum** (`/apforum`): one post per slot; the hint is added to the posts of **both** players involved (a single ping). Otherwise, it is posted in the configured channel.
- **Found hint** (the location is checked): the message is struck through and marked ✅. Hints found while the bot was stopped are struck through at restart.
- The reply to `/hint` repeats the hint message with a link to the exact forum message, or "✅ Item already found" if the location is already checked.
- A hint that has already been posted is never reposted (for example "Hint was previously used, no points deducted.").

## Server logs

`/aplogs` relays: item sends (📦), connections (🟢) and disconnections (🔴), goals (🏆), chat (💬), release / collect / countdown. Hints are not relayed (they have their forum), nor are connections from text clients such as this bot. No pings: mentions are disabled. Lines are batched every 3 s to respect Discord's limits; during a large burst, the oldest may be dropped.

## Keeping the room alive (archipelago.gg)

archipelago.gg shuts a room down after 2 h without a **new location checked**. Staying connected does not count as activity. With `/aproom url:https://archipelago.gg/room/<id>`, if all linked slots are disconnected, the bot notifies in the channel and visits the room page, which restarts it (save preserved). Its connections re-establish themselves.

Limitations: the port may change after a restart (rerun `/apserver` if needed); with no linked slot, the bot cannot detect the shutdown and visits the page every 10 min. This timeout exists to save resources on a shared site: it's up to you to judge whether that's acceptable for your game, otherwise host the room yourself.

## Notes

- Only players who ran `/link` are mentioned and detected; others appear by their slot name. A hint where neither the receiver nor the owner is linked is not seen.
- Hints posted before message tracking was added cannot be struck through automatically.
- `data.json` contains passwords in plain text (permissions 600): do not commit it.
- Tests: `pip install -r requirements-dev.txt && python -m pytest`.
- Dependencies: `discord.py`, `websockets`, `python-dotenv`, `regex` (timeout against slow regexes).
