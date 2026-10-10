# Private VPS deployment

This is an additional installation derived from the live Mac version. The Mac app, virtual environment, LaunchAgent and Tailscale settings are untouched. A full backup including the LaunchAgent is provided beside this folder. Only deploy this copy to the VPS.

## Architecture and defaults

Devices with Tailscale → private HTTPS Serve on the Ubuntu host → `127.0.0.1:8000` → non-root Docker container → FastAPI + yt-dlp + FFmpeg. Deno and bundled yt-dlp EJS support modern YouTube extraction; no cookies, credentials, remote script components or access-control bypass are enabled. Some sites reject cloud IPs; that cannot be solved by making this service public.

Use Ubuntu 24.04 LTS (22.04 and 26.04 also accepted), 2 GB RAM and at least 20 GB disk. Docker Engine 28+ is required because older versions had a localhost publishing issue. The installer uses Docker's official APT repository and Tailscale's official Linux installer. It refreshes package indexes and installs required packages; it does not blindly upgrade/remove unrelated packages.

Defaults in `.env.example`: one download plus one link analysis at a time; 1 GiB per final file/individual stream; 3 GiB temporary space per job; 6 GiB total temporary storage; 1 GiB disk reserve; 30 minute job timeout; 90 second analysis timeout; 2 hour fixed file retention; 30 history entries. Oldest completed entries/files can be evicted earlier when this history cap is reached. Downloads above those limits fail cleanly. Unknown sizes have inherited OS file-size limits as well as progress/storage checks. Disk usage checks every 250 ms are safety thresholds, not filesystem quotas; an in-flight write may briefly exceed a threshold. FFmpeg can need more space than the final file.

Media lives in one Docker volume, so crashes leave removable partial files rather than files embedded in an image. On startup, abandoned job folders are removed because in-memory jobs cannot survive a restart. Completed files are also cleared on restart/update. Periodic cleanup never removes an active job or a file being streamed. Serving a file does not extend its fixed expiry. Save important files to your browser before updates. There is no permanent download archive or database.

## Initial installation from the Mac

Once a key-based SSH login to the VPS works:

```sh
./deploy/update-vps.sh username@server --install
```

This transfers only a whitelist of source/deployment files; builds the image on the VPS; backs up root-only remote config; waits for container health; verifies localhost publication and private HTTPS. `sudo` may ask for your VPS sudo password in the terminal, never in a source file. SSH uses BatchMode and normal host-key verification: enroll a new server's host key through a verified first SSH connection before running this command. A server nickname/custom port can be configured in your Mac's SSH config.

If Tailscale is not logged in, the installer displays a one-time authorization URL and waits up to 60 seconds. If it is still pending it exits cleanly; open that URL in your browser and rerun the same command. Tailscale HTTPS/Serve may separately require tailnet admin authorization. Follow the CLI's authorization URL, then rerun. No reusable Tailscale key is saved. The installed `serve --help` is checked before configuring `tailscale serve --bg --https=443 http://127.0.0.1:8000`. Existing public sharing or an unrelated Serve proxy makes installation stop for inspection rather than overwrite it.

Configuration: `/opt/open-video-downloader/shared/.env` (root, mode 0600). Keep reserved `OVD_PORT`, `OVD_IMAGE` and `DOWNLOAD_DIR` out of it. The application must use a single Uvicorn worker. Do not scale Compose replicas: history and admission are intentionally in one process.

## Updates and rollback

```sh
./deploy/update-vps.sh username@server
```

Config is preserved and backed up before each update. New images build while the old app is running. A failed build leaves the old app running; failed startup/verification rolls back to the old image. Downloads/history do reset when containers are replaced, including rollback. Current and three previous releases/config backups are retained. The uploader never copies your `.venv`, local `.env`, downloaded media, logs, tests or Mac LaunchAgent scripts. Images are tagged by release. Old release images are removed; global Docker build cache is not pruned because other apps may use it. On a dedicated VPS review disk usage periodically with `sudo docker system df`; prune old build cache deliberately if needed.

Manual rollback on the VPS, after saving completed files:

```sh
sudo /opt/open-video-downloader/releases/PREVIOUS_RELEASE/deploy/service.sh start
```

Then update `/opt/open-video-downloader/current` to that same release so reboot uses it. The automated updater performs both steps on failure.

## Security and firewall changes

