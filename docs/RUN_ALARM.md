# Repeating local failure alarm

Terminal launches of these commands enable the alarm by default:

- `bise_allocate http-submit`, `pilot`, `http-pilot`
- `youthgames_register submit` (browser, headed or headless)
- `bise_results collect`

Add `--no-alarm` to opt out, or `--alarm` to make the choice explicit. Status, normalization, sync, export and help do not arm alarms. Programmatic `main()` calls default off; the terminal entrypoints enable them.

The alert repeats after an **error or unexpected termination**, including a killed process or a closed terminal. Successful completion (exit 0) and user interruption with Ctrl+C (KeyboardInterrupt or the CLI's exit 130 after draining in-flight work) stay silent. Silent exits finish the watcher and remove its prepared tone without playing audio or sending a desktop notification. Error alerts continue until acknowledged. Existing temporary-error recovery remains unchanged: a cooldown that the collector is still handling does not end the run and does not ring the alarm. A process that remains alive but hangs is not diagnosed by this exit alarm.

Resume registration normally; the existing SQLite registration ledger still controls idempotence:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && GAMES_WORKERS=6 && .venv/bin/python -m bise_allocate http-submit --config allocation_profiles/lahore_9th_raiwind.json --workers "$GAMES_WORKERS" --prepare-ahead 2 --in-flight 2 --dispatch-interval 6 --limit 0 --auto-recover --alarm
```

Silence ringing alarms from another terminal, safely repeating the command if necessary:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m run_alarm stop
```

This acknowledges only ringing alarms. It does not stop collection/registration or disarm other runs that are still working. Inspect watcher state, run label, exit reason, last playback and audio errors:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m run_alarm status
```

Check the real speakers with a bounded three-second test (audible):

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m run_alarm test --seconds 3
```

## Implementation

The laptop already provides open-source PipeWire. [`pw-play`](https://pipewire.pages.freedesktop.org/pipewire/page_man_pw-cat_1.html) plays a generated two-tone PCM WAV at full stream volume through the current default output. Fallbacks are `paplay`, `ffplay`, then `aplay`; no additional Python dependency is needed. Failed or stuck players are bounded and retried, and acknowledgment interrupts current playback. The master volume and mute settings are left unchanged. If no supported player binary exists, an alarm-enabled job fails before collection/submission starts, with an explicit `--no-alarm` escape hatch.

A Python watchdog runs in a separate OS session with detached input/output. It owns the read end of a private pipe; only the monitored command owns the write end. EOF detects both normal exit and SIGKILL without depending on a `finally` or `atexit` callback in the killed process. Normal exits also record the exit code. The watcher ignores terminal SIGHUP/SIGINT and posts a local desktop notification when available. It never accesses applicant data, the registration database, browsers or the network, and never restarts or replays submissions.

Per-run UUID files prevent collisions and avoid killing unrelated processes by reused PID. Acknowledgment uses a per-run file, not PID signaling. State, small logs and the prepared WAV live in a project-specific directory under `$XDG_RUNTIME_DIR` (fallback: the OS temporary directory), so a full project/output disk does not normally suppress the alarm. The WAV is created before the watcher is armed. Runs already active when this policy was updated retain their loaded watcher behavior; the new policy applies on the next launch. Playback continues even if a later state-file update fails. The tone file is removed after acknowledgment; the desktop runtime directory is temporary.

A dark/locked screen does not stop the detached watcher while the OS is running. A sleeping, hibernating or powered-off laptop cannot play an alarm; playback resumes after wake if the watcher survives. Muted or disconnected audio still prevents audible sound. The alarm uses the selected system output, including headphones/Bluetooth if selected. This feature does not change power-management settings.

## Silent regression command

The tests replace all audio players with temporary fake executables and disable notifications. They exercise real detached processes and SIGKILL without making sound or submitting applicants:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && PYTHONPATH=tests .venv/bin/python -m unittest tests.test_run_alarm tests.test_http_concurrent.CliTests -v
```
