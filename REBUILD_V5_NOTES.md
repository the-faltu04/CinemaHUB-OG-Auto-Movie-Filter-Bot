# Cinema HUB OG — V5 Rebuild Notes

## What changed

- Removed the series-search candidate-title bottleneck. Search counts now operate on the full media collection using searchable tokens and series-family keys.
- Added `series_key`, `season_number`, `episode_number`, `search_tokens`, and `index_version` metadata for media records.
- Added background v2 search-metadata backfill for existing MongoDB records without deleting data.
- Large episode-heavy webseries are now grouped by a searchable series stem such as `tuu juliet jatt di` while individual episode titles remain intact.
- Added fuzzy series-family correction for spelling mistakes after exact-token search fails.
- Search result counts are no longer limited by the old 24-title candidate cap.
- Added safe bulk MongoDB upserts for historical indexing.
- Added resumable historical indexing state and `/reindex` improvements.
- Added `/index_status` and `/reconcile` admin commands.
- Telegram polling no longer discards pending updates on startup.
- New live database posts are indexed immediately; caption formatting is handled by a durable background worker so caption failures do not lose the media index record.
- Caption worker retries Telegram rate limits and persists pending/failed state in MongoDB.
- Added dynamic `SEND ALL <N> FILES` labeling for search results.
- Added GitHub Actions workflow plus additional static/index tests.

## Important Render variable

The main bot now expects the existing `SESSION_STRING` variable for historical `/reindex` and `/reconcile` operations.

Do not place the streaming account session in `SESSION_STRING`. Keep the streaming service's `STREAM_SESSION_STRING` separate.

## Safe deployment sequence

1. Deploy the rebuilt main bot.
2. Confirm Render reaches `Telegram polling started successfully.`
3. Add a valid main-bot `SESSION_STRING` to Render Environment before using `/reindex` or `/reconcile`.
4. Run `/stats`.
5. Run `/index_status`.
6. For a full source-of-truth check, run `/reconcile`.
7. Test a large serial/webseries search such as `Tuu Juliet Jatt Di`.

The rebuild does not delete the existing `movies` collection or any user/premium data.
