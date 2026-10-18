# Automatic Ingestion — Simple Setup

## What this adds

`Internet Archive -> license filter -> new-item detector -> download -> Telegram database channel -> MongoDB -> existing bot search`

Your existing F-Sub, Softurl verification, premium UI, search-group flow, and
result delivery are unchanged.

## First deployment: safe test

1. Keep `IA_INGEST_ENABLED=false`.
2. Deploy and make sure the bot is healthy.
3. Confirm the Telethon `SESSION_STRING` belongs to an account that can post to
   your database channel.
4. Set `IA_INGEST_ENABLED=true` and keep `IA_BATCH_SIZE=1`.
5. Redeploy.
6. Check `/iastats` as admin.
7. Check the database channel for the imported item.
8. Search that title in the bot.
9. If everything is correct, increase `IA_BATCH_SIZE` gradually.

## Initial catalogue

For a controlled initial backfill set:

`IA_INITIAL_BACKFILL=true`

and choose a modest `IA_INITIAL_PAGES` and `IA_INITIAL_LIMIT` value. The worker processes at most
`IA_BATCH_SIZE` items per scan. Increase gradually rather than starting with a
huge import.

## Important

The default query is intentionally restricted to public-domain or selected
non-NC Creative Commons license metadata. Internet Archive says uploader
metadata is not guaranteed to be legally accurate. Verify every collection or
license before enabling automatic redistribution.
