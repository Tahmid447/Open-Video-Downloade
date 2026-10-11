# Bounded Oracle Always Free recovery

This workflow checks capacity from a standard GitHub-hosted Linux runner. It does
not require the owner's computer to be running. It provisions only one Tokyo
A1 Flex (2 OCPUs / 12 GB), a 50 GB boot volume and a 150 GB data volume. It never
selects another shape, region, trial-credit allocation or paid upgrade.

The public workflow contains no tenancy identifiers or credentials. Its GitHub
Environment is restricted to `main`. An Oracle Free identity-domain trust checks
the exact GitHub subject, immutable repository and owner IDs, branch and workflow.
An OAuth client without administrator roles plus the GitHub OIDC assertion obtains
a 20-minute OCI session bound to a newly generated in-memory RSA key. The public
key is base64 DER, matching Oracle's SDK. A wrong-audience exchange must fail
before the real exchange succeeds. The OAuth client secret and private deployment
configuration are encrypted GitHub Environment secrets, not source files. No
personal Oracle session, Oracle API private key, SSH private key, provider cookie,
application encryption key or media is supplied to the runner.

Oracle policies allow account inventory and narrowly scoped provisioning in a
separate compartment. Hard quotas deny other compute shapes and regions and cap
that compartment at 2 A1 OCPUs, 12 GB memory and 200 GB total disks. IAM changes,
firewall changes, resource termination, paid network services and account upgrades
are not granted. Inventory is reconciled across the root and project compartment;
new compartments, regions, unknown resources, resized disks or quota changes stop
provisioning. Published permanent free terms are checked again before allocation.

A tiny private Object Storage JSON receipt uses `If-Match` updates and a 15-minute
lease, in addition to GitHub workflow concurrency. Create requests are recorded
before submission, with OCI idempotency tokens. Unknown outcomes remain pending;
a later run reconciles the actual tagged resources before proceeding. Missing
previously approved resources are never silently recreated. A definite capacity
rejection is retried only after confirming no instance or boot volume appeared.
Each invocation performs at most four placement reports and one launch request.
There are no SDK retries or continuous capacity loops.

When capacity becomes available, the runner launches the fixed platform Ubuntu
24.04 ARM64 image with the owner's public SSH key and hash-verified cloud-init
installer. The data disk is allocated only after the 50 GB boot disk is verified.
Only the newly attached blank 150 GB disk may be formatted; any existing signature
stops installation. The UUID mount is mandatory. Temporary media, the library,
database and configuration all bind to that mount. The initial media budget is
measured from the actual filesystem with 16 GiB reserved for processing and state;
it is not advertised as 150 GB of usable video space.

Installation runs on the VM even after the runner exits. It reuses the existing
Docker/Caddy installer, creates the separate provider-encryption key on the server,
verifies private health and isolation, replaces the container and checks a SQLite
persistence probe. The runner reads the SSH fingerprint through authenticated OCI
console history and requests one reboot. A different boot ID must report successful
mount, database and private-container checks before the watch declares **private
installation ready** and disables itself. The owner must still enroll password/MFA,
take custody of recovery keys, complete backup and permitted media tests, and pass
HTTPS/external-device verification. Provider session transfer requires the secure
owner-authenticated provisioning procedure. This milestone is not a finished public
deployment.

All application ingress remains closed during this process. OCI SSH ingress is one
approved source `/32`; the installer also applies that host firewall restriction.
The application binds only to server loopback. The worker does not contact a Mac or
fall back to a Mac disk.

## Operation and limitations

The deployed schedule is bounded to once hourly, away from the top of the hour.
GitHub schedules can be delayed or skipped, and public-repository schedules are
disabled after 60 days without repository activity. This is best-effort recovery,
not a capacity reservation or availability guarantee. Oracle may reclaim idle free
instances. No artificial utilization is generated. An ambiguous mutation stops for
reconciliation; the workflow never guesses whether a request succeeded.

The small Object Storage receipt is control state, not the video library. It cannot
replace the 150 GB attached disk. Full encrypted media backups need an independently
verified destination and enough capacity; free Object Storage is too small for a
full library at the maximum size.

The initial provisioner is tested with synthetic SDK responses and safety tests.
Real cloud creation, ARM64 installation and reboot checks require Oracle capacity.
A green capacity-check job alone does not prove that a server exists or the website
is deployed. Public workflow output contains fixed status labels only; resource IDs,
console content and private configuration stay out of logs and artifacts.

Sources: [Oracle Always Free](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm),
[Oracle workload identity](https://docs.oracle.com/en-us/iaas/Content/Identity/api-getstarted/token_exchange_grant_type_workload_id-federation.htm),
[Oracle quota precedence](https://docs.oracle.com/en-us/iaas/Content/Quotas/Concepts/quota_policy_syntax.htm),
[GitHub free standard public runners](https://docs.github.com/en/actions/concepts/billing-and-usage),
[GitHub schedule limits](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
