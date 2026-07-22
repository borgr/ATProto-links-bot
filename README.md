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

## Maintenance & troubleshooting

**Health check:** `python check_keys.py` verifies the Discord, Bluesky, and Semble
credentials in your local `.env`.

**Alerts:** a failed run pings Discord (as "ATProto-links-bot CI") **and** emails the repo
owner. Alerts are **de-duplicated** — only the first failure after a success pings, so an
ongoing outage won't spam you every hour.

**Secrets live in TWO places that can drift apart:**
- **GitHub repo secrets** (Settings → Secrets and variables → Actions) — used by the
  scheduled run: `DISCORD_BOT_TOKEN`, `ATPROTO_APP_PASSWORD`, `SEMBLE_API_KEY`,
  `DISCORD_WEBHOOK_URL`.
- **Local `.env`** — used only for local runs / `check_keys.py`.
- A run can fail in CI while `.env` looks fine (or vice-versa). Check both.

**Most common failure — Semble `preflight failed (HTTP 401)`:** the `SEMBLE_API_KEY`
secret is invalid or out of sync. Fix:
1. `python check_keys.py` — is the Semble key in `.env` valid?
   - **Valid** → GitHub's secret is just stale. Re-sync it (no new key needed):
     `sed -n 's/^SEMBLE_API_KEY=//p' .env | tr -d '\n' | gh secret set SEMBLE_API_KEY -R borgr/ATProto-links-bot`
   - **Invalid** → make a fresh key at semble.so, put it in `.env` *and* the GitHub secret.
2. Re-run: `gh workflow run relay.yml -R borgr/ATProto-links-bot`. Links that failed are
   **not** marked done, so they auto-post on the next successful run (no manual backfill).

**Semble API** is undocumented alpha (`network.cosmik.*` on `api.semble.so/api`) — the most
likely thing to change. If `card.addUrl` starts rejecting the payload, re-inspect the web
app's network calls.

**Local dev:** `cp .env.example .env`, fill it, `pip install -r requirements.txt`, then
`python backfill.py` (scan only), and `python test_chunking.py && python test_semble.py`.

## Two ways to run, pick one (don't run both — they'd double-post)
- **Scheduled Action** (recommended): batched, free, no machine to babysit.
- **Always-on LaunchAgent** (`./service.sh start`): real-time, but tied to a machine
  being on/online. To switch from this to the Action cleanly, see the cutover note in
  the setup conversation: stop the service, run `python backfill.py --seed-only`, then
  enable the Action.
