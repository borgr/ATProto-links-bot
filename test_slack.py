"""Tests for the Slack ingestion front end (slack_source) and proof that its message
shim satisfies the reused publishing path (backfill.post_message). All network mocked.

  python3 test_slack.py
"""
from datetime import datetime
import relay
import backfill
import slack_source as ss


def _run():
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"  [{'ok' if cond else 'FAIL'}] {name}")
        if not cond:
            fails += 1

    # --- slack_to_plain ---
    check("<url|label> -> url",
          ss.slack_to_plain("see <https://arxiv.org/abs/1|cool paper>")
          == "see https://arxiv.org/abs/1")
    check("<url> -> url",
          ss.slack_to_plain("<https://arxiv.org/abs/2>") == "https://arxiv.org/abs/2")
    check("unescape & < >",
          ss.slack_to_plain("a &amp; b &lt;3 &gt;") == "a & b <3 >")
    check("&amp;lt; round-trips to &lt;",
          ss.slack_to_plain("x &amp;lt; y") == "x &lt; y")
    check("channel ref -> #name",
          ss.slack_to_plain("in <#C123|related-work> ok") == "in #related-work ok")
    check("special <!here> -> @here",
          ss.slack_to_plain("<!here> look") == "@here look")
    check("user mention dropped",
          ss.slack_to_plain("hi <@U123> there") == "hi  there")
    check("URL_RE recovers the link from plain text",
          relay.URL_RE.findall(ss.slack_to_plain("x <https://a.b/c|t> y")) == ["https://a.b/c"])

    # --- fetch_history: pagination + bot/subtype/no-user filtering (mock _api_get) ---
    pages = [
        {"ok": True, "messages": [
            {"type": "message", "user": "U1", "ts": "1700000002.0",
             "text": "paper <https://arxiv.org/abs/9|p>"},
            {"type": "message", "subtype": "channel_join", "user": "U1",
             "ts": "1700000001.5", "text": "has joined"},
            {"type": "message", "bot_id": "B1", "ts": "1700000001.7", "text": "bot noise"},
            {"type": "message", "ts": "1700000001.8", "text": "no user field"},
        ], "response_metadata": {"next_cursor": "CUR"}},
        {"ok": True, "messages": [
            {"type": "message", "user": "U2", "ts": "1700000000.0",
             "text": "older <https://x.com/y>"},
        ], "response_metadata": {"next_cursor": ""}},
    ]
    calls = {"hist": 0}

    def fake_api(method, token, params, timeout=30):
        if method == "users.info":
            return {"ok": True, "user": {"profile": {"display_name": "Name" + params["user"]}}}
        i = calls["hist"]
        calls["hist"] += 1
        return pages[i]

    orig = ss._api_get
    ss._api_get = fake_api
    try:
        msgs = ss.fetch_history(token="xoxb-x", channel="C1",
                                channel_name="related-work", pause=0)
    finally:
        ss._api_get = orig

    check("followed cursor across 2 pages", calls["hist"] == 2)
    check("kept only 2 human text msgs (dropped join/bot/no-user)", len(msgs) == 2)
    check("sorted oldest->newest",
          msgs[0].id == "1700000000.0" and msgs[1].id == "1700000002.0")

    # --- SlackMsg surface the reused code reads ---
    m = msgs[1]
    check("created_at is tz-aware datetime",
          isinstance(m.created_at, datetime) and m.created_at.tzinfo is not None)
    check("author.display_name resolved via users.info", m.author.display_name == "NameU1")
    check("channel.name / channel.id", m.channel.name == "related-work" and m.channel.id == "C1")
    check("attachments empty (v1)", m.attachments == [])
    check("author.bot False, guild None", m.author.bot is False and m.guild is None)
    check("content carries the bare url", "https://arxiv.org/abs/9" in m.content)

    # --- the shim satisfies backfill.post_message unchanged ---
    led = set()
    posts = []
    t = backfill.Target("t", True, post=lambda mm, urls, author: posts.append((mm.id, author)))
    urls = relay.URL_RE.findall(m.content)
    ok = backfill.post_message([t], m, urls, m.author.display_name, led)
    check("post_message posts + ledgers a SlackMsg",
          ok is True and posts == [("1700000002.0", "NameU1")]
          and ("t", "1700000002.0") in led)

    # --- dormant: no token/channel -> empty, no API calls ---
    check("no token -> dormant []", ss.fetch_history(token="", channel="") == [])

    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'} (slack)")
    return fails


if __name__ == "__main__":
    raise SystemExit(1 if _run() else 0)
