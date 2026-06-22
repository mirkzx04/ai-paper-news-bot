# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` is the canonical product/deploy doc (commands, feedback loop, privacy
policy, full command map, GitHub Actions setup). This file covers only what's not
obvious from reading one file.

## Commands

```bash
python -m unittest discover -s tests          # full suite (stdlib unittest, NO pytest)
python -m unittest tests.test_pipeline        # one module
python -m unittest tests.test_pipeline.TestRun.test_scoring_error_is_skipped  # one test
python main.py --lookback-days 2 -v           # console run, prints matched papers
python main.py --dry-run                      # don't persist seen-ids (re-show next run)
```

There is no lint/build step and no CI test job — the only automation is the digest
cron in `.github/workflows/bot.yml`.

## main.py run modes (mutually exclusive branches)

`main.py` is pure CLI + manual DI; all real wiring lives in `src/app.py`. The CLI
selects one mode:

- `--register-menu` → push the slash-command menu to Telegram, exit.
- `--poll-commands` → drain incoming commands + 👍/👎 callbacks, mutate overlays, register senders, exit.
- `--serve` → long-running loop (`src/serve.py`): real-time long-poll + internal digest scheduler. For a VM.
- `--all-users` → `app.run_digest_for_all`: fan a per-user digest out to every registered user.
- (default) → single-user/owner digest via `app.run_digest_once`.

The cron does poll-commands then `--all-users` each run.

## Layering (Clean Architecture; dependencies point inward)

```
domain/   Item, UserProfile — frozen dataclasses, zero deps, no I/O. The stable core.
sources/  Source(ABC) → ArxivSource. Maps external feeds INTO Item.
scoring/  Scorer(ABC) → Keyword/Author/Embedding/FieldClassifier → CombinedScorer.
embedding/ SpecterEmbedder (+ CachingEmbedder), profile_vector, feedback_vectors.
store/    Store(ABC) → SqliteStore; ProfileStore, PreferenceDataset, UserRegistry, SentItemsStore.
notify/   Notifier(ABC) → Console/Telegram; RateLimiter.
flow/     ProfileFlow — multi-step /creare_profile onboarding.
commands/ /add_* /remove_* /report → CommandDispatcher.
pipeline.py  fetch → dedup → score → route → notify → persist.
app.py    constructs every component (build_pipeline/build_notifier/build_poller) + run_digest_*.
```

`src/app.py` is deliberately **framework-free** (no argparse). Missing Telegram
creds raise `MissingCredentialsError` (a `ValueError` subclass) so each caller maps
it to its own idiom; `main.py` turns it into `parser.error`. A real failure inside
`pipeline.run` re-raises as a plain `ValueError` and crashes — keep that distinction.

## Invariants that span files

- **Scoring vs routing are separate on purpose.** `CombinedScorer` produces a
  *saturating weighted sum* `total = min(1, Σ wᵢ·sᵢ)` — absolute contributions, not a
  convex average, so one strong signal surfaces an item alone. The alert-vs-digest
  decision lives in `pipeline.py`, not the scorer. **A followed-author match always
  alerts and is never capped** (`_author_hit` → breakdown["author"] ≥ 1.0).
- **Feedback (👍/👎) acts only on the embedding channel.** Votes never touch declared
  keywords/authors; they're weighted below onboarding seeds, time-decayed, capped,
  cold-started. With zero votes, scoring equals the embedding-only baseline.
- **Dedup key is cross-source.** `Item.canonical_key` = `arxiv:<id>` when an arXiv id
  can be extracted (so a Bluesky post linking a paper dedups against the paper), else
  `<source>:<external_id>`.
- **Multi-user seen-sets must stay independent.** The fan-out wraps the shared store in
  `ScopedSeenStore` (prefixes keys with `<user_id>::`) so one user marking a paper seen
  never hides it from another. Per-user caches go under `data/users/<anon-id>/` via
  `app._scoped_user_path`; per-user cadence uses a `last_digest_at:<user_id>` meta key.
- **Anonymous identity is a hard constraint.** Users are keyed by `u_<digest>` =
  HMAC-SHA256(telegram_id, `USER_ID_SECRET`). Raw telegram id / username / nickname are
  **never** written to the preference dataset (`data/preferences.jsonl`). The routable
  chat id lives only in `data/user_registry.json`, encrypted at rest. Don't break this.
- **Shared expensive resources in fan-out.** `run_digest_for_all` injects ONE
  `CachingEmbedder` (each candidate embedded once across all users) and ONE
  `RateLimiter` (global ~30 msg/s Telegram cap). One user's failure must never abort
  the run; a blocked user (`PermanentSendError`) gets marked `blocked` and skipped.
- **Resilience: one bad item/source/user never sinks the run.** `_fetch_all`,
  `_score_all`, and the fan-out loop each swallow per-element exceptions and continue.

## State persistence (the repo is PUBLIC)

`data/` is gitignored — runtime state cannot live on a branch. `tools/state_store.py`
tars `data/` → base64 → a **private gist** (`state.b64`). The cron pulls before the run
and pushes after with `if: always()`. State = Telegram offset, seen-ids, profile
overlays, feedback/profile vector caches, user registry, logs, preference dataset.

## Conventions

- Tests are stdlib `unittest` with in-memory fakes (no network/DB). Each test file
  starts with `sys.path.insert(0, <repo root>)` then imports `src.*` with `# noqa: E402`.
- Comments here carry intent and edge-case rationale — match that density when editing.
- `config/profile.yaml` is the single-user YAML seed (keywords, authors, seed papers,
  thresholds, `feedback:` tunables). Per-user runtime edits are JSON overlays merged on top.
- **State-file durability.** A full-rewrite state file (registry, profile overlay, reports,
  caches) must be written via `src.atomic_write.atomic_write_text` (temp + fsync + `os.replace`),
  never a direct `open(path, "w")` — a torn in-place write corrupts state and then propagates
  to the gist. Append-only logs (`preferences.jsonl`, `error_log.jsonl`) instead tolerate a
  torn *last* line on read (corrupt-line skip), which is why they can append directly.
- `SqliteStore` runs in WAL + `synchronous=NORMAL` + `busy_timeout`; bulk seen-marking goes
  through `mark_seen_many` (one transaction/run), and `close()` checkpoints the WAL so the
  gist tar is sidecar-free.
