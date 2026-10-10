# Open Video Downloader

An owner-only web application for downloading authorized, non-DRM media and keeping it in a private library. It uses FastAPI, yt-dlp and FFmpeg, with a browser interface and durable SQLite metadata.

## Features

- Analyze a single media URL and inspect available formats, codecs, resolution, duration and exact or estimated sizes.
- Download at the best available quality, choose a resolution ceiling or select an exact format. Merge separate video and audio streams; extract audio as M4A.
- Follow download progress, cancel jobs, retry interrupted work and submit a bounded batch queue.
- Save media to a persistent library or use temporary Quick Transfer. Search, sort, organize collections, track watched status and resume playback.
- Stream private library files with HTTP byte ranges or download them explicitly to a device.
- Protect access with an owner password and optional local TOTP MFA; production configuration requires MFA and HTTPS.
- Connect a signed-in Chrome profile on a Mac without a permanent cookie export. Only YouTube cookies are retained for requests, in memory, over HTTPS.
- Download accessible YouTube members-only videos. Optional membership discovery is limited to a verified channel, includes only explicitly classified members-only items and never claims complete coverage.

A live authorized members-only test verified analysis, a 720p H.264/AAC download, private-library storage, browser playback and persistence after restart. Personal test URLs, media, sessions and screenshots are not included in this repository. Membership discovery remains experimental; that test did not scan an entire channel.

## Requirements

- Python 3.11 or 3.12; the production container uses Python 3.12.
- FFmpeg and FFprobe available on `PATH`.
- Deno available on `PATH` for modern YouTube extraction. The container includes Deno and the pinned yt-dlp EJS package.
- Enough disk space for the permanent library, temporary streams, merging and backups.
- For the local Chrome connection: macOS, ordinary Google Chrome and a YouTube account already authorized to watch the requested video.

Dependency versions are pinned in [requirements.txt](requirements.txt). Docker installation includes the media tools; installing Python packages alone does not install FFmpeg or Deno.

## Install and run locally

Clone the source and use a separate virtual environment:

```sh
git clone https://github.com/Tahmid447/Open-Video-Downloade.git
cd Open-Video-Downloade
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Run the owner-authenticated local application with isolated runtime storage:

```sh
export DOWNLOAD_DIR="$PWD/.local-runtime/downloads"
export LIBRARY_DIR="$PWD/.local-runtime/library"
export STATE_DIR="$PWD/.local-runtime/state"
export CONFIG_DIR="$PWD/.local-runtime/config"
export COOKIE_SECURE=0
export OVD_LOCAL_SETUP=1
export OVD_LOCAL_BROWSER=1
export OVD_ALLOW_UNAUTHENTICATED_LOCAL=0
export OVD_REQUIRE_MFA=0
export OVD_REQUIRE_HTTPS=0
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8001 --workers 1 --no-access-log
```

Open <http://127.0.0.1:8001/> and create your owner password directly in the browser. It must contain at least 12 characters. MFA can be enrolled in Settings. These HTTP flags are for an isolated loopback preview only: never use them for a public service. Local setup and the Chrome connection require the loopback host on port 8001. Stop the foreground server with Ctrl+C. This command does not register a LaunchAgent or change another application on port 8000.

Always run exactly one Uvicorn worker. Run tests with synthetic sessions in their isolated temporary directories, not against a personal runtime directory.

### Connect YouTube without exporting a cookie file

1. In ordinary Chrome, sign into YouTube and confirm that one members-only video you are authorized to watch plays. Enter credentials only on Google's login page.
2. Open **Settings → Account connections → Connect YouTube once**. Select that Chrome profile, paste the single authorized test video and select **Verify and save connection**. macOS may ask for Chrome Safe Storage access; enter any Mac password only in the native OS dialog. The app saves the profile only after authenticated membership analysis succeeds.
3. Return to **Quick Download**, leave account mode on **Automatic**, paste a URL and select **Analyze**. Choose a quality and save to **Server library**. Analysis and download use the same connection; changing it requires fresh analysis.
4. Open **My Library → Play**, or explicitly download the completed file to your device.

Normal use needs no profile selector, cookie file or consent checkbox. **Change or recheck connection** reveals setup only when needed. Only the selected profile is read; other profiles are never tested. Automatic uses the saved connection for HTTPS YouTube URLs and public extraction for unrelated providers. **Public** explicitly disables provider credentials and stays selected after refresh and restart. Preferences and connection metadata are stored on the server.

Settings distinguishes a configured profile, a usable provider session and channel-specific membership evidence. Authenticated protected-video analysis confirms access; a completed members-only download supplies stronger evidence and enables the existing channel import gate. Membership evidence records the last successful check, not a permanent entitlement. Public-video success alone does not prove sign-in. Expired, missing or rejected sessions require rechecking; storage, format, rate-limit and channel eligibility failures do not automatically invalidate the connection.

The app uses the official yt-dlp browser reader behind [`--cookies-from-browser`](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp). That reader briefly copies the selected Chrome cookie database into a private worker directory and decrypts it locally. The downloader retains only unexpired YouTube cookies in memory with an HTTPS-only policy. Temporary copies are removed on completion, cancellation, timeout and the next startup after an interrupted server. Connection metadata records a browser/profile selection, not session values. Disconnect removes the app connection; it does not sign out Chrome.

The Codex in-app browser has no supported session-export interface. The Chrome connection is disabled on non-Mac servers and is not enabled by production Compose. A cloud server cannot automatically reuse your Mac's session.

**Advanced Account Setup** is closed by default. Manual session-file import requires explicit ownership and access consent once, never for ordinary Chrome downloads. If a separate server needs authentication, the existing import accepts only a YouTube-only Netscape cookie file. Unrelated domains are rejected. Imported files belong in private configuration storage outside source control, with permission `0600`. Never combine browser reading with `--cookies FILE` to export an entire browser session. Production encrypts owner configuration and imported sessions with AES-256-GCM using a separate private key file. Replacement requires fresh owner password/MFA and verification of one members-only video before swapping the connection. See the cloud security and recovery guide. Disconnect unlinks an imported file; it does not guarantee physical erasure from SSDs or backups. Revoke the session with the provider when necessary.

## Architecture and storage

```text
Owner browser → FastAPI authentication and API → single durable job queue
                                             → isolated yt-dlp/FFmpeg workers
                                             → FFprobe validation → private library
                       SQLite stores jobs, preferences and media metadata
