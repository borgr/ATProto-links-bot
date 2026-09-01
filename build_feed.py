"""Render feed_items.json into a static site under docs/:
  - docs/feed.xml   Atom 1.0 feed (subscribe in any reader; bridge into anything)
  - docs/index.html a browsable, newest-first list of the shared links

Pure stdlib. Deterministic output (feed <updated> = newest item), so re-runs with no
new links produce no diff and the workflow's commit step is a no-op.

  python3 build_feed.py            # reads ./feed_items.json, writes ./docs/
"""
import os
import re
import json
import html
from datetime import datetime, timezone

FEED_PATH = os.environ.get("FEED_PATH", "feed_items.json")
OUT_DIR = os.environ.get("FEED_OUT_DIR", "docs")
BASE_URL = os.environ.get("PAGES_BASE_URL", "https://borgr.github.io/ATProto-links-bot").rstrip("/")
TITLE = os.environ.get("FEED_TITLE", "CoLab paper links")
SUBTITLE = os.environ.get(
    "FEED_SUBTITLE", "Links shared in the CoLab Discord #papers-links-n-sharing channels.")

URL_RE = re.compile(r"https?://[^\s<>()]+[^\s<>().,!?;:'\"]")


def load_items():
    try:
        items = json.load(open(FEED_PATH))
    except Exception:
        items = []
    # newest first for display; drop anything without a usable link
    items = [it for it in items if it.get("urls")]
    items.sort(key=lambda it: it.get("created_at", ""), reverse=True)
    return items


def discord_url(it):
    g, c, i = it.get("guild_id"), it.get("channel_id"), it.get("id")
    return f"https://discord.com/channels/{g}/{c}/{i}" if (g and c and i) else ""


def linkify(text):
    """Escape HTML, then turn bare URLs into anchors."""
    out, pos = [], 0
    for m in URL_RE.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        u = m.group(0)
        out.append(f'<a href="{html.escape(u)}">{html.escape(u)}</a>')
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out).replace("\n", "<br>")


def entry_title(it):
    """A readable title: first line of text, else the first URL's domain."""
    text = (it.get("text") or "").strip()
    first_line = text.splitlines()[0].strip() if text else ""
    first_line = re.sub(r"\s+", " ", URL_RE.sub("", first_line)).strip(" -–—|:")
    if first_line:
        return first_line[:120]
    u = it["urls"][0]
    return re.sub(r"^https?://(www\.)?", "", u).split("/")[0]


def rfc3339(iso):
    """Normalize an ISO timestamp to RFC-3339 with a trailing Z (Atom-friendly)."""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return "1970-01-01T00:00:00Z"


def build_atom(items):
    updated = rfc3339(items[0]["created_at"]) if items else "1970-01-01T00:00:00Z"
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<feed xmlns="http://www.w3.org/2005/Atom">',
           f'  <title>{html.escape(TITLE)}</title>',
           f'  <subtitle>{html.escape(SUBTITLE)}</subtitle>',
           f'  <link href="{BASE_URL}/feed.xml" rel="self"/>',
           f'  <link href="{BASE_URL}/"/>',
           f'  <id>{BASE_URL}/</id>',
           f'  <updated>{updated}</updated>']
    for it in items:
        u = it["urls"][0]
        body = it.get("text") or u
        extra = "".join(f'<p><a href="{html.escape(x)}">{html.escape(x)}</a></p>'
                        for x in it["urls"][1:])
        content = f"<p>{linkify(body)}</p>{extra}"
        src = discord_url(it)
        src_html = f'<p>Shared in <a href="{html.escape(src)}">Discord</a></p>' if src else ""
        out += ['  <entry>',
                f'    <title>{html.escape(entry_title(it))}</title>',
                f'    <link href="{html.escape(u)}"/>',
                f'    <id>tag:atproto-links-bot,{it["id"]}</id>',
                f'    <updated>{rfc3339(it["created_at"])}</updated>',
                f'    <author><name>{html.escape(it.get("author") or "unknown")}</name></author>',
                f'    <content type="html">{html.escape(content + src_html)}</content>',
                '  </entry>']
    out.append('</feed>')
    return "\n".join(out) + "\n"


def build_html(items):
    rows = []
    last_day = None
    for it in items:
        day = rfc3339(it["created_at"])[:10]
        if day != last_day:
            rows.append(f'<h2>{html.escape(day)}</h2>')
            last_day = day
        u = it["urls"][0]
        text = (it.get("text") or "").strip()
        title = html.escape(entry_title(it))
        body = linkify(text) if text else f'<a href="{html.escape(u)}">{html.escape(u)}</a>'
        more = "".join(f' · <a href="{html.escape(x)}">link {n+2}</a>'
                       for n, x in enumerate(it["urls"][1:]))
        src = discord_url(it)
        src_html = f' · <a class="src" href="{html.escape(src)}">source</a>' if src else ""
        rows.append(
            f'<article><a class="h" href="{html.escape(u)}">{title}</a>'
            f'<div class="b">{body}</div>'
            f'<div class="m">{html.escape(it.get("author") or "unknown")}{more}{src_html}</div></article>')
    body = "\n".join(rows) or "<p>No links yet.</p>"
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(TITLE)}</title>
<link rel="alternate" type="application/atom+xml" title="{html.escape(TITLE)}" href="feed.xml">
<style>
  :root {{ color-scheme: light dark; --fg:#111; --mut:#666; --bg:#fff; --card:#f6f7f9; --link:#1a56db; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --fg:#e7e9ea; --mut:#9aa0a6; --bg:#0d1117; --card:#161b22; --link:#6ea8fe; }} }}
  body {{ font: 16px/1.55 -apple-system, system-ui, Segoe UI, Roboto, sans-serif; max-width: 720px;
          margin: 0 auto; padding: 2rem 1rem 4rem; color: var(--fg); background: var(--bg); }}
  header p {{ color: var(--mut); }}
  a {{ color: var(--link); text-decoration: none; }} a:hover {{ text-decoration: underline; }}
  h2 {{ font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; color: var(--mut);
        margin: 2rem 0 .5rem; border-bottom: 1px solid var(--card); padding-bottom: .3rem; }}
  article {{ background: var(--card); border-radius: 10px; padding: .8rem 1rem; margin: .6rem 0; }}
  a.h {{ font-weight: 600; display: block; }}
  .b {{ margin: .3rem 0; word-break: break-word; }}
  .m {{ color: var(--mut); font-size: .85rem; }}
  .feedlink {{ font-size: .85rem; }}
</style></head>
<body>
<header>
  <h1>{html.escape(TITLE)}</h1>
  <p>{html.escape(SUBTITLE)} <span class="feedlink">· <a href="feed.xml">RSS/Atom feed</a></span></p>
</header>
{body}
</body></html>
"""


def main():
    items = load_items()
    os.makedirs(OUT_DIR, exist_ok=True)
    open(os.path.join(OUT_DIR, "feed.xml"), "w", encoding="utf-8").write(build_atom(items))
    open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8").write(build_html(items))
    # tell GitHub Pages not to run these files through Jekyll
    open(os.path.join(OUT_DIR, ".nojekyll"), "w").write("")
    print(f"[feed] wrote {len(items)} item(s) to {OUT_DIR}/feed.xml + index.html")


if __name__ == "__main__":
    main()
