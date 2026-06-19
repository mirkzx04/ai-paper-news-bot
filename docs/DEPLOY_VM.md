# Deploy on a VM (Oracle Always Free, ARM) — long-running `--serve`

This runbook puts the bot on a small always-on VM as a **systemd service** in
**serve mode**: one persistent process that long-polls Telegram for
commands/votes **in real time** *and* runs the digest scheduler internally
(cadence from `/set_frequency`). No GitHub Actions, no gist — **state is local**
under `data/`.

> Serve mode is started by `python main.py --serve`. Everything else (`--register-menu`,
> `--poll-commands`, `--notifier telegram`, `--lookback-days`, `--dry-run`, `-v`) is
> unchanged from the CLI you already know (`main.py:225-234`).

**No inbound port is needed.** The bot is a *client* toward Telegram (long-poll,
see `src/telegram_poller.py:128-159`) — it never opens a listening socket. So
there is **no webhook and no HTTPS** to set up, and the VM firewall only needs
outbound 443 (open by default).

---

## 0. What you'll end up with

```
/opt/paper-news-bot/        # the git checkout (WorkingDirectory)
  .venv/                    # Python 3.12 venv with torch-CPU + requirements
  .env                      # secrets, gitignored (TELEGRAM_BOT_TOKEN, ...)
  data/                     # LOCAL state: bot.db + *.json + preferences.jsonl
```
Owned by a non-root user `paperbot`, run by `systemd` unit `paper-news-bot`.

---

## 1. Provision the instance (Oracle Always Free, ARM Ampere A1)

1. Oracle Cloud console → **Compute → Instances → Create instance**.
2. **Shape:** *Ampere* → **VM.Standard.A1.Flex**. The Always-Free allowance is
   up to **4 OCPU / 24 GB RAM** total across A1 instances — even 1 OCPU / 6 GB is
   plenty here (CPU-only SPECTER, tiny workload). Pick **Canonical Ubuntu 24.04**
   (ships Python 3.12).
3. **Be ready for "Out of capacity."** ARM A1 capacity is chronically scarce on
   Oracle Free — `Create` may fail with *"Out of host capacity"*. Real options,
   roughly in order:
   - **Retry** — the error is transient; try again over minutes/hours.
   - **Change Availability Domain** (AD-1/2/3) in the create dialog, or pick a
     **less busy home region** (your free tenancy is pinned to one home region;
     choosing a quieter one at signup helps).
   - **Smaller flex** (1 OCPU / 6 GB) is sometimes grantable when 4/24 isn't.
   - **Fall back to a free x86 micro** — **VM.Standard.E2.1.Micro** (1 OCPU /
     1 GB, 2 of them are Always Free). It's slower and 1 GB RAM is *tight* for
     SPECTER (see §3 swap note), but it actually provisions. Everything below is
     arch-independent — torch-CPU has both arm64 and x86_64 wheels.
4. **SSH keys:** upload/generate a keypair so you can log in. Note the public IP.
5. **Networking / firewall — keep it minimal.** Inbound: leave only the default
   **SSH (22/tcp)** rule. **Do NOT open 80/443 inbound** — long-poll needs no
   inbound port. (If you later restrict SSH to your IP, do it in the subnet's
   Security List / NSG.) Oracle images also run a host `iptables`; the default
   already permits outbound + established, which is all the bot needs.

> **Idle-reclaim policy (free tier):** Oracle may **stop/reclaim "idle" Always
> Free compute** — historically aimed at instances under ~10% CPU / low
> network. A bot that long-polls 24/7 keeps a steady connection and some
> baseline activity, which helps, but it's not a guarantee. Mitigations: keep
> the service genuinely running (this runbook), and **back up `data/`** (§7) so a
> reclaim is recoverable. Paid/PAYG tenancies are exempt from idle-reclaim.

---

## 2. First login & base packages

```bash
ssh ubuntu@<PUBLIC_IP>

sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.12 python3.12-venv git
python3.12 --version          # expect 3.12.x

# Dedicated non-root service user that owns the install (matches bot.service).
sudo useradd --system --create-home --shell /usr/sbin/nologin paperbot
```

---

## 3. Clone, venv, dependencies

Install **as the `paperbot` user** so the checkout, venv and `data/` are owned by
the same account the service runs as.

```bash
sudo install -d -o paperbot -g paperbot /opt/paper-news-bot
sudo -u paperbot -H bash       # become paperbot in a login shell
cd /opt/paper-news-bot

git clone https://github.com/mirkzx04/ai-paper-news-bot.git .

python3.12 -m venv .venv
source .venv/bin/activate

# torch must be the CPU wheel (no CUDA on these VMs) — install it FIRST, exactly
# as CI does, then the rest. arm64 and x86_64 wheels both exist on this index.
python -m pip install -U pip
python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements.txt
```
(torch-CPU + index URL mirrors `.github/workflows/bot.yml:35-36`; the comment in
`requirements.txt:9-10` says the same.)

> **SPECTER download:** the *first* digest with seed papers in your profile pulls
> the SPECTER model (~a few hundred MB) into `~/.cache/huggingface`. With **no**
> seeds the embedding scorer is a no-op and nothing is downloaded
> (`main.py:163-164`). On the **1 GB x86 micro** this download + model load can
> OOM — add swap before the first run:
> ```bash
> sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
> sudo mkswap /swapfile && sudo swapon /swapfile
> echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
> ```
> The 6 GB ARM flex does not need swap.

### Create the `.env`

State and secrets are local. Copy the template and fill in your real values:

```bash
cp .env.example .env
nano .env          # set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
```

