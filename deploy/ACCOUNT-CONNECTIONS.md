# Server account connections

## Implemented security boundary

The server owns the provider connection and Automatic/Public preference. Authorized devices receive metadata and authenticated media streams; provider cookies never return to devices. Analysis is bound to the resolved connection identity. Replacement invalidates prior analysis, and workers recheck authorization before downloading. Public mode sends no provider credentials.

Production Compose requires HTTPS, owner password, MFA and encrypted credential storage. Owner configuration (including password hash and authenticator secret) and provider sessions use AES-256-GCM with a fresh nonce and purpose-bound authentication. Only safe connection metadata and hashed browser session/reauthentication tokens are stored in SQLite. The master key is a separate 32-byte regular file, mode 0600, in a private directory outside source, images, runtime data volumes and the database. It is generated on the server at `/etc/open-video-downloader/keys/provider/master.key`, mounted read-only. Missing, invalid or mismatched keys, modified ciphertext and plaintext secret configuration fail closed. Host root can still access a running application's credentials; encryption does not protect a compromised server administrator.

Fresh password and MFA are required for replacement and disconnect. The one-use authorization grant expires after five minutes, is bound to the action and owner session, is kept only in browser memory, and is hashed on the server. Password attempts are durably throttled; MFA codes cannot be replayed. These checks also apply to the local Chrome workflow in this source version, without changing existing running Mac installations.

The local Chrome reader is restricted to the explicitly selected Mac profile on loopback port 8001. Linux/production disables it. It cannot forward or restore a Mac browser connection onto a cloud server.

## Provisioning after explicit transfer approval

1. Complete the private server installation, independent key recovery copies, owner password/MFA enrollment and backup/restore rehearsal. Review the exact hostname, TLS and network configuration before approving exposure. Test the server from another network.
2. Obtain the owner's separate authorization to transfer a YouTube session. Nothing in installation reads a Mac browser or imports provider credentials. No supported YouTube OAuth download connection is implemented.
3. Prepare only a YouTube-domain Netscape file locally using a supported browser/export method. Do not export the entire browser profile or unrelated domains. Keep it outside repositories in a private directory, mode 0600; enter Google credentials only in Google's login pages. Browser export requires a separate guided step, not a deployment-script action.
4. In the authenticated HTTPS website open **Advanced Account Setup**, choose that file, supply one accessible members-only video URL, confirm authorization, then enter fresh owner password and a fresh authenticator code. Credentials are sent only in the HTTPS body, never a URL. Avoid browser developer/network recordings. Requests are limited to 256 KB by Caddy.
5. The server rejects unrelated domains and scans no channel. It verifies that single video with candidate credentials held in memory, requires membership evidence and supported formats, then writes an encrypted immutable generation. Only after a durable write does it atomically replace the connection pointer in SQLite. Failed verification or database commit keeps the previous connection. Concurrent analysis/downloads block replacement; owner revocation during verification blocks commit.
6. Worker subprocesses receive decrypted YouTube-only cookies through stdin and keep them in memory, with HTTPS-only cookie policy. No permanent decrypted file is created and no raw cookies enter job records. Core dumps are disabled. Python/browser memory cannot promise byte-for-byte erasure; private host administration remains necessary.
7. Remove the local temporary export when no longer needed. Unlinking on SSDs, APFS snapshots or backups is not a physical-erasure guarantee; revoke the provider session if required. Retain only approved encrypted backups, and never include exports in Git or public deployment archives.

## Replacement, failures and recovery

Disconnect deletes metadata and the active encrypted generation; it does not sign out a browser. Orphan encrypted generations from interrupted replacement are cleaned at startup. Lost or expired access requires authorized reconnection. Membership evidence is channel-specific; a public extraction does not prove authenticated access. HTTP 403/429, CAPTCHA, DRM and cloud-IP rejection are reported accurately, without bypassing protections. Successful access from the Mac does not guarantee YouTube will accept Oracle's IP.

Use the offline encrypted backup/restore commands in [the HTTPS guide](README-HTTPS.md). Backups use an independent encryption key and authenticated streaming chunks, include a consistent SQLite snapshot and media, exclude key files, and restore into a new directory only. Wrong keys, tampering, truncation, unsafe archive paths and symlinks fail closed. Restored browser sessions and sensitive-action grants are invalidated. Retain the matching master key and independent backup key separately from the archive; losing either can prevent complete recovery.

Key rotation stages a re-encrypted config copy while stopped, validates it, and leaves the original intact. Switching the active config and key is an explicit offline administrative operation. Keep matching old keys for retained historical backups. Never replace a missing key with a random new one and expect old ciphertext to work.

## Remaining production verification

These controls have been tested with synthetic credentials locally on native ARM64. Actual Oracle mounts/reboots, public certificates, external IPv4/IPv6 firewall checks, independent recovery storage and real provider acceptance still require a provisioned server. Actual session transfer and public exposure each require owner approval. No cloud resources or credentials are provisioned automatically.
