"""One-shot backfill of historical links from the papers-links-n-sharing
channels to Semble and/or Bluesky. Reuses config + helpers from relay.py.

  python3 backfill.py                 # SCAN ONLY: count + sample, no posting
  python3 backfill.py --run semble    # post historical links to Semble
  python3 backfill.py --run bluesky   # post historical links to Bluesky
  python3 backfill.py --run both      # both targets
  python3 backfill.py --run both --max 25   # cap how many messages to post

Safety:
  - Processes oldest -> newest (chronological).
  - A ledger file (backfill_done.json) records (target, message_id) pairs already
    posted, so re-running never double-posts. Delete it to force a fresh run.
  - Pauses ~2s between messages to stay well under rate limits.
"""
import os
import sys
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord
import relay  # config + helpers (chunk_text, build_richtext, make_embed, semble_add_url, ...)

# ---- args ----
argv = sys.argv[1:]
RUN = None
MAX = None
SINCE_DAYS = None
SEED_ONLY = "--seed-only" in argv
EXCLUDE_SUBSTR = None
for i, a in enumerate(argv):
    if a == "--run" and i + 1 < len(argv):
        RUN = argv[i + 1]
    if a == "--max" and i + 1 < len(argv):
        MAX = int(argv[i + 1])
    if a == "--since-days" and i + 1 < len(argv):
        SINCE_DAYS = int(argv[i + 1])
    if a == "--exclude-substr" and i + 1 < len(argv):
        EXCLUDE_SUBSTR = argv[i + 1]
DO_SEMBLE = RUN in ("semble", "both", "all")
DO_BLUESKY = RUN in ("bluesky", "both", "all")
DO_MASTODON = RUN in ("mastodon", "both", "all")
AFTER = (datetime.now(timezone.utc) - timedelta(days=SINCE_DAYS)) if SINCE_DAYS else None

LEDGER_PATH = "backfill_done.json"
FEED_PATH = "feed_items.json"    # content archive that build_feed.py renders into docs/

# A per-cycle auth/login blip is a non-event: unposted links stay unledgered and are
# re-attempted every run, so a transient outage self-heals with no data loss. We therefore
# alert ONLY on SUSTAINED failure — a link that has stayed undelivered for longer than this
# (≈ 2-3 missed hourly cycles), which means a real credential/outage problem, not a hiccup.
STUCK_AFTER_HOURS = 3


def load_ledger():
    if os.path.exists(LEDGER_PATH):
        try:
            return set(tuple(x) for x in json.load(open(LEDGER_PATH)))
        except Exception:
            return set()
    return set()


def save_ledger(ledger):
    json.dump([list(x) for x in ledger], open(LEDGER_PATH, "w"))


def update_feed_archive(all_msgs):
    """Upsert every scanned link-message into feed_items.json (deduped by id, kept
    sorted oldest->newest). This is the source build_feed.py renders the RSS/HTML from.
    Runs every scan so the feed reflects channel history within the window and persists
    older entries already recorded — independent of what posted to social targets."""
    try:
        items = json.load(open(FEED_PATH))
    except Exception:
        items = []
    by_id = {str(it["id"]): it for it in items}
    for m, urls in all_msgs:
        by_id[str(m.id)] = {
            "id": str(m.id),
            "created_at": m.created_at.isoformat(),
            "author": m.author.display_name,
            "text": m.content or "",
            "urls": list(dict.fromkeys(urls)),
            "channel": m.channel.name,
            "guild_id": str(m.guild.id) if m.guild else "",
            "channel_id": str(m.channel.id),
        }
    merged = sorted(by_id.values(), key=lambda it: it["created_at"])
    json.dump(merged, open(FEED_PATH, "w"), ensure_ascii=False, indent=0)
    return len(merged)


ledger = load_ledger()

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


EXIT_CODE = 0


def _fail(msg):
    """Record a hard failure so the process exits non-zero -> the workflow's
    'Notify Discord on failure' step fires."""
    global EXIT_CODE
    EXIT_CODE = 1
    print(f"[FAIL] {msg}")

# Bluesky posting lives in relay.post_to_bluesky (shared with the live listener).


async def collect(channel):
    out = []
    async for m in channel.history(limit=None, after=AFTER, oldest_first=True):
        if m.author.bot:
            continue
        if EXCLUDE_SUBSTR and EXCLUDE_SUBSTR in (m.content or ""):
            continue
        urls = relay.URL_RE.findall(m.content or "")
        if urls:
            out.append((m, urls))
    return out


class AuthDegrade(Exception):
    """A target's credentials were systemically rejected mid-run. We disable that target for
    the rest of the cycle (stop hammering it) but do NOT alert per-cycle — the end-of-run
    sustained-failure check decides if it's a real problem. Distinct from a transient post
    error, which just gets counted and left unledgered to retry next run."""


