"""Connectivity test: confirms the bot logs in, sees the target channel,
and receives message content. No posting anywhere yet."""
import os
import discord

# Minimal .env loader (no extra dependency).
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

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])

intents = discord.Intents.default()
intents.message_content = True  # requires Message Content Intent enabled in portal
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"[OK] Logged in as {client.user}")
    ch = client.get_channel(CHANNEL_ID)
    if ch is None:
        print(f"[WARN] Cannot see channel {CHANNEL_ID} yet — check permissions.")
    else:
        print(f"[OK] Can see channel: #{ch.name} in '{ch.guild.name}'")
    print("Now post a message in that channel — it should appear below.")
    print("Press Ctrl+C to stop.")


@client.event
async def on_message(message):
    if message.channel.id != CHANNEL_ID:
        return
    author = message.author.display_name
    content = message.content or "(no text — maybe Message Content Intent is off, or attachment-only)"
    print(f"[MSG] {author}: {content}")
    if message.attachments:
        print(f"      attachments: {[a.url for a in message.attachments]}")


client.run(TOKEN)
