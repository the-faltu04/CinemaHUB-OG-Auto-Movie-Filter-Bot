# CinemaHUB OG bot update — 18 October 2026

This update is based on the uploaded repository snapshot and preserves the existing Free/Premium, force-subscribe, payment, referral, indexing, admin, filtering, verification, and streaming flows except for the explicitly requested changes below.

## Included changes

- Search resolution is shared between counting and page fetching; a search/page call no longer independently repeats the previous count + find + fuzzy-resolution work.
- Short-lived query/page caching and per-query single-flight resolution reduce repeated work during a burst of identical requests. Cache invalidation remains connected to indexing/deletion changes.
- Search candidate collection uses indexed `series_key` values and only scans legacy `title` values when `series_key` metadata is absent, rather than always querying distinct values for both collections of fields.
- Fuzzy scoring runs outside the asyncio event loop, is guarded by a small semaphore, caches recent ranked suggestions, precomputes token bigrams per scoring pass, and caps expensive edit-distance scoring to a bounded shortlist.
- The Telegram application uses configurable, bounded update concurrency (`MAX_CONCURRENT_UPDATES`, default `16`). Search reactions and analytics recording run in background tasks so they do not hold the critical search path.
- The NEXT callback is acknowledged before page fetching. Page fetching uses a single `search_page` method for the shared total and rows; timing is logged for follow-up measurement.
- **Send All materializes file IDs only after the user opens the Send All deep link.** Previously the initial search result rendering fetched up to 1,000 IDs just to prepare that button; lazy query batches remove that work from normal search. Existing materialized batch records are still accepted for backward compatibility, and force-subscribe/verification/Premium gates still apply before delivery.
- Episode query parsing accepts `E189`, `E 189`, `Ep 189`, `Epi 189`, and `Episode 189`; explicit episode searches target that episode. Selecting a spelling suggestion retains the requested episode marker.
- The spelling-check status/suggestion flow is preserved while the candidate work is cached and moved off the main event loop.
- Free delivered media now shows a Stream & Download button that is locked behind the existing Premium gate. Clicking it produces the requested alert and a View Premium Plans action; no signed streaming URL is issued to Free users.
- Completed verification access defaults to 12 hours. Pending verification sessions stay at 30 minutes.
- Request-group user search messages expire five minutes after the search starts; bot result/suggestion/error messages expire five minutes after appearing. The cleanup sweep runs every 10 seconds and checks each message's own expiry. Private/Premium search-result retention stays at 10 minutes; delivered media remains at five minutes.

## Render environment

Please set or verify the following on the **main bot** Render service:

```text
VERIFICATION_ACCESS_TTL_SECONDS=43200
REQUEST_GROUP_SEARCH_DELETE_AFTER_SECONDS=300
MAX_CONCURRENT_UPDATES=16
SEARCH_DB_CONCURRENCY=8
SEARCH_RESULT_DELETE_AFTER_SECONDS=600
```

`SEARCH_RESULT_DELETE_AFTER_SECONDS=600` deliberately preserves the existing private/Premium result retention. `VERIFICATION_SESSION_TTL_SECONDS=1800` should remain unchanged.

## Validation status

- Python syntax/AST parsing passed for the main bot and related modules.
- Static and focused helper tests passed locally. The runtime environment used for this work does not have `pymongo` or `python-telegram-bot` installed, and package download was unavailable; tests were therefore run with lightweight import stubs for those external packages. They do not test Telegram or MongoDB integration.
- No live connection to the user's Render service, Telegram bot, or Atlas database was made. The under-10-second pagination target and concurrent-load capacity have **not** been claimed as measured. After deployment, benchmark 1, 5, 10, then higher simultaneous queries, inspect elapsed-time logs, and tune concurrency only if Atlas/Render metrics remain healthy.
