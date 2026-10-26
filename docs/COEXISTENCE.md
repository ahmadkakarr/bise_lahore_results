# Running collection, registration and messaging together

On 23 October 2026 the automation runtime history recorded WhatsApp
`Stream Errored (conflict)` / 401 at 11:18:40 Asia/Karachi. At 11:43:14 the
supervisor received signal 2 (Ctrl+C), then finished as `stopped`; it was a
terminal-owned stack. At diagnosis it was not running. The available kernel
log has no OOM kill for this date. These observations do not establish that
BISE caused the session conflict or the acknowledgement delays preceding it.
Do not describe the clean operator stop as a collector-induced crash.

The applications share laptop CPU, RAM, disk, networking and power state.
The BISE browser creates its own browser/contexts and closes only those owned
objects. It does not kill WhatsApp processes or change the ordinary network
route. Games submission uses independently bound routes. Existing USB isolation
preserves normal Wi-Fi routing/DNS; see `USB_ROUTING_FIX.md`.

## Collection defaults

Browser worker count is unchanged. OCR now defaults to two simultaneous
processes, one OpenMP thread each, niceness 10 and a 20-second per-call timeout.
`--ocr-workers 1..8` changes OCR concurrency independently of browser concurrency.
Below 1,536 MiB available RAM, the collector pauses **before claiming new rolls**
and resumes when memory recovers. Ctrl+C remains cancellable and committed rows
are preserved. `--min-available-memory-mb` sets that threshold (0 disables it).
These limits are scoped to the collector and its future subprocesses, not the
shell, existing applications or global OS environment.

## Independent workload scopes

The new launcher uses Linux systemd/cgroups v2 and coreutils nice. Every launch
gets a unique scope in `bise-background.slice`; it never stops existing jobs,
changes network routes or resets database state. It retains original application
arguments, terminal interaction, exit status and restart/idempotency contracts.

Collector defaults: CPU weight 25, at most four CPU cores, RAM high 4 GiB / hard
max 6 GiB, swap max 512 MiB, and task limit 1,024. Registration defaults: at most
two CPU cores, RAM high 768 MiB / hard max 1,536 MiB, same weight/swap/task limits.
Hard caps contain overload in that scope; they cannot promise that no process
will ever fail. If a cap terminates a process, rerunning uses its existing durable
database/receipt recovery. Change limits in `scripts/run_workload.py` after
measuring the workload, rather than increasing all limits blindly.

9th results (existing verified rolls are skipped):

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python scripts/run_workload.py collect -- roll_number_slips/2026/9th/roll_numbers_only.csv --year 2026 --class 9 --limit 0 --workers 18 --captcha-mode auto --headless --retry-unresolved
```

Current Model Town allocation (existing confirmed registrations are skipped):

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python scripts/run_workload.py register -- --config allocation_profiles/lahore_private_9th_model_town.json --connections connection_profiles/wifi-usb.json --workers-per-connection 8 --prepare-ahead 2 --in-flight 6 --dispatch-interval 5 --limit 0 --auto-recover --retry-uncertain --uncertain-retries 2 --alarm
```

Do not launch a second copy over an active registration run. Its existing DB
lock remains authoritative. Use Ctrl+C once to finish the current process before
switching launchers. To print either launch without running it, put `--dry-run`
before `collect` or `register`.

## Automation service

The persistent `automation-platform-dev.service` now has CPU/IO weight 200 and
512 MiB memory protection, with no newly imposed hard RAM cap. Relative weights
help under contention; `MemoryLow` is best-effort reclaim protection, not a
reservation or an uptime guarantee. The updated unit was installed and verified
without starting services, dispatching messages or interrupting the active
registration process. Existing Restart/Watchdog and intentional-stop behavior
remain unchanged. Prefer this supervisor over a terminal-owned stack:

```sh
cd /home/ahmad/code/deomeeautomations/automation-platform && systemctl --user start automation-platform-dev.service && ./scripts/dev.sh status
```

The WhatsApp worker also has a kernel lease on its real auth directory, in
addition to its legacy diagnostic PID lock. Another local worker cannot acquire
that auth directory through a symlink or another configured PID file. The lease
survives CPU stalls and is released automatically on process death. It does not
coordinate copied credentials on another host. A server login rejection still
requires explicit operator recovery; no automatic auth deletion or delivery
replay was added. Its stopped-worker diagnostic is documented in the worker
README and never sends business messages.

Suspend/power-off and real ISP/server outages still pause network work. No local
resource isolation can guarantee external service availability. Read-only incident
evidence and regression/scope results are in `artifacts/coexistence/`.
