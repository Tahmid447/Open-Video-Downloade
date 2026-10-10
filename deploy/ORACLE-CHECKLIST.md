# Oracle setup and final configuration review

No Oracle account, cloud resources, real provider credentials or public exposure have been created by this preparation. Start only when signup maintenance ends. The owner must personally complete identity/card verification and accept terms. Do not give passwords, card details, private SSH keys, cookies or MFA secrets to chat.

## Minimum owner steps

1. Open [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/), choose **Start for free**, complete signup and select the home region carefully. The free compute/storage allowance applies there. Keep the account free; do not upgrade to Pay As You Go or select a paid capacity fallback. An available signup page does not prove successful account activation.
2. Once the console is available, use **Compute → Instances → Create instance** in the home region. Select an Always Free eligible Ubuntu 24.04 ARM64 image and **VM.Standard.A1.Flex**, **2 OCPUs**, **12 GB RAM**, **50 GB boot volume**. Use a public subnet with public IPv4; no load balancer, NAT gateway, managed database, marketplace licensing, paid DNS or extra compute. Check the cost/eligibility summary before creating anything. Count any existing resources against the tenancy total.
3. Generate a dedicated SSH key on your Mac or import an existing approved **public** key. Keep the private key locally outside Git, permission 0600. A public key can be supplied to Oracle; a private key must never be uploaded. Record the public IP and Ubuntu SSH username. Verify the SSH host fingerprint before accepting it.
4. Under the instance's subnet security rules/NSG, permit TCP 22 only from your current approved public IP `/32`. Keep 80, 443 and 8000 closed during preparation. Do not remove required provider routing/ICMP rules blindly. Do not publish an IPv6/AAAA address during this first installation.
5. Open **Storage → Block Storage → Block Volumes → Create block volume**, home region/same availability domain, **150 GB**, eligible free settings. Verify total boot plus block allocation is at most **200 GB**, including other instances. Attach to this instance; choose an attachment supported by the selected shape and follow Oracle's instructions. Do not format a disk with existing data.
6. Provide only the confirmed home region, eligibility/cost summary, chosen hostname and server public IP/SSH username so the technical preparation can continue. The agent can handle private installation over authorized key-based SSH; you need not enter Linux commands manually. Credentials stay in the owner browser/private terminal. Do not open HTTPS or transfer YouTube sessions yet.

Current labels can vary. If Always Free capacity is unavailable, wait or try another available domain in the home region. Do not switch to a paid shape. If the console shows a charge or eligibility is uncertain, stop for review before resource creation.

## Technical steps handled after SSH authorization

Verify ARM64, approved volume identity and an empty new device before any formatting. Mount at `/srv/ovd-data`, persist by UUID, and record the approved UUID at `/etc/open-video-downloader/data-volume.uuid` (root-owned, 0600). Test a reboot. Preserve the boot volume and all existing data. The installer refuses absent/mismatched mounts.

Install the reviewed clean source privately, Docker Engine 28+ and Caddy. Generate server-only master and independent backup keys; arrange separate offline private recovery copies. Enroll owner password/MFA in a private terminal. Keep proxy stopped and incoming HTTP(S) closed. Test encrypted recovery into a fresh directory, filesystem quotas, container isolation, private egress and restart persistence.

The source build is native ARM64. Docker runs yt-dlp/Deno/FFmpeg and stores the library on the mounted block volume. No connection to a Mac worker, Mac disk or Tailscale is required for normal use. A Mac Chrome connection is deliberately not portable or automatically restored.

## Proposed final configuration — pending approval

| Setting | Proposed value |
| --- | --- |
| Compute | One home-region A1 Flex, 2 OCPUs, 12 GB; Always Free confirmed in owner console |
| Storage | 50 GB boot + 150 GB data; UUID-verified `/srv/ovd-data` |
| Library budget | 100 GiB application cap; approximately 140 GiB physical data volume before filesystem overhead |
| Processing | One job; 1 GiB output cap; 3 GiB temporary job budget; 6 GiB temporary total; 2 GiB RAM/1.5 CPUs per container |
| Public address | Owner-selected hostname/A record; hostname and IPv4 still pending |
| SSH | Owner-approved source `/32`, dedicated public-key authentication |
| Public ingress | TCP 80 for redirect/ACME and TCP 443 HTTPS only after approval; no public 8000 |
| App listener | VPS loopback 127.0.0.1:8000 behind host Caddy |
| IPv6 | No DNS AAAA/public IPv6 exposure until independently reviewed |
| Authentication | Owner password + MFA, Secure/HttpOnly/SameSite session cookies, sensitive-action fresh reauthentication |
| Secrets | Encrypted credentials, separate private master key, independent backup key; actual provider transfer separately approved |
| Recovery | Approved independent private destination and matching offline key copies; destination still pending |

No public exposure is approved by this table. Present actual resolved values and verification results for approval before changing OCI/host 80/443. Then perform trusted TLS and external device tests. Obtain a separate authorization before transferring a YouTube-only session through the verified HTTPS import. Do not scan a channel; test one already-authorized member video. Access may be refused because of the cloud IP/provider protections; report that limitation precisely.

## Capacity and reliability limits

[Oracle's Always Free documentation](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm), checked October 10, 2026, lists 2 OCPUs/12 GB, 200 GB combined boot/block allocation, five combined volume backups, 20 GB object storage in an Always Free-only tenancy and 10 TB monthly outbound traffic. Confirm actual eligibility/allowance in the tenancy before provisioning. Trial credits must not be used to hide a paid configuration.

The proposed 100 GiB library cap leaves headroom on the 150 GB data volume. It does not leave enough for another full 100 GiB archive on the same volume. At that size use an existing independent private backup destination, or reduce the cap; no automatic paid expansion is permitted. Oracle object storage's free allowance is too small for that full archive. Device downloads consume server outbound bandwidth; processing is CPU-limited and one queued job runs at a time. Oracle capacity shortages and reclamation of idle instances after its seven-day utilization test prevent an unconditional always-on guarantee. Do not generate artificial load to evade reclamation.

The milestone is complete only after Oracle creation, approved installation, backup/key recovery rehearsal, approved DNS/TLS/public rules, separate authorized provider provisioning, one server-side member download, external/reboot tests, and iPhone access with the Mac switched off. Existing Mac apps, connections and library stay independent and untouched.
