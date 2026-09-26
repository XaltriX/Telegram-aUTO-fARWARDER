"""
Shared conversation-flow constants, kept in one place to avoid import
cycles between handler modules and to give the single text-router in
app/bot.py a full picture of which module owns which flow.
"""

# account.py - login is done by pasting a pre-generated Telethon
# StringSession (see generate_session.py), never by typing an OTP into
# the bot chat. Telegram's anti-scam system blocks logins whose code was
# typed into any Telegram chat (including this bot), so an in-bot
# phone/code/2FA flow cannot work reliably - see README section 8.
LOGIN_SESSION_STRING = "login_session_string"

# sources.py
ADD_SOURCE_ID = "add_source_id"
ADD_SOURCE_SCAN_LAST_X = "add_source_scan_last_x"
ADD_SOURCE_SCAN_DATE = "add_source_scan_date"

# destination.py
SET_DESTINATION_ID = "set_destination_id"
