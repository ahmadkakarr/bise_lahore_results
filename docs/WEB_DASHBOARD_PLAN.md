# BISE Operations Dashboard — implementation plan

Status: design and handoff only; no dashboard container, agent, website, or job controls have been deployed by this document.
Prepared: 2026-10-26, Asia/Karachi. Inventory below is a point-in-time observation, not a UI constant.

## Outcome

One private web dashboard, accessible with the laptop off, covering result collection, portraits, normalization, central delivery, and Youth Games submissions. Views are filterable by source, exam, year, class/part, input batch, normalization profile, registration cohort, institution/district, tehsil, UC and sport where those fields actually exist.

Keep the existing Python application and native systemd workers. Add a separate dashboard LXC and small observation agents. Static Astro HTML/JS/CSS is the frontend; authenticated runtime APIs provide live data. Static hosting does not eliminate the API or make private student records suitable for static assets.

## Verified baseline and handoff caveat

Source repository: `https://github.com/ahmadkakarr/bise_lahore_results` (private).
Local checkout: `/home/ahmad/code/personal/bise_lahore_results`.
Infrastructure checkout: `/home/ahmad/code/personal/digitalstudio-platform/ops/proxmox-platform`.
Operator SSH alias: `pve-prod-01`; credentials remain in their existing secret/SSH configuration.

At inspection, local HEAD was `7ffdbf4c1cb1e50b15708e6dc4a9c945985abe0b` with substantial uncommitted server/photo/16-worker fixes. A GitHub clone alone is therefore not proof of the deployed baseline. Preserve the local working tree. If the laptop checkout is unavailable, inspect the owned guest's current immutable source release and carry its fixes into the dashboard feature branch before relying on GitHub source. Never copy `.venv`, secrets, student databases, photos or receipts into Git. New tests/docs may exist only in the local checkout and must be included in the handoff when available.

Observed sources:

| Source | Location / service | Observation |
|---|---|---|
| SSC Part-I / 9th Annual 2026 | LXC 1001, `/var/lib/bise-results/workflows/ssc-2026-part1`, `bise-ssc-2026-part1` | 16 headless collectors; 132,258 saved at inspection; separate portrait backfill, normalization, and inbox relay. Source registration disabled. |
| HSSC Part-II / 12th Annual 2024 collection | LXC 950, `/var/lib/bise-results/jobs/inter-2024-part2-annual`, `bise-inter-2024` | Collection inactive; 29,128 verified results remain in the source DB. |
| Inter 2024 Universities / Open registration | LXC 950, `/var/lib/bise-results/workflows/inter-2024-universities-open-20261025`, `youthgames-inter2024` | Four HTTP workers active. 19,426 locally confirmed registrations and 806 external duplicates at inspection; these are separate metrics. |
| SSC Part-II / 10th Annual 2026 history | Laptop `data/results.sqlite3`, filtered by year/course/part | 3,776 saved locally; not a verified live server source. Import a coherent scoped historical snapshot/summary if it is to appear while the laptop is off. |
| Local SSC Part-I history | Same laptop DB | Older copy of the migrated 9th cohort; not an additional independent student population. |

Current SSC release: `/opt/bise-results/releases/ac9f2b8bdad8efeb2aaf`.
Current Inter registration release: `/opt/bise-results/releases/10af8f16b862401919c9`.
Discover current deployment metadata again before implementation; paths, counts and release hashes can change.

Relevant modules: `bise_results`, `bise_normalize`, `bise_allocate`, `youthgames_register`, `youthgames_shared`, `bise_remote`.
Inspect `bise_remote/{ssc,worker,photo_recovery,pipeline,handoff,relay,registration,registration_worker,snapshots,bundle}.py`, the result/registration stores, profile schemas, and `docs/{SSC_SERVER_OPERATIONS,PROJECT_HANDOFF,GITHUB_REPOSITORY,REGISTRATION_LEDGER}.md`.

## Architecture

```text
LXC 950: existing collector + central registration authority
  small read-only observation agent ─────┐
                                        │ authenticated private connections
LXC 1001: SSC collector + photos + norm  ├──> NEW dashboard LXC
  small read-only observation agent ─────┘      ├─ source aggregator / projections
                                               ├─ FastAPI authenticated API + SSE
Historical scoped snapshots ───────────────────>├─ dashboard metadata/history SQLite
                                               └─ Caddy + static Astro dist/
                                                          │ HTTPS + owner login
                                                          v
                                                desktop/mobile web browser
```

