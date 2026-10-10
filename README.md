# Open Video Downloader — durable library development edition

This is a separate local Git development copy on branch `feature/durable-library`. The live Mac installation, LaunchAgent, original VPS package, ZIP and backup remain intact. No server has been provisioned or deployed.

Read [Caddy HTTPS deployment instructions](deploy/README-HTTPS.md). This edition adds durable SQLite jobs, a conservative queue, owner login, a persistent server library, real media validation and richer yt-dlp previews. YouTube account/membership workflows are experimental and have not been verified with a real authorized subscription.

## Storage and execution

The server runs exactly one Uvicorn worker. Each analysis/download uses a separate process group. Cancel/shutdown terminates that group and its FFmpeg children. Cancellation during final FFprobe validation can wait for its bounded 20-second check; it cannot subsequently publish the file. Restart marks queued/active work interrupted; Retry begins a fresh download, rather than silently starting network activity. History, preferences, manifests and library metadata survive restart.

Four separate writable roots are required: `DOWNLOAD_DIR` for temporary workers/Quick Transfer, `LIBRARY_DIR` for permanent media, `STATE_DIR` for SQLite, `CONFIG_DIR` for owner/session secrets. Production Compose mounts four named volumes. The existing `media` volume remains the temporary volume; new volumes do not replace it. TTL, history pruning and orphan cleanup apply only to temporary job directories. Library media remains until an authenticated owner explicitly confirms deletion. Incomplete library saves remain recoverable or explicitly deletable. Never run `docker compose down -v` on a real library.

A configured external volume should mount directly at the library root with `LIBRARY_REQUIRE_MOUNT=1`. Missing mounts make health fail and prevent writes, rather than falling back to root storage. Back up the media volume, config and consistent SQLite snapshots together. SQLite is metadata; the browser never archives video in localStorage/IndexedDB/OPFS or a service worker. Normal browser playback uses HTTP byte ranges and device downloads use normal browser saving. Browser codec and multiple-download policies still apply.

## Owner login

Production requires an owner password in the private config volume. Until configured, data endpoints fail closed; only login status and health are accessible. Configure interactively using `python manage.py set-password` and `python manage.py enroll-mfa` inside the container through SSH. `set-password` is also server recovery: it resets MFA, which must be enrolled again before exposure. Passwords are scrypt hashes, and session identifiers are stored as hashes, expire after seven days and are invalidated on password rotation or MFA enrollment. Login guesses are throttled in SQLite across restarts. Six-digit TOTP codes cannot be replayed. Secure, HttpOnly, SameSite=Strict cookies and cross-site mutation checks protect browser access. Production requires TOTP MFA and trusted HTTPS from Caddy. The application publishes only to host loopback; ports 80/443 remain closed until the operator approves final exposure. Public deployment does not require Tailscale clients.

For an explicitly isolated LOCAL development server only, set `OVD_ALLOW_UNAUTHENTICATED_LOCAL=1` and bind `127.0.0.1` on a spare port. Only loopback clients qualify. Compose forces this off. To test owner login on local HTTP, use `COOKIE_SECURE=0`; production forces Secure cookies. Do not use real account secrets during fixture testing.

## Extraction and quality

Maintained yt-dlp site extractors and its built-in Generic/HTML5/HLS/DASH support share one adapter. Generic pages with several media items return up to ten explicit choices; Quick Download never imports a channel or downloads a playlist. Native browser automation and arbitrary yt-dlp plugins are disabled. Supported non-DRM access depends on the site, region, IP and current extractor. DRM, live streams, upcoming premieres and access-control bypass are unsupported.

Previews include available metadata, format codecs/container/resolution, exact versus estimated size, listed subtitle/caption languages and only the advertised standard height choices. Missing fields stay unknown. Best quality and ceiling presets merge separate video/audio, prefer H.264/AAC when quality ties, and prefer MP4 for merge output. Exact video-only selections explicitly add audio. Audio extraction uses M4A. We do not transcode every incompatible source merely to force browser playback. Subtitle languages are listed; subtitle downloading/embedding is not implemented.

Every final file must contain FFprobe-readable audio/video streams and a positive finite duration. An extension alone is insufficient. This does not promise complete corruption detection or playback compatibility in every browser. Temporary quota and free-space checks remain thresholds, not hard filesystem quotas.

## Accounts and membership

Only an explicit server upload of a Netscape file containing `youtube.com` cookies is supported. No browser database is read, no website password is requested, and no whole-browser export occurs. Files containing unrelated domains are rejected. The cookie file lives outside Git/source in private 0600 storage; it is loaded read-only, never sent over HTTP and never returned by an API. The file picker is cleared after upload. The server's disk/backups still need appropriate encryption and access control: sessions are not encrypted at rest by this application.

YouTube OAuth is currently unavailable in yt-dlp. Use official current [yt-dlp session instructions](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies). Your Mac and VPS do not share sessions automatically. Account status reports disconnected/unverified/verified/expired/needs-attention; a failed account download requires checking/reconnecting the session, since expiry cannot be reliably inferred from all failures. Disconnect waits for account activity and deletes the stored session. Deletion is unlinking, not a secure physical erase of disks/backups; revoke the session with the provider if necessary.

Before Membership Import, one completed accessible video must have yt-dlp's `subscriber_only` classification and a channel ID. Scanning is limited to that verified channel's `/membership` tab, max 200 candidates, with request delays and ten seconds between account download starts. Only explicit members-only classifications enter the durable manifest; unknown/public items are excluded. Import rechecks classification before transfer. IDs deduplicate rescans and selected downloads. Public/Premium/unlisted/private videos do not count as membership verification. This is partial discovery, not an assertion that all posts, streams or member playlists are covered.

## Development tests

Use a separate virtual environment and `pip install -r requirements.txt -r requirements-test.txt`, then `python -m pytest -q`. Tests use isolated temporary volumes and synthetic sessions. FFmpeg/FFprobe are required. The real-site test log distinguishes public network attempts from mocks. Local Docker testing writes temporary VM/image data to the Mac; it does not establish a remote library or zero SSD usage. No paid service or object storage is required for this increment.

## Review locally without changing the installed Mac app

A local preview is an explicit alternative and uses temporary Mac disk space. From this source folder, use a fresh virtual environment, the existing FFmpeg/FFprobe, a separate runtime directory and a spare port:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export DOWNLOAD_DIR="$PWD/.local-runtime/downloads"
export LIBRARY_DIR="$PWD/.local-runtime/library"
export STATE_DIR="$PWD/.local-runtime/state"
export CONFIG_DIR="$PWD/.local-runtime/config"
export COOKIE_SECURE=0
export OVD_LOCAL_SETUP=1
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8001 --workers 1 --no-access-log
```

Open `http://127.0.0.1:8001`, create your owner password directly in the browser, optionally enroll MFA in Settings, and stop the preview with Ctrl+C. Nothing here registers a LaunchAgent or restarts port 8000. Do not treat this Mac preview directory as the future remote archive. Do not copy it, `.venv` or secrets into deployment source. For synthetic fixture-only work, the explicit loopback development bypass described above is available; real sessions should use owner login.

## Public source review

Runtime directories, credentials, account sessions, databases, personal media, local backups and private reports are excluded from the public source package and its entire history. Pattern scans and manual fixture review are preparation, not proof that arbitrary future additions are safe. Review the exact commit and file list before the first public push. No repository license has been chosen by the owner yet.
