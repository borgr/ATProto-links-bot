"""Read message history from a Slack channel and adapt it to the same
(message, urls) shape backfill.post_message consumes, so the Discord relay's
publishing machinery (Target table, ledger dedup, feed archive, sustained-failure
alerting) works unchanged.

Stdlib only (urllib) — no slack_sdk dependency. Dormant until SLACK_BOT_TOKEN is
set: fetch_history() then returns [] and callers no-op cleanly.

Config (env):
  SLACK_BOT_TOKEN    xoxb-... bot token with channels:history (+ groups:history if
                     the channel is private) and users:read
  SLACK_CHANNEL_ID   the C.../G... id of #related-work
  SLACK_CHANNEL_NAME display label for the feed (default 'related-work')
"""
import os
import re
import time
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SLACK_API = "https://slack.com/api"

# Slack renders links as <url> or <url|label>, channel refs as <#C123|name>, user
# mentions as <@U123>, and specials as <!here>/<!channel|label>. It escapes ONLY the
# three characters & < > in user text (as &amp; &lt; &gt;) — everything else is literal.
_LINK_RE = re.compile(r"<(https?://[^>|]+)(?:\|([^>]*))?>")
_CHAN_RE = re.compile(r"<#[CGD][A-Z0-9]+(?:\|([^>]*))?>")
_SPECIAL_RE = re.compile(r"<!(\w+)(?:\|([^>]*))?>")
_USER_RE = re.compile(r"<@[UW][A-Z0-9]+(?:\|[^>]*)?>")
_LEFTOVER_RE = re.compile(r"<[#@!][^>]*>")

# System / non-content messages to drop (joins, topic changes, edits, etc.). Real
# user posts carry no subtype; keeping the rest and letting the URL filter downstream
# decide means we never drop a link-bearing message.
_SKIP_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive", "pinned_item",
    "bot_message", "message_changed", "message_deleted", "thread_broadcast",
}


class SlackError(RuntimeError):
    """A Slack Web API call returned ok:false or failed to reach the API."""


def slack_to_plain(text):
    """Convert Slack message text to plain text with bare URLs.

    <url|label>/<url> -> the URL (so relay.URL_RE finds it), <#C123|name> -> #name,
    <!here|label> -> @here, <@U123> -> "" (dropped). Entities &amp;/&lt;/&gt; are
    unescaped AFTER tag stripping (tags use literal < >, so order matters), and &amp;
    is unescaped last so "&amp;lt;" round-trips to "&lt;" not "<".
    """
    if not text:
        return ""
    text = _LINK_RE.sub(lambda m: m.group(1), text)
    text = _CHAN_RE.sub(lambda m: "#" + (m.group(1) or ""), text)
    text = _SPECIAL_RE.sub(lambda m: "@" + (m.group(2) or m.group(1)), text)
    text = _USER_RE.sub("", text)
    text = _LEFTOVER_RE.sub("", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return text.strip()


class _Author:
    __slots__ = ("display_name", "bot")

    def __init__(self, display_name, bot=False):
        self.display_name = display_name
        self.bot = bot


class _Channel:
    __slots__ = ("name", "id")

    def __init__(self, name, cid):
        self.name = name
        self.id = cid


class SlackMsg:
    """Duck-types the discord.Message surface the relay's publish + feed code reads:
    .id .created_at (tz-aware datetime) .content .channel.name .channel.id
    .author.display_name .author.bot .guild .attachments — nothing else is touched."""
    __slots__ = ("id", "created_at", "content", "channel", "author", "guild", "attachments")

    def __init__(self, ts, content, author_name, channel_name, channel_id, bot=False):
        self.id = ts                       # Slack ts is unique within a channel
        self.created_at = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        self.content = content
        self.channel = _Channel(channel_name, channel_id)
        self.author = _Author(author_name, bot)
        self.guild = None                  # feed guards truthiness -> guild_id ""
        self.attachments = []              # v1: images skipped (Slack file auth)


def _api_get(method, token, params, timeout=30):
    url = f"{SLACK_API}/{method}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    if not data.get("ok"):
        raise SlackError(f"{method}: {data.get('error', 'unknown error')}")
    return data


def _resolve_user(uid, token, cache):
    if uid in cache:
        return cache[uid]
    name = uid
    try:
        prof = _api_get("users.info", token, {"user": uid}).get("user", {}).get("profile", {})
        name = prof.get("display_name") or prof.get("real_name") or uid
    except SlackError:
        pass
    cache[uid] = name
    return name


def fetch_history(token=None, channel=None, channel_name=None, limit=200, pause=0.4):
    """Return [SlackMsg] (oldest->newest) for human text messages in the channel.

    Fetches the whole history each run (the channel is small) and relies on the
    ledger to dedup posting, so there's no cursor state to persist. Returns [] when
    no token/channel is configured (dormant)."""
    token = token or os.environ.get("SLACK_BOT_TOKEN", "")
    channel = channel or os.environ.get("SLACK_CHANNEL_ID", "")
    channel_name = channel_name or os.environ.get("SLACK_CHANNEL_NAME", "related-work")
    if not token or not channel:
        return []

    raw, cursor = [], None
    while True:
        params = {"channel": channel, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = _api_get("conversations.history", token, params)
        raw.extend(data.get("messages", []))
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
        time.sleep(pause)                  # Slack tier-3 rate limit (~50/min)

    users, msgs = {}, []
    for m in raw:
        if m.get("type") != "message":
            continue
        if m.get("bot_id") or m.get("subtype") in _SKIP_SUBTYPES:
            continue
        uid = m.get("user")
        if not uid:
            continue
        text = slack_to_plain(m.get("text", ""))
        if not text:
            continue
        author = _resolve_user(uid, token, users)
        msgs.append(SlackMsg(m["ts"], text, author, channel_name, channel))
    msgs.sort(key=lambda x: x.created_at)  # Slack returns newest-first
    return msgs
