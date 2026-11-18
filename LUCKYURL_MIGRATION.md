# LuckyURL provider switch — 18 November 2026

This update changes only the Free-user verification shortener from ARLinks to LuckyURL.
It preserves Premium bypass, F-Sub, 12-hour verification access, existing verification
callbacks, link persistence/idempotency, and the other bot/streaming features.

## Main Bot Render variables

Set:

```env
LUCKYURL_API=<your LuckyURL API token>
LUCKYURL_BASE_URL=https://luckyurl.in/api
```

Remove the old `ARLINKS_API` and `ARLINKS_BASE_URL` values after deploying this build.
The API token must remain in Render Environment Variables and must never be committed.

No LuckyURL token is required by the Streaming Service. No MongoDB reset or streaming
service redeployment is required.

## Compatibility

New verification sessions are created with provider `luckyurl` and use a `luckyurl`
deep-link stage. Existing ARLinks/GPlinks/Softurl return-stage names are still accepted
so unexpired verification links issued before this deployment can complete. New requests
will use LuckyURL only.

The provider response is parsed from JSON (`status=success`, `shortenedUrl`). The request
uses GET parameters `api`, `url`, and `format=json`. The bot does not blindly retry a
request after a transport timeout because the provider may have already created the link.

## Validation

- Python syntax compilation and focused API-contract tests should be run before deployment.
- No live LuckyURL API request is performed by this package build.
- Configure a valid API token in Render, then test one Free-user verification end-to-end.
- Premium users continue to bypass F-Sub and shortlink verification.

## Provider policy reminder

LuckyURL's Terms list Telegram among allowed social-media destinations, while prohibiting
bot-generated traffic and self-clicks. Keep every shortlink visit genuine and user-initiated;
do not automate clicks or generate artificial traffic. Review the current Terms before use:
https://luckyurl.in/pages/terms
