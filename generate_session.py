"""
Run this LOCALLY on your own computer (never inside any Telegram chat) to
generate a Telethon StringSession for the personal account used by the
forwarder.

Why this script exists: Telegram's server-side anti-scam system blocks a
login the instant its OTP code is typed into any Telegram chat - including
a private chat with our own control bot. Doing the phone/code/2FA exchange
here, in a plain terminal, avoids that block entirely because the code is
never sent through Telegram as a message.

A StringSession does NOT expire on its own. It stays valid until you:
  - explicitly log it out (Account -> Logout Account in the bot, or
    Telegram's own Settings -> Devices -> end that session), or
  - Telegram revokes it for a security reason (e.g. you change your
    account password).
So you only need to run this once per account, unless you deliberately
log the session out later.

USAGE
-----
    pip install telethon
    python generate_session.py

You will be prompted for API_ID, API_HASH, your phone number, the login
code Telegram sends you, and your 2FA password if you have one enabled -
all of it typed directly into this terminal, never into any bot or chat.

The resulting string is printed to your terminal. Copy the ENTIRE string
(it is long and looks like base64 gibberish) and paste it into the bot's
Account -> Login (Paste Session String) prompt.

SECURITY: this string grants full access to the Telegram account it was
generated for - treat it exactly like a password. Never share it, never
paste it anywhere except this application's own login prompt, and never
commit it to a repository.
"""
import getpass
import os
import sys

try:
    from telethon.sync import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    print("Telethon is not installed. Run: pip install telethon")
    sys.exit(1)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def main():
    print("=" * 60)
    print(" Telegram Forwarder - Session String Generator")
    print("=" * 60)
    print()
    print("Get API_ID / API_HASH from https://my.telegram.org")
    print("(these are for the PERSONAL account, not the bot)")
    print()

    api_id_env = os.environ.get("API_ID", "")
    api_hash_env = os.environ.get("API_HASH", "")

    api_id_raw = ask("API_ID", api_id_env)
    api_hash = ask("API_HASH", api_hash_env)

    try:
        api_id = int(api_id_raw)
    except ValueError:
        print("API_ID must be a number.")
        sys.exit(1)

    if not api_hash:
        print("API_HASH is required.")
        sys.exit(1)

    print()
    print("A login code will be sent to your Telegram account now.")
    print("Enter it below when prompted - this terminal is NOT a Telegram")
    print("chat, so Telegram will not block this login.")
    print()

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        me = client.get_me()
        session_string = client.session.save()

        print()
        print("=" * 60)
        print(f" Logged in as: {me.first_name or ''} (@{me.username or 'no username'})")
        print("=" * 60)
        print()
        print("Your session string (copy the ENTIRE line below):")
        print()
        print(session_string)
        print()
        print("Paste this into the bot: Account -> Login (Paste Session String)")
        print("Keep it secret - it is equivalent to your account password.")


if __name__ == "__main__":
    main()
