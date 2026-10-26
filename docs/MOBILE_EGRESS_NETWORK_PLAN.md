# Private mobile egress network: proposed plan

Date: 25 October 2026, Asia/Karachi. Status: planning only. No Android app,
gateway, device enrollment or changes to the running registration service have
been deployed by this plan.

Build a reusable, owner-controlled service: consenting Android owners enable
their devices as private outbound connections for explicitly approved workloads.
Youth Games is the first adapter. This is not a public proxy marketplace.
Portal concurrency permission is operator-reported; its permitted quota and scope
must be represented in configuration rather than inferred from phone count.

## Working assumptions and boundaries

- V1: Android, signed private APK, one organization, 2 physical pilot phones.
- A phone owner can pause, impose data limits and revoke participation. The app
  displays a persistent service notification and never hides its operation.
- A device is useful only while powered, connected and allowed by Android to run.
  Do not promise permanent execution after force-stop or phone shutdown.
- Different phones/SIMs need not have different public IPs. Carrier NAT and shared
  Wi-Fi can produce shared egress. Observe actual exits and share their limits.
- More exit addresses do not establish more portal quota. Respect configured
  organization/campaign limits, upstream Retry-After and portal refusals. Do not
  switch an attempt to another phone to clear a throttle or evade an access rule.
- Keep the currently running registration service unchanged during development.

## Components and traffic path

```text
Existing registration worker, other approved clients
                    |
       private, authenticated proxy access
                    |
        Gateway / device tunnel registry
                    |
     existing outbound secure tunnel from phone
                    |
            Android mobile agent
                    |
        selected cellular or Wi-Fi network
                    |
              approved HTTPS site
```

The phone initiates its tunnel to a reachable server; no inbound mobile port,
USB tethering or phone public address is required. Forward traffic through the
phone's outbound socket: terminating a proxy on the server alone would still
use the server's exit address.

The public gateway must be independently reachable when the laptop is off. A
Proxmox private address alone is insufficient. Before deployment, check existing
owned ingress/443 capacity and routing. If unavailable, use an owned public VM
as the tunnel endpoint, connected privately to the worker. Do not alter another
production application's ports or depend on a laptop-hosted tunnel.

V1 supports authenticated HTTPS CONNECT to approved destinations on port 443.
Portal TLS terminates at the Python client and the official website. The mobile
agent forwards encrypted bytes without installing a CA or intercepting student
data. Phones can still observe destination, timing and traffic volume. Keep
student CSVs, photos, cookies and request/response bodies off phone storage.

## Stack and transport feasibility gate

The selected stable Android versions and responsibilities are recorded in
[the technology baseline](MOBILE_EGRESS_TECH_STACK.md), including Material 3,
Navigation 3 and Room 3. That document refines this initial component list.

| Component | Proposed choice |
| --- | --- |
| Android UI/lifecycle | Kotlin, Jetpack Compose, coroutines, foreground service |
| Android settings/keys | DataStore, Android Keystore, bounded local diagnostics |
| Control networking | OkHttp with explicit network selection |
| Reverse tunnel | pinned upstream FRP release, thin Go/mobile wrapper |
| Phone egress | bounded authenticated CONNECT listener, network-bound sockets |
| Device API | Python FastAPI, authenticated operator access |
| Initial registry DB | SQLite WAL, one writer; no student records in this DB |
| Gateway supervision | dedicated container/service, health checks, resource caps |
| Existing client | HTTPX plus a new managed-device route provider |

FRP supplies a maintained reverse-tunnel foundation, TLS and authentication
mechanisms. It does not provide the finished Android application, owner-consent
flow or per-device administration. Embedding/building it as an Android native
library is an engineering spike, not a verified drop-in AAR. Pin dependencies,
review their licenses and retain notices.

First prove one physical phone can expose an authenticated loopback CONNECT
listener through the tunnel, with correct cellular socket/DNS selection. If
FRP embedding or required per-device authentication does not pass, revisit the
transport before building a large dashboard. Do not design custom cryptography.

## Android application

Owner workflow: install signed APK -> review what is shared -> scan one-use
enrollment QR -> operator approves device -> owner presses Start sharing.
Permission denial leaves the device inactive with an actionable explanation.

App and persistent notification show:

- Device name, active organization/workload, connected/paused/retrying state.
- Cellular-only / Wi-Fi-only / explicitly allowed auto mode.
- Bytes sent/received today, daily allowance and sharing schedule.
- Battery threshold, charging-only option, temperature pause, stop button.
- Recent diagnostic reason and server acknowledgment; no applicant details.

