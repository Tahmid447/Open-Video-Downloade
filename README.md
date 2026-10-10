# Open Video Downloader — private cloud edition

This separate copy is prepared for an always-on Ubuntu VPS. Your installed Mac app stays intact.

Read [deployment instructions](deploy/README-VPS.md), [audit](AUDIT.txt), and [test/change report](REPORT.txt).

After SSH access is available, deploy from this folder with `./deploy/update-vps.sh user@server --install`. Future updates use `./deploy/update-vps.sh user@server`.

Access is through private Tailscale Serve to a localhost Docker port. No public downloader or public sharing is configured. Remote installation and device verification are pending server access.
