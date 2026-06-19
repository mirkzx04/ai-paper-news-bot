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

## Bot commands

- **Onboarding** — `/creare_profile` walks you through seed papers → authors → topics.
- **Profile** — `/add_author`, `/add_keywords`, `/add_topic`, `/add_conference` (and `/remove_*`).
- **Misc** — `/report <text>` (flag a bug or inaccuracy), `/clear` (wipe recent bot messages).
- **Owner-only** — `/reports`, `/errors` surface the saved user reports and runtime
  errors; the bot also pushes a summary when a run logs new errors.

## Roadmap

- [x] **Phase 1** — arXiv backbone, keyword + author scoring, console output
- [x] **Phase 1.5** — Telegram notifier, profile commands, onboarding flow
- [x] **Phase 2** — SPECTER embeddings + profile vector + similarity ranking
- [x] **Phase 3a** — 👍/👎 feedback loop (inline buttons, preference dataset, eval hook)
- [x] **Deploy** — GitHub Actions cron + private-gist state store
- [ ] **Phase 3b** — Bluesky + Hugging Face Papers sources

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
python main.py --poll-commands -v          # process incoming commands + 👍/👎 votes
python main.py --notifier telegram -v      # send matched papers (with vote buttons)
```
