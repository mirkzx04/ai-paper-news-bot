# Dry run — real end-to-end validation

The whole test suite (165+ tests) **mocks the HTTP layer**: the bot has never
actually talked to the real Telegram Bot API. Before trusting the unattended
cron (twice a day), the owner must run one **real round-trip**: receive the
messages on their own Telegram, vote for real, and confirm every moving part
works against the live API.

This runbook is **executed by the owner**, by hand — not by an agent. It takes
~10 minutes. Run it on the `harden-mvp` branch.

> Throughout, the `-v` (`--verbose`) flag turns on `INFO` logging — keep it on so
> you can watch what the bot does. See `main.py:199`.

---

## 1. Prerequisites

A local `.env` (gitignored — copy from `.env.example`) with at least:

```ini
TELEGRAM_BOT_TOKEN=123456789:AA-your-botfather-token   # from @BotFather
TELEGRAM_CHAT_ID=123456789                             # your own chat with the bot
```

- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are **both required** for any
  telegram-mode run (`main.py:101-107`).
- **Don't know your chat id?** Write any message to your bot in Telegram, then:
  ```bash
  python tools/telegram_setup.py            # prints the chat ids seen recently
  ```
  Copy the printed id into `TELEGRAM_CHAT_ID`. (`tools/telegram_setup.py:48-64`)
- The `GIST_*` vars are **not** needed for a *local* dry run — state just lives in
  `data/`. They only matter for the GitHub Actions path (§6).

Install deps into a venv:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

> First telegram run downloads the SPECTER model (a few hundred MB) **only if you
> have seed papers** in your profile; with no seeds the embedding scorer is a
> no-op and nothing is downloaded (`main.py:126-130`).

---

## 2. Quick smoke (30 s)

Before the full loop, confirm the token + chat id reach Telegram and that the
👍/👎 buttons + callback round-trip actually work:

```bash
python tools/telegram_smoke.py
```

> `tools/telegram_smoke.py` is a shortcut helper (added alongside this runbook):
> it sends you one test message **with the 👍/👎 inline buttons**, then polls for
> your tap so you can see the callback come back end-to-end. If the file isn't
> there yet, fall back to the bare delivery check:
> ```bash
> python tools/telegram_setup.py --send-test    # sends "✅ paper-news-bot connesso."
> ```
> (`tools/telegram_setup.py:35-46`)

You should receive the message on your phone/desktop within a second. If not,
stop and fix the token/chat id before going further.

---

## 3. Full round-trip (step by step)

### 3a. Register the command menu

```bash
python main.py --register-menu
```

- Flag: `main.py:193`. It calls `setMyCommands` and **exits** (`main.py:210-221`).
- **Check:** in Telegram, tap the **menu (☰) / "/" button** in the chat with the
  bot. You should see the command list: `/creare_profile`, `/add_author`,
  `/add_keywords`, `/add_topic`, `/add_conference`, the matching `/remove_*`,
  `/report`, `/clear`. (You may need to reopen the chat for the client to refresh.)
- The console prints `setMyCommands: 200 ...` on success.

### 3b. Receive the alert + digest

```bash
python main.py --notifier telegram --lookback-days 2 -v
```

- Flags: `--notifier telegram` (`main.py:190`), `--lookback-days 2` (`main.py:195`),
  `-v` (`main.py:199`). This runs the full pipeline: fetch arXiv → score → route →
  send.
- **Check the message format** (`telegram_notifier.py:274-307`) — each paper is one
  message with these blocks: `📄 Title` (top matches are prefixed `🔔 ALERT —`),
  `👤 Authors`, optional `🏷 Field`, optional `🎓 Venue`, `📅 Date`, `📝 Summary`,
  `🔗` link.
- **Check the buttons:** every paper message must carry a single row with **👍** and
  **👎** inline buttons (`telegram_notifier.py:131-137`).
- If you get **no papers**, widen the window (e.g. `--lookback-days 5`) or loosen
  thresholds in `config/profile.yaml` — you need at least one paper with buttons to
  test the vote.

> **Tip:** if you want to re-receive the same papers on a later run (e.g. to retry
> voting), add `--dry-run` (`main.py:197`) so seen-ids aren't persisted
> (`main.py:280`, `mark_seen=not args.dry_run`).

