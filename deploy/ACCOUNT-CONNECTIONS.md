# Server account connections

## Current implementation

The owner signs into Open Video Downloader from a device. The server owns provider connection metadata and the Automatic/Public preference in SQLite; a device does not need its own provider browser configuration. Analysis records the resolved provider and connection identity, and Quick Download rejects a different connection or an expired analysis token. Workers recheck access before downloading.

The Chrome reader is explicitly enabled only for the local Mac app on loopback port 8001. It reads only the selected profile using yt-dlp, retains YouTube cookies in memory and restricts their use to HTTPS YouTube requests. It is disabled on Linux and in production Compose. A Mac profile cannot be reused by a VPS automatically.

The existing manual import accepts a YouTube-only Netscape session file with explicit ownership/access consent. It rejects unrelated domains, stores the file in CONFIG_DIR with mode 0600 and never returns its contents in account status. Configuration directories must be outside Git and private (0700). Account summaries contain metadata, not credential values. Imported sessions currently rely on disk protection; application-level encryption at rest is not implemented.

## Provisioning a future remote connection

1. Configure the server's owner password, required MFA and HTTPS before accepting any sensitive import. Provision the owner interactively; keep credentials out of commands and logs.
2. Require a fresh owner authentication/MFA check for credential replacement. This sensitive-action reauthentication still needs implementation; a normal signed-in session alone is not the final production provisioning policy.
3. Offer only provider-supported authorized connection methods. The current fallback is an explicitly authorized, locally prepared YouTube-only session import over HTTPS. Do not upload a browser profile or automatically forward a Mac session. No supported YouTube OAuth download flow is implemented.
4. Before cloud provisioning, implement an encrypted credential store with authenticated encryption and a key held outside source control, images and the metadata database. Restrict service/volume access, encrypt backups independently and test restoration without exposing secrets. Imported sessions are currently plaintext files protected by filesystem permissions; do not claim this future encryption already exists.
5. Verify one owner-provided accessible members-only video, persist only safe connection metadata and channel evidence, then allow that server connection to serve authenticated owner devices. Do not scan channels during setup. Apply the same successful-verification-before-replacement policy to remote imports; the current manual import is marked pending until subsequent analysis.
6. Keep provider secrets server-side. Devices receive media metadata and private authenticated library streams, never cookies or provider tokens. An explicitly chosen Public preference must remain respected across devices and restarts.

## Expiration and access failures

Do not request a new session merely because membership verification for another channel is pending. Show the last channel-specific evidence separately from provider state. Public extraction success is not proof of authenticated access. A revoked/expired or missing credential needs authorized reconnection; a channel eligibility rejection needs an entitlement check. Storage, format, HTTP 403/429 and browser verification failures are reported without inventing credentials or treating them as proof of session expiration. Explicit Public mode remains available and sends no provider credentials.

During rotation, prevent active account work, invalidate the connection identity and require new analysis. Disconnect removes the app's credential file and metadata, without signing out the user's browser. Unlinking cannot guarantee physical erasure from SSDs or backups; revoke the provider session and apply private backup retention controls when required.

## Cloud readiness boundary

No cloud resources, credential transfer, public exposure or deployment are performed by this change. Complete sensitive-action reauthentication, encrypted credential storage, verified remote provisioning, backup/restore, TLS/MFA, network isolation, quotas and reboot/device testing before connecting a production server. Use provider-supported access only; no DRM, CAPTCHA or access-control bypass.
