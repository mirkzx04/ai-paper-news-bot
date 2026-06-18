# AI Paper News Bot

Telegram bot that surfaces new AI research relevant to *you* — from arXiv,
Semantic Scholar, Bluesky and Hugging Face Papers — ranked by **keywords +
followed authors + embedding similarity** to your interest profile. Sends
instant alerts for top matches and a daily digest, and learns from your 👍/👎.

Hosted free on **GitHub Actions** (public repo); state lives as JSON on a
dedicated `state` branch. Recommender design borrows from
[Scholar Inbox](https://arxiv.org/abs/2504.08385) (SPECTER2 + active-learning
cold-start) and the Telegram feedback loop from
[arXiv_recbot](https://github.com/yuandong-tian/arXiv_recbot).

## Architecture (Clean Architecture / SOLID)

```
src/
  domain/     Item, UserProfile          # entities, zero dependencies
  sources/    Source(ABC) -> ArxivSource # + S2/Bluesky/HF (Phase 2-3)
  scoring/    Scorer(ABC) -> Keyword, Author -> CombinedScorer
  store/      Store(ABC)  -> SqliteStore  # JSON-on-branch store for CI later
  notify/     Notifier(ABC) -> Console    # + Telegram (Phase 1.5)
  pipeline.py fetch -> dedup -> score -> route -> notify -> persist
main.py       manual dependency injection wiring
config/profile.yaml   your keywords / authors / seed papers / thresholds
```

Scoring fuses signals as a saturating weighted sum `total = min(1, Σ wᵢ·sᵢ)`;
routing (alert vs digest) is decided in the pipeline — a followed author always
alerts.

## Roadmap

- [x] **Phase 1** — arXiv backbone, keyword + author scoring, console output
- [x] **Phase 1.5** — Telegram notifier (send via Bot API)
- [ ] **Phase 2** — SPECTER2 embeddings + profile vector + similarity ranking
- [ ] **Phase 3** — Bluesky + HF Papers sources; 👍/👎 feedback loop
- [ ] **Deploy** — GitHub Actions cron + JSON state on `state` branch

## Run (Phase 1, local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py --lookback-days 2 -v        # prints matched arXiv papers
python main.py --dry-run                   # don't persist seen-ids
```

Edit `config/profile.yaml` to tune keywords, authors and thresholds.

### Telegram

```bash
cp .env.example .env                       # fill TELEGRAM_BOT_TOKEN + chat id
# write any message to your bot first, then discover your chat id:
python tools/telegram_setup.py
python tools/telegram_setup.py --send-test # verify delivery
python main.py --notifier telegram -v      # send matches to Telegram
```
