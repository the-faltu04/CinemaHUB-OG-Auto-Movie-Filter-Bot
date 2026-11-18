# CinemaHUB OG — ARLinks Update Notes (16 November 2026)

This package records the ARLinks revision based on the supplied repository snapshot. It does not migrate, reset, or delete the existing MongoDB database.

## Included changes

### Free-user shortlink verification
- Replaced active Softurl link generation with the ARLinks Developers API.
- Main bot reads `ARLINKS_API` and `ARLINKS_BASE_URL` (default `https://arlinks.in/api`).
- Parses the JSON response `status=success` and `shortenedUrl` field.
- Creates a single-step bot deep link for the verification callback.
- Persists the resulting short URL in the active verification session and reuses it for the same user/file batch.
- Serializes concurrent link-generation attempts for the same user/file batch.
- Does not blindly retry network timeouts because the provider may have processed the request even when the response was lost. An uncertain session must expire before a new short link can be generated.
- Adds `HOW TO VERIFY?` using the supplied private invite link `https://t.me/+WpwWsLHNVWA2ZmNl` (the old environment value ended with a different final character).
- Adds a Premium-only “CONTINUE WITHOUT VERIFICATION” action. Free users see the same Premium-only alert used by Stream & Download and can open Premium plans. The pending action remains valid until expiry, so the user can tap it again after buying Premium to receive the originally selected file(s).
- Existing already-issued legacy verification sessions remain readable for compatibility.

### Premium and delivery behavior
- Premium members bypass both F-Sub and shortlink verification.
- Premium direct delivery does not receive the Free-tier joke message.
- A single styled Premium-upgrade reminder appears after the 5-minute temporary-delivery warning for a Free-user delivery batch:
  `KAB TAK FREE ME DEKHEGA BHAI? / BUY KRLE PREMIUM PLAN!`
- The final Premium reminder is included in the same delivery cleanup job as the delivered files and 5-minute warning (default 300 seconds).

### Captions
- Removes only the targeted promotional usernames `primeXofficial`, `luffyautofilter`, and `Aapkafreetv` rather than stripping every Telegram mention.
- Preserves legitimate credits and existing useful title/quality/audio/language caption text.
- Retains the configured Cinema HUB footer behavior.
- Adds a blank-line separator before the first recognized audio/language/quality/subtitle metadata line when applicable.
- A separate fixed “audio note” mentioned in the recording was not added because its exact wording could not be recovered reliably. Existing audio metadata is preserved.

### Request Group and streaming configuration
- Request-message reaction uses a short 👀 transition followed by a big Telegram reaction animation; the original search text/results are unchanged.
- Main bot now honors `REQUEST_GROUP_ID` for Telegram chat routing and retains `REQUEST_GROUP_USERNAME` for user-facing links (default `moviesearchoffc`).
- Request Group search messages have their own configurable cleanup variable `REQUEST_GROUP_SEARCH_DELETE_AFTER_SECONDS` (default 600 seconds).
- Streaming service reads `STREAM_CHUNK_SIZE`, but clamps it to Telegram/Telethon's 512 KiB maximum. A configured value of 1 MiB is therefore reduced to 512 KiB at runtime.

## Render environment changes

### Main bot service
Add:

```text
ARLINKS_API=<your ARLinks API token>
ARLINKS_BASE_URL=https://arlinks.in/api
VERIFICATION_TUTORIAL_URL=https://t.me/+WpwWsLHNVWA2ZmNl
REQUEST_GROUP_SEARCH_DELETE_AFTER_SECONDS=600
```

The supplied `REQUEST_GROUP_ID=-1004474182964` is now consumed by the main bot. Keep `REQUEST_GROUP_USERNAME=moviesearchoffc` unless the public Request Group username has changed; if it has, set the actual username there.

`SOFTURL_API`, `SOFTURL_BASE_URL`, `GPLINKS_API`, and `GPLINKS_BASE_URL` are no longer used by this build and can be removed from the main bot's Render environment after deployment. Do not put the ARLinks token in GitHub or in this ZIP.

The main bot's `SESSION_STRING` is separate from the streaming service's `STREAM_SESSION_STRING`; keep the former configured if you use `/reindex` or `/reconcile`.

### Streaming service
No ARLinks token is needed in the streaming service. Set `STREAM_CHUNK_SIZE=524288` (or leave the value already configured; larger values are clamped in code).

## Validation performed
- Python compilation: passed for all application Python files.
- Focused ARLinks API-contract, static-flow, and final-static tests: 20 passed.
- Search-rebuild and caption-formatting tests could not be collected here because `pymongo` is not installed in this workspace.
- No live ARLinks API call, Telegram, MongoDB Atlas, or Render deployment test was performed.
## Deployment safety
Upload the project contents to the existing GitHub repository root and deploy both existing Render services from that repository. Do not create a new MongoDB database or reset the existing one. Add the new main-bot environment variables in Render before testing Free-user verification.

