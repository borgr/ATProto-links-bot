"""Regression tests for Semble error handling — the logic that decides whether a
run should fail loudly and whether a link gets marked done (and thus not retried),
plus the app-password session auth (createSession) recovery path.

These use mocks (no network). Run:  python test_semble.py
"""
import urllib.error
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


if __name__ == "__main__":
    raise SystemExit(1 if (main() + test_session()) else 0)
