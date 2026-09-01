"""Backfill + go-forward relay of the babyLM Slack #related-work channel to the same
targets as the Discord relay (Bluesky, Semble, dormant Mastodon), plus its OWN feed page.

Reuses the Discord relay's publishing machinery unchanged:
  backfill.Target / _post_* / _preflight_* / run_preflight / post_message,
  backfill.load_ledger / save_ledger / update_feed_archive (path-parameterized),
  relay.links_stuck_since.
The Slack-specific side (Web API read + message shim) lives in slack_source.py.

Own ledger + feed files (separate from the Discord relay so the two never double-post
and their commits don't clobber each other):
  slack_done.json          (target, ts) pairs already posted
  slack_feed_items.json     content archive build_feed.py renders into docs/babylm/

Dormant until SLACK_BOT_TOKEN is set (no token -> fetch returns [] -> clean no-op).

  python3 slack_backfill.py            # post to all enabled targets
  python3 slack_backfill.py --max 25   # cap messages posted this run
  DRY_RUN=1 python3 slack_backfill.py  # show what would post; refresh feed; no posting
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import relay
import backfill
import slack_source

LEDGER_PATH = "slack_done.json"
FEED_PATH = "slack_feed_items.json"

argv = sys.argv[1:]
MAX = None
for i, a in enumerate(argv):
    if a == "--max" and i + 1 < len(argv):
        MAX = int(argv[i + 1])


def build_targets():
    """The same target table as the Discord relay, enabled from relay's config flags.
    Mastodon is drip-capped and excluded from age-based stuck-alerting, exactly as in
    backfill.py's Discord table."""
    return [
        backfill.Target("semble", relay.SEMBLE_ENABLE,
                        post=backfill._post_semble, preflight=backfill._preflight_semble),
        backfill.Target("bluesky", relay.BLUESKY_ENABLE,
                        post=backfill._post_bluesky, preflight=backfill._preflight_bluesky),
        backfill.Target("mastodon", relay.MASTODON_ENABLE,
                        post=backfill._post_mastodon, preflight=backfill._preflight_mastodon,
                        cap=relay.MASTODON_MAX_PER_RUN, alert_stuck=False),
    ]


def main():
    msgs = slack_source.fetch_history()
    all_msgs = [(m, relay.URL_RE.findall(m.content or "")) for m in msgs]
    all_msgs = [(m, u) for m, u in all_msgs if u]
    all_msgs.sort(key=lambda mu: mu[0].created_at)
    print(f"[slack] {len(msgs)} message(s) scanned, {len(all_msgs)} with link(s)")

    if not os.environ.get("SLACK_BOT_TOKEN"):
        print("[slack] SLACK_BOT_TOKEN not set — dormant, nothing to do.")
        return 0

    n_feed = backfill.update_feed_archive(all_msgs, path=FEED_PATH)
    print(f"[feed] archive now holds {n_feed} item(s) -> {FEED_PATH}")

    ledger = backfill.load_ledger(path=LEDGER_PATH)

    if relay.DRY_RUN:
        targets = build_targets()
        pending = [(m, u) for m, u in all_msgs
                   if any(t.enabled and (t.name, str(m.id)) not in ledger for t in targets)]
        print(f"[slack] DRY_RUN — {len(pending)} link-message(s) would post "
              "(feed refreshed, nothing sent).")
        for m, u in pending[:10]:
            print(f"  would post {m.id} ({m.author.display_name}): {u[0]}")
        return 0

    targets = build_targets()
    backfill.run_preflight(targets)

    posted = 0
    for m, urls in all_msgs:
        if MAX is not None and posted >= MAX:
            print(f"[stop] reached --max {MAX}")
            break
        if backfill.post_message(targets, m, urls, m.author.display_name, ledger):
            posted += 1
            backfill.save_ledger(ledger, path=LEDGER_PATH)
            time.sleep(2)  # gentle pacing
    backfill.save_ledger(ledger, path=LEDGER_PATH)

    # SUSTAINED-failure alerting — identical policy to the Discord relay: alert (exit 1
    # -> workflow ping) only for a link we MEANT to post (t.wanted, snapshotted before
    # degrade) that has stayed undelivered past STUCK_AFTER_HOURS across multiple cycles.
    exit_code = 0
    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=backfill.STUCK_AFTER_HOURS)
    for t in targets:
        if not (t.wanted and t.alert_stuck):
            continue
        stuck = relay.links_stuck_since(all_msgs, ledger, t.name, now, max_age)
        if stuck:
            oldest_h = max((now - m.created_at).total_seconds() for m in stuck) / 3600
            exit_code = 1
            print(f"[FAIL] {len(stuck)} {t.name} link(s) stuck >{backfill.STUCK_AFTER_HOURS}h "
                  f"(oldest {oldest_h:.0f}h) — {t.name} failed across cycles; check auth/outage")

    per_target = "  ".join(f"{t.name}={t.sent}/{t.fail}" for t in targets)
    summary = f"posted={posted}  [{per_target}]  ledger={len(ledger)}"
    print(f"\nDONE. {summary}")
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        try:
            with open(step, "a") as fh:
                fh.write(f"### babyLM slack relay\n\n`{summary}`\n")
        except OSError:
            pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
