"""Regression tests for the Mastodon target — chunking limits, thread visibility,
preflight, and auth-error propagation. Mocks only (no network). Run: python test_mastodon.py
"""
import relay


class _Att:
    def __init__(self, content_type=None):
        self.content_type = content_type
        self.filename = "x.png"
        self.description = ""
        self.url = "http://example.com/x.png"


class _Msg:
    def __init__(self, content, attachments=None):
        self.content = content
        self.attachments = attachments or []


def main():
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"  [{'ok' if cond else 'FAIL'}] {name}")
        if not cond:
            fails += 1

    relay.MASTODON_ENABLE = True
    relay.MASTODON_TOKEN = "tok"
    relay.DRY_RUN = False

    orig_req = relay._mastodon_request
    try:
        # capture every status POST
        calls = []

        def fake_req(path, method="GET", body=None, raw=None, content_type=None,
                     timeout=45, retries=2):
            if path == "/api/v1/statuses":
                calls.append(body)
                return {"id": str(len(calls))}
            if path == "/api/v1/accounts/verify_credentials":
                return {"username": "colab_links"}
            return {}

        relay._mastodon_request = fake_req

        # short message -> single public post, no reply ref
        calls.clear()
        relay.post_to_mastodon(_Msg("nice paper https://arxiv.org/abs/1.2 ty"), ["https://arxiv.org/abs/1.2"], "me")
        check("short -> 1 status", len(calls) == 1)
        check("root is public", calls[0]["visibility"] == "public")
        check("root has no in_reply_to", "in_reply_to_id" not in calls[0])
        check("author suffix appended", calls[0]["status"].endswith("— me"))

        # long message -> thread; replies are unlisted and chained
        calls.clear()
        long = "word " * 400  # ~2000 chars, well over the 480 limit
        n = relay.post_to_mastodon(_Msg(long), [], "me")
        check("long -> multiple statuses", n > 1 and len(calls) == n)
        check("replies are unlisted", all(c["visibility"] == "unlisted" for c in calls[1:]))
        check("replies chain via in_reply_to", all("in_reply_to_id" in c for c in calls[1:]))
        check("each status within limit", all(len(c["status"]) <= relay.MASTODON_LIMIT + len("\n— me") for c in calls))

        # long URLs count as 23 chars, not their full length -> fewer splits than raw length implies
        calls.clear()
        big_url = "https://scholar.google.com/scholar_url?" + "x" * 300
        relay.post_to_mastodon(_Msg(f"see {big_url} end"), [big_url], "me")
        check("huge URL stays in one post (counts as 23)", len(calls) == 1)

        # preflight: healthy
        ok, detail = relay.mastodon_check()
        check("mastodon_check healthy -> ok", ok is True and "colab_links" in detail)

        # preflight: auth error surfaces as not-ok
        def _auth(*a, **k):
            raise relay.MastodonAuthError("HTTP 401 — token invalid")
        relay._mastodon_request = _auth
        ok, detail = relay.mastodon_check()
        check("mastodon_check auth -> not ok", ok is False and "401" in detail)

        # disabled -> check is a no-op success
        relay.MASTODON_ENABLE = False
        ok, detail = relay.mastodon_check()
        check("disabled -> ok/disabled", ok is True and detail == "disabled")
    finally:
        relay._mastodon_request = orig_req

    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'}")
    return fails


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
