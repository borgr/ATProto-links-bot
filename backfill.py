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
DO_SEMBLE = RUN in ("semble", "both")
DO_BLUESKY = RUN in ("bluesky", "both")
AFTER = (datetime.now(timezone.utc) - timedelta(days=SINCE_DAYS)) if SINCE_DAYS else None

LEDGER_PATH = "backfill_done.json"


def load_ledger():
    if os.path.exists(LEDGER_PATH):
        try:
            return set(tuple(x) for x in json.load(open(LEDGER_PATH)))
        except Exception:
            return set()
    return set()


def save_ledger(ledger):
    json.dump([list(x) for x in ledger], open(LEDGER_PATH, "w"))


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
    global bsky, DO_SEMBLE
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

    print(f"\nPOSTING (semble={DO_SEMBLE}, bluesky={DO_BLUESKY}, max={MAX}) ...")
    relay.DRY_RUN = False

    # Preflight: verify credentials up front so a bad key fails fast and loudly
    # (the workflow turns a non-zero exit into a Discord alert).
    if DO_SEMBLE:
        ok, detail = relay.semble_check()
        print(f"[preflight] Semble key: {detail}")
        if not ok:
            # Degrade, don't abort: a dead Semble key must not stop Bluesky too.
            # _fail() still makes the run exit non-zero, so the alert fires.
            _fail(f"Semble preflight failed ({detail}) — set a fresh SEMBLE_API_KEY; "
                  "continuing with Bluesky only")
            DO_SEMBLE = False
    if DO_BLUESKY:
        from atproto import Client as BskyClient
        try:
            bsky = BskyClient(base_url=relay.ATPROTO_PDS)
            bsky.login(relay.ATPROTO_HANDLE, relay.ATPROTO_APP_PASSWORD)
        except Exception as e:
            _fail(f"Bluesky login failed: {e}")
            await client.close()
            return
        relay.bsky = bsky  # make_embed / post_to_bluesky use this
        print("[OK] Bluesky logged in")

    posted = semble_fail = bluesky_fail = 0
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
                    _fail("Semble rejected the API key mid-run — rotate SEMBLE_API_KEY")
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
        if did_any:
            posted += 1
            save_ledger(ledger)
            time.sleep(2)  # gentle pacing

    save_ledger(ledger)
    summary = (f"posted={posted}  semble_fail={semble_fail}  "
               f"bluesky_fail={bluesky_fail}  ledger={len(ledger)}")
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
