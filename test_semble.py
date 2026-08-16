"""Regression tests for Semble error handling — the logic that decides whether a
run should fail loudly and whether a link gets marked done (and thus not retried),
plus the app-password session auth (createSession) recovery path.

These use mocks (no network). Run:  python test_semble.py
"""
import urllib.error
from datetime import datetime, timedelta, timezone
import relay


class _Chan:
    name = "papers-links-n-sharing"


class _Msg:
    def __init__(self, content):
        self.content = content
        self.channel = _Chan()


def main():
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"  [{'ok' if cond else 'FAIL'}] {name}")
        if not cond:
            fails += 1

    # force Semble "on" and live (not dry-run) for the duration of the test
    relay.SEMBLE_ENABLE = True
    relay.DRY_RUN = False
    relay.SEMBLE_COLLECTION_IDS = ["col-1"]
    msg = _Msg("great paper https://arxiv.org/abs/1234.5678 thanks all")
    urls = relay.URL_RE.findall(msg.content)
    orig_add, orig_req = relay.semble_add_url, relay._semble_request
    try:
        # success -> (all_ok=True, auth_failed=False), link may be marked done
        relay.semble_add_url = lambda u, n: {"urlCardId": "c1"}
        check("success -> (True, False)", relay.relay_to_semble(msg, urls, "me") == (True, False))

        # auth failure -> (False, True): must NOT be marked done, must fail the run
        def _auth(*a, **k):
            raise relay.SembleAuthError("HTTP 401 — key invalid")
        relay.semble_add_url = _auth
        check("auth error -> (False, True)", relay.relay_to_semble(msg, urls, "me") == (False, True))

        # transient/other error -> (False, False): not marked done (retries), run not failed on auth
        def _boom(*a):
            raise RuntimeError("read timed out")
        relay.semble_add_url = _boom
        check("generic error -> (False, False)", relay.relay_to_semble(msg, urls, "me") == (False, False))

        # preflight: auth error surfaces as (ok=False)
        relay._semble_request = _auth
        ok, detail = relay.semble_check()
        check("semble_check auth -> not ok", ok is False and "401" in detail)

        # preflight: healthy key -> ok
        relay._semble_request = lambda *a, **k: {"collections": []}
        ok, _ = relay.semble_check()
        check("semble_check healthy -> ok", ok is True)

        # disabled Semble is a no-op success (never blocks a Bluesky-only run)
        relay.SEMBLE_ENABLE = False
        check("disabled -> (True, False)", relay.relay_to_semble(msg, urls, "me") == (True, False))
    finally:
        relay.semble_add_url, relay._semble_request = orig_add, orig_req

    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'}")
    return fails


def test_session():
    """App-password session auth: a 401 mid-run must re-login once and retry (the
    self-healing behavior), a persistent 401 must raise, and semble_check must do a
    real createSession (verifying WRITE access, not just reads)."""
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"  [{'ok' if cond else 'FAIL'}] {name}")
        if not cond:
            fails += 1

    def _http_err(code):
        return urllib.error.HTTPError("http://x", code, "err", {}, None)

    saved = (relay.SEMBLE_SESSION_AUTH, relay.SEMBLE_ENABLE, relay.SEMBLE_HANDLE,
             relay._semble_http, relay._semble_login)
    relay.SEMBLE_SESSION_AUTH = True
    relay.SEMBLE_ENABLE = True
    relay.SEMBLE_HANDLE = "test.bsky.social"
    try:
        # 401 on first call -> re-login once -> retry succeeds (session Semble deleted mid-run)
        calls = {"http": 0, "login": 0}

        def http_401_then_ok(url, method, data, timeout):
            calls["http"] += 1
            if calls["http"] == 1:
                raise _http_err(401)
            return {"urlCardId": "ok"}
        relay._semble_http = http_401_then_ok
        relay._semble_login = lambda: calls.__setitem__("login", calls["login"] + 1)
        res = relay._semble_request("http://x", "POST", {"url": "u"})
        check("session 401 -> relogin -> retry ok",
              res == {"urlCardId": "ok"} and calls["login"] == 1 and calls["http"] == 2)

        # persistent 401 even after a fresh session -> SembleAuthError (don't loop forever)
        relay._semble_http = lambda *a: (_ for _ in ()).throw(_http_err(401))
        relay._semble_login = lambda: None
        try:
            relay._semble_request("http://x", "POST", {"url": "u"})
            check("session persistent 401 raises", False)
        except relay.SembleAuthError:
            check("session persistent 401 raises", True)

        # preflight does a real createSession -> ok, and surfaces the handle
        relay._semble_login = lambda: None
        ok, detail = relay.semble_check()
        check("semble_check session ok (createSession)", ok is True and "session" in detail)

        # bad app password -> createSession raises -> preflight not ok
        def _bad_login():
            raise relay.SembleAuthError("createSession HTTP 401 — app password rejected")
        relay._semble_login = _bad_login
        ok, _ = relay.semble_check()
        check("semble_check bad app-password -> not ok", ok is False)
    finally:
        (relay.SEMBLE_SESSION_AUTH, relay.SEMBLE_ENABLE, relay.SEMBLE_HANDLE,
         relay._semble_http, relay._semble_login) = saved

    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'} (session)")
    return fails


