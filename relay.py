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
import uuid
import urllib.request
import urllib.error
import http.cookiejar
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
        v = re.split(r"\s+#", v, 1)[0]  # drop inline "  # comment" (keeps '#' inside values)
        os.environ.setdefault(k.strip(), v.strip())


load_env()

DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")  # empty is fine for importing (e.g. tests)
CHANNEL_MATCH = os.environ.get("DISCORD_CHANNEL_MATCH", "papers-links-n-sharing").lower()
DRY_RUN = os.environ.get("DRY_RUN", "1") not in ("0", "false", "False", "")
INCLUDE_AUTHOR = os.environ.get("INCLUDE_AUTHOR", "1") not in ("0", "false", "")
INCLUDE_IMAGES = os.environ.get("INCLUDE_IMAGES", "1") not in ("0", "false", "")
INCLUDE_LINK_CARD = os.environ.get("INCLUDE_LINK_CARD", "1") not in ("0", "false", "")

ATPROTO_HANDLE = os.environ.get("ATPROTO_HANDLE", "")
ATPROTO_APP_PASSWORD = os.environ.get("ATPROTO_APP_PASSWORD", "")
ATPROTO_PDS = os.environ.get("ATPROTO_PDS", "https://bsky.social")

# Semble (network.cosmik) — add each shared link as a card to one or more collections.
# Two auth modes, session preferred:
#   1. App-password SESSION (durable): SEMBLE_HANDLE + SEMBLE_APP_PASSWORD -> createSession
#      mints a fresh cookie session each run. This is the reliable path: sk_ "API keys"
#      silently ride on a login session that Semble's backend deletes, after which reads
#      keep working but WRITES 401 ("session was deleted by another process"). Minting a
#      new session per run self-heals that. A 401 mid-run re-logs in once and retries.
#   2. Legacy Bearer sk_ key (fallback): used only when no app password is configured.
SEMBLE_API_KEY = os.environ.get("SEMBLE_API_KEY", "")
SEMBLE_HANDLE = os.environ.get("SEMBLE_HANDLE", "")          # collection owner, e.g. lchoshen.bsky.social
SEMBLE_APP_PASSWORD = os.environ.get("SEMBLE_APP_PASSWORD", "")
SEMBLE_COLLECTION_IDS = [c.strip() for c in os.environ.get("SEMBLE_COLLECTION_IDS", "").split(",") if c.strip()]
SEMBLE_SESSION_AUTH = bool(SEMBLE_HANDLE and SEMBLE_APP_PASSWORD)   # prefer session over sk_ key
SEMBLE_ENABLE = bool(SEMBLE_COLLECTION_IDS and (SEMBLE_SESSION_AUTH or SEMBLE_API_KEY)) and \
    os.environ.get("SEMBLE_ENABLE", "1") not in ("0", "false", "")
SEMBLE_ADD_URL = "https://api.semble.so/api/network.cosmik.card.addUrl"
SEMBLE_LIST_MINE = "https://api.semble.so/api/network.cosmik.collection.listMine"
SEMBLE_CREATE_SESSION = "https://api.semble.so/api/network.cosmik.server.createSession"

# Post to Bluesky? (separate toggle so you can run Semble-only or Bluesky-only)
BLUESKY_ENABLE = os.environ.get("BLUESKY_ENABLE", "1") not in ("0", "false", "")

# Mastodon (e.g. sigmoid.social) — free, open API. Activates only when a token is set,
# so it stays dormant until you paste one in. Token from: Preferences -> Development ->
# New application (scopes: write:statuses, write:media, write:accounts / profile).
MASTODON_BASE = os.environ.get("MASTODON_BASE_URL", "https://sigmoid.social").rstrip("/")
MASTODON_TOKEN = os.environ.get("MASTODON_ACCESS_TOKEN", "")
MASTODON_ENABLE = bool(MASTODON_TOKEN) and \
    os.environ.get("MASTODON_ENABLE", "1") not in ("0", "false", "")
MASTODON_LIMIT = int(os.environ.get("MASTODON_LIMIT", "480"))     # margin under the 500 cap
# Drip cap: cap Mastodon posts per run so a backlog trickles out instead of flooding the
# public timeline (sigmoid rule). Steady state is ~0-1/run, so this only bites on catch-up.
MASTODON_MAX_PER_RUN = int(os.environ.get("MASTODON_MAX_PER_RUN", "8"))
MASTODON_URL_LEN = 23    # Mastodon counts every URL as 23 chars regardless of length

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


