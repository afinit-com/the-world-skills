# Running a harness all the time

The kit sets up no service. A harness runs while `harness.py` runs, and a reboot, a
closed terminal or a crash stops it until someone starts it again. That stays the
default: a service the user never asked for is a process they don't know to look for.

A harness with a morning briefing or a poll usually needs to outlive that the week it
starts working. `bin/ops-kit.py` renders a launcher and a watchdog for that, into the
harness's own `scripts/`. It writes files and prints commands. It never
loads, enables or starts anything.

```bash
uv run python bin/ops-kit.py                        # macOS: launchd, Linux: systemd user timer
uv run python bin/ops-kit.py --notify --prefix OBS  # also post restarts to the Slack channel
uv run python bin/ops-kit.py --keep-days 30         # delete rolled console logs after 30 days
```

Windows is not supported yet. On a `windows` profile the script says so and writes
nothing.

## What it writes

| File | What it does |
|---|---|
| `scripts/<name>-up` | `up` / `down` / `restart` / `status` / `running` / `healthy` / `attach` |
| `scripts/<name>-watchdog` | One round: roll the console logs, restart the harness if it died |
| `scripts/launchd/<label>.plist` | macOS: run the watchdog every 120s and at login |
| `scripts/systemd/<name>-watchdog.{service,timer}` | Linux: the same as a user timer |

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
  Each restart and failed restart goes to `logs/watchdog.log`, and with `--notify` to the
  Slack channel. `--notify` is refused unless `env/harness.env` already holds the bot
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
a library's own print) and leaves the rest alone, so no line carries two.

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

## The console writer

The harness's stdout and stderr go through a pipe to `bin/ops-log.py`, which is the only
process writing `logs/console.out`. It holds an `flock` on `logs/.console.out.writer`
while it runs, so a second writer exits at once. What happens when it fails:

| Failure | What happens | Who notices |
|---|---|---|
| A line cannot be written (disk full, file made read-only) | The writer drops it and keeps reading, so the harness never gets EPIPE. The count goes to `logs/.console.out.dropping` and stderr (`logs/ops-log.err`). When writing works again, the log gets one line saying how many were lost. | `status` shows `DROPPING`. The watchdog notes it once per episode, with `--notify` in Slack too. It does not restart: that would not free the disk. |
| The writer dies | The harness keeps running until its next line of output, which fails with EPIPE and kills it at a moment nobody chose. | `status` shows `UNHEALTHY` and `healthy` exits 1. The next watchdog round restarts the harness with a fresh writer and says so (`--notify`: in Slack). |
| The writer does not exit after the harness stops | Something the harness started still holds its output open. | `down`, `restart` and `up` wait `WRITER_WAIT_S` (30s) for it, then fail loudly and name it. Nothing is started beside it. |

When a full disk cannot take `logs/.console.out.dropping` either, only stderr has the
count, and that file may be on the same disk.

## Stopping it

| To | Run |
|---|---|
| Stop it for a while | `scripts/<name>-up down`. The watchdog leaves it down until `up`. |
| Take it down for good | Remove the watchdog first, then `down`. On macOS `launchctl bootout gui/$(id -u)/<label>`, on Linux `systemctl --user disable --now <name>-watchdog.timer`. |

`down` sends SIGTERM and waits up to 5 minutes for runs already started to finish, then
up to 30 seconds for the console writer to drain. If either is still going it says so
and leaves the harness marked down.
