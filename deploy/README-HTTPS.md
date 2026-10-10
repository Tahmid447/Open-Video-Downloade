# Oracle A1 and owner-authenticated HTTPS

Prepared and tested locally on native Linux ARM64; not deployed. [Oracle checklist](ORACLE-CHECKLIST.md) contains the remaining console steps and approval boundaries. Use this HTTPS deployment path; older Tailscale scripts are historical alternatives and are not validated for this security configuration.

Ordinary device browser → Caddy HTTPS on VPS → localhost-only FastAPI → private server library. yt-dlp, Deno, FFmpeg, queue processing and all permanent library storage run on the VPS. The Mac is used only for administration and explicitly approved provisioning. Caddy is a host service; the downloader has a dedicated bridge with private/metadata/tailnet egress denied. Forwarded headers are trusted only from gateway 172.30.80.1; browser headers cannot establish owner identity. Check subnet overlap before installation.

## Prepare privately

Use the home-region Always Free A1 plan in the checklist. Attach the approved block volume, mount persistently at `/srv/ovd-data`, verify its filesystem UUID and record it in root-owned `/etc/open-video-downloader/data-volume.uuid` (0600). Verify the mount after reboot. `prepare-cloud-storage.sh` refuses a missing or mismatched mount; it never formats a disk or falls back to the boot disk. Compose binds the library to that host mount. Temporary media, SQLite and encrypted configuration use separate private Docker volumes on the boot disk.

Keep OCI 80/443/8000 closed, permit SSH only from your approved current source address. Review `deploy/.env.example`: 100 GiB library cap, one processing job, 1 GiB file limit, 3 GiB temporary budget per job, 6 GiB total temporary cap, 1 GiB minimum free space, 2 GiB container memory and 1.5 CPUs. Caps are admission controls, not reserved disk space; monitor the actual filesystems. The 150 GB data volume needs an independent destination for a full backup near the library cap.

From the reviewed clean Git checkout run:

```sh
./deploy/update-https-vps.sh ubuntu@SERVER --install
```

This transfers only tracked allowlisted source, installs/builds privately, leaves Caddy stopped, preserves runtime volumes and applies firewall/startup mount guards. No browser profiles, keys, owner files or media are copied. A server-only random master key is created at `/etc/open-video-downloader/keys/provider/master.key` (0600, private directory, UID 10001). The app mounts it read-only outside data volumes. Missing/wrong keys and plaintext or tampered secret configuration fail closed. Keep a private offline recovery copy of this key, separately from data backups; never paste its bytes into a command, chat or environment variable.

Over a private SSH terminal:

```sh
cd /opt/open-video-downloader/current
sudo docker compose --project-name open-video-downloader --env-file deploy/.env --env-file deploy/image.env -f deploy/docker-compose.yml exec downloader python manage.py set-password
sudo docker compose --project-name open-video-downloader --env-file deploy/.env --env-file deploy/image.env -f deploy/docker-compose.yml exec downloader python manage.py enroll-mfa
```

Enter the password and MFA code only at the private terminal prompts. Do not record enrollment or paste its setup key into chat/logs. `auth-status` reports booleans only. Owner password hash and TOTP secret are encrypted, and changing them invalidates old owner sessions. Production requires MFA and HTTPS. The only plaintext credential material is transient process/browser memory; host root can access it, so encryption is not a defense against a compromised administrator.

## Encrypted backup and recovery

Full backups include encrypted config, WAL-consistent SQLite and private library media. They exclude key files. A distinct backup key is mandatory; an archive alone does not contain either key. Restore needs both the matching provider master key and independent backup key. Preserve old matching keys for retained historical archives. Do not use `backup-db` alone as a disaster-recovery backup.

Example on the server, after privately arranging an independent mounted backup destination with sufficient free space at `/srv/ovd-backups` (directory 0700, owned by UID 10001). Do not create paid storage. Generate the independent backup key without displaying its value:

```sh
sudo install -d -m 0700 -o 10001 -g 10001 /etc/open-video-downloader/keys/backup
sudo docker compose --project-name open-video-downloader --env-file deploy/.env --env-file deploy/image.env -f deploy/docker-compose.yml run --rm --no-deps -T --volume /etc/open-video-downloader/keys/backup:/run/ovd-backup-keys --entrypoint python downloader manage.py init-key --key-file /run/ovd-backup-keys/master.key
```