# Tokens ending in "." that are NOT sentence ends (kept lowercase, dot-stripped).
ABBREV = {"e.g", "i.e", "eg", "ie", "al", "et", "fig", "vs", "dr", "mr", "mrs",
          "ms", "prof", "st", "cf", "etc", "no", "vol", "eq", "sec", "ref",
          "pp", "approx", "resp", "inc", "ltd", "figs", "eqs"}


def _default_url_eff(u):
    """Bluesky: a URL costs its shortened *display* length."""
    return len(display_for(u))


def _url_reductions(text, url_eff=None):
    """(start, end, saved) per URL: full length minus its *effective* (counted) length,
    so a very long URL (e.g. a Scholar alert link) doesn't force a needless split.
    `url_eff(url)->int` defaults to Bluesky's shortened display; Mastodon passes a flat 23."""
    if url_eff is None:
        url_eff = _default_url_eff
    return [(m.start(), m.end(), len(m.group(0)) - url_eff(m.group(0)))
            for m in URL_RE.finditer(text)]


def _eff(text, a, b, red):
    """Effective (display) length of text[a:b]. URLs contain no whitespace, so each
    URL is always fully inside one chunk -> subtract its display saving in full."""
    n = b - a
    for s, e, saved in red:
        if a <= s and e <= b:
            n -= saved
    return n


def _is_sentence_end(text, i, red):
    """Whether text[i] ('.', '!' or '?') really ends a sentence (not a decimal,
    arXiv id, URL, or abbreviation like 'e.g.'/'et al.')."""
    if any(s <= i < e for s, e, _ in red):      # inside a URL
        return False
    if text[i] in "!?":
        return True
    if i > 0 and text[i - 1].isdigit():          # 3.14, 2606.24579, v2.
        return False
    j = i                                        # trailing alpha run before the dot
    while j > 0 and text[j - 1].isalpha():
        j -= 1
    word = text[j:i].lower()
    if len(word) <= 1:                           # "e.g.", "U.S.", initials
        return False
    return word not in ABBREV


def _boundaries(text, red):
    """Every whitespace run as (content_end, next_start, priority):
    4 = paragraph (newline), 3 = sentence, 2 = clause (,;:), 1 = plain space."""
    out, i, n = [], 0, len(text)
    while i < n:
        if not text[i].isspace():
            i += 1
            continue
        a = i
        while i < n and text[i].isspace():
            i += 1
        prev = text[a - 1] if a > 0 else ""
        if "\n" in text[a:i]:
            p = 4
        elif prev in ".!?" and _is_sentence_end(text, a - 1, red):
            p = 3
        elif prev in ",;:":
            p = 2
        else:
            p = 1
        out.append((a, i, p))
    return out


