# GitHub source repository

This private repository contains the complete application source: result
collection/parsing/storage, normalization, allocation, registration, remote
server operation, alarms, shared utilities, profiles, documentation and tests.
The HTML test fixtures use synthetic candidate identities.

Runtime data stays outside Git: student databases and photos, original roll
lists/workbooks, normalized registration inputs, result cards, generated reports,
live evidence/receipts, exports, virtual environments and credentials. These
files remain on the original laptop/server. A fresh clone does not contain
registered-applicant state and must not be treated as a replacement for the
existing live SQLite databases.

## Set up a clone

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m playwright install chromium
```

Install the system Tesseract executable separately. Supply the original input
files and approved photos locally before running collection/registration. Review
the checked-in profiles for paths, network interfaces, infrastructure checkout,
SSH alias and location settings on a new machine. Private deployment credentials
come from the operator's existing SSH/secret configuration and are not included.
The main README documents the commands and their operational boundaries.

## Keep backups separate

Use the existing verified SQLite snapshot/download and context-export workflows
for data backups and private handoffs. Git stores source history, while runtime
databases retain registration confirmations, uncertainty state and tracking IDs.
Publishing this repository does not start, stop or resubmit any collection or
registration workload.

Future reviewed source changes can be published with ordinary `git add`,
`git commit` and `git push`. Keep local data and secrets excluded. Re-running a
push uploads only new Git objects; it does not rerun application commands.