The host-side SSC inbox relay and existing registration authority continue unchanged. The dashboard does not replace, copy into a second writable authority, or independently dispatch registration work.

Initial dashboard placement: an unprivileged Debian LXC with 2 vCPUs, 2 GiB RAM and 32 GiB persistent storage, subject to live inventory. Reserve its VMID/IP through the existing allocator and ownership marker. Do not assume a particular next VMID, edit existing guest networking, or resize existing workers to create this dashboard. Native systemd services are sufficient; a Docker layer is optional and not required.

The dashboard must not hold a host-root SSH key. Source agents expose allowlisted observation functions only, with service identities, private network binding, narrowly scoped firewall rules, and authenticated/encrypted connections. Reuse the existing private ingress/owner identity system after discovery. Agent credentials stay server-side. Monitoring agents use separate environments and service units; installing them must not redeploy a collector release or restart a running registration worker.

## Stack and UI compatibility

Use latest stable compatible releases, validate peer dependencies/Node engines, and pin the resolved versions in lockfiles. Registry metadata inspected on 2026-10-26:

| Package | Observed stable version | Purpose |
|---|---:|---|
| Astro | 7.4.2 | Static pages, layouts and selective hydration |
| @astrojs/react | 7.1.1 | Interactive React islands |
| React | 19.3.1 | Tables, live views, dialogs and profile editor |
| TypeScript | 7.0.2 | Strict frontend types |
| Bootstrap | 5.3.8 | Selected responsive grid/container helpers |
| Tailwind CSS | 4.3.3 | Utility styling required by the chosen UI components |
| daisyUI | 5.7.50 | Astro shell, status cards, progress/badges/themes |
| shadcn CLI | 4.22.1 | Generate owned React component source; not a runtime UI version |
| @tanstack/react-table | 9.3.1 | Controlled table state with server-side data |
| @tanstack/react-query | 5.104.5 | Request caching, invalidation and reconnect behavior |
| @tanstack/react-virtual | 3.14.22 | Only where large visible log/row windows justify it |
| Vitest | 5.0.5 | Frontend units/component behavior |
| @playwright/test | 1.64.1 | Browser acceptance against mocks and private read-only deployment |

Use a currently supported Node LTS satisfying the resolved Astro engine (observed requirement `>=22.23.4`); pin its exact runtime. Recheck all versions when implementing. Do not use Bootstrap 6 alpha or prerelease framework builds. TanStack v9 must use its actual v9 APIs, not copied v8 examples.

Interpret the requested “shared CN UI” as **shadcn/ui** unless corrected. Its Astro integration requires React and Tailwind. All requested CSS systems have defined, limited roles:

- Bootstrap: compile only grid/containers/needed helpers, scoped to the layout wrapper. Do not import its full component bundle or run Bootstrap JS on React-owned controls.
- daisyUI: explicit class prefix (for example `d-`) and selected components/themes.
- Tailwind: a configured utility prefix where needed; configure generated shadcn classes consistently with that prefix. Match real generated prefix syntax for the installed versions.
- shadcn/ui: React tables/toolbars, filters, accessible dialogs and forms. Reuse a single shared component directory.
- Exactly one global CSS reset; avoid combining Bootstrap Reboot and Tailwind Preflight. Use shared design tokens and one synchronized light/dark preference. Never style one widget using all three systems.
- Before the full UI, test a compatibility page containing the responsive grid, daisyUI cards, shadcn dialog/select and TanStack table. Include portal rendering, focus, z-index, mobile, and light/dark checks.

Backend: FastAPI + Pydantic, HTTPX for private agent calls, SQLite WAL for dashboard-owned metadata/projections and bounded history, Caddy for static serving and `/api` reverse proxy. Reuse the application's existing Python dependencies/helpers where appropriate, in separate service environments. No initial Redis/Kafka/Celery cluster or duplicate submission engine.

## Dashboard modules

### Overview

Show separate collection, photo, normalization, delivery and registration totals; active runs; current throughput; pending work; agent health; freshness; disk headroom. A filter affects every card/table/chart consistently. Display metric scope and whether totals represent records, distinct identities, batches or memberships. Detail links retain current filters in the URL.

