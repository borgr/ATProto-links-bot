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


def post_bluesky(message, urls, author):
    from atproto import client_utils, models
    suffix = f"\n— {author}" if relay.INCLUDE_AUTHOR else ""
    chunks = relay.chunk_text(message.content, reserve=len(suffix))
    if suffix and len(chunks[-1]) + len(suffix) <= relay.LIMIT:
        chunks[-1] = chunks[-1] + suffix
    embed, _ = relay.make_embed(message, urls)
    root_ref = parent_ref = None
    for i, c in enumerate(chunks):
        tb = relay.build_richtext(c, client_utils)
        reply_to = (models.AppBskyFeedPost.ReplyRef(parent=parent_ref, root=root_ref)
                    if parent_ref is not None else None)
        resp = bsky.send_post(tb, reply_to=reply_to, embed=embed if i == 0 else None)
        ref = models.create_strong_ref(resp)
        if root_ref is None:
            root_ref = ref
        parent_ref = ref
        time.sleep(1)


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
    global bsky
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
    if DO_BLUESKY:
        from atproto import Client as BskyClient
        bsky = BskyClient(base_url=relay.ATPROTO_PDS)
        bsky.login(relay.ATPROTO_HANDLE, relay.ATPROTO_APP_PASSWORD)
        relay.bsky = bsky  # make_embed uploads blobs through this
        print("[OK] Bluesky logged in")

    posted = 0
    for m, urls in all_msgs:
        if MAX is not None and posted >= MAX:
            print(f"[stop] reached --max {MAX}")
            break
        author = m.author.display_name
        did_any = False
        if DO_SEMBLE and ("semble", str(m.id)) not in ledger:
            try:
                ok = relay.relay_to_semble(m, urls, author)
                if ok:
                    ledger.add(("semble", str(m.id)))  # only mark done if every URL succeeded
                did_any = True
            except Exception as e:
                print(f"  [semble][ERR] msg {m.id}: {e}")
        if DO_BLUESKY and ("bluesky", str(m.id)) not in ledger:
            try:
                post_bluesky(m, urls, author)
                ledger.add(("bluesky", str(m.id)))
                print(f"  [bluesky] posted msg {m.id} ({author})")
                did_any = True
            except Exception as e:
                print(f"  [bluesky][ERR] msg {m.id}: {e}")
        if did_any:
            posted += 1
            save_ledger(ledger)
            time.sleep(2)  # gentle pacing
    print(f"\nDONE. Posted {posted} message(s). Ledger: {len(ledger)} entries.")
    await client.close()


client.run(relay.DISCORD_TOKEN)
