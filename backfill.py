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
bsky = None

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


@client.event
async def on_ready():
    global bsky, DO_SEMBLE, DO_BLUESKY, DO_MASTODON
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

    # Preflight: verify credentials up front so a bad key fails fast and loudly
    # (the workflow turns a non-zero exit into a Discord alert).
    if DO_SEMBLE:
        ok, detail = relay.semble_check()
        print(f"[preflight] Semble key: {detail}")
        if not ok:
            # Degrade, don't abort: a dead Semble credential must not stop Bluesky too.
            # _fail() still makes the run exit non-zero, so the alert fires.
            _hint = ("check SEMBLE_APP_PASSWORD (create a fresh Bluesky App Password)"
                     if relay.SEMBLE_SESSION_AUTH else "set a fresh SEMBLE_API_KEY")
            _fail(f"Semble preflight failed ({detail}) — {_hint}; continuing with Bluesky only")
            DO_SEMBLE = False
    if DO_MASTODON and not relay.MASTODON_ENABLE:
        DO_MASTODON = False              # no token configured -> dormant, skip silently
    elif DO_MASTODON:
        ok, detail = relay.mastodon_check()
        print(f"[preflight] Mastodon: {detail}")
        if not ok:
            _fail(f"Mastodon preflight failed ({detail}) — check MASTODON_ACCESS_TOKEN; "
                  "continuing without it")
            DO_MASTODON = False
        else:
            relay.mastodon_ensure_bot()
    if DO_BLUESKY:
        from atproto import Client as BskyClient
        # bsky.social's login endpoint occasionally returns a transient 5xx / rate-limit
        # (surfaces as an exception with an empty message). Retry with backoff instead of
        # failing the whole run on a one-off blip — mirrors the Semble retry policy.
        err = None
        for attempt in range(3):
            try:
                bsky = BskyClient(base_url=relay.ATPROTO_PDS)
                bsky.login(relay.ATPROTO_HANDLE, relay.ATPROTO_APP_PASSWORD)
                err = None
                break
            except Exception as e:
                err, bsky = e, None
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
        if bsky is None:
            # Degrade, don't abort: a transient Bluesky outage must not also block
            # Semble/Mastodon this cycle. _fail() still exits non-zero -> the alert fires,
            # and unposted links stay unledgered so they retry next run.
            detail = f"{type(err).__name__}: {err}".strip(": ") if err else "unknown error"
            _fail(f"Bluesky login failed after 3 attempts ({detail}) — likely a transient "
                  "bsky.social outage; continuing with other targets this cycle")
            DO_BLUESKY = False
        else:
            relay.bsky = bsky  # make_embed / post_to_bluesky use this
            print("[OK] Bluesky logged in")

    posted = semble_fail = bluesky_fail = mastodon_fail = mastodon_posted = 0
    for m, urls in all_msgs:
        if MAX is not None and posted >= MAX:
            print(f"[stop] reached --max {MAX}")
            break
        author = m.author.display_name
        did_any = False
        if DO_SEMBLE and ("semble", str(m.id)) not in ledger:
            ok, auth_failed = relay.relay_to_semble(m, urls, author)
            if ok:
                ledger.add(("semble", str(m.id)))  # only mark done if every URL succeeded
                did_any = True
            else:
                semble_fail += 1
                if auth_failed:                 # systemic: stop Semble, keep Bluesky
                    _hint = ("check SEMBLE_APP_PASSWORD" if relay.SEMBLE_SESSION_AUTH
                             else "rotate SEMBLE_API_KEY")
                    _fail(f"Semble rejected our credentials mid-run — {_hint}")
                    DO_SEMBLE = False
        if DO_BLUESKY and ("bluesky", str(m.id)) not in ledger:
            try:
                relay.post_to_bluesky(m, urls, author)
                ledger.add(("bluesky", str(m.id)))
                print(f"  [bluesky] posted msg {m.id} ({author})")
                did_any = True
            except Exception as e:
                print(f"  [bluesky][ERR] msg {m.id}: {e}")
                bluesky_fail += 1
        if DO_MASTODON and ("mastodon", str(m.id)) not in ledger:
            if mastodon_posted >= relay.MASTODON_MAX_PER_RUN:
                pass  # drip cap hit: leave unledgered so the backlog continues next run
            else:
                try:
                    relay.post_to_mastodon(m, urls, author)
                    ledger.add(("mastodon", str(m.id)))
                    mastodon_posted += 1
                    print(f"  [mastodon] posted msg {m.id} ({author})")
                    did_any = True
                except relay.MastodonAuthError as e:
                    _fail(f"Mastodon rejected the token mid-run — rotate MASTODON_ACCESS_TOKEN: {e}")
                    DO_MASTODON = False
                    mastodon_fail += 1
                except Exception as e:
                    print(f"  [mastodon][ERR] msg {m.id}: {e}")
                    mastodon_fail += 1
        if did_any:
            posted += 1
            save_ledger(ledger)
            time.sleep(2)  # gentle pacing

    save_ledger(ledger)
    summary = (f"posted={posted}  semble_fail={semble_fail}  "
               f"bluesky_fail={bluesky_fail}  mastodon_fail={mastodon_fail}  "
               f"mastodon_posted={mastodon_posted}  ledger={len(ledger)}")
    print(f"\nDONE. {summary}")
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        try:
            with open(step, "a") as fh:
                fh.write(f"### relay catch-up\n\n`{summary}`\n")
        except OSError:
            pass
    await client.close()


client.run(relay.DISCORD_TOKEN)
raise SystemExit(EXIT_CODE)