The application port is published exactly as `127.0.0.1:8000:8000`. Binding `0.0.0.0` inside the container is needed for Docker bridging and does not publish port 8000 on the host's public interface. The container has a read-only root filesystem, no Linux capabilities, no privilege escalation, a non-root UID/GID, a process limit, a memory/CPU limit and bounded Docker logs (3 × 10 MB). Tailscale on the host provides access control; this app intentionally has no public login system. Other authorized tailnet peers may reach it if tailnet policy allows: restrict your VPS's TCP 443 access to your account/devices in Tailscale grants/ACLs if your tailnet has other people. Never configure public sharing.

Installer firewall changes (VPS only): allow the SSH port from the current SSH connection; deny host TCP 8000; allow authenticated `tailscale0` inbound traffic; default deny other inbound traffic; default allow host outbound traffic; enable UFW. Existing UFW rules are retained; audit old allow rules and provider firewall rules during handoff. No public HTTP/HTTPS rule is added. TCP/UDP 41641 is not required: Tailscale can use relays. SSH password authentication is not disabled automatically, because the installer cannot prove your recovery access. After independently testing your key login and provider console recovery, disable SSH password/keyboard-interactive authentication if needed; validate with `sshd -t` before reloading SSH.

Docker traffic can bypass UFW. Additional rules attached to `DOCKER-USER` block direct inbound routing to the dedicated `ovd0` bridge and block downloader egress to private, tailnet, loopback, link-local, metadata and reserved IPv4 ranges. Only public HTTP/HTTPS and DNS may leave that bridge. The container uses public DNS resolvers (1.1.1.1 and 9.9.9.9) so a provider's private DNS address does not require weakening egress rules. An INPUT rule also blocks container-initiated host access. IPv6 is disabled for this bridge. Docker systemd `ExecStartPre`/`ExecStartPost` hooks apply these rules after daemon restart/reboot. The installer refuses an incompatible Docker firewall backend; it requires the standard iptables DOCKER-USER hook. Unrelated firewall chains are retained. The app validates initial URLs, and workers guard Python DNS/socket connections to block private redirects and DNS rebinding; the bridge rules protect native helpers too. Do not run this image with host networking or remove the guards.

The browser API rejects cross-site mutations and sends security/cache headers. URL credentials, local URLs, non-web ports, invalid format selectors and live streams are rejected. User filenames are sanitized and never used as yt-dlp output templates or shell commands. The UI renders source text through `textContent`.

## Reboot and verification

Docker, Tailscale and the downloader systemd unit are enabled on boot; Docker's restart policy restarts a crashed container. The healthcheck reports readiness if FFmpeg/ffprobe, yt-dlp or storage access is missing. Docker marks an unhealthy container but does not automatically restart merely unhealthy containers; inspect logs if it remains running but unhealthy.

```sh
sudo /opt/open-video-downloader/current/deploy/verify-vps.sh
sudo systemctl status open-video-downloader docker tailscaled
sudo tailscale serve status
```

The verifier checks container health, UID, read-only root, restart policy, localhost port mapping, local and private HTTPS health, Tailscale connection and absence of public sharing. During actual handoff also test TCP 8000 from outside the VPS (IPv4 and IPv6), check all host/provider firewall rules and SSH key access, reboot the VPS and rerun verification. From your iPhone with Tailscale enabled, open the private `.ts.net` URL and save a small file. An unauthorized device must fail to connect. Turn the Mac off and repeat the iPhone test. These device/reboot/external checks cannot be claimed from local preparation alone. Review Tailscale node key expiry in the admin console for this personal always-on server; authentication remains required.

## Local tests

Install `requirements.txt` and `requirements-test.txt` in an isolated virtual environment, then run `pytest -q`. Tests use mocked/small jobs, including real worker-process cancellation and child cleanup. `node --check static/app.js` validates JavaScript. Test Compose with a copy of `.env.example` as `deploy/.env`; change the local test host port to 18080 using `OVD_PORT=18080` so the Mac's port 8000 stays untouched. Linux container smoke tests use this test port. The production updater prohibits that override.

Sources: [Docker Ubuntu installation](https://docs.docker.com/engine/install/ubuntu/), [localhost publishing caveat](https://docs.docker.com/engine/network/port-publishing/), [Docker and firewall rules](https://docs.docker.com/engine/network/packet-filtering-firewalls/), [Tailscale Linux installation](https://tailscale.com/docs/install/linux), [Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve), [yt-dlp dependencies](https://github.com/yt-dlp/yt-dlp#dependencies).
