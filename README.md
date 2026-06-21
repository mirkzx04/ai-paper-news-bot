# AI Paper News Bot

Telegram bot that surfaces new AI research relevant to *you* — from arXiv
(Bluesky and Hugging Face Papers planned) — ranked by **keywords + followed
authors + embedding similarity** to your interest profile. Sends instant alerts
for top matches and a digest for the rest, and **learns from your 👍/👎**.

Hosted free on **GitHub Actions** (public repo); runtime state (seen ids,
profile overlay, preference dataset, logs) is tarred into a **private GitHub
gist** between cron runs. Recommender design borrows from
[Scholar Inbox](https://arxiv.org/abs/2504.08385) (SPECTER2 + active-learning
cold-start) and the Telegram feedback loop from
[arXiv_recbot](https://github.com/yuandong-tian/arXiv_recbot).

## Architecture (Clean Architecture / SOLID)

```
src/
  domain/     Item, UserProfile               # entities, zero dependencies
  sources/    Source(ABC) -> ArxivSource      # + Bluesky/HF (planned)
  embedding/  SpecterEmbedder, profile + feedback vectors
  scoring/    Scorer(ABC) -> Keyword, Author, Embedding -> CombinedScorer
  store/      SqliteStore (seen/meta), ProfileStore, PreferenceDataset,
              SentItemsStore                  # all file-based, gist-synced
  notify/     Notifier(ABC) -> Console, Telegram
  flow/       ProfileFlow                     # /creare_profile onboarding
  commands/   /add_*, /remove_*, /report dispatcher
  telegram_poller.py  incoming commands + 👍/👎 callbacks
  pipeline.py fetch -> dedup -> score -> route -> notify -> persist
main.py       manual dependency injection wiring
config/profile.yaml   keywords / authors / seed papers / thresholds / feedback
```

Scoring fuses signals as a saturating weighted sum `total = min(1, Σ wᵢ·sᵢ)`;
routing (alert vs digest) is decided in the pipeline — a followed author always
alerts.

## Feedback loop (👍/👎)

Every paper is sent with inline **👍 / 👎** buttons (native message reactions
aren't delivered to bots in 1:1 chats, so inline buttons it is). A tap logs a
`vote` event in the gist-synced preference dataset; re-tapping the same emoji
clears the vote. Votes act **only on the embedding channel**, leaving declared
keywords/authors untouched:

- **👍** → a dynamic *positive seed* (a new center of interest).
- **👎** → a soft, margined *penalty* (`s = clamp01(pos − λ·neg)`), never a deletion.

The loop is deliberately **balanced**: feedback is weighted below the onboarding
seeds, time-decayed, capped, and cold-started, so a handful of votes barely moves
the ranking. With no votes, scoring is identical to the embedding-only baseline.
Shown-but-unvoted papers are logged as `impression`s for evaluation only — they
never feed scoring. All tunable under `feedback:` in `config/profile.yaml`.

## Multi-user delivery

The bot is fully multi-user. Anyone who messages it is recorded in a **delivery
registry** (`data/user_registry.json`) and receives their *own* per-user digest:
their own profile overlay, their own 👍/👎 feedback vectors, their own
`/set_frequency` cadence, and their own independent "seen" set. Delivery fans out
over all active users (`main.py --notifier telegram --all-users`, used by the cron
workflow); a single shared SPECTER embedder + a shared rate limiter keep the
fan-out within Telegram's per-chat (~1 msg/s) and global (~30 msg/s) limits and
embed each candidate paper once. One user's failure never aborts the run, and a
user who blocks the bot (HTTP 403) is automatically marked `blocked` and skipped.

## Privacy policy

Your privacy is a first-class design constraint, because your preferences are used
to build a training dataset.

- **Anonymous identity only.** You are identified solely by an anonymous id
  `u_<digest>`, derived with HMAC-SHA256 from your numeric Telegram id and a
  server secret (`USER_ID_SECRET`). Your **nickname, username, display name and
  raw Telegram id are never stored** with your preferences.
- **What the dataset contains.** Your profile edits, 👍/👎 votes and impressions
  are appended to `data/preferences.jsonl` keyed by that anonymous id and nothing
  else. **These preferences will be used to train a RankNet** (a learning-to-rank
  model) to power a more accurate recommendation system in the future.
- **Delivery data is separate and encrypted.** To send your digest the bot needs a
  routable Telegram chat id. That id lives *only* in a separate delivery registry
  (`data/user_registry.json`), **encrypted at rest** (when `USER_ID_SECRET` /
  `REGISTRY_SECRET` is set), and is **never written into the training dataset**.
- **Your controls.** `/stop` unsubscribes you (digests stop; your data is kept).
  `/delete_me` erases everything the bot holds about you — your profile, your
  feedback history (your rows in the dataset), and your delivery entry.
- **Production setup.** Set `USER_ID_SECRET` to a long random value and keep it
  stable (changing it rotates all anonymous ids). Set `BOT_ENV=production` (or
  `REQUIRE_USER_ID_SECRET=1`) to make the bot refuse to start with a missing or
  weak secret.

## Bot commands

Full command map (every command the bot understands):

| Command | Description |
| --- | --- |
| `/start` | Welcome message + the privacy notice. |
| `/privacy` | Show the privacy policy (what's stored, the RankNet use, your controls). |
| `/creare_profile` | Onboarding flow: read papers → authors → topics. |
| `/annulla` (`/cancel`) | Abort an in-progress onboarding flow. |
| `/add_author <name…>` | Add one or more followed authors (a followed author always alerts). |
| `/add_keywords <kw…>` | Add interest keywords. |
| `/add_topic <name>: <kw…>` | Create/extend a named topic with keywords. |
| `/add_conference <name…>` | Add conferences/venues of interest. |
| `/remove_author <name…>` | Remove followed authors. |
| `/remove_keywords <kw…>` | Remove interest keywords. |
| `/remove_topic <name>` | Remove a topic (or specific keywords from it). |
| `/remove_conference <name…>` | Remove conferences/venues. |
| `/set_frequency <2x \| daily \| weekly>` | Choose how often you receive the digest. |
| `/report <text>` | Report a bug, inaccuracy, or feature request. |
| `/clear` | Delete recent bot messages from your chat. |
| `/stop` | Unsubscribe — stop digests (your data is kept). |
| `/delete_me` | Erase everything the bot holds about you (right to erasure). |
| `/reports` *(owner)* | Show saved user reports. |
| `/errors` *(owner)* | Show recent runtime errors. |

Every recommended paper carries inline **👍 / 👎** buttons; tapping logs a vote
(re-tapping your own vote withdraws it). Owner-only commands are silently ignored
for non-owners (no hint they exist), and incoming commands are flood-controlled
per user.

## Roadmap

- [x] **Phase 1** — arXiv backbone, keyword + author scoring, console output
- [x] **Phase 1.5** — Telegram notifier, profile commands, onboarding flow
- [x] **Phase 2** — SPECTER embeddings + profile vector + similarity ranking
- [x] **Phase 3a** — 👍/👎 feedback loop (inline buttons, preference dataset, eval hook)
- [x] **Deploy** — GitHub Actions cron + private-gist state store
- [x] **Multi-user** — per-user delivery + registry, GDPR (`/stop`, `/delete_me`),
  flood control, encrypted chat-id at rest, shared embedder + rate limiter
- [ ] **Phase 3b** — Bluesky + Hugging Face Papers sources
- [ ] **RankNet** — train a learning-to-rank model on the collected preference dataset
- [ ] **Scale** — move per-user vector caches off the single gist (state guardrail in place)

## Run (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py --lookback-days 2 -v        # prints matched arXiv papers
python main.py --dry-run                   # don't persist seen-ids
python -m unittest discover -s tests       # test suite
```

Edit `config/profile.yaml` to tune keywords, authors, thresholds and the
`feedback:` parameters.

### Telegram

```bash
cp .env.example .env                       # fill TELEGRAM_BOT_TOKEN + chat id
# write any message to your bot first, then discover your chat id:
python tools/telegram_setup.py
python tools/telegram_setup.py --send-test # verify delivery
python main.py --register-menu             # register the slash-command menu
python main.py --poll-commands -v          # process incoming commands + 👍/👎 votes (registers users)
python main.py --notifier telegram -v      # send the owner's digest (single-user)
python main.py --notifier telegram --all-users -v  # fan out a per-user digest to every registered user
```

## Deploy (GitHub Actions)

The repo is public, so runtime state can't live on a branch: `tools/state_store.py`
tars `data/` to base64 and stores it as `state.b64` in a **private gist**. The
`.github/workflows/bot.yml` workflow runs on cron `0 7,19 * * *` (twice a day,
07:00 & 19:00 UTC) plus `workflow_dispatch`, and each run does: pull state from the
gist → `--register-menu` → `--poll-commands` → `--notifier telegram` → push state
back (the push runs `if: always()`).

To deploy from scratch:

1. **Create a private gist** with a placeholder file `state.b64` (any content,
   e.g. `init`). Copy the gist id from its URL → `GIST_ID`.
2. **Create a Personal Access Token** with the `gist` scope → `GIST_TOKEN`.
3. **Set the GitHub Actions Secrets** (repo *Settings → Secrets and variables →
   Actions*): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GIST_ID`, `GIST_TOKEN`,
   `USER_ID_SECRET` (a long random value — required for the public multi-user
   deployment), and optionally `REGISTRY_SECRET` and `SEMANTIC_SCHOLAR_API_KEY`.
4. The workflow then runs **twice a day**; trigger it on demand from the *Actions*
   tab with **Run workflow** (`workflow_dispatch`).
5. **Before trusting the cron, validate end-to-end:** follow the runbook in
   [`docs/DRY_RUN.md`](docs/DRY_RUN.md) for a real dry-run test.