- **Required:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (both checked for any
  telegram-mode run, `main.py:137-141`). **Optional:** `SEMANTIC_SCHOLAR_API_KEY`
  (author-id enrichment; no-op without it, `main.py:196-197`).
- **Ignore the `GIST_*` lines** in `.env.example` — they're only for the GitHub
  Actions path (persisting state to a gist between stateless runs). On a VM the
  state just lives in `data/`, so they are **not needed**.
- `.env` and `data/` are gitignored, so `git pull` (§6) never touches them.
- `load_env()` uses `setdefault` (`src/env.py:21`), so a real shell/systemd env
  var always wins over the file — fine to keep both.

---

## 4. Smoke-test by hand (before the service)

Still as `paperbot`, with the venv active, confirm Telegram is reachable and the
menu registers — then exit the loop with Ctrl-C:

```bash
python main.py --register-menu          # one-off: registers the /-command menu
python main.py --serve -v               # start the loop; Ctrl-C to stop
```
You should see the menu appear in Telegram (tap ☰ / "/") and, in `-v` logs, the
serve loop start long-polling. Stop it (Ctrl-C) before handing over to systemd.
For a full live round-trip (votes, commands, heartbeat) see **`docs/DRY_RUN.md`**.

```bash
exit            # leave the paperbot shell, back to your sudo user
```

---

## 5. Install the systemd service

```bash
# Edit the four placeholders (User/WorkingDirectory/ExecStart/EnvironmentFile)
# if your paths differ from the /opt/paper-news-bot + paperbot defaults.
sudo nano /opt/paper-news-bot/deploy/bot.service

sudo cp /opt/paper-news-bot/deploy/bot.service /etc/systemd/system/paper-news-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now paper-news-bot      # start now + on every boot

systemctl status paper-news-bot                 # should read: active (running)
```

The unit sets `Restart=always` (10s backoff) with a crash-loop guard, runs as
the non-root `paperbot`, reads the `.env` via `EnvironmentFile=`, and logs to the
journal. See `deploy/bot.service` for the inline comments.

### Watch the logs
```bash
journalctl -u paper-news-bot -f          # live tail
journalctl -u paper-news-bot --since "10 min ago"
journalctl -u paper-news-bot -p warning  # warnings/errors only
```

---

## 6. Operate it

**Register the menu (once):** done in §4. Re-run only if you change the command
set: `sudo -u paperbot /opt/paper-news-bot/.venv/bin/python /opt/paper-news-bot/main.py --register-menu`.

**How `--serve` behaves:** one process, two jobs in parallel — (a) Telegram
**long-poll** so `/commands` and 👍/👎 votes are handled within seconds (real-time
ack, unlike the cron — see the "delayed callback ack" note in `docs/DRY_RUN.md`),
and (b) the **digest scheduler**, which sends on the cadence the user set with
`/set_frequency` (`2x_daily` / `daily` / `weekly`; the due-check logic lives in
`main.py:98-113`). No external cron/timer is involved.

**Update the bot:**
```bash
sudo -u paperbot -H bash -c 'cd /opt/paper-news-bot && git pull'
sudo systemctl restart paper-news-bot
journalctl -u paper-news-bot --since "1 min ago"   # confirm clean restart
```
If `requirements.txt` changed, reinstall inside the venv before restarting:
`sudo -u paperbot /opt/paper-news-bot/.venv/bin/pip install -r /opt/paper-news-bot/requirements.txt`.

**Stop / start / restart:**
```bash
sudo systemctl stop paper-news-bot
sudo systemctl start paper-news-bot
sudo systemctl restart paper-news-bot
```

---

## 7. Back up the local state (`data/`)

All runtime state — `bot.db` (Telegram offset, seen-ids, meta), the JSON files,
and the append-only `preferences.jsonl` — lives under `data/`. With no gist, a
lost VM means lost votes/history unless you back it up. A nightly tar is enough:

```bash
sudo install -d -o paperbot -g paperbot /opt/paper-news-bot/backups
sudo -u paperbot crontab -e
```
Add (3:30 AM daily; keeps a rolling copy, removes backups older than 14 days):
```cron
30 3 * * * tar -czf /opt/paper-news-bot/backups/data-$(date +\%F).tgz -C /opt/paper-news-bot data
35 3 * * * find /opt/paper-news-bot/backups -name 'data-*.tgz' -mtime +14 -delete
```
Pull a copy off-box periodically (`scp ubuntu@<IP>:/opt/paper-news-bot/backups/*.tgz .`)
so a full instance reclaim (§1) is recoverable. **Restore:** stop the service,
`tar -xzf <backup>.tgz -C /opt/paper-news-bot`, start it again.

---

## 8. VM vs GitHub Actions — what changes

| | **VM `--serve`** (this doc) | **GitHub Actions** (`.github/workflows/bot.yml`) |
| --- | --- | --- |
| Command/vote latency | **Real-time** (seconds, long-poll) | Batched at the next cron tick (hours) |
| Vote callback ack (👍 toast) | **Shown** (polls within seconds) | Often missed; vote still recorded (`docs/DRY_RUN.md` "delayed callback ack") |
| Digest cadence | **Any** `/set_frequency` value, scheduled in-process | Bounded by the cron tick (2×/day; `bot.yml:5`) |
| State | **Local** `data/` (back up via §7) | Tarred to a **private gist** between runs |
| Inbound network | **None** (long-poll client) | N/A |
| Ops | **Manual** (provision, systemd, patch, backup) | **Zero** (managed runners) |
| Cost | Free *if* you get an Always-Free VM (capacity caveat, §1) | Free (public-repo minutes) |

Validate the live round-trip the same way in both setups: **`docs/DRY_RUN.md`**.
