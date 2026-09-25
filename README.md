# Telegram Channel Forwarder

A production-ready Telegram forwarding manager: a **personal account** monitors
authorized source channels and forwards eligible messages to **one**
destination channel, coordinated by a rate-limit-aware scheduler with a
strict LIVE-over-BACKLOG priority queue. A separate **bot** is the
owner-only control/dashboard interface.

> This project is intended only for channels you own or are explicitly
> authorized to manage. It uses Telegram's normal API behavior, respects
> every FloodWait/rate-limit response Telegram returns, and does not
> attempt to rotate accounts/proxies/sessions to evade limits.

---

## 1. Project tree

```
project/
├── app/
│   ├── bot.py                 control bot: client setup, owner-only guards, central text/forward router
│   ├── config.py               env var loading/validation
│   ├── dashboard.py             single persistent dashboard message + temp notifications
│   ├── database.py              MongoDB (motor) persistence layer, indexes, atomic ops
│   ├── models.py                 shared enums/constants (JobStatus, Priority, ScanMode, ...)
│   ├── monitor.py                live event listeners + periodic recovery sync + initial backlog scan
│   ├── queue_manager.py          turns Telethon messages into deduped job documents
│   ├── scheduler.py              the ONE forwarding loop (LIVE > BACKLOG, FloodWait/retry handling)
│   ├── telegram_client.py        personal account login state machine + session persistence
│   ├── handlers/
│   │   ├── flows.py              shared conversation-flow constants
│   │   ├── start.py              /start, dashboard bootstrap
│   │   ├── account.py            login/logout UI + login flow steps
│   │   ├── sources.py            add/list/remove source channels, scan mode selection
│   │   ├── destination.py        set/view destination channel
│   │   ├── queue.py               queue view, failed jobs, retry, pause/resume
│   │   ├── settings.py            media type filter toggles
│   │   └── stats.py               statistics view
│   └── utils/
│       ├── logging.py             structured logging setup
│       └── helpers.py             media classification, misc helpers
├── worker.py                    MAIN entrypoint - runs bot + account + scheduler + monitor + dashboard
├── web.py                       OPTIONAL health-check web dyno (read-only, never forwards)
├── requirements.txt
├── Procfile
├── runtime.txt
├── .env.example
└── README.md
```

---

## 2. Why Telethon

**Telethon** (MTProto) is used for both the personal account client and the
control bot, for one library across the whole system (point 39):

- Full **user-account authentication** (phone + code + 2FA), which the Bot
  API cannot do - required because only a personal account can read
  arbitrary channels it's a member of and forward from them.
- Native **`events.Album`** support - Telegram's grouped-media (album)
  events arrive pre-collected, which is essential for point 12 (preserving
  albums instead of splitting them into individual messages).
- `forward_messages(dest, [ids], source)` accepts a **list of message ids
  in one call**, which is what actually keeps an album grouped at the
  destination.
- Mature, explicit **`FloodWaitError`** with a `.seconds` attribute, plus a
  full RPC error taxonomy (`ChannelPrivateError`, `ChatAdminRequiredError`,
  etc.) used to distinguish retryable vs. permanent failures (point 37).
- `StringSession` lets the personal account's session live entirely in
  MongoDB instead of a local file, which Heroku's ephemeral filesystem
  would otherwise destroy on every restart (point 22).
- Async-native throughout, which is required for a single-threaded,
  single-worker scheduler that never issues concurrent conflicting
  requests through the same account (point 16/36).

The control bot also runs on Telethon (via `BOT_TOKEN`) using a
`MemorySession`, so nothing bot-related touches disk either - avoiding any
mixed-library complexity between "the bot" and "the account".

---