### Collections

Year/class/part/session and batch tables; saved/pending/in-flight/review/failed; requested workers versus observed fresh worker activity; OCR slots; rolling saved/minute; retry state/reason/countdown; last successful save; last error; expected stop/completion versus unexpected failure. Separate total historical saves from saves during the selected run.

Batch detail contains an input fingerprint, cohort metadata, source owner, original-photo availability, recovery backlog, bounded worker/event tables, and result lookup. Preserve roll numbers/IDs as strings. Do not treat roll alone as a global identity. Existing optional historical result cards need not be regenerated.

Portraits have their own stage: original file ready, missing, retry, quarantined, oversized-for-upload, and legacy BLOB provider where applicable. The source retains files/DB binding; the dashboard fetches authorized previews on demand rather than mirroring all images.

### Normalization

Profiles, source scopes, selection rules and immutable revisions; counts evaluated/not evaluated, age eligible/ineligible/unknown, other blockers, duplicate identity conflicts, photo blockers and candidate ready rows. Show age-rule/catalog version, actual versus provisional DOB provenance, and original institution/district labels.

A profile editor can offer validated fields and filters for class/year, school/institution, roll bounds or batch; category/subcategory, tehsil/UC and sport settings; current official options; and operator-approved defaults. Preview the exact selection and before/after impact before applying a new revision. Already registered applicants keep their original payload, allocations and receipt history. A UI must never silently regenerate DOB, infer new residence, reshuffle completed registrations, or change source board fields.

Initial UI supports viewing and previewing profiles. Applying changes is a separately enabled audited command, not a direct browser write to source SQLite.

### Central delivery

Show source-ready revisions, backlog, durable central acknowledgements, awaiting policy, existing central jobs, registered duplicates, and delivery errors. Inbox acknowledgement means staging, not registration. A record awaiting registration policy is not shown as registered. Distinguish the latest source revision from superseded or historical deliveries.

### Registration

Campaign/profile/batch and Raiwind/Model Town/UC/sport filters; ready, preparing, dispatched/awaiting response, registered with actual tracking ID, external duplicate, uncertain, rejected, invalid and deferred. Show route-level pacing/cooldowns, workers, throughput and redacted response reasons where the runner provides them.

Use the existing canonical `(portal, actual campaign, canonical BForm)` registration authority and receipt/terminal guards. Cross-profile and cross-source registrations cannot be counted or dispatched as new merely because a new normalization version exists. External duplicates do not have a locally verified tracking receipt unless the system actually holds one. Keep missing tracking IDs explicitly missing.

Provide an authorized student timeline: original source key -> photo reference -> normalization revision -> inbox acknowledgement -> registration attempt/confirmation/tracking. Where provenance cannot establish attribution, use an “unattributed” bucket rather than guessing a class, batch or tehsil.

### Sources, history and operations

Source registry, live versus historical/replica designation, capabilities, current service state, source and dashboard timestamps, known compatibility/schema version, guest versus host free storage, restart/recovery information, bounded sanitized logs and incidents. Historical 10th data is labeled with snapshot time. A disconnected laptop is not a failing remote collector.

Downloads use existing consistent backup/bundle flows as asynchronous durable jobs, with manifests, content hashes, capacity checks, expiry and resumable transfer. Default CSV/JSON; no automatic Excel generation or result-card URLs. Large artifacts must not be loaded into API RAM or the browser merely to produce a progress bar.

## Data and metric contract

Each source has a stable `source_id`, source generation/epoch, canonical exam scope, owner location, run/batch/profile IDs, schema version, capabilities and replica relationship. Each snapshot carries source-generated time, agent observation time, dashboard receipt time, sequence/version and freshness state.

Collection identity is exam-scoped: board/course/year/part-or-exam-type/session/roll. Preserve existing job IDs as source-local references. Batches are memberships and can overlap. Never sum them as distinct students without deduplication. Local 9th history and its server migration are replicas, not two independent populations.

Registration identity uses the existing portal/campaign/BForm key. Counters distinguish confirmed receipts and external duplicates. Stage totals are not all disjoint: an age-eligible student may still lack a photo; a delivered row may still await policy. Publish documented partitions per stage and a separate funnel; never add overlapping stage totals together.