The command refuses to overwrite an existing key. Retain its separate private offline copy. The long-running app never mounts this backup key. Stop processing for a consistent backup; maintenance locking rejects backup/key rotation against an active app:

```sh
sudo ./deploy/service.sh stop
sudo docker compose --project-name open-video-downloader --env-file deploy/.env --env-file deploy/image.env -f deploy/docker-compose.yml run --rm --no-deps -T --volume /etc/open-video-downloader/keys/backup:/run/ovd-backup-keys:ro --volume /srv/ovd-backups:/backup --entrypoint python downloader manage.py backup --output /backup/rehearsal.ovdbak --backup-key-file /run/ovd-backup-keys/master.key
sudo ./deploy/service.sh start
```

Encrypted chunk authentication detects alteration, truncation and reordering. Backups are 0600 and reject symlinks/special files. A conservative free-space check reserves at least the source size plus overhead and 1 GiB. An interrupted backup deletes its partial archive. Use unique archive names; existing archives are never overwritten.

Rehearse restoration into a new private directory, without replacing live volumes:

```sh
sudo docker compose --project-name open-video-downloader --env-file deploy/.env --env-file deploy/image.env -f deploy/docker-compose.yml run --rm --no-deps -T --volume /etc/open-video-downloader/keys/backup:/run/ovd-backup-keys:ro --volume /srv/ovd-backups:/backup --entrypoint python downloader manage.py restore --input /backup/rehearsal.ovdbak --output /backup/restored-rehearsal --backup-key-file /run/ovd-backup-keys/master.key
```

Allow space for authenticated staging plus restored media (roughly twice archive/library size for a large recovery). Wrong keys, unsafe paths, symlinks, invalid SQLite and unportable Mac Chrome connections fail before restored data is published. Successful recovery relocates media paths, invalidates all owner sessions/action grants and requires a fresh MFA code. Verify media hashes and permissions privately. Actual cutover requires stopping the app, preserving old volumes, installing the restored config/state/library as a matched set and checking mount/key identity. Keep public access closed during recovery. Never run `docker compose down -v` on data you need.

`manage.py rekey-config --output NEW_DIRECTORY --key-file NEW_PRIVATE_KEY_FILE` stages and validates new encrypted config while stopped; it leaves the original and database intact. Mount both key paths read-only in a temporary maintenance container. Deliberately switch the active config and corresponding key as a pair before starting; keep old copies for rollback/historical backups. Image rollback does not roll back data. Lost MFA requires private `set-password`, then re-enrollment before exposure.

Oracle's free 20 GB object-storage allowance cannot hold a full 100 GiB library archive. Block-volume snapshots have a five-backup combined allowance and do not replace tested application recovery or separate offline keys. A backup on the same volume is only a local recovery copy; it is not independent disaster recovery. Reduce the library cap or provide an approved existing private backup destination if no independent free destination is available. No automatic paid expansion or backup purchase is configured.

## Final exposure review

Choose a hostname; [Duck DNS](https://www.duckdns.org/about.jsp) is one free option. Put its A record on the VPS public IPv4. Keep update tokens in private provider/server configuration. No AAAA record until IPv6 is independently verified. [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https) obtains and renews trusted certificates; its [official Ubuntu package](https://caddyserver.com/docs/install) supports ARM64.

Present the exact VM/storage/free eligibility, SSH source, hostname/DNS, Caddyfile, OCI ingress, host firewall, localhost-only 8000 mapping, owner MFA status, key recovery and backup destination to the owner. After explicit approval, allow OCI TCP 80/443 (IPv4 only initially) and run:

```sh
sudo /opt/open-video-downloader/current/deploy/enable-https.sh --approved chosen-name.duckdns.org
```

OCI ingress must be ready for certificate validation before the script's HTTPS probe. Its failed verification stops Caddy and closes host 80/443. Never open public 8000. No access log is enabled; do not enable request-body/debug recording. The exact production certificate and network behavior remain unverified until this server step.

Test from another network: redirects/trusted TLS, wrong-password/MFA denial, owner login, anonymous GET/HEAD/Range/download denial, owner streaming/download, raw-port IPv4/IPv6 denial, private egress, reboot/mount persistence and recovery. Then explicitly approve provider transfer and use the [verified account provisioning workflow](ACCOUNT-CONNECTIONS.md). Verify one authorized members-only video; cloud-IP refusal must be reported without bypass. Finally test on an iPhone with the Mac switched off. Free-tier capacity and idle reclamation prevent an unconditional always-on guarantee.