## 3. Environment variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | yes | Control bot token from @BotFather |
| `API_ID` | yes | From https://my.telegram.org |
| `API_HASH` | yes | From https://my.telegram.org |
| `MONGO_URI` | yes | MongoDB connection string |
| `OWNER_ID` | yes | Your numeric Telegram user ID - only this ID can use the bot |
| `LOG_LEVEL` | no | Default `INFO` |
| `DASHBOARD_UPDATE_INTERVAL` | no | Seconds between dashboard refresh checks (default 5) |
| `RECOVERY_SYNC_INTERVAL` | no | Seconds between recovery sync cycles (default 300) |
| `NOTIFICATION_DELETE_SECONDS` | no | Auto-delete delay for temp notifications (default 8) |
| `MONGO_DB_NAME` | no | Default `tg_forwarder` |
| `SCAN_BATCH_SIZE` | no | Messages per backlog-scan batch (default 100) |
| `SCAN_BATCH_DELAY` | no | Seconds paused between scan batches (default 1.0) |
| `MAX_ATTEMPTS` | no | Retry attempts before a job is marked FAILED (default 3) |
| `RETRY_BACKOFF_BASE` | no | Backoff seconds multiplier per attempt (default 5.0) |
| `PORT` | no | Used only by `web.py` (Heroku sets this automatically) |

Never commit real values - copy `.env.example` to `.env` for local runs
(loaded automatically via `python-dotenv`), and use `heroku config:set` for
production.

---

## 4. MongoDB setup

