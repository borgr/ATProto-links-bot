"""Regression tests for Semble error handling — the logic that decides whether a
run should fail loudly and whether a link gets marked done (and thus not retried).

These use mocks (no network). Run:  python test_semble.py
"""
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


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
