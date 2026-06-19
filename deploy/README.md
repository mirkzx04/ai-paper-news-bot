# deploy/

Artifacts for running the bot as an always-on service in **serve mode**
(`python main.py --serve`: real-time Telegram long-poll + in-process digest
scheduler) on a small VM. This is an alternative to the stateless GitHub Actions
cron (`.github/workflows/bot.yml`); here **state is local** under `data/`.

| File | What it is |
| --- | --- |
| [`bot.service`](bot.service) | systemd unit for `main.py --serve` (non-root user, `Restart=always`, `EnvironmentFile=.env`, journald logging). Edit the four placeholders before installing. |
| [`../docs/DEPLOY_VM.md`](../docs/DEPLOY_VM.md) | Step-by-step runbook: provision an Oracle Always-Free **ARM A1** VM (with the capacity caveat + x86-micro fallback), set up Python 3.12 + torch-CPU, install this unit, operate, and back up `data/`. |

No webhook / HTTPS / inbound port is required — the bot only reaches **out** to
Telegram (long-poll). Validate the live round-trip with
[`../docs/DRY_RUN.md`](../docs/DRY_RUN.md).
