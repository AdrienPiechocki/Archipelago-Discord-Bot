# Archipelago Discord Bot

Bot Discord pour utiliser la console d'une room [Archipelago](https://archipelago.gg) sans quitter Discord : `/hint` avec autocomplétion, hints classés par joueur dans un forum (et barrés une fois validés), pings des joueurs concernés, logs du serveur, relance de la room.

## Fonctionnement

Le bot se connecte au serveur Archipelago comme un client `TextOnly`, **une connexion par slot lié** à un joueur Discord (le serveur attribue `!hint` au slot connecté, d'où une connexion par joueur). Il lit les messages du serveur, l'inventaire du slot et la liste de ses hints. Il ne coche jamais de location.

## Installation

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # puis remplir (voir ci-dessous)
.venv/bin/python bot.py
```

### Créer le bot Discord

1. https://discord.com/developers/applications → **New Application** (l'adresse e-mail du compte doit être vérifiée).
2. Onglet **Bot** : **Reset Token** (c'est le `DISCORD_TOKEN`) et activer **Message Content Intent** (nécessaire pour les `!hint`).
3. **OAuth2 → URL Generator** : scopes `bot` + `applications.commands`, puis ouvrir l'URL pour inviter le bot.
4. Permissions utiles : voir le salon, envoyer des messages, lire l'historique ; dans le forum des hints : créer des posts, envoyer des messages dans les posts et gérer les posts (pour rouvrir un post archivé).

### Configuration (`.env`)

| Variable | Rôle |
|---|---|
| `DISCORD_TOKEN` | Obligatoire. Token du bot. |
| `GUILD_ID` | Optionnel. ID de ton serveur Discord (mode développeur → clic droit sur le serveur → copier l'identifiant) : les commandes slash apparaissent instantanément au lieu de jusqu'à 1 h. À laisser vide si le bot sert plusieurs serveurs. |
| `ADMIN_ROLE` | Optionnel. Nom du rôle autorisé à utiliser les commandes admin (défaut : `ArchipelagoAdmin`). Le rôle doit exister sur le serveur. |
| `DATA_FILE` | Optionnel. Fichier de stockage (défaut : `data.json`). |

## Mise en place sur un serveur Discord (admins)

Réservé au rôle `@ArchipelagoAdmin`.

| Commande | Effet |
|---|---|
| `/apserver host:… [password] [channel]` | Adresse du serveur (ex : `archipelago.gg:38281`), mot de passe de la room éventuel et salon où le bot poste ses messages. **Première commande à lancer.** |
| `/apforum [forum]` | Poste les hints dans un forum, un post par slot. Sans argument : désactive. |
| `/aplogs [channel] [items]` | Relaie les messages du serveur dans un salon. `items` : importants seulement (défaut), tous, ou aucun. Sans salon : désactive. |
| `/aproom [url]` | Relance la room si elle s'arrête (voir plus bas). Sans argument : désactive. |
| `/ap !admin …` | Commandes d'administration du serveur Archipelago (réponse privée). |

## Commandes des joueurs

**Lier ses slots.** Chaque joueur fait `/link slot:<nom exact du slot>` ; un joueur peut en lier **plusieurs**. Le dernier slot lié devient le *slot actif*, utilisé par défaut.

| Commande | Effet |
|---|---|
| `/link slot [password]` | Lie un slot (vérifie qu'il est joignable). |
| `/unlink [slot]` | Retire un slot (par défaut l'actif). |
| `/slot [slot]` | Sans argument : liste tes slots (⭐ = actif). Avec : change de slot actif. |
| `/hint [item] [slot]` | Demande un hint. Sans item : affiche les points de hint. |
| `/hint_location location [slot]` | Hint sur une location de ton monde. |
| `/items [filter] [filter_regex] [slot]` | Liste les items du jeu en colonnes. `filter` : texte contenu ; `filter_regex` : expression régulière (insensible à la casse, ex : `^Progressive\|Key$`). |
| `/ap commande [slot]` | N'importe quelle commande de la console (`players`, `status`, `release`, `collect`, `remaining`, `missing`, `help`…). |

Les commandes `!xxx` tapées dans le salon configuré sont aussi transmises à la console avec ton slot actif (`!hint Sword`, `!players`, `!release`…). Les commandes `!admin` (qui contiennent un mot de passe) ne passent que par `/ap`.

**Pastilles** dans l'autocomplétion et `/items` : ✅ item déjà reçu · 🔃 déjà hinté (aussi pour les locations de `/hint_location`). Discord limite l'autocomplétion à 25 propositions : les plus pertinentes passent en premier (début du nom, puis tous les mots saisis dans n'importe quel ordre ; les items ni reçus ni hintés avant les autres) et une dernière ligne indique combien sont cachés. `/items` les affiche tous.

## Hints

Quand un hint est créé, le bot le poste avec un ping du joueur qui reçoit l'item et de celui qui le possède :

```
💡 @slot1 a besoin de Sword chez @slot2 dans Chest
```

- **Forum** (`/apforum`) : un post par slot, le hint est ajouté dans le post des **deux** joueurs concernés (un seul ping). Sinon, il est posté dans le salon configuré.
- **Hint validé** (la location est cochée) : le message est barré et marqué ✅. Les hints validés pendant que le bot était arrêté sont barrés au redémarrage.
- La réponse à `/hint` reprend le message du hint avec un lien vers le message précis du forum, ou « ✅ Objet déjà trouvé » si la location est déjà cochée.
- Un hint déjà posté n'est jamais reposté (par exemple « Hint was previously used, no points deducted. »).

## Logs du serveur

`/aplogs` relaie : envois d'items (📦), connexions (🟢) et déconnexions (🔴), objectifs (🏆), chat (💬), release / collect / countdown. Les hints ne sont pas relayés (ils ont leur forum), ni les connexions des clients texte comme ce bot. Aucun ping : les mentions sont désactivées. Les lignes sont regroupées par paquets toutes les 3 s pour respecter les limites de Discord ; en cas de grosse rafale, les plus anciennes peuvent être omises.

## Garder la room en vie (archipelago.gg)

archipelago.gg arrête une room après 2 h sans **nouvelle location cochée**. Rester connecté ne compte pas comme une activité. Avec `/aproom url:https://archipelago.gg/room/<id>`, si tous les slots liés sont déconnectés, le bot prévient dans le salon et visite la page de la room, ce qui la relance (sauvegarde conservée). Ses connexions se rétablissent seules.

Limites : le port peut changer après une relance (refaire `/apserver` si besoin) ; sans slot lié, le bot ne peut pas détecter l'arrêt et visite la page toutes les 10 min. Ce timeout sert à économiser les ressources d'un site partagé : à toi de juger si c'est acceptable pour ta partie, sinon héberge la room toi-même.

## Notes

- Seuls les joueurs ayant fait `/link` sont mentionnés et détectés ; les autres apparaissent par leur nom de slot. Un hint dont ni le receveur ni le possesseur n'est lié n'est pas vu.
- Les hints postés avant l'ajout du suivi des messages ne peuvent pas être barrés automatiquement.
- `data.json` contient les mots de passe en clair (permissions 600) : ne pas le committer.
- Dépendances : `discord.py`, `websockets`, `python-dotenv`, `regex` (timeout contre les regex trop lentes).