```

| Storage root | Purpose | Retention |
| --- | --- | --- |
| `DOWNLOAD_DIR` | Worker staging and Quick Transfer | Temporary; quota/TTL cleanup |
| `LIBRARY_DIR` | Completed private media | Until the owner explicitly deletes it |
| `STATE_DIR` | SQLite metadata and hashed login sessions | Persistent |
| `CONFIG_DIR` | Owner configuration and optional imported sessions | Private, persistent |

Cancel and shutdown terminate worker process groups, including FFmpeg children. Restart marks unfinished jobs interrupted; Retry starts fresh network activity. Library publication uses validation and recoverable staging. FFprobe must find audio/video streams and a positive finite duration before a file is published.

The browser uses streaming and normal device downloads; it does not archive the library in browser storage. Back up media, private configuration and a consistent SQLite snapshot together. `manage.py backup-db` uses SQLite's backup API. External media volumes require a verified mount; avoid falling back to the system disk when a mount is absent. Never use `docker compose down -v` on a library you need to preserve.

Defaults include one simultaneous download, a 100-job queue, a 1 GiB final-file limit, 3 GiB temporary space per job, 6 GiB total temporary space, a 10 GiB library limit and a 1 GiB free-space reserve. Temporary job retention is two hours. Limits are admission/runtime thresholds, not hard filesystem quotas or reserved disk capacity.

## Security and limitations

- Only download media you are authorized to access. There is no DRM, paywall, CAPTCHA or access-control bypass. Live streams and upcoming premieres are unsupported.
- Site support depends on yt-dlp, region, account eligibility and network conditions. YouTube may reject a session, require browser verification or return HTTP 403/429. The app reports the limitation; it does not manufacture tokens or evade restrictions.
- Format lists come from the source; unknown metadata stays unknown. Not every advertised format is guaranteed to download or play in every browser. Subtitle/caption languages are listed, but subtitle download/embedding is not implemented.
- Generic pages can offer up to ten explicit media choices. Quick Download does not automatically download playlists or channels.
- Membership Import requires a completed `subscriber_only` download from that channel first. It scans at most 200 candidates, accepts only explicitly confirmed members-only items, deduplicates IDs and rechecks access before downloading. Posts, member playlists and streams may be missed.
- Passwords use scrypt hashes. Login sessions are stored as hashes and invalidated by password/MFA changes. TOTP codes cannot be replayed; login throttling persists across restarts. Production cookies are Secure, HttpOnly and SameSite=Strict, with cross-site mutation checks.
- Private media/API endpoints require authentication. Socket guards block local, private, metadata and non-web destinations; production adds a host-managed container egress firewall for native helpers.
- Worker diagnostics are bounded and do not expose raw cookie values, request headers or signed media URLs. Arbitrary yt-dlp plugins and remote script components are disabled.
- `.gitignore` and the Docker build allowlist exclude runtime state, cookies, secrets, private reports and media. They do not remove already tracked files: audit both files and history before publication. No production credentials belong in configuration examples.

## Tests

```sh
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest -q
```

Tests cover owner access/MFA, durable jobs and library behavior, cancellation, format handling, network restrictions, scoped browser sessions and cleanup, account preference persistence, provider scoping, connection verification, analysis/download consistency and session failure classification. Cookie values and credentials in tests are synthetic fixtures; the TOTP vector is published in RFC 6238. FFmpeg/FFprobe are required. The final public source is tested separately from the running personal app, without reading real Chrome sessions or downloading media.

## Deployment status

Source publication is separate from deployment. No GitHub Pages, Oracle host or public application is configured by this repository's publication.

[Server account connection design](deploy/ACCOUNT-CONNECTIONS.md) describes shared device access, credential storage and reauthorization, including implemented encryption, fresh reauthentication, verified replacement and tested backup/recovery controls. Actual server provisioning and provider transfer still require separate approval. No Mac session is transferred automatically.

[Caddy HTTPS deployment guidance](deploy/README-HTTPS.md) documents the staged production path. Compose binds the API to host loopback, requires owner MFA and HTTPS, uses separate private volumes and runs a non-root, read-only container. Configure the owner interactively with `manage.py`; do not pass passwords or authenticator secrets as command-line arguments. The older [Tailscale deployment guide](deploy/README-VPS.md) is an alternative and should not be mixed with the Caddy path.

Before Oracle deployment, verify capacity/cost eligibility, storage mounts and backups, SSH access, hostname/DNS, TLS, MFA and IPv4/IPv6 firewall rules. Test restoration, reboot persistence and access from another network with the Mac shut down. Public exposure needs a separate review and approval.

## License

No license has been selected yet. Publishing the source does not grant an open-source license; a licensing decision remains pending.