Start from user interaction. Select the legitimate foreground-service type after
the physical feasibility spike. Do not label a permanent tunnel as dataSync to
claim unlimited runtime: Android 15+ imposes a six-hour/24-hour dataSync budget.
specialUse is a candidate only if the actual use qualifies, with its manifest
explanation and distribution review. Sideloading does not remove OS limits.
WorkManager can perform permitted maintenance; it is not a permanent socket host.

Use Android Network callbacks, per-network SocketFactory/bindSocket and that
network's DNS. Do not change the phone's global default route. Cellular-only
means never silently substitute Wi-Fi. Reconnect the control/tunnel channel when
the selected network changes; invalidate prepared portal sessions on that epoch.
No root, SMS/contacts/photos permission, remote shell or broad phone-control API.

Owner Stop/force-stop must be respected. Resume after reboot/process eviction only
where Android allows and the owner opted in; otherwise show Resume needed. Test
Samsung and another OEM with screen off and battery saver. iOS requires a separate
feasibility/distribution plan and is outside V1.

## Enrollment, isolation and policy

- Expiring one-use pairing invitations; immutable random device IDs independent
  of phone numbers, USB names and public IP addresses.
- Device-scoped credentials stored using platform key protection, authenticated
  TLS control connections, credential rotation and server-side revocation.
- Select FRP's supported auth plus an admission policy that restricts each device
  to its assigned proxy identity/endpoint. No fleet-wide secret as the sole trust
  boundary. Prove revoked devices lose existing sessions and cannot re-enroll.
- CONNECT listeners are authenticated and loopback/private-network bound; public
  ingress accepts authenticated device tunnels only. No public open proxy ports.
- Server assigns destination policy and port limits. Enforce approved hostname,
  resolved address and connection target at the phone too; block loopback,
  private/link-local ranges, metadata endpoints, DNS rebinding and arbitrary ports.
  Permit IPv6 only with equivalent validation; do not leave it an ACL bypass.
- Organization/workload-scoped access, bounded connections, buffers, timeouts,
  traffic allowance and disk retention. Operator can revoke a device immediately.
- Keep enrollment/security/audit logs separate from student's registration DB.

## Device discovery and health

Use authenticated registry events for dynamic add/remove; do not scan a friend's
network. New devices become schedulable only after approval, a fresh health lease,
successful tunneled HTTPS check and current workload assignment.

Proposed states: pending approval, connecting, ready, busy, suspect, reconnecting,
cooldown, paused by owner, quota exhausted, offline, revoked.

Initial heartbeat proposal: 20 seconds; 60 seconds without acknowledgment makes
the route suspect and stops new leases. Mark it offline only after tunnel and
selected-network/HTTPS evidence or an explicit owner stop. These timings are
tunable and must be measured for battery use and false alerts.

Track separately: control channel alive, tunnel usable, selected network ready,
approved destination reachable, quota/cooldown, and owner availability. A failed
IP-echo endpoint must not alone mark the whole mobile connection dead. Egress
observations must traverse the same selected network as portal traffic.

Use bounded exponential reconnect delay with jitter. Successful verified recovery
clears transport backoff but preserves server throttling deadlines. Public-IP
changes are observations, not a new identity or a reset of quota/cooldown.

## Quotas, scheduling and idempotency

Represent separate limits per organization/workload, device, observed shared exit
group and upstream portal scope. Deny admission when any applicable limit is
exhausted. Devices sharing the same observed public exit share that budget. A
public address is an imperfect grouping signal, not device authentication or proof
of the portal's exact limiter. Maintain conservative global controls as well.

Persist pacing and Retry-After in the existing registration ledger. Portal refusals
reduce work/cause cooldown; additional devices do not reset those deadlines.
Measured successful registrations/minute, not queued workers, is the performance
metric. Workers and sockets are separately bounded from dispatch rate.

An individual form attempt leases one device and network epoch for GET, cookies,
challenge and POST. Do not hop routes between those steps. Disconnect before
dispatch allows safe fresh preparation later; disconnect after dispatch creates
a durable uncertain outcome. Reuse the existing bounded uncertain retry and
portal-duplicate handling policy, with no automatic HTTP-layer POST replay.

Continue using one authoritative registration ledger with atomic claims. Known
confirmed identities skip before any network request, regardless of device or
profile. Save tracking IDs and receipts before marking confirmed. An ambiguous
POST cannot honestly be guaranteed exactly once without portal idempotency or
reconciliation; preserve that distinction instead of inventing success.

Retain the Raiwind/Model Town alternator centrally across all routes; never run
one independent alternator per phone. New/reconnected devices join via versioned
leases without restarting the campaign. Drain revoked/offline devices while
healthy routes continue within policy; all-offline mode waits and resumes.

## Integration with this checkout