1. Create a free MongoDB Atlas cluster (or use any MongoDB 5+ instance).
2. Create a database user and allow network access from `0.0.0.0/0` (or
   Heroku's dynamic IP range if you prefer stricter rules - Heroku dynos
   don't have static IPs on standard dynos).
3. Copy the connection string into `MONGO_URI`. The app creates all
   collections, indexes, and singleton documents automatically on first
   connect (`database.py::_ensure_documents` / `create_indexes`).

Collections created: `sources`, `jobs`, `processed_message_ids`,
`settings`, `account_session`, `stats`, `dashboard_state`, `counters`.

Key indexes:
- `jobs`: unique `(source_channel_id, source_message_id)` - the core
  duplicate-protection constraint (point 13).
- `jobs`: compound indexes for the LIVE pick (`status, priority_type,
  sequence`) and BACKLOG pick (`status, priority_type, source_order,
  source_message_id`) so `claim_next_job()` is a fast indexed query even
  with a large queue.
- `sources`: unique `channel_id`.

---

## 5. Telegram API ID / API HASH

1. Go to https://my.telegram.org, log in with the **personal account**
   you intend to use for monitoring/forwarding.
2. **API Development Tools** → create an app (any name/platform) → copy
   `api_id` and `api_hash` into `API_ID` / `API_HASH`.

These belong to the personal account, not the bot.

---

## 6. Creating the control bot

1. Message **@BotFather** → `/newbot` → follow the prompts.
2. Copy the token into `BOT_TOKEN`.
3. Start a private chat with your new bot (send it any message) so it can
   message you first-hand - Telegram bots cannot initiate DMs.

---

## 7. Heroku deployment

```bash
heroku create your-app-name
heroku config:set BOT_TOKEN=... API_ID=... API_HASH=... MONGO_URI=... OWNER_ID=...
git push heroku main
heroku ps:scale worker=1 web=0   # web is optional; enable only if you want the health endpoint
```

- `Procfile` declares two process types: `worker` (the actual forwarding
  engine, point 32) and `web` (an optional read-only health endpoint -
  **it never runs the scheduler**, so scaling it up never creates a second
  forwarding worker).
- `runtime.txt` pins Python 3.11.
- Only run `worker=1`. Do not scale `worker` above 1 - the design assumes a
  single scheduler instance (point 36); running two would let two
  processes issue conflicting requests through the same personal account.

---

## 8. First-run login through the bot

1. Deploy, then open a DM with your bot and send `/start`.
2. Only messages from `OWNER_ID` are accepted - anyone else gets no
   response (point 24).
3. Tap **📡 Sources** is greyed out until the account is connected; tap
   the dashboard's account status, or open **Account → 🔐 Login Account**.
4. Send the phone number (international format, e.g. `+15551234567`).
5. Send the login code Telegram texts/sends to that account.
6. If 2FA is enabled, send the password when prompted.
7. On success, the session string is saved to MongoDB - you will not need
   to log in again after restarts unless Telegram itself revokes the
   session (point 22/23).

OTPs and 2FA passwords are read from plain chat messages but are **never
written to logs** (see `app/telegram_client.py` and `app/handlers/account.py`
- the raw values only ever touch Telethon's `sign_in()` calls).

---

## 9. Adding source channels

1. **📡 Sources → ➕ Add Source**.
2. Identify the channel either by:
   - **🆔 Enter Channel ID** - paste the numeric ID (e.g. `-1001234567890`)
     or a public `@username`.
   - **📨 Forward Message** - forward any message from that channel to the
     bot; the source channel is identified from the forward header
     (`fwd_from.from_id`), then resolved via the **personal account**
     (which must already have access to it).
3. Choose what existing content to import:
   - **📦 All Files** - full history.
   - **🔢 Last X Files** - most recent N.
   - **📅 From Date** - everything since a given date.
   - **🆕 New Files Only** - nothing existing; only new arrivals from now on.
4. The initial scan runs in the background (`monitor.py::scan_initial_backlog`),
   respecting `SCAN_BATCH_SIZE` / `SCAN_BATCH_DELAY` and any FloodWait
   Telegram returns.

---

## 10. Configuring the destination

**🎯 Destination → Set Destination**, same two identification options as
sources. On set, the bot checks (best-effort) that the personal account
has post/admin rights there, and warns if not - it still saves the
setting either way so you can fix permissions afterward without redoing
the flow.

---

## 11. Queue behavior, with examples

The **only** rule that matters: `claim_next_job()` in `database.py` always
tries a **LIVE** job first (`sort=[("sequence", 1)]`), and only falls back
to **BACKLOG** (`sort=[("source_order", 1), ("source_message_id", 1)]`)
when no LIVE job is pending. This check runs after every single completed
job (the scheduler loop calls `claim_next_job()` again immediately), so:

```
Backlog A1 → A2 → A3 is running (A3 in progress)
  D1, D2, D3 arrive while A3 is forwarding
  A3 completes → scheduler checks LIVE first → D1 → D2 → D3
  D3 completes, no more LIVE jobs pending → resume backlog at A4
```

If a live item (`C1`) arrives while `D2` is being forwarded:

```
D1 done → D2 done → (C1 now in LIVE queue, arrived before D3) → LIVE sort by
sequence means whichever of {C1, D3} has the lower arrival sequence goes
next - global arrival order, not source name (point 4/42).
```

Backlog always follows **source registration order** then **message id**
within that source (point 5) - so once no live items are pending, sources
are drained in the order they were added, oldest message first.

The current forward operation is **never aborted** mid-flight (point 7) -
`forward_messages()` is awaited to completion before the scheduler looks
at the queue again.

---

## 12. Recovery after restart

On startup (`database.py::recover_stuck_jobs`, called from
`scheduler.run()`):
- Any job left in `PROCESSING` from an unclean shutdown is reset to
  `PENDING`. Because the unique `(source_channel_id, source_message_id)`
  index and the `processed_message_ids` reservation ledger both exist
  *before* a job is created, and `mark_job_done` is the only thing that
  finalizes a forward, a job can safely be retried without ever producing
  a duplicate forward.
- The personal account reconnects using the stored `StringSession`
  (`telegram_client.py::bootstrap`) - no OTP required unless Telegram
  revoked the session.
- Live event handlers are re-installed for all enabled sources.
- The periodic recovery sync (`monitor.py::run_recovery_sync`) resumes
  from each source's stored `last_seen_message_id` checkpoint, so it never
  rescans an entire channel - only the gap that may have been missed.

---

## 13. Rate-limit / FloodWait handling

`scheduler.py::_process_job` wraps every `forward_messages()` call:

1. Catch `FloodWaitError` explicitly.
2. Read `e.seconds` (the server-provided wait).
3. Persist `flood_wait_until` in `settings` (survives a restart mid-wait).
4. Put the job back to `PENDING` in its original queue - nothing is lost,
   nothing is duplicated.
5. Send a temporary "🚨 RATE LIMITED" notification.
6. `await asyncio.sleep(e.seconds)`, then clear `flood_wait_until` and
   resume automatically.

This **minimizes** unnecessary requests (single worker, indexed queries,
throttled dashboard edits, checkpointed recovery scans) but, as required,
this implementation does **not** and cannot claim FloodWait will never
happen - Telegram's limits are outside the application's control.

Non-FloodWait errors are classified via `utils/helpers.py::is_retryable_exception`:
permanent errors (`ChannelPrivateError`, `ChatAdminRequiredError`, banned,
invalid peer, etc.) go straight to `FAILED`; everything else gets up to
`MAX_ATTEMPTS` (default 3) with linear backoff before failing.

---

## 14. Security considerations

- Every bot command and callback query is gated by `owner_only_message` /
  `owner_only_callback` (`app/bot.py`), which check `event.sender_id ==
  config.OWNER_ID`. Unauthorized users get silence (commands) or a
  toast-only "Not authorized." (callbacks) - no data is ever exposed to
  them.
- Secrets (`BOT_TOKEN`, `API_HASH`, `MONGO_URI`) are read only from
  environment variables, never hardcoded.
- OTPs and 2FA passwords are passed directly into Telethon's `sign_in()`
  and never logged (`utils/logging.py`'s docstring flags this
  responsibility explicitly to every call-site).
- The personal account's session string is stored in MongoDB, not on
  Heroku's ephemeral disk. Treat your `MONGO_URI` credentials with the
  same sensitivity as the account's password - anyone with database
  access can reconstruct the session.
- Logout (`Account → 🔐 Logout Account`) requires an explicit confirm tap,
  calls `client.log_out()` (which revokes the session on Telegram's side
  too), and clears the stored session string.

---

## 15. Testing notes

The 20 scenarios from the spec map to these code paths and were reasoned
through during design:

1. Single new file → `monitor._handle_new_message` → LIVE job → scheduler.
2. Simultaneous multi-source new files → each enqueued with a strictly
   increasing global `sequence` via `db.next_sequence()`, so arrival order
   is preserved regardless of source.
3/4/18/19. Backlog + continuous live arrivals → `claim_next_job()` always
   re-checks LIVE first; no artificial "one backlog per N live" rule exists.
5. Heroku restart → `recover_stuck_jobs` + `StringSession` reconnect +
   `refresh_handlers`.
6. DB reconnect → Motor's connection pool retries transparently;
   `serverSelectionTimeoutMS` bounds failures.
7. FloodWait → section 13 above.
8. Three failed attempts → `attempts >= MAX_ATTEMPTS` → `fail_job`.
9. Duplicate update → `processed_message_ids` ledger + unique job index.
10. Source removal → `remove_source(clear_queue=...)`, two explicit modes.
11/12. Pause during forward / resume → pause only stops picking up *new*
   jobs (`scheduler.run`'s `if settings.get("paused")` check happens before
   `claim_next_job`, never mid-forward); resume re-enters the same
   LIVE-first loop.
13. Logout/login → `account_manager.logout()` / login state machine.
14. Album → `events.Album` + `queue_manager.enqueue_album` + one
   `forward_messages(dest, [ids], source)` call.
15. Caption → untouched; `forward_messages` preserves original content.
16. Disabled media type → `media_type_enabled()` filter checked before
   any job is created.
17. Missed update recovered by sync → `monitor.run_recovery_sync`, backlog
   priority (it wasn't "live").
20. Destination temporarily inaccessible → surfaces as a permanent or
   retryable RPC error depending on type; classified by
   `is_retryable_exception`.

---

## 16. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Bot doesn't respond at all | Confirm you're messaging from `OWNER_ID`'s account; check `heroku logs --tail` for a `ConfigError` (missing env var). |
| "No stored account session" forever | Login never completed - open **Account** and run the login flow again. |
| Jobs stuck in queue, nothing forwards | Check the dashboard's "ACCOUNT" status - it must be `CONNECTED`. Also check `paused` state. |
| Repeated `FloodWaitError` in logs | Expected occasionally under heavy volume; the scheduler waits and resumes automatically. If constant, you're adding too many sources at once - stagger initial scans. |
| Album arrives split into separate messages | Verify all album parts pass the media filter - the whole album is forwarded together, or entirely skipped if none of its items are enabled. |
| "Couldn't access that channel with the personal account" | The personal account (not the bot) must be a member of / have visibility into that channel first. |
| Dashboard message disappeared | Deleted manually in the chat - next `ctl:refresh` / `/start` re-creates it (`MessageIdInvalidError` is caught and handled). |
| Duplicate forwards after a rough restart | Should not happen - check for manual edits to the `processed_message_ids` or `jobs` collections; the unique indexes are the safety net. |