@dataclass
class Target:
    """One delivery target. The posting loop, preflight, degrade, stuck-check and summary all
    iterate a list of these, so a target's behavior lives in ONE row instead of a copy-pasted
    block. `post(m, urls, author)` posts one message: returns normally on success, raises
    AuthDegrade on systemic auth failure, or any other Exception on a transient error.
    `preflight()` (if set) returns (ok, detail) and does per-target setup (e.g. login). `cap`
    is a per-run drip limit; `alert_stuck` excludes drip-capped targets from age-based
    alerting (old-but-pending is normal when a cap throttles delivery)."""
    name: str
    enabled: bool
    post: object
    preflight: object = None
    cap: int = None
    alert_stuck: bool = True
    sent: int = 0
    fail: int = 0

    def __post_init__(self):
        self.wanted = self.enabled   # snapshot intent before preflight/degrade can flip it


# --- per-target adapters: normalize each target to the uniform post() contract above ---
def _post_semble(m, urls, author):
    ok, auth_failed = relay.relay_to_semble(m, urls, author)
    if ok:
        return
    if auth_failed:
        raise AuthDegrade("check SEMBLE_APP_PASSWORD" if relay.SEMBLE_SESSION_AUTH
                          else "rotate SEMBLE_API_KEY")
    raise RuntimeError("Semble post failed")            # transient: retry next run


def _post_bluesky(m, urls, author):
    relay.post_to_bluesky(m, urls, author)              # raises on error (treated transient)


def _post_mastodon(m, urls, author):
    try:
        relay.post_to_mastodon(m, urls, author)
    except relay.MastodonAuthError as e:
        raise AuthDegrade(f"rotate MASTODON_ACCESS_TOKEN: {e}")


def _preflight_semble():
    return relay.semble_check()


def _preflight_bluesky():
    """Log in with 3x backoff — bsky.social's login endpoint blips intermittently from CI.
    On success stashes the client on relay.bsky (post_to_bluesky / make_embed read it)."""
    from atproto import Client as BskyClient
    err = None
    for attempt in range(3):
        try:
            c = BskyClient(base_url=relay.ATPROTO_PDS)
            c.login(relay.ATPROTO_HANDLE, relay.ATPROTO_APP_PASSWORD)
            relay.bsky = c
            return True, "logged in"
        except Exception as e:
            err = e
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    return False, (f"{type(err).__name__}: {err}".strip(": ") if err else "unknown error")


def _preflight_mastodon():
    ok, detail = relay.mastodon_check()
    if ok:
        relay.mastodon_ensure_bot()
    return ok, detail


def run_preflight(targets):
    """Verify each enabled target's credentials up front. A failure degrades that target
    (disabled for the cycle) silently — a single failed cycle isn't actionable; sustained
    failure is caught at end of run. Never blocks the other targets."""
    for t in targets:
        if not t.enabled or t.preflight is None:
            continue
        ok, detail = t.preflight()
        print(f"[preflight] {t.name}: {detail}")
        if not ok:
            print(f"[degraded] {t.name} preflight failed ({detail}); "
                  "skipping it this cycle (links retry next run)")
            t.enabled = False


def post_message(targets, m, urls, author, ledger):
    """Post one message to every enabled target that hasn't already posted it. Mutates
    `ledger` and each target's counters; returns True if at least one target posted. Systemic
    auth failure disables that target for the rest of the cycle; a transient error just counts
    and leaves the link unledgered to retry next run. (Pure of Discord/network beyond the
    injected post() adapters, so it's unit-tested with fakes.)"""
    mid = str(m.id)
    did_any = False
    for t in targets:
        if not t.enabled or (t.name, mid) in ledger:
            continue
        if t.cap is not None and t.sent >= t.cap:
            continue                     # drip cap hit: leave unledgered, continue next run
        try:
            t.post(m, urls, author)
            ledger.add((t.name, mid))
            t.sent += 1
            did_any = True
            print(f"  [{t.name}] posted msg {mid} ({author})")
        except AuthDegrade as e:
            print(f"[degraded] {t.name} rejected our credentials mid-run — {e}; "
                  "skipping it this cycle (unposted links retry next run)")
            t.enabled = False
            t.fail += 1
        except Exception as e:
            print(f"  [{t.name}][ERR] msg {mid}: {e}")
            t.fail += 1
    return did_any