### 3c. Vote for a paper (👍 then 👎)

On one paper message:

1. Tap **👍**.
   - **Check toast:** a small confirmation pops up: **"👍 registrato"**
     (`telegram_poller.py:42`).
   - **Check affordance:** the button row re-renders so 👍 becomes **"✅ 👍"** and 👎
     stays neutral (`telegram_poller.py:50`, `_refresh_feedback_markup`).
2. Now tap **👎** on the *same* paper.
   - **Check toast:** **"👎 registrato"**.
   - **Check affordance:** the mark moves — 👎 becomes **"✅ 👎"**, 👍 goes back to
     neutral.

> The vote is **not** yet in the file — the bot is poll-based and stateless, so the
> tap sits in Telegram's update queue until you poll in the next step.

### 3d. Collect the votes

```bash
python main.py --poll-commands -v
```

- Flag: `--poll-commands` (`main.py:191`). It reads pending updates, turns 👍/👎
  callbacks into `vote` events, replies to any commands, and **exits**
  (`main.py:233-251`).
- **Check:** the console prints `comandi processati, risposte inviate: N` and (with
  `-v`) lines like `logged vote down on arxiv:...`.
- **Check the dataset** — a `vote` event must appear in `data/preferences.jsonl`
  (`telegram_poller.py:367-374`, schema in `preference_dataset.py:39-50`):
  ```bash
  grep '"type": "vote"' data/preferences.jsonl | tail -n 3
  ```
  You should see your last action as `"signal": "down"` with the paper's
  `canonical_key`, `score`, `breakdown`, and `text`.

### 3e. Toggle-off (withdraw a vote)

In Telegram, **re-tap the emoji that is currently marked** (the one showing ✅).

1. Re-tap the marked emoji.
   - **Check toast:** **"↩️ voto rimosso"** (`telegram_poller.py:43`).
   - **Check affordance:** both buttons return to neutral (no ✅).
2. Collect again:
   ```bash
   python main.py --poll-commands -v
   grep '"type": "vote"' data/preferences.jsonl | tail -n 1
   ```
   - **Check:** the newest `vote` line for that paper now has **`"signal": "none"`**
     (`telegram_poller.py:302-312`). The log is append-only — a withdrawal is a
     fresh `none` event, not a deletion (`preference_dataset.py:46-50`).

### 3f. Commands

Send these as chat messages, then run `python main.py --poll-commands -v` to make
the bot process and reply (each `--poll-commands` drains the queue once).

- **`/creare_profile`** — starts the 3-step onboarding. The bot replies with the
  step-1 prompt (papers, one title per line); reply to it, then it asks for authors,
  then topics, then confirms `✅ Profile created!` (`flow/profile_flow.py:20-83`).
  Each step needs its own poll tick: send your answer → `--poll-commands` → read the
  next prompt. Cancel anytime with `/annulla`.
- **`/add_keywords mixture of experts, interpretability`** — bot confirms the
  keywords were added; verify a `profile_add` event lands in
  `data/preferences.jsonl` (`preference_dataset.py:140-162`) and the overlay file
  `data/profile_overlay.json` grew.
- **`/report the venue field was wrong on paper X`** — bot replies "Thanks! Your
  report has been received..." (`commands/report.py:25-32`).
- **Owner-only** (only work from *your* `TELEGRAM_CHAT_ID`, `main.py:237`):
  - **`/reports`** — renders your saved reports; you should see the `/report` you
    just sent (`telegram_poller.py:434-447`).
  - **`/errors`** — renders the recent runtime errors (`telegram_poller.py:449-474`).
    From any *other* account these two look like unknown commands — they don't even
    reveal they exist.

### 3g. Observability (heartbeat + error push)

- **Heartbeat:** after **any** successful telegram digest (§3b), the bot pushes you a
  one-liner like
  `✅ digest: 2 alert + 5 digest inviati · 7 nuovi · 0 scoring-error`
  (`main.py:70-80`, sent at `main.py:281-282`). Confirm you received it.
