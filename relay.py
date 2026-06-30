"""Discord -> Bluesky (ATProto) relay.

Relays messages that contain links, from any channel whose name matches
DISCORD_CHANNEL_MATCH (default 'papers-links-n-sharing'), to a Bluesky account.

Formatting:
  - URLs are clickable (facets).
  - Long messages are split into a reply thread (300-grapheme posts).
  - Author name is appended at the END of the last post, only if it fits.
  - First post gets an embed: image attachments if present, else a link card
    for the first URL (Bluesky allows only one embed per post).

Run dry first (no posting, no Bluesky creds needed):
    DRY_RUN=1 python3 relay.py
Then go live once ATPROTO_HANDLE/ATPROTO_APP_PASSWORD are set in .env:
    DRY_RUN=0 python3 relay.py
"""
import os
import re
import json
import time
import html
import urllib.request
from urllib.parse import urlparse

import discord


# ---------- config ----------
def load_env(path=".env"):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env()

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_MATCH = os.environ.get("DISCORD_CHANNEL_MATCH", "papers-links-n-sharing").lower()
DRY_RUN = os.environ.get("DRY_RUN", "1") not in ("0", "false", "False", "")
INCLUDE_AUTHOR = os.environ.get("INCLUDE_AUTHOR", "1") not in ("0", "false", "")
INCLUDE_IMAGES = os.environ.get("INCLUDE_IMAGES", "1") not in ("0", "false", "")
INCLUDE_LINK_CARD = os.environ.get("INCLUDE_LINK_CARD", "1") not in ("0", "false", "")

ATPROTO_HANDLE = os.environ.get("ATPROTO_HANDLE", "")
ATPROTO_APP_PASSWORD = os.environ.get("ATPROTO_APP_PASSWORD", "")
ATPROTO_PDS = os.environ.get("ATPROTO_PDS", "https://bsky.social")

# Semble (network.cosmik) — add each shared link as a card to one or more collections
SEMBLE_API_KEY = os.environ.get("SEMBLE_API_KEY", "")
SEMBLE_COLLECTION_IDS = [c.strip() for c in os.environ.get("SEMBLE_COLLECTION_IDS", "").split(",") if c.strip()]
SEMBLE_ENABLE = bool(SEMBLE_API_KEY and SEMBLE_COLLECTION_IDS) and \
    os.environ.get("SEMBLE_ENABLE", "1") not in ("0", "false", "")
SEMBLE_ADD_URL = "https://api.semble.so/api/network.cosmik.card.addUrl"

# Post to Bluesky? (separate toggle so you can run Semble-only or Bluesky-only)
BLUESKY_ENABLE = os.environ.get("BLUESKY_ENABLE", "1") not in ("0", "false", "")

LIMIT = 290          # leave margin under Bluesky's 300-grapheme cap
MAX_IMAGES = 4       # Bluesky max images per post
URL_RE = re.compile(r"https?://[^\s<>()]+[^\s<>().,!?;:'\"]")
UA = {"User-Agent": "Mozilla/5.0 (compatible; discord-atproto-bridge/1.0)"}


# ---------- text formatting ----------
def display_for(url):
    """Shortened display text for long URLs (full URL is still the link target)."""
    if len(url) <= 60:
        return url
    netloc = urlparse(url).netloc
    return f"{netloc}/…"


def tok_len(token):
    return len(display_for(token)) if URL_RE.fullmatch(token) else len(token)


def chunk_text(text, reserve=0):
    """Split text into <=LIMIT pieces at whitespace boundaries.
    `reserve` characters are kept free in the LAST chunk (for the author suffix)."""
    words = text.split()
    chunks, cur, cur_len = [], [], 0
    for w in words:
        wl = tok_len(w)
        add = wl + (1 if cur else 0)
        if cur and cur_len + add > LIMIT:
            chunks.append(" ".join(cur))
            cur, cur_len = [], 0
            add = wl
        if wl > LIMIT and not URL_RE.fullmatch(w):
            # hard-split an over-long non-URL token
            for i in range(0, len(w), LIMIT):
                chunks.append(w[i:i + LIMIT])
            continue
        cur.append(w)
        cur_len += add
    if cur:
        chunks.append(" ".join(cur))
    if not chunks:
        chunks = [""]
    # If author suffix won't fit on the last chunk, start a new last chunk.
    if reserve and len(chunks[-1]) + reserve > LIMIT and len(chunks[-1]) > 0:
        chunks.append("")
    return chunks