class _AgedMsg:
    """Minimal stand-in for a discord.Message: just an id and a created_at."""
    def __init__(self, mid, age_hours):
        self.id = mid
        self.created_at = datetime.now(timezone.utc) - timedelta(hours=age_hours)


def test_stuck():
    """links_stuck_since drives SUSTAINED-failure alerting: a link only counts as stuck
    once it's both unledgered for the target AND older than the max_age (multiple missed
    cycles). Fresh unposted links and already-posted links must never count."""
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"  [{'ok' if cond else 'FAIL'}] {name}")
        if not cond:
            fails += 1

    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=3)
    old = _AgedMsg(1, 5)      # 5h old — past the window
    fresh = _AgedMsg(2, 0.5)  # 30 min old — just arrived
    all_msgs = [(old, ["u1"]), (fresh, ["u2"])]

    # nothing ledgered: only the OLD one is "stuck" (fresh one is a normal in-flight retry)
    stuck = relay.links_stuck_since(all_msgs, set(), "bluesky", now, max_age)
    check("old unposted -> stuck; fresh unposted -> not", stuck == [old])

    # old one already posted (ledgered) -> nothing stuck
    led = {("bluesky", "1")}
    check("ledgered old -> not stuck",
          relay.links_stuck_since(all_msgs, led, "bluesky", now, max_age) == [])

    # per-target: a bluesky-ledgered link is still stuck for semble
    check("ledger is per-target",
          relay.links_stuck_since(all_msgs, led, "semble", now, max_age) == [old])

    # empty when everything is posted
    both = {("bluesky", "1"), ("bluesky", "2")}
    check("all posted -> empty",
          relay.links_stuck_since(all_msgs, both, "bluesky", now, max_age) == [])

    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'} (stuck)")
    return fails


def test_targets():
    """post_message drives every target from one loop: success ledgers + counts, an
    already-ledgered link is skipped, a drip cap leaves links unledgered, a systemic auth
    failure (AuthDegrade) disables that target for the cycle, and a transient error just
    counts (target stays enabled, link stays unledgered to retry)."""
    import backfill
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"  [{'ok' if cond else 'FAIL'}] {name}")
        if not cond:
            fails += 1

    class _M:
        def __init__(self, mid):
            self.id = mid

    def ok_post(m, urls, author):
        return None

    def auth_post(m, urls, author):
        raise backfill.AuthDegrade("bad creds")

    def boom_post(m, urls, author):
        raise RuntimeError("timeout")

    # one healthy target: posts, ledgers, counts, returns True
    led = set()
    t = backfill.Target("x", True, post=ok_post)
    check("healthy -> posts + ledgers", backfill.post_message([t], _M(1), ["u"], "me", led) is True
          and ("x", "1") in led and t.sent == 1)
    # same message again -> already ledgered, skipped, no double count
    check("ledgered -> skip", backfill.post_message([t], _M(1), ["u"], "me", led) is False
          and t.sent == 1)

    # drip cap: cap=1, second distinct message is left unledgered
    led = set()
    c = backfill.Target("cap", True, post=ok_post, cap=1)
    backfill.post_message([c], _M(1), ["u"], "me", led)
    check("cap -> first posts", ("cap", "1") in led and c.sent == 1)
    backfill.post_message([c], _M(2), ["u"], "me", led)
    check("cap -> second unledgered", ("cap", "2") not in led and c.sent == 1)

    # AuthDegrade disables the target for the cycle; link not ledgered
    led = set()
    a = backfill.Target("a", True, post=auth_post)
    check("auth -> not posted", backfill.post_message([a], _M(1), ["u"], "me", led) is False
          and ("a", "1") not in led and a.enabled is False and a.fail == 1)
    # disabled target is skipped on the next message
    check("auth -> disabled skips next",
          backfill.post_message([a], _M(2), ["u"], "me", led) is False and a.fail == 1)

    # transient error: counts, stays enabled, link unledgered (retries next run)
    led = set()
    b = backfill.Target("b", True, post=boom_post)
    check("transient -> counts, stays enabled",
          backfill.post_message([b], _M(1), ["u"], "me", led) is False
          and ("b", "1") not in led and b.enabled is True and b.fail == 1)

    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'} (targets)")
    return fails


if __name__ == "__main__":
    raise SystemExit(
        1 if (main() + test_session() + test_stuck() + test_targets()) else 0)
