"""Credential health check for the relay. Reads .env, verifies each service,
prints a report. Never prints full secrets. Run: python check_keys.py

NOTE: this checks the keys in your local .env — GitHub Actions uses its own
repo secrets, which can differ. If a key is OK here but the CI run 401s, the
GitHub secret is stale and just needs re-setting from this value.
"""
import json
import urllib.request
import urllib.error
import relay


def mask(s):
    return (s[:6] + "…" + s[-4:]) if s and len(s) > 12 else ("(set)" if s else "(EMPTY)")


def main():
    # Semble
    ok, detail = relay.semble_check()
    print(f"Semble  {mask(relay.SEMBLE_API_KEY)}: {'OK' if ok else 'FAIL'} — {detail}")

    # Bluesky
    if relay.ATPROTO_HANDLE and relay.ATPROTO_APP_PASSWORD:
        try:
            from atproto import Client
            p = Client(base_url=relay.ATPROTO_PDS).login(
                relay.ATPROTO_HANDLE, relay.ATPROTO_APP_PASSWORD)
            print(f"Bluesky {relay.ATPROTO_HANDLE}: OK — {p.did}")
        except Exception as e:
            print(f"Bluesky {relay.ATPROTO_HANDLE}: FAIL — {e}")
    else:
        print("Bluesky: (no handle/app-password in .env)")

    # Discord bot token
    try:
        req = urllib.request.Request(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {relay.DISCORD_TOKEN}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            u = json.load(r)
        print(f"Discord bot {mask(relay.DISCORD_TOKEN)}: OK — {u.get('username')}")
    except urllib.error.HTTPError as e:
        print(f"Discord bot {mask(relay.DISCORD_TOKEN)}: FAIL — HTTP {e.code}")
    except Exception as e:
        print(f"Discord bot {mask(relay.DISCORD_TOKEN)}: FAIL — {e}")


if __name__ == "__main__":
    main()
