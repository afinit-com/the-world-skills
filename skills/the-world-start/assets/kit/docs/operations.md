# Running a harness all the time

The kit sets up no service. A harness runs while `harness.py` runs, and a reboot, a
closed terminal or a crash stops it until someone starts it again. That stays the
default: a service the user never asked for is a process they don't know to look for.

A harness with a morning briefing or a poll usually needs to outlive that the week it
starts working. `bin/ops-kit.py` renders a launcher and a watchdog for that, into the
harness's own `scripts/`. It writes files and prints commands. It never
loads, enables or starts anything.

```bash
uv run python bin/ops-kit.py                        # macOS: launchd, Linux: systemd user timer,
                                                    # Windows: a Task Scheduler task
uv run python bin/ops-kit.py --notify --prefix OBS  # also post restarts to the Slack channel
uv run python bin/ops-kit.py --keep-days 30         # delete rolled console logs after 30 days
```

It renders for the machine it runs on. A profile for another OS is refused, since the
scripts carry this machine's paths. On Windows the scripts are PowerShell (5.1, as
shipped with Windows; nothing to install) and are described under [Windows](#windows).

Before the first `up`, stop any foreground `uv run python harness.py` or
`.venv/bin/python harness.py` in its original terminal. A relative `harness.py` launch
does not reveal its home in the process list. Ops refuses to start a second copy if a
relative launch belongs to this home or its home cannot be identified. Linux uses
`/proc/<pid>/cwd` and macOS uses `lsof` to ignore confirmed other homes; without access
to that information it refuses safely. It never stops an unidentified process. Windows
cannot query cwd through `Win32_Process`, so starting refuses while any relative
launch exists, including one in another home. `running` and `healthy` still inspect only
this home's absolute launch. Process-query errors return exit 4 on both platforms; the
watchdog skips actions it cannot check, backs off retries, and notifies once per failure
state until it changes or a healthy round clears it.

Use the canonical absolute path to `harness.py` for manual absolute launches. The
process matcher does not recognize arbitrary symbolic-link aliases to the same home;
such a launch can be missed and `up` or the watchdog can start a second copy. Stop an
alias-launched process in its original terminal before using ops. The POSIX matcher
recognizes macOS's `/private` and `/var` or `/tmp` spellings, but cannot enumerate other
aliases created after rendering. Resolving each process's script argument would require
reliably separating its interpreter and first argument; POSIX `pgrep` exposes flattened
command text, which is ambiguous when paths contain spaces. The current strict matcher
avoids treating a shell command mentioning `harness.py` as a process to stop. General
alias recognition is therefore deferred; use the resolved home path on every platform.

## What it writes

| File | What it does |
|---|---|
| `scripts/<name>-up` | `up` / `down` / `restart` / `status` / `running` / `healthy` / `attach` |
| `scripts/<name>-watchdog` | One round: roll the console logs, restart the harness if it died |
| `scripts/launchd/<label>.plist` | macOS: run the watchdog every 120s and at login |
| `scripts/systemd/<name>-watchdog.{service,timer}` | Linux: the same as a user timer |
| `scripts/<name>-up.ps1`, `scripts/<name>-watchdog.ps1` | Windows: the same two scripts |
| `scripts/<name>-ops-common.ps1` | Windows: shared timestamp, exception and lock helpers, dot-sourced by both scripts |
| `scripts/windows/<name>-watchdog.xml` | Windows: a scheduled task running the watchdog every 2 minutes from logon |

The paths inside are resolved once, on the machine that rendered them. After moving the
harness or changing its venv, render again with `--force`.

## What each one gets right

Each of these was a bug in a hand-built launcher first.

- **One copy.** `up` does nothing when this harness is already running. It finds the
  process by a python interpreter whose first argument is this `harness.py`, not by the
  venv's path, because a macOS framework Python re-executes `.venv/bin/python` under
  another name. Checking and starting happen under one lock (`bin/ops-lock.py`, an
  `flock` the kernel releases when its holder exits), so six `up`s at once start one
  harness. `up`, `down`, `restart` and every watchdog round take the same lock. The
  script doing the work holds it on fd 9, not just the helper that took it, so killing
  the helper does not let a second `up` in halfway through; the harness and its console
  writer are started with fd 9 closed, so neither holds it while running.
- **The subscription login stays on.** `ANTHROPIC_API_KEY` is removed from the
  harness's environment, and so is every key `env/harness.env` declares. Under the
  launcher the file wins over a stale export in the shell that ran `up`.
- **The agent CLI is found.** launchd and systemd start with a bare `PATH`, where
  `claude` or `codex` is missing and every run fails. The login shell's `PATH`, with the
  CLI's directory first, is fixed into `<name>-up` when it is rendered. A CLI that cannot
  be found stops the render.
- **A crash loop backs off.** The watchdog restarts a harness that died, waits 30s before
  the next try, doubles that up to 30 minutes, and resets once a round finds it running.
  Every round's restart, failed restart or unreadable state goes to `logs/watchdog.log`.
  `--notify` also posts it to the Slack channel, one thread per incident: the first round
  that finds the harness down posts a new message, every later line about that outage
  is a reply in its thread, and the first round that finds it running again replies
  "recovered" and closes the incident (`logs/.watchdog/incident` holds the thread while
  it is open). The next outage starts a new message. A restart that worked is posted
  every time, so a crash loop shows up as a run of replies; a failure that repeats
  unchanged is posted once and logged every round. Only successful delivery marks a
  failure as posted; failed delivery is retried on the next backoff attempt. A failed
  recovery notification keeps the incident open: `logs/.watchdog/recovery-failures`
  records pending recovery retries, using the same exponential backoff without
  increasing the restart count or restarting a healthy harness. Successful recovery
  delivery clears the incident and retry state. If Slack says the thread is gone (its
  message was deleted), the line is posted as a new message, which becomes the thread.
  Any other failure, a timeout included, is retried in the same thread, so delivery is
  at least once: a line Slack took but whose answer was lost can appear twice. `--notify` is refused unless `env/harness.env` already holds the bot
  token and the channel.
- **A deliberate stop stays stopped.** `down` leaves a marker
  (`logs/.watchdog/down`) the watchdog respects until the next `up`. Before it existed, a
  harness stopped on purpose came back within two minutes. The watchdog reads the marker
  under the lock and never removes it; only an explicit `up` does, and it does so even
  when the harness is still running after a `down` that timed out.
- **What the watchdog starts outlives the round.** The plist sets `AbandonProcessGroup`
  and the systemd unit `KillMode=process`; without them launchd or systemd kills the
  restarted harness as soon as the round that started it exits.
- **A laptop stays awake** (macOS) while the harness runs, with `caffeinate -is -w <pid>`.

## Logs

| File | Written by | Rolled by |
|---|---|---|
| `logs/harness.jsonl` | the harness, one line per run | the harness |
| `logs/console.out` | `bin/ops-log.py`, fed the harness's stdout and stderr | `bin/ops-log.py` |
| `logs/watchdog.log` | the watchdog, under the lock | the watchdog, at the start of a round |

Each log line from `Harness.log` starts with an ISO time and its UTC offset
(`2026-10-14T09:00:00+09:00`). `ops-log.py` stamps the lines that lack one (a traceback,
a library's own print) and leaves the rest alone, so no line carries two. Every line of
`logs/watchdog.log` starts the same way, including what `<name>-up` printed during the
round, and so does the `down` marker. Each `logs/harness.jsonl` entry keeps its
epoch-seconds `ts` for compatibility and also carries `time`, the same moment in ISO 8601
with the local UTC offset.

Rolling is by date. The first write of a day moves what came before to
`<stem>.<YYYY-MM-DD><ext>.gz`, so "the log from the 13th" is found by name. Past 10MB
within one day the file is rolled early to `<stem>.<YYYY-MM-DD>-<HHMMSS><ext>.gz`.

Every log is rolled by the process that writes it, by renaming the file between two
writes. Rolling from outside with copy-and-truncate, even of an `O_APPEND` file, loses
the lines appended after the copy reached the end and before the truncate: they are in
neither the copy nor the file. The .gz is written under a temp name and the plain file
removed last, so a roll cut short leaves the lines uncompressed, and the next write
finishes it.

Nothing rolled is deleted by default, so `logs/` grows by about one compressed day per
day. To bound it, set `h.run_log_keep_days = 30` in `harness.py` for `harness.jsonl`, and
render with `--keep-days 30` for the console logs.

Writer warnings use `ops-log.py --err-file logs/ops-log.err`: each warning opens and
closes the file under the log lock and rolls it at 1 MiB. The watchdog does
not rename a file held open as stderr. Unexpected writer tracebacks go to
`logs/ops-log.crash` on both platforms.

## The console writer

The harness's stdout and stderr go through a pipe to `bin/ops-log.py`, which is the only
process writing `logs/console.out`. It holds an `flock` on `logs/.console.out.writer`
while it runs, so a second writer exits at once. What happens when it fails:

| Failure | What happens | Who notices |
|---|---|---|
| A line cannot be written (disk full, file made read-only) | The writer drops it and keeps reading, so the harness never gets EPIPE. The count goes to `logs/.console.out.dropping`; bounded warnings go to `logs/ops-log.err`. When writing works again, the log gets one line saying how many were lost. | `status` shows `DROPPING`. The watchdog notes it once per episode, with `--notify` in Slack too. It does not restart: that would not free the disk. |
| The writer dies | The harness keeps running until its next line of output, which fails with EPIPE and kills it at a moment nobody chose. | `status` shows `UNHEALTHY` and `healthy` exits 1. The next watchdog round restarts the harness with a fresh writer and says so (`--notify`: in Slack). |
| The writer does not exit after the harness stops | Something the harness started still holds its output open. | `down`, `restart` and `up` wait `WRITER_WAIT_S` (30s) for it, then fail loudly and name it. Nothing is started beside it. |

When a full disk cannot take the dropping marker or the warning log either, the writer
falls back to stderr (`logs/ops-log.crash`), which may be on the same disk.

## Stopping it

| To | Run |
|---|---|
| Stop it for a while | `scripts/<name>-up down`. The watchdog leaves it down until `up`. |
| Take it down for good | Remove the watchdog first, then `down`. On macOS `launchctl bootout gui/$(id -u)/<label>`, on Linux `systemctl --user disable --now <name>-watchdog.timer`, on Windows `schtasks /Delete /TN the-world-<name>-watchdog /F`. |

`down` sends SIGTERM and waits up to 5 minutes for runs already started to finish, then
up to 30 seconds for the console writer to drain. If either is still going it says so
and leaves the harness marked down. On Windows `down` does not wait for runs; see below.

## Windows

```powershell
uv run python bin/ops-kit.py
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\<name>-up.ps1 status
schtasks /Create /TN the-world-<name>-watchdog /XML scripts\windows\<name>-watchdog.xml
```

The commands, the down marker, the backoff, the logs and the order for taking it down
are the ones above. What differs, and why:

- **The lock is held by the script itself.** A Windows process cannot hand a lock to its
  child, so `bin/ops-lock.py` is not used. `<name>-up.ps1` and `<name>-watchdog.ps1`
  open `logs\.watchdog\lock` allowing no sharing, and the OS frees it when that
  PowerShell process ends, however it ends. The watchdog runs `<name>-up.ps1` in its own
  process, so a round is one lock holder from start to end. The handle is not
  inheritable, so the harness never holds it.
- **The harness is the console writer's child.** PowerShell 5.1 cannot pipe one
  background process into another, so `bin/ops-log.py --detach` starts the writer with
  no window and in its own process group, and the writer starts the harness and reads
  its output. The rules are unchanged: one writer per file, it exits on EOF, and a
  harness whose writer died fails at its next line. The writer also leaves the job
  object of the scheduled task that started it, where Windows allows that, so a round
  ending does not take the harness with it.
- **One copy** is found from each process's command line (`Get-CimInstance
  Win32_Process`): a python interpreter whose first argument is this `harness.py`. A
  venv's `python.exe` starts the real interpreter as its child with the same arguments,
  and that pair counts as one harness.
- **`down` does not drain.** Windows has no SIGTERM for a Python process and the
  harness hooks no signal there, so `down` ends it with `taskkill /T /F`, together with
  what it started. A run in progress ends without a record in `logs/harness.jsonl`.
  Stop it between runs when that matters.
- **The agent CLI is pinned the same way.** Windows has no login shell to ask, and a
  scheduled task starts with the user's own environment, so the render takes the
  current `PATH` and puts the directory of the `claude` (or `codex`) it finds first.
  Render from a terminal opened after the CLI was installed.
- **The task is not registered for you.** It runs every 2 minutes from the user's logon
  and from the time it was rendered, only while that user is logged on, with the user's
  own rights and no stored password. Its action is the venv's `pythonw.exe` starting
  PowerShell without a window, so no console flashes up every round.
- **Following the log holds up its roll.** Windows cannot rename a file another process
  has open. While `<name>-up.ps1 attach` (or any `Get-Content -Wait`) follows
  `logs\console.out`, a roll that falls due fails, warns once in the bounded
  `logs\ops-log.err`, and retries after backoff. Nothing is lost; the console file can
  grow past 10MB until the follower stops and a later retry succeeds.
- **No sleep prevention.** There is no `caffeinate`; set the power plan so the machine
  does not sleep while it should be running.