def chunk_text(text, reserve=0, limit=None, url_eff=None):
    """Split into <=limit posts, preferring sentence > clause > word breaks and
    balancing sizes so a thread doesn't end in a tiny orphan. `reserve` chars are
    kept free on the LAST post (for the author suffix). `limit`/`url_eff` default to
    Bluesky (290 chars, shortened-URL display); Mastodon passes 480 and a flat-23 url_eff."""
    if limit is None:
        limit = LIMIT
    text = text.strip()
    if not text:
        return [""]
    red = _url_reductions(text, url_eff)
    if _eff(text, 0, len(text), red) <= limit:
        chunks = [text]
    else:
        bounds = _boundaries(text, red)
        chunks, start = [], 0
        while start < len(text):
            if _eff(text, start, len(text), red) <= limit:   # remainder fits
                chunks.append(text[start:].strip())
                break
            remaining = _eff(text, start, len(text), red)
            target = remaining / (-(-remaining // limit))     # balanced size (ceil posts)
            feasible = [(a, b, p) for (a, b, p) in bounds
                        if a > start < len(text)
                        and 0 < _eff(text, start, a, red) <= limit]
            if not feasible:                                  # unbreakable token > limit
                chunks.append(text[start:start + limit])
                start += limit
                continue
            accept = [x for x in feasible
                      if _eff(text, start, x[0], red) >= target * 0.6]  # not too short
            a, b, _ = min(accept or feasible,
                          key=lambda x: (-x[2], abs(_eff(text, start, x[0], red) - target)))
            chunks.append(text[start:a].strip())
            start = b
    if not chunks:
        chunks = [""]
    # keep room for the author suffix on the last post
    last = chunks[-1]
    if reserve and last and _eff(last, 0, len(last), _url_reductions(last, url_eff)) + reserve > limit:
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


class SembleAuthError(Exception):
    """Semble rejected our credentials (401/403). Systemic — not a per-URL problem."""


# Cookie-backed opener for session auth (mode 1). Lazily created by _semble_login().
_semble_opener = None


def _semble_login():
    """Mint a fresh Semble session from the app password and return a cookie-backed
    opener authorized for WRITES. Raises SembleAuthError if the handle/app-password
    is rejected. (Semble's createSession sets accessToken + refreshToken cookies.)"""
    global _semble_opener
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = json.dumps({"identifier": SEMBLE_HANDLE, "appPassword": SEMBLE_APP_PASSWORD}).encode("utf-8")
    req = urllib.request.Request(SEMBLE_CREATE_SESSION, data=body, method="POST",
                                 headers={"Content-Type": "application/json", **UA})
    try:
        with opener.open(req, timeout=45) as r:
            r.read()
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise SembleAuthError(f"createSession HTTP {e.code} — SEMBLE_HANDLE / "
                                  "SEMBLE_APP_PASSWORD rejected (create a fresh Bluesky App Password)")
        raise
    _semble_opener = opener
    return opener


def _semble_http(url, method, data, timeout):
    """One authenticated Semble call. Session mode uses the cookie opener; key mode uses
    the sk_ Bearer header. Raises urllib errors to the caller for retry/re-login logic."""
    headers = {**UA}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if SEMBLE_SESSION_AUTH:
        opener = _semble_opener or _semble_login()
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    headers["Authorization"] = f"Bearer {SEMBLE_API_KEY}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _semble_request(url, method="GET", body=None, timeout=90, retries=2):
    """Call the Semble API. Retries transient (5xx / network / timeout) errors with
    backoff. On 401/403 in session mode, re-logs in ONCE and retries (a session Semble
    deleted mid-run is recoverable); otherwise raises SembleAuthError."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    last = None
    relogged = False
    for attempt in range(retries + 1):
        try:
            return _semble_http(url, method, data, timeout)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                if SEMBLE_SESSION_AUTH and not relogged:
                    relogged = True
                    _semble_login()          # raises SembleAuthError if app-password is bad
                    try:                     # immediate retry with the fresh session
                        return _semble_http(url, method, data, timeout)
                    except urllib.error.HTTPError as e2:
                        if e2.code in (401, 403):
                            raise SembleAuthError(f"HTTP {e2.code} — Semble auth failed after re-login")
                        raise
                raise SembleAuthError(
                    f"HTTP {e.code} — Semble auth failed "
                    + ("(session/app-password)" if SEMBLE_SESSION_AUTH else "(sk_ API key invalid or expired)"))
            if e.code < 500:            # other 4xx: not retryable
                raise
            last = e                    # 5xx: retryable
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e                    # network/timeout: retryable
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    raise last if last else SembleAuthError("Semble request failed with no response")


def semble_add_url(url, note):
    """Add a URL as a card to the configured Semble collection(s)."""
    return _semble_request(SEMBLE_ADD_URL, "POST",
                           {"url": url, "note": note, "collectionIds": SEMBLE_COLLECTION_IDS})


def semble_check():
    """Preflight: verify we can actually WRITE. Returns (ok: bool, detail: str).
    Session mode does a real createSession (the credential writes use), which is why it
    catches the write-auth failure the old read-only listMine check silently missed."""
    if not SEMBLE_ENABLE:
        return True, "disabled"
    try:
        if SEMBLE_SESSION_AUTH:
            _semble_login()
            return True, f"ok (session as {SEMBLE_HANDLE})"
        _semble_request(SEMBLE_LIST_MINE, "GET", timeout=30, retries=1)
        return True, "ok (sk_ key; read-only check — writes not verified)"
    except SembleAuthError as e:
        return False, str(e)
    except Exception as e:
        return False, f"unreachable: {e}"


def links_stuck_since(all_msgs, ledger, target, now, max_age):
    """Messages whose link still isn't in `ledger` for `target` AND that are older than
    `max_age`. Because every run re-attempts unledgered links, message age is a stateless
    proxy for "how long has this link been failing to post" — so this is the set that has
    been undelivered across MULTIPLE cycles (a sustained failure), as opposed to a one-off
    transient blip on a freshly-arrived link. Alerting keys on this, not per-cycle errors.

    `all_msgs` is the [(discord.Message, [url,...]), ...] list; `now` and `max_age` are a
    timezone-aware datetime and a timedelta (passed in so callers/tests control the clock)."""
    return [m for (m, _urls) in all_msgs
            if (target, str(m.id)) not in ledger and (now - m.created_at) > max_age]


def relay_to_semble(message, urls, author):
    """Add each (deduped) URL in the message to Semble.
    Returns (all_ok: bool, auth_failed: bool)."""
    if not SEMBLE_ENABLE:
        return True, False
    text_only = URL_RE.sub("", message.content or "").strip()
    note = (text_only + "\n\n" if text_only else "") + f"— {author} · Discord #{message.channel.name}"
    all_ok, auth_failed = True, False
    for u in dict.fromkeys(urls):  # dedupe, preserve order
        if DRY_RUN:
            print(f"  [semble] would add {u}  -> collections {SEMBLE_COLLECTION_IDS}")
            continue
        try:
            res = semble_add_url(u, note)
            print(f"  [semble] added {u} -> card {res.get('urlCardId', '?')}")
        except SembleAuthError as e:
            print(f"  [semble][AUTH] {u}: {e}")
            all_ok, auth_failed = False, True
            break  # key is dead — stop hammering; the run will be failed loudly
        except Exception as e:
            print(f"  [semble][ERR] {u}: {e}")
            all_ok = False
    return all_ok, auth_failed


def post_to_bluesky(message, urls, author):
    """Post a message to Bluesky as a thread (uses the module-global `bsky` client).
    Shared by the live listener and the scheduled catch-up. Returns #posts made."""
    from atproto import client_utils, models
    suffix = f"\n— {author}" if INCLUDE_AUTHOR else ""
    chunks = chunk_text(message.content, reserve=len(suffix))
    if suffix and len(chunks[-1]) + len(suffix) <= LIMIT:
        chunks[-1] = chunks[-1] + suffix
    embed, _ = make_embed(message, urls)
    root_ref = parent_ref = None
    for i, c in enumerate(chunks):
        tb = build_richtext(c, client_utils)
        reply_to = (models.AppBskyFeedPost.ReplyRef(parent=parent_ref, root=root_ref)
                    if parent_ref is not None else None)
        resp = bsky.send_post(tb, reply_to=reply_to, embed=embed if i == 0 else None)
        ref = models.create_strong_ref(resp)
        if root_ref is None:
            root_ref = ref
        parent_ref = ref
        time.sleep(1)  # gentle rate limiting
    return len(chunks)


# ---------- mastodon (sigmoid.social & any Mastodon instance) ----------
class MastodonError(Exception):
    """Mastodon API returned an error."""


class MastodonAuthError(MastodonError):
    """Token invalid/expired (401/403) or lacks scope. Systemic, not per-post."""


def _mastodon_eff(u):
    return MASTODON_URL_LEN


def _multipart(file_field, filename, mime, data, fields=None):
    """Build a multipart/form-data body. Returns (bytes, content_type)."""
    boundary = uuid.uuid4().hex
    nl = b"\r\n"
    buf = []
    for k, v in (fields or {}).items():
        buf += [b"--", boundary.encode(), nl,
                f'Content-Disposition: form-data; name="{k}"'.encode(), nl, nl,
                str(v).encode(), nl]
    buf += [b"--", boundary.encode(), nl,
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode(), nl,
            f"Content-Type: {mime}".encode(), nl, nl, data, nl,
            b"--", boundary.encode(), b"--", nl]
    return b"".join(buf), f"multipart/form-data; boundary={boundary}"


def _mastodon_request(path, method="GET", body=None, raw=None, content_type=None,
                      timeout=45, retries=2):
    """Call the Mastodon API. Retries transient (5xx / network / timeout) with backoff;
    raises MastodonAuthError on 401/403; re-raises other 4xx."""
    url = MASTODON_BASE + path
    headers = {"Authorization": f"Bearer {MASTODON_TOKEN}", **UA}
    data = None
    if raw is not None:
        data = raw
        if content_type:
            headers["Content-Type"] = content_type
    elif body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read().decode("utf-8", "ignore")
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise MastodonAuthError(f"HTTP {e.code} — Mastodon token invalid/expired or missing scope")
            if e.code < 500:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "ignore")[:200]
                except Exception:
                    pass
                raise MastodonError(f"HTTP {e.code} {detail}")
            last = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    raise last


def mastodon_check():
    """Preflight: verify the token works. Returns (ok: bool, detail: str)."""
    if not MASTODON_ENABLE:
        return True, "disabled"
    try:
        acct = _mastodon_request("/api/v1/accounts/verify_credentials", "GET",
                                 timeout=20, retries=1)
        return True, f"ok (@{acct.get('username', '?')} on {MASTODON_BASE})"
    except MastodonAuthError as e:
        return False, str(e)
    except Exception as e:
        return False, f"unreachable: {e}"


def mastodon_ensure_bot():
    """Mark the account as a bot (sigmoid rule: software-driven accounts must be
    identified as bots). Idempotent, best-effort — never blocks posting."""
    if not MASTODON_ENABLE:
        return
    try:
        _mastodon_request("/api/v1/accounts/update_credentials", "PATCH",
                          body={"bot": True}, timeout=20, retries=1)
    except Exception as e:
        print(f"  [mastodon] could not set bot flag: {e}")


def _mastodon_media(message):
    """Upload image attachments; returns a list of media ids (best-effort — a failed
    upload is skipped, never blocks the post). Handles async (202) processing."""
    ids = []
    images = [a for a in message.attachments
              if (a.content_type or "").startswith("image/")]
    for a in images[:MAX_IMAGES]:
        data = download(a.url)
        if not data:
            continue
        try:
            raw, ctype = _multipart("file", getattr(a, "filename", None) or "image",
                                    a.content_type or "image/png", data,
                                    {"description": a.description or ""})
            res = _mastodon_request("/api/v2/media", "POST", raw=raw,
                                    content_type=ctype, timeout=90)
            mid = res.get("id")
            if mid and not res.get("url"):        # 202 accepted -> still processing
                for _ in range(6):
                    time.sleep(2)
                    chk = _mastodon_request(f"/api/v1/media/{mid}", "GET", timeout=30, retries=1)
                    if chk.get("url"):
                        break
            if mid:
                ids.append(mid)
        except MastodonAuthError:
            raise
        except Exception as e:
            print(f"  [mastodon][media] skip {getattr(a, 'filename', '?')}: {e}")
    return ids


def post_to_mastodon(message, urls, author):
    """Post a message to Mastodon as a thread. Root is public; thread replies are
    `unlisted` so a multi-post message doesn't flood the public timeline. Returns #posts."""
    suffix = f"\n— {author}" if INCLUDE_AUTHOR else ""
    chunks = chunk_text(message.content, reserve=len(suffix),
                        limit=MASTODON_LIMIT, url_eff=_mastodon_eff)
    if suffix:
        last = chunks[-1]
        red = _url_reductions(last, _mastodon_eff)
        if _eff(last, 0, len(last), red) + len(suffix) <= MASTODON_LIMIT:
            chunks[-1] = last + suffix
    if DRY_RUN:
        print(f"  [mastodon] would post {len(chunks)} status(es) to {MASTODON_BASE}")
        return len(chunks)
    media_ids = _mastodon_media(message) if INCLUDE_IMAGES else []
    reply_to = None
    for i, c in enumerate(chunks):
        body = {"status": c, "visibility": "public" if i == 0 else "unlisted"}
        if reply_to:
            body["in_reply_to_id"] = reply_to
        if i == 0 and media_ids:
            body["media_ids"] = media_ids
        res = _mastodon_request("/api/v1/statuses", "POST", body=body, timeout=45)
        reply_to = res.get("id")
        time.sleep(1)  # gentle pacing
    return len(chunks)


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
          f"Semble={'on -> ' + str(SEMBLE_COLLECTION_IDS) if SEMBLE_ENABLE else 'off'}  "
          f"Mastodon={'on -> ' + MASTODON_BASE if MASTODON_ENABLE else 'off'}")
    if not DRY_RUN and BLUESKY_ENABLE:
        from atproto import Client as BskyClient
        bsky = BskyClient(base_url=ATPROTO_PDS)
        bsky.login(ATPROTO_HANDLE, ATPROTO_APP_PASSWORD)
        print(f"[OK] Bluesky logged in as {ATPROTO_HANDLE} via {ATPROTO_PDS}")
    if not DRY_RUN and MASTODON_ENABLE:
        mastodon_ensure_bot()
        print(f"[OK] Mastodon ready on {MASTODON_BASE}")
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

    print(f"[RELAY] {author} in #{message.channel.name}: {len(urls)} link(s)")

    # --- Semble ---
    ok, auth_failed = relay_to_semble(message, urls, author)
    if auth_failed:
        _hint = "check SEMBLE_APP_PASSWORD" if SEMBLE_SESSION_AUTH else "set a fresh SEMBLE_API_KEY"
        print(f"  [semble][AUTH] credentials rejected — {_hint}")

    # --- Bluesky (shared code path with the scheduled catch-up) ---
    if BLUESKY_ENABLE and not DRY_RUN:
        n = post_to_bluesky(message, urls, author)
        print(f"  [bluesky] posted {n} post(s)")

    # --- Mastodon ---
    if MASTODON_ENABLE and not DRY_RUN:
        try:
            n = post_to_mastodon(message, urls, author)
            print(f"  [mastodon] posted {n} post(s)")
        except Exception as e:
            print(f"  [mastodon][ERR] {e}")
    print()


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN not set (put it in .env or the environment)")
    client.run(DISCORD_TOKEN)
