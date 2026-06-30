# CoLab links relay

Relays links shared in the CoLab Discord `#papers-links-n-sharing` channels to:
- **Bluesky** — posts to [@colab-links.bsky.social](https://bsky.app/profile/colab-links.bsky.social)
- **Semble** — adds cards to the *CoLab Links* collection

## How it runs (recommended: zero-maintenance)

A **scheduled GitHub Action** (`.github/workflows/relay.yml`) runs every 6 hours. Each
run scans the last 7 days of channel history and posts anything new. A committed
ledger (`backfill_done.json`) records what's already been posted, so nothing is
duplicated. There is **no server and no always-on process** — links appear within
~6h of being shared.

If a run fails (e.g. a key expired, or Semble's API changed), GitHub emails the repo
owner. That's the only monitoring you need.

### Setup (one time)
1. Push this repo to GitHub.
2. **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `DISCORD_BOT_TOKEN`
   - `ATPROTO_APP_PASSWORD`  (Bluesky app password for colab-links)
   - `SEMBLE_API_KEY`
3. **Actions tab → relay → Run workflow** to test. Then it runs on schedule.

> Scheduled workflows are paused after 60 days of no repo activity (GitHub policy);
> the ledger commits keep it active while links keep coming. Re-enable with one click.

## Files
| File | Purpose |
|---|---|
| `relay.py` | Config + helpers, and an optional **always-on** live listener (real-time). Not needed if you use the scheduled Action. |
| `backfill.py` | The catch-up/import job the Action runs. Flags below. |
| `service.sh` | Manage the optional macOS LaunchAgent (laptop always-on mode). |
| `backfill_done.json` | De-dupe ledger (committed, persists state). |

### `backfill.py` flags
```
--run semble|bluesky|both   which target(s) to post to
--since-days N              only scan the last N days (default: all history)
--max N                     cap messages posted this run
--exclude-substr "..."      skip messages containing this text (used to skip a test post)
--seed-only                 mark current history as done WITHOUT posting (cutover helper)
```

## Maintenance
- **Rotate secrets**: regenerate in Discord/Bluesky/Semble, update the GitHub Action secrets.
- **Semble API** is undocumented/alpha (`network.cosmik.*` on `api.semble.so/api`) — the
  most likely thing to break. A failed run = email; check `card.addUrl`'s shape if so.
- **Local dev**: `cp .env.example .env`, fill it, `pip install -r requirements.txt`,
  `python backfill.py` (scan only) or `--run both`.

## Two ways to run, pick one (don't run both — they'd double-post)
- **Scheduled Action** (recommended): batched, free, no machine to babysit.
- **Always-on LaunchAgent** (`./service.sh start`): real-time, but tied to a machine
  being on/online. To switch from this to the Action cleanly, see the cutover note in
  the setup conversation: stop the service, run `python backfill.py --seed-only`, then
  enable the Action.
