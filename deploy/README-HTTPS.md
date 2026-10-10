# Oracle A1 and owner-authenticated Caddy HTTPS

Status: prepared for review, not deployed. Local tests cannot establish Oracle availability, public certificate issuance, external firewall protection or Mac-off access. The older Tailscale scripts remain available as historical alternatives; use only the `*-https-*` path for this architecture.

Browser → Caddy HTTPS on the VPS → localhost-only FastAPI → private server library. Caddy runs as a host system service, so the downloader's dedicated bridge firewall stays intact. Compose fixes its gateway at 172.30.80.1 and trusts forwarded headers only from that gateway. Check subnet overlap before installation. Client-supplied forwarded headers do not establish identity; owner password and MFA do.

## Free-only provisioning by the owner

Current [Oracle Always Free documentation](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) lists A1 totals of 2 OCPUs/12 GB RAM and 200 GB combined boot/block storage in the home region. Use one VM.Standard.A1.Flex with an Always Free eligible Ubuntu ARM64 image, 2 OCPUs, 12 GB, a 50 GB boot volume and at most 150 GB block volume. Confirm existing tenancy resources also fit these totals and the console says Always Free. Do not use trial credits, upgrade to Pay As You Go, choose a paid fallback or add billable resources. If capacity is unavailable, wait or try another permitted availability domain in the home region. The owner handles signup, card/identity verification and terms.

## Prepare while public access is closed

1. Establish and verify SSH key access. In OCI ingress allow SSH only from the owner's current source address; keep 80/443 closed and never allow 8000. Preserve required provider image rules rather than indiscriminately replacing them.
2. Attach and mount the block volume persistently. Verify after reboot. Use Docker's **library volume backed by that mount** (configure its host backing through Docker's local volume driver before first start). A mount check inside a container proves a volume exists, not that the host has mounted the intended block device. Verify host `findmnt`/device UUID too. Do not create a second library volume on the boot disk by accident.
3. Review `deploy/.env.example`. Recommended library cap on the 150 GB volume: 100 GiB; retain room for temporary media, backups and filesystem overhead. Temporary downloads, SQLite and owner/session config are separate named volumes. The free-space guard and byte thresholds reject new jobs; library content never expires automatically.
4. From the reviewed local source run `./deploy/update-https-vps.sh user@server --install`. This installs/builds privately, keeps Caddy stopped, denies public 80/443/8000, sets Docker egress rules and boot hooks, verifies health and preserves volumes across updates. This command needs a real server address and SSH authorization. It does not provision Oracle or open public access.
5. Over a private SSH terminal, run from `/opt/open-video-downloader/current` with sudo:

```sh
docker compose --project-name open-video-downloader --env-file deploy/.env --env-file deploy/image.env -f deploy/docker-compose.yml exec downloader python manage.py set-password
docker compose --project-name open-video-downloader --env-file deploy/.env --env-file deploy/image.env -f deploy/docker-compose.yml exec downloader python manage.py enroll-mfa
```

Enter your own password and authenticator code there. Do not record or paste the setup key into chat/logs. Enrollment invalidates previous sessions. `manage.py auth-status` reports booleans without returning credentials. The secret config volume is private, outside the source and web root. Provider disk encryption and restricted encrypted backups still matter; the application does not encrypt session files itself.

## Hostname and final exposure approval

[Duck DNS](https://www.duckdns.org/about.jsp) supplies free subdomains that point to a chosen IP. The owner creates one and sets its A record to the VPS public IPv4. Its update token stays in the provider's UI or private server config, never Git or query logs. No third-party free DNS service offers a permanence guarantee. Prefer a single IPv4 A record initially; an AAAA record requires equally verified IPv6 routing/firewall rules.

[Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https) obtains and renews public certificates and redirects HTTP to HTTPS. Its [official Ubuntu package](https://caddyserver.com/docs/install) supports the target host architecture. Review the exact hostname, DNS records, Caddyfile, OCI ingress, SSH source, loopback publication, MFA status, tested image and backup locations with the owner **before** opening 80/443.

After that explicit approval, the owner runs:

```sh
sudo /opt/open-video-downloader/current/deploy/enable-https.sh --approved chosen-name.duckdns.org
```

Then allow OCI TCP 80/443 ingress for browsers and ACME. The script requires enrolled MFA, configures allowed hosts, validates Caddy, opens host 80/443 and verifies trusted HTTPS plus anonymous 401. A failed verification stops the proxy and closes those host ports. Never add public 8000. No downloader cookies or media are published as static paths. No Caddy access log is enabled.

Finally test from another network: HTTP redirects, trusted HTTPS, wrong-password/code denial, owner MFA login, unauthorized GET/HEAD/Range/download denial, successful owner streaming/download, blocked raw IPv4/IPv6 port 8000, reboot/mount persistence, backups, and access with the Mac shut down. Public exposure remains unverified until these pass. Keep source updates separate from opening or changing access.

## Backup and recovery

Use `manage.py backup-db --output /data/state/backup.sqlite3` for a WAL-consistent metadata backup; protect and copy that snapshot, library media and config to private storage. Secrets and media never go to public GitHub. Keep no more than the current Always Free backup allowance and never automatically buy additional backup capacity. Image rollback does not restore stale metadata or delete library files. For lost MFA, use the private server `set-password` command, then re-enroll before reopening public access. Any failed or interrupted owner config write should be inspected privately before removing its `.new` file.