Current evidence: `connections.py` loads a static list of Linux interfaces,
requires 2-8 routes, and the optional `proxy_routes.py` route accepts a public
IPv4 HTTP endpoint without credentials. `http_multi.py` fixes worker assignment
from that list at startup. None supplies dynamic authenticated phone discovery.
The university worker currently uses the single-route engine with the durable
alternator. Adding a proxy URL alone does not implement this proposal.

Add a RouteProvider abstraction and a ManagedDeviceRoute implementation using
stable device IDs and generation/health leases. Keep existing Linux/public-proxy
validation intact; the new private, authenticated endpoints get a separate loader.
Refactor fixed startup assignment to bounded dynamic device slots, sharing the
existing HTTPX submit, receipt, pacing and idempotency logic. Preserve ordinary
server egress as an explicitly configured route, never a hidden proxy fallback.

Extend the existing remote monitor with a devices table: name, selected network,
health reason, in-flight requests, confirmed/external/uncertain counts, cooldown,
traffic used and last-seen. Count confirmed registrations from persisted attempts
and receipts, not from sockets sent. Include current run and lifetime separately.

Registry tables: devices, enrollment_invitations, credential_refs, owner_consent,
sessions, health_observations, workload_assignments, egress_observations,
quota_buckets, usage_counters and audit_events. Store only secret references and
bounded metadata. Add device ID/session epoch to existing attempt-route evidence;
do not make a second mutable copy of applicant completion state.

Proposed project boundary: a separate reusable mobile-egress repository with
android/, tunnel-wrapper/, gateway/, api/, client-sdk/, deploy/, tests/ and docs/.
This BISE repo contains its workload profile and thin provider adapter. The app
works with future approved workloads through the same CONNECT/registry contract.
No marketplace/payments, arbitrary UDP, iPhone support or unrestricted public
egress in the first release.

## Proposed commands (design only; not available yet)

```text
mobile-egress devices list
mobile-egress devices invite --label Phone-A
mobile-egress devices check --device DEVICE_ID
mobile-egress devices pause --device DEVICE_ID
mobile-egress devices revoke --device DEVICE_ID
mobile-egress watch
```

Keep the existing `python -m bise_remote watch` as the workload monitor. Add an
explicit managed-devices config/provider option to registration after tests; do
not invent a ready-to-run production command before that implementation exists.

## Delivery and acceptance gates

1. Inventory owned public ingress, physical phone models/OS versions and
   distribution. Record approved workload domains, organization quota and owners'
   sharing settings. Review tunnel dependency/auth interfaces before freezing API.
2. One-phone physical spike: pairing, visible sharing, correct selected-network
   exit, HTTPS relay, stop/revoke and screen-off operation. No applicant POSTs.
3. Two-phone registry MVP: independently connect from different networks, dynamic
   discovery, owner caps, shared-exit grouping and stable reconnect. Run a measured
   screen-off soak; publish battery/thermal/MB-hour and recovery measurements.
4. Integrate provider behind an off-by-default option. Test claims, alternating
   order, persistent quotas, credential redaction and legacy route compatibility.
   Existing production campaign keeps its current writer until controlled handoff.
5. Bounded authorized registration pilot using pending, confirmed input: one
   attempt per device, verify server receipts/tracking and device identity. Then
   repeat to prove zero new POSTs for confirmed rows. No worker-count speed promise.
6. Increase active device count gradually while measuring successful throughput,
   memory, traffic, failures and upstream throttles. Roll back by draining managed
   routes and returning to explicitly approved baseline egress, preserving ledger.

Required regressions: duplicate invitation/clone/revocation, unauthorized target,
DNS rebinding/IPv6/private addresses, shared-IP quota, heartbeat-only failures,
network/egress change, owner stop, data cap, heat/low battery, app/gateway reboot,
screen-off/OS stop, pre-/post-dispatch disconnect, two workers claiming one B-Form,
no confirmed resubmission, uncertain evidence, global R/M order, lost registry,
all devices offline/rejoin, secret-free logs and unmodified legacy routes.

## Primary references checked for this plan

- Android service types and specialUse declaration/review:
  https://developer.android.com/develop/background-work/services/fgs/service-types
- Android 15 dataSync time budget:
  https://developer.android.com/about/versions/15/behavior-changes-15
- Android per-network sockets and DNS:
  https://developer.android.com/reference/android/net/Network
- User stopping a foreground service:
  https://developer.android.com/develop/background-work/services/fgs/handle-user-stopping
- FRP upstream and license: https://github.com/fatedier/frp
- FRP auth and admission hooks:
  https://gofrp.org/en/docs/features/common/authentication/
  https://gofrp.org/en/docs/features/common/server-plugin/
- FRP listener isolation and TLS:
  https://gofrp.org/en/docs/reference/server-configures/
  https://gofrp.org/en/docs/features/common/network/network-tls/
- Shared carrier addressing: https://www.rfc-editor.org/info/rfc6598/