## Provider policy note
ARLinks Terms of Use currently list Telegram as an allowed traffic source, but prohibit bot traffic and shortening anime video/APK links. Use only with genuine human clicks, do not route anime media through ARLinks, and get written clarification from ARLinks if their “bot traffic” rule could apply to this Telegram bot workflow.


## 18 November 2026 — Premium gate, missing-title requests and navigation refresh

### Premium verification bypass
- Premium accounts are checked before every F-Sub/ARLinks decision in `begin_gate()` and `send_arlinks()`; active members receive direct delivery.
- If a member activates Premium while an ARLinks verification page is open, the return handler retrieves the pending session and delivers its selected files without asking for verification.

### Unindexed-title admin requests
- Both Request Group searches and Premium private searches keep the existing exact-search → closest-spelling suggestion flow.
- Only when there are no exact results and the best suggestion score is below `CLOSE_MATCH_THRESHOLD` (0.60) does the bot notify configured numeric admin IDs.
- The requester receives the requested confirmation only after at least one admin message is successfully sent. If no admin recipient is configured or Telegram blocks delivery, the bot gives an honest configuration/delivery warning instead of falsely claiming the request was sent.
- Set `MISSING_TITLE_ADMIN_IDS=<comma-separated numeric Telegram user IDs>` in the Main Bot Render service. It falls back to `ADMIN_IDS` if left blank.
- Alerts are sent first through the configured payment bot (`@Visionaryowner_bot`) to numeric admin user IDs, then fall back to the main bot for any recipient not reached. Admins must have opened the payment bot once (and the main bot too if fallback is needed).
- Telegram bots cannot DM another bot by username. The owner/admin must be a human Telegram account addressable by numeric user ID; use `/id` in the main bot to see that account's ID.

### Premium screen flow
- `UPGRADE` now opens a dedicated Premium Features overview. `BUY PREMIUM` opens the plans screen.
- The plans screen's Back button returns to Premium Features, and the overview's Back button returns to a neutral Main Menu options screen, not the `/start` Jai Shri Ram welcome text.
- Premium overview/plans/status transitions edit the existing menu message and no longer post/delete a fake loader message.
- The cinematic loader now has a bundled GIF asset and a shorter minimum display time for longer-running operations.

### Animation limits
- Telegram's Bot API does not expose control over client-side fade-in, vertical slide, or scroll animations for ordinary messages. The bot now uses message edits and state-aware Back navigation where supported, with a short branded GIF for genuine loading states; final message entry animations remain controlled by Telegram clients.

### Validation
- Added static regression checks for Premium gate bypass, no-match forwarding in both search paths, spelling-suggestion preservation, and Premium navigation/back routing.
- Deployment still requires numeric admin IDs in Render and live verification in Telegram.


## 18 Nov 2026 — Premium bypass, unindexed-title admin requests and Premium navigation

- Premium accounts now bypass both Force Subscribe and ARLinks at the file gate. The bypass is enforced in `begin_gate`, `send_arlinks`, and the verification-return path as defence in depth.
- Premium activation confirmation now correctly states that Premium bypasses both F-Sub and ARLinks.
- Genuine no-match searches now notify the configured admin recipient from both Request Group search and Premium private search. Existing exact/close-match and spelling-suggestion handling remains first; notification only occurs when no suggestion meets the 0.60 threshold.
- Admin notifications include the requested title, requester name/username/user ID, and source/chat. The bot replies to the requester with the requested not-indexed notice only after at least one admin delivery succeeds.
- `MISSING_TITLE_ADMIN_IDS` accepts numeric Telegram user/chat IDs and falls back to `ADMIN_IDS`. The existing `@Visionaryowner_bot` payment-bot token is used to send admin notifications; an admin must start that bot once. Telegram bots cannot initiate private chats to another bot by username.
- Premium navigation now uses a dedicated Premium Features page, a separate Plans page, Plans → Back to Premium Features, and Premium Features → Back to the neutral options menu. Menu transitions edit the existing message rather than posting fake loading messages.
- Added a branded animated loader GIF. Telegram Bot API cannot force a native message fade/slide animation or control a user's client scrolling; the GIF and same-message edits are the supported improvements.
- No MongoDB schema/data reset is required.

### Required Render configuration

Set `ADMIN_IDS` to the numeric owner/admin Telegram user ID(s). Optionally set `MISSING_TITLE_ADMIN_IDS` to a separate comma-separated list; if left blank it falls back to `ADMIN_IDS`. Ensure each recipient has opened `@Visionaryowner_bot` at least once so that Telegram permits that bot to send them a private message. If notifications should go to a group, use that group's numeric chat ID and add the payment bot to the group.