def build_richtext(chunk, client_utils):
    """Build a TextBuilder so URLs in the chunk are clickable."""
    tb = client_utils.TextBuilder()
    pos = 0
    for m in URL_RE.finditer(chunk):
        if m.start() > pos:
            tb.text(chunk[pos:m.start()])
        url = m.group(0)
        tb.link(display_for(url), url)
        pos = m.end()
    if pos < len(chunk):
        tb.text(chunk[pos:])
    return tb


# ---------- link card (OpenGraph) ----------
def fetch_og(url):
    """Best-effort OpenGraph (title, description, thumb_bytes). Never raises."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read(600_000).decode("utf-8", "ignore")
    except Exception:
        return None
    def og(prop):
        m = re.search(
            rf'<meta[^>]+(?:property|name)=["\']og:{prop}["\'][^>]+content=["\']([^"\']+)["\']',
            raw, re.I)
        if not m:
            m = re.search(
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:{prop}["\']',
                raw, re.I)
        return html.unescape(m.group(1)) if m else ""
    title = og("title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
        title = html.unescape(m.group(1).strip()) if m else url
    desc = og("description")
    thumb = None
    img_url = og("image")
    if img_url:
        try:
            req = urllib.request.Request(img_url, headers=UA)
            with urllib.request.urlopen(req, timeout=8) as r:
                data = r.read(2_000_000)
            if data:
                thumb = data
        except Exception:
            thumb = None
    return {"title": title[:300], "description": desc[:1000], "thumb": thumb, "uri": url}


def download(url, cap=2_000_000):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.read(cap)
    except Exception:
        return None


def semble_add_url(url, note):
    """Add a URL as a card to the configured Semble collection(s). Raises on HTTP error."""
    body = json.dumps({
        "url": url,
        "note": note,
        "collectionIds": SEMBLE_COLLECTION_IDS,
    }).encode("utf-8")
    headers = {"Authorization": f"Bearer {SEMBLE_API_KEY}",
               "Content-Type": "application/json", **UA}
    req = urllib.request.Request(SEMBLE_ADD_URL, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=90) as r:  # Semble fetches metadata server-side; can be slow
        return json.loads(r.read().decode("utf-8", "ignore"))


def relay_to_semble(message, urls, author):
    """Add each (deduped) URL in the message to Semble, with the message text as a note."""
    if not SEMBLE_ENABLE:
        return
    text_only = URL_RE.sub("", message.content or "").strip()
    note = (text_only + "\n\n" if text_only else "") + f"— {author} · Discord #{message.channel.name}"
    all_ok = True
    for u in dict.fromkeys(urls):  # dedupe, preserve order
        if DRY_RUN:
            print(f"  [semble] would add {u}  -> collections {SEMBLE_COLLECTION_IDS}")
            continue
        try:
            res = semble_add_url(u, note)
            print(f"  [semble] added {u} -> card {res.get('urlCardId', '?')}")
        except Exception as e:
            print(f"  [semble][ERR] {u}: {e}")
            all_ok = False
    return all_ok


# ---------- discord ----------
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
bsky = None  # set on_ready when not dry-run


@client.event
async def on_ready():
    global bsky
    print(f"[OK] Logged in as {client.user}  (DRY_RUN={DRY_RUN})")
    matched = []
    for g in client.guilds:
        for ch in g.text_channels:
            if CHANNEL_MATCH in ch.name.lower():
                matched.append(ch)
                print(f"[OK] Watching #{ch.name} ({ch.id}) in '{g.name}'")
    if not matched:
        print(f"[WARN] No channels matched '{CHANNEL_MATCH}'.")
    print(f"[CFG] Bluesky={'on' if BLUESKY_ENABLE else 'off'}  "
          f"Semble={'on -> ' + str(SEMBLE_COLLECTION_IDS) if SEMBLE_ENABLE else 'off'}")
    if not DRY_RUN and BLUESKY_ENABLE:
        from atproto import Client as BskyClient
        bsky = BskyClient(base_url=ATPROTO_PDS)
        bsky.login(ATPROTO_HANDLE, ATPROTO_APP_PASSWORD)
        print(f"[OK] Bluesky logged in as {ATPROTO_HANDLE} via {ATPROTO_PDS}")
    print("Listening. Ctrl+C to stop.\n")


def make_embed(message, urls):
    """Image embed (preferred) or external link card. Returns (embed, None on dry)."""
    from atproto import models
    images = [a for a in message.attachments
              if (a.content_type or "").startswith("image/")]
    if INCLUDE_IMAGES and images:
        blobs = []
        for a in images[:MAX_IMAGES]:
            data = download(a.url)
            if data:
                blobs.append((data, a.description or ""))
        if blobs:
            if DRY_RUN:
                return ("IMAGES", [f"{len(d)}B" for d, _ in blobs])
            uploaded = [models.AppBskyEmbedImages.Image(
                alt=alt, image=bsky.upload_blob(data).blob) for data, alt in blobs]
            return (models.AppBskyEmbedImages.Main(images=uploaded), None)
    if INCLUDE_LINK_CARD and urls:
        og = fetch_og(urls[0])
        if og:
            if DRY_RUN:
                return ("LINK_CARD", f"{og['title']!r} (thumb={'yes' if og['thumb'] else 'no'})")
            thumb_blob = bsky.upload_blob(og["thumb"]).blob if og["thumb"] else None
            ext = models.AppBskyEmbedExternal.External(
                uri=og["uri"], title=og["title"], description=og["description"],
                thumb=thumb_blob)
            return (models.AppBskyEmbedExternal.Main(external=ext), None)
    return (None, None)


@client.event
async def on_message(message):
    if message.author.bot or message.author == client.user:
        return
    if CHANNEL_MATCH not in message.channel.name.lower():
        return
    urls = URL_RE.findall(message.content or "")
    if not urls:
        return  # only relay messages that contain links

    author = message.author.display_name

    # --- Semble: add one card per link to the collection(s) ---
    relay_to_semble(message, urls, author)

    # --- Bluesky ---
    if not BLUESKY_ENABLE:
        return
    suffix = f"\n— {author}" if INCLUDE_AUTHOR else ""
    chunks = chunk_text(message.content, reserve=len(suffix))
    # append author to last chunk if it fits
    if suffix and len(chunks[-1]) + len(suffix) <= LIMIT:
        chunks[-1] = chunks[-1] + suffix

    embed, dry_detail = make_embed(message, urls)

    print(f"[RELAY] {author} in #{message.channel.name}: {len(chunks)} post(s)"
          + (f", embed={embed if DRY_RUN else type(embed).__name__}" if embed else ""))

    if DRY_RUN:
        for i, c in enumerate(chunks):
            print(f"  ┌ post {i+1}/{len(chunks)} ({len(c)} chars)")
            print("  │ " + c.replace("\n", "\n  │ "))
        if embed:
            print(f"  └ embed: {embed} -> {dry_detail}")
        print()
        return

    # ---- live posting with threading ----
    from atproto import client_utils, models
    root_ref = parent_ref = None
    for i, c in enumerate(chunks):
        tb = build_richtext(c, client_utils)
        reply_to = None
        if parent_ref is not None:
            reply_to = models.AppBskyFeedPost.ReplyRef(parent=parent_ref, root=root_ref)
        try:
            resp = bsky.send_post(
                tb, reply_to=reply_to, embed=embed if i == 0 else None)
        except Exception as e:
            print(f"  [ERR] post {i+1} failed: {e}")
            break
        ref = models.create_strong_ref(resp)
        if root_ref is None:
            root_ref = ref
        parent_ref = ref
        time.sleep(1)  # gentle rate limiting
    print(f"  [OK] posted thread ({len(chunks)} post(s))\n")


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
