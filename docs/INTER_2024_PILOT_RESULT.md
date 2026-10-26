# Inter Part-II 2024 server pilot — 24 October 2026

Four actual board lookups, rolls 500001–500004, completed successfully in a
dedicated unprivileged Debian 13 Proxmox LXC (950, 192.168.10.239). The guest has
4 vCPU, 6 GiB RAM and a 32 GiB disk on **local-lvm**, not ZFS. Existing guests,
host routes and laptop registration data were not changed.

After the initial two-worker pilot, the operator requested eight workers.
The current profile/deployment now specifies **8 headless browser workers +
2 OCR workers**, with the same four-record pilot input. The launcher reads
these settings from the profile when setup runs; it no longer hard-codes two
browser workers. A cache-only rerun verifies eight in the launch configuration;
it is not an eight-worker sustained-throughput benchmark.

Verified outcome:

- Four source-matched student records and four distinct original student JPEGs.
- All four DOB fields remain empty. An explicitly unverified cohort assumption
  of approximately 18 in 2024 implies approximately 20 in 2026. It is separate
  from official DOB and does not confer games eligibility.
- Portraits are 2,404–19,156 bytes each, all within the current 256 KiB upload
  budget; original MIME, dimensions, source and SHA-256 are saved in SQLite.
- Student identity and photo commit together before raw capture deletion.
- Zero raw capture files remain for the four completed records.
- Repeated starts queued **zero** lookups. Attempts remain **four**; records and
  photos remain **four**. Repeated photo export wrote zero files, skipping four.
- Online SQLite snapshot downloaded with matching SHA-256. Integrity is `ok`,
  foreign keys are valid, and the original photo bytes/checksums survived backup
  and download. Snapshot/download connections close before file publication;
  regression covers WAL-mode input without leftover temporary WAL files.
- **84 relevant regressions pass**: HSSC, SSC collection/cache, photo/age policy,
  retention, actual browser fixtures, resource limits, snapshots and pilot setup.

Local downloaded DB: `artifacts/inter-2024-server-pilot/results.sqlite3`.
Exported photos: `artifacts/inter-2024-server-pilot/Student Photos/2024/HSSC/Part-2/`.
Exact applicant/photo mapping: `Student Photos/Photo Mapping.json`.
Structured proof: [pilot evidence](evidence/inter-2024-age-photo-pilot.json).

From the project directory:

```sh
.venv/bin/python -m bise_remote.pilot status
.venv/bin/python -m bise_remote.pilot download
```

`bise_remote.pilot start` resumes **only these four pilot rolls**. This initial
pilot did not start bulk collection or make games registrations. Subsequently,
the full 194,851-roll service and independent host-storage guardian were deployed
and started with 8 headless workers. Use `python -m bise_remote watch` for the
full batch; see [full launch evidence](evidence/inter-2024-full-start.json) and
[the remote plan](INTER_2024_REMOTE_PLAN.md).
