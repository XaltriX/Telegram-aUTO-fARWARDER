"""
Shared conversation-flow constants, kept in one place to avoid import
cycles between handler modules and to give the single text-router in
app/bot.py a full picture of which module owns which flow.
"""

# account.py
LOGIN_PHONE = "login_phone"
LOGIN_CODE = "login_code"
LOGIN_2FA = "login_2fa"

# sources.py
ADD_SOURCE_ID = "add_source_id"
ADD_SOURCE_SCAN_LAST_X = "add_source_scan_last_x"
ADD_SOURCE_SCAN_DATE = "add_source_scan_date"

# destination.py
SET_DESTINATION_ID = "set_destination_id"
