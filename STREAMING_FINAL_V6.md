# Cinema HUB OG — Final Render Streaming Build

This release keeps the existing main bot behavior intact and rebuilds only the streaming service path.

## Browser behavior
- Browser-compatible MP4: direct HTTP range playback.
- MKV/other containers: browser Play uses a progressive fragmented-MP4 compatibility endpoint.
- Files whose names strongly indicate H.264/AVC (`x264`, `h264`, `avc`) use a fast video-copy remux with AAC audio.
- Files that indicate HEVC/H.265 (`x265`, `hevc`, `h265`) use an ultrafast H.264 compatibility transcode.
- HLS remains available as a manual fallback for compatibility playback.

## External players
- VLC uses an Android `intent://` handoff with the authenticated media URL.
- MX Player uses an Android package-targeted intent.
- More / Copy Stream URL provide browser and player fallback paths.

## Download
- Download uses the same authenticated Telegram-backed HTTP Range pipeline as playback.
- The server never has to finish downloading the entire file before the browser can begin receiving it.

## Branding
The exact supplied Cinema HUB OG logo is bundled at `stream_server/assets/cinema_hub_og_logo.jpg`.
The player title line is exactly: `CINEMA HUB OG` / `Your cinema. Instantly on screen.`

## Zero-cost Render note
The service is compatible with the Render Free plan, but CPU limits still affect HEVC/H.265 transcoding speed. Direct MP4/H.264 and external-player streaming avoid that CPU-heavy path.