- **Error push:** force a failure to confirm crashes surface. Easiest way — point
  the run at a bad arXiv config so the fetch throws, e.g. temporarily break
  `sources.arxiv` in `config/profile.yaml`, or run with an unreachable network.
  On failure the bot:
  1. records the full traceback to `data/error_log.json` (`main.py:284-289`),
  2. pushes you `⚠️ digest run failed: <ErrorType>: <first line>`
     (`main.py:290-293`),
  3. re-raises (non-zero exit; in CI this also emails you) (`main.py:294`).
  Then send **`/errors`** (after a `--poll-commands`) — the new error should show up.
  **Revert your config change afterwards.**

---

## Known limitation — delayed callback ack (cron)

Telegram invalidates a `callback_query` a few seconds after the tap, so
`answerCallbackQuery` only succeeds if the bot polls **within seconds** of the
vote — i.e. during a local test where you poll right away. Under the unattended
**cron (twice a day)**, votes are collected *hours* later, so:

- the **"👍 registrato" toast does not appear**, and the client spinner just times
  out after a couple of seconds;
- but the **vote is still recorded** (the ack is unrelated to writing the event),
  and the **✅ affordance still applies** at the next poll (`editMessageReplyMarkup`
  works for up to 48h).

So the feedback is fully captured; only the *instant* visual confirmation is lost in
cron mode. A real-time ack would require switching from polling to a **webhook**,
which is out of scope for the current MVP.

---

## 4. Final checklist — what must work

- [ ] **§2** Smoke message arrives (token + chat id are correct).
- [ ] **§3a** `--register-menu` populates the "/" command menu in Telegram.
- [ ] **§3b** Digest arrives; each paper is correctly formatted **and** has 👍/👎
      buttons; top matches are prefixed `🔔 ALERT —`.
- [ ] **§3c** 👍 shows the **"👍 registrato"** toast and marks the button **"✅ 👍"**;
      👎 moves the mark to **"✅ 👎"**.
- [ ] **§3d** `--poll-commands` collects the vote → a `vote` event with the right
      `signal` appears in `data/preferences.jsonl`.
- [ ] **§3e** Re-tapping the marked emoji shows **"↩️ voto rimosso"** and writes a
      fresh `"signal": "none"` event.
- [ ] **§3f** `/creare_profile` onboarding completes; `/add_keywords` mutates the
      overlay; `/report` is saved; `/reports` and `/errors` work **only** from the
      owner chat.
- [ ] **§3g** Heartbeat received after a successful digest; a forced failure pushes
      the `⚠️ digest run failed` alert and is visible via `/errors`.

### Where to look

| What | Where |
| --- | --- |
| Live trace of what the bot did | console output of any `-v` run |
| Recorded votes / impressions / profile edits | `data/preferences.jsonl` |
| Recorded runtime errors (also via `/errors`) | `data/error_log.json` |
| Saved user reports (also via `/reports`) | `data/reports.json` |
| Telegram update offset / seen-ids | `data/bot.db` |
| Current profile additions | `data/profile_overlay.json` |

---

## 5. Reset note (optional)

A local dry run leaves real votes/impressions in `data/preferences.jsonl` and a
real error in `data/error_log.json`. If you don't want the test data to flow into
the actual recommender, back up and clear those files (or run the whole test from a
throwaway copy of `data/`) before going live. The vote events do feed the embedding
loop — but the loop is heavily down-weighted, so a handful of test votes barely
moves the ranking.

---

## 6. Alternative: run the dry run on GitHub Actions

Instead of running locally, you can trigger the exact production path on demand:

- **Actions** tab → workflow **bot** → **Run workflow** (this is the
  `workflow_dispatch` trigger, `.github/workflows/bot.yml:6`).
- It runs the same sequence the cron does: pull state ← gist → `--register-menu` →
  `--poll-commands -v` → `--notifier telegram -v` → push state → gist
  (`bot.yml:44-65`). The messages, votes, heartbeat and error push behave exactly as
  in §3, and state persists into the private gist (`if: always()` push) so a follow-up
  manual run sees your votes from the previous one.
- Requires the Actions Secrets to be set: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `GIST_ID`, `GIST_TOKEN`.

This is the recommended final check: it validates the real cron job, not just the
local CLI.