Represent unavailable/unsupported counts as unknown, not zero. Distinguish configured workers, current in-flight jobs, and historical worker rows. If the existing event contract has no worker timestamp, show that limitation rather than inventing fresh worker activity. Throughput uses persisted run/generation baselines and a rolling window; handle restarts, counter resets and overlapping source snapshots. ETA is unknown when no valid rate is available.

## Live transport and source load

1. Each agent reads existing atomic `runtime/status.json` and bounded runtime events as the fast path. Read canonical SQLite only for supported indexed counters, scoped detail queries, or reconciliation, using `mode=ro`, short transactions and explicit query budgets. Never use `immutable=1` for a live WAL database, SQLite over NFS, long write transactions for observation, or full DB copy/hash on every tick.
2. A single dashboard aggregator polls each agent roughly every 5 seconds with bounded concurrency/timeouts, jitter, exponential recovery and independent failure handling. Database reconciliation runs less frequently. Many browsers do not multiply source polling.
3. Dashboard API serves cached/projection state. One SSE stream per browser carries scoped changed data; TanStack Query handles query caches and targeted invalidation. Use event IDs/generation, bounded replay, reset/full-snapshot handling, keepalives and reconnect. If SSE is unavailable, use bounded 5–10 second snapshot polling. A gap never silently drops state.
4. SSC can expose committed `result_events` and pipeline cursor semantics. Legacy Inter sources without the same event schema use capabilities and coherent snapshot versions; do not invent an append-only log or rely on job ID alone for mutable status updates. Any new exporter sequence must be durable and advance only after its own export commit.
5. Detail/table endpoints use server-side scope filters, allowlisted sorting, cursor pagination, page/byte/time limits and stable source-qualified row keys. Start at 50/100 rows per page. No 200,000-row browser payload. Query plans and reader budgets must be verified against real large databases.
6. Redacted logs use incremental offsets, bounded windows and rotation-aware cursors. Do not broadcast raw HTML, photos, BForms, full DOBs or full DB rows in the global stream.
7. On source outage retain last-known values with a visible age and source-specific stale/offline status. Dashboard and healthy sources continue. Reconnect revalidates identity and source generation, resumes/reconciles cursors, and does not repeatedly restart workers.

The recent SQLite incident must remain covered: collector resume now uses 500-row pages and releases write locks before retention/file checks; photo claims use a compact partial index before loading one result. Existing regression `tests/test_remote_queue_contention.py` reproduces the old lock failure. Monitoring must not undo these protections.

## API and package layout

Suggested additions within the existing repository:

```text
apps/operations-dashboard/   Astro + React + shared components/styles/tests
bise_dashboard/             FastAPI, aggregator, projections, source adapters
bise_dashboard_agent/       narrowly scoped read-only guest observation API
contracts/dashboard/       OpenAPI/JSON schemas and synthetic fixtures
ops/dashboard/             systemd/Caddy/guest config and idempotent deploy scripts
dashboard_profiles/        nonsecret source definitions; secret references only
docs/                      operator guide, architecture, rollout, rollback
```

Example API surface: `/api/v1/overview`, `/sources`, `/collections`, `/batches/{id}`, `/normalizations`, `/deliveries`, `/registrations`, `/students/{source-qualified-id}`, `/events`, `/downloads/{job-id}` and health/readiness. Typed API errors, request IDs, source freshness and feature capabilities are part of responses. Arbitrary file paths, SQL and shell commands are never API parameters.

Dashboard state stores source registrations, latest snapshots/cursors, compact metric history, incidents, operator audit and future durable command receipts. It is a rebuildable projection, not the authority for student results, registration state or photos.

Retention proposal: detailed metric samples for 24 hours, one-minute aggregates for 30 days, hourly history for six months, bounded redacted logs for seven days with a hard size cap, and persistent compact run summaries. Configure these instead of retaining an unlimited copy of every full status payload.

## Authentication and command boundaries

Reuse current private owner ingress/SSO if available; otherwise implement a maintained OIDC library with explicit issuer/audience/session validation and exact origins. Use HttpOnly/Secure sessions and CSRF protection for state changes. Static UI assets are generic; protected data and image responses are authorized and appropriately noncacheable. Viewer/operator/owner permissions are enforced in the API, not just hidden buttons. Keep secrets out of built assets and Git.