@client.event
async def on_ready():
    print(f"[OK] Logged in as {client.user}")
    chans = [c for g in client.guilds for c in g.text_channels
             if relay.CHANNEL_MATCH in c.name.lower()]
    all_msgs = []
    for c in chans:
        ms = await collect(c)
        print(f"  #{c.name} ({c.id}): {len(ms)} link-messages")
        all_msgs += ms
    all_msgs.sort(key=lambda mu: mu[0].created_at)

    total_urls = sum(len(u) for _, u in all_msgs)
    with_img = sum(1 for m, _ in all_msgs
                   if any((a.content_type or "").startswith("image/") for a in m.attachments))
    multi = sum(1 for _, u in all_msgs if len(u) > 1)
    print(f"\nTOTAL: {len(all_msgs)} link-messages | {total_urls} links | "
          f"{with_img} with image(s) | {multi} with multiple links")

    # Always refresh the feed archive (independent of posting) so the RSS/HTML feed
    # reflects the channel even on scan-only runs.
    n_feed = update_feed_archive(all_msgs)
    print(f"[feed] archive now holds {n_feed} item(s) -> {FEED_PATH}")

    print("--- sample: oldest 3 ---")
    for m, u in all_msgs[:3]:
        print(f"  [{m.created_at:%Y-%m-%d}] {m.author.display_name}: "
              f"{(m.content or '')[:100].replace(chr(10),' ')}  | {len(u)} link(s)")
    print("--- sample: newest 3 ---")
    for m, u in all_msgs[-3:]:
        print(f"  [{m.created_at:%Y-%m-%d}] {m.author.display_name}: "
              f"{(m.content or '')[:100].replace(chr(10),' ')}  | {len(u)} link(s)")

    if SEED_ONLY:
        for m, _ in all_msgs:
            ledger.add(("semble", str(m.id)))
            ledger.add(("bluesky", str(m.id)))
        save_ledger(ledger)
        print(f"\nSEED ONLY — marked {len(all_msgs)} messages as done (no posting). "
              f"Ledger: {len(ledger)} entries. Future runs post only NEW messages.")
        await client.close()
        return

    if not RUN:
        print("\nSCAN ONLY — nothing posted. Re-run with --run both to post.")
        await client.close()
        return

    print(f"\nPOSTING (semble={DO_SEMBLE}, bluesky={DO_BLUESKY}, "
          f"mastodon={DO_MASTODON and relay.MASTODON_ENABLE}, max={MAX}) ...")
    relay.DRY_RUN = False

    # Build the target table: preflight, the posting loop, degrade, the stuck-check and the
    # summary all iterate this one list, so each target's behavior lives in a single row.
    targets = [
        Target("semble", DO_SEMBLE, post=_post_semble, preflight=_preflight_semble),
        Target("bluesky", DO_BLUESKY, post=_post_bluesky, preflight=_preflight_bluesky),
        # Mastodon: dormant unless a token is set; drip-capped, so age-based stuck-alerting
        # doesn't apply (old-but-pending is normal under the cap) -> alert_stuck=False.
        Target("mastodon", DO_MASTODON and relay.MASTODON_ENABLE, post=_post_mastodon,
               preflight=_preflight_mastodon, cap=relay.MASTODON_MAX_PER_RUN, alert_stuck=False),
    ]

    run_preflight(targets)

    posted = 0
    for m, urls in all_msgs:
        if MAX is not None and posted >= MAX:
            print(f"[stop] reached --max {MAX}")
            break
        if post_message(targets, m, urls, m.author.display_name, ledger):
            posted += 1
            save_ledger(ledger)
            time.sleep(2)  # gentle pacing

    save_ledger(ledger)

    # SUSTAINED-failure alerting. Transient blips above degraded silently; here we alert
    # (non-zero exit → workflow ping) only if a link we MEANT to post (t.wanted, snapshotted
    # before degrade) has stayed undelivered longer than STUCK_AFTER_HOURS — it survived
    # multiple retry cycles, so it's a real credential/outage problem, not a hiccup. A one-off
    # failed cycle (idle, or with a freshly-arrived link) never trips this. Drip-capped
    # targets are excluded (alert_stuck=False): old-but-pending is normal when a cap throttles.
    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=STUCK_AFTER_HOURS)
    for t in targets:
        if not (t.wanted and t.alert_stuck):
            continue
        stuck = relay.links_stuck_since(all_msgs, ledger, t.name, now, max_age)
        if stuck:
            oldest_h = max((now - m.created_at).total_seconds() for m in stuck) / 3600
            _fail(f"{len(stuck)} {t.name} link(s) stuck >{STUCK_AFTER_HOURS}h "
                  f"(oldest {oldest_h:.0f}h) — {t.name} has failed across multiple cycles; "
                  f"check its auth/outage")

    per_target = "  ".join(f"{t.name}={t.sent}/{t.fail}" for t in targets)  # posted/failed
    summary = f"posted={posted}  [{per_target}]  ledger={len(ledger)}"
    print(f"\nDONE. {summary}")
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        try:
            with open(step, "a") as fh:
                fh.write(f"### relay catch-up\n\n`{summary}`\n")
        except OSError:
            pass
    await client.close()


if __name__ == "__main__":
    client.run(relay.DISCORD_TOKEN)
    raise SystemExit(EXIT_CODE)