Monitoring-only is the first live release. A later control layer may invoke allowlisted existing application operations through a dedicated scoped broker. Never give a web request `pct exec`, unrestricted shell, root SSH, arbitrary systemd units, DB overwrite or a bare `prepare` endpoint. Commands need a persistent idempotency key, actor/scope, current expected source/profile revision, an execution lease, timeout/cancel handling, audit and a terminal receipt. A double click or browser reconnect must not create a second run.

Distinguish collection start/stop, normalization preview/apply, and registration enable/submit permissions. Monitoring installation cannot start a new applicant batch, replay uncertain POSTs, erase confirmation state, or enable currently disabled SSC registration. Reuse existing pacing/Retry-After and uncertainty policy; do not change them as a dashboard shortcut.

Optional browser notifications/audio require user opt-in; intentional stop and normal completion remain silent, matching the current operator preference. An unexpected stop/offline incident can be recorded once and acknowledged; avoid repeat alert storms.

## Delivery phases and acceptance

1. **Discovery and baseline:** inspect live policy/capabilities, repo/working tree, deployed hashes, owners, runtime services, schema/counts, auth/ingress and storage. Resolve the newer-production-versus-GitHub source gap without resets. Produce the source registry and metric dictionary. Reserve a dashboard guest only after inventory/ownership checks.
2. **Read-only live MVP:** deploy the separate dashboard LXC, private agents, authenticated static UI/API, overview and all pipeline-stage summaries. Both server sources appear; historical 10th is clearly historical/unavailable until a valid snapshot is imported. Verify counts against actual source queries and the existing CLI, including registration duplicate distinctions.
3. **Details and normalization workspace:** server-paginated tables, filters/search, provenance timeline, actual tracking IDs, profile inspection/preview, original-photo preview, history and async verified downloads. Add 10th historical import through a coherent scoped snapshot, not a copied live DB file.
4. **Controlled operations:** prepare/test the audited idempotent command broker behind permissions/feature flags. Enable only the operations explicitly selected by the owner. Registration activation remains separate from dashboard deployment.
5. **Hardening and handoff:** source disconnect/reconnect, process/browser restart, stale heartbeat, cursor gap, agent/database busy, 500k synthetic rows, authentication failures, unsafe path/sort inputs, simultaneous viewers, command double click, dark/light/mobile, SSE fallback, data leakage checks, storage bounds, backup/restore and rollback. Confirm collectors/registration remain healthy and laptop-off access works through the private gateway.

Definition of done for the first live release: a real authenticated URL; deployed source revision/artifact hashes; working live read-only overview and pipeline modules; honest historical/unsupported states; bounded source load; verified data freshness and source recovery; no source-state mutations or new portal submissions caused by monitoring; passing tests; idempotent deploy/verify/rollback; concise commands and operator documentation. A mock screenshot or successful frontend build alone is not live acceptance.

## Decisions to discover, not guess

- Actual free dashboard VMID/IP and host capacity.
- Existing private hostname/ingress and owner-auth integration. An exact new public domain, if desired, remains an operator choice; do not create DNS/public exposure without it.
- Which local historical dataset/snapshot is available to import for 10th.
- Which control operations should eventually be enabled. Default is read-only.

These do not block writing the read-only frontend/API against explicit mock contracts and live read-only agents. No credentials need to be pasted into chat.

## Primary references

- Astro islands: https://docs.astro.build/en/concepts/islands/
- Static versus server endpoints: https://docs.astro.build/en/guides/endpoints/
- shadcn/ui Astro setup: https://ui.shadcn.com/docs/installation/astro
- Bootstrap selective imports: https://getbootstrap.com/docs/5.3/customize/optimize/
- daisyUI prefix/theme configuration: https://daisyui.com/docs/config/
- Tailwind reset configuration: https://tailwindcss.com/docs/preflight
- TanStack Table: https://tanstack.com/table/latest/docs/overview
- FastAPI scopes: https://fastapi.tiangolo.com/advanced/security/oauth2-scopes/
- Caddy static serving / reverse proxy: https://caddyserver.com/docs/caddyfile/directives/file_server and https://caddyserver.com/docs/caddyfile/directives/reverse_proxy
- Exact package versions above were read from the official npm registry's package `/latest` metadata. Resolve and verify again at implementation time.
