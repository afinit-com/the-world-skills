# Running a harness all the time

The kit sets up no service. A harness runs while `harness.py` runs, and a reboot, a
closed terminal or a crash stops it until someone starts it again. That stays the
default: a service the user never asked for is a process they don't know to look for.

A harness with a morning briefing or a poll usually needs to outlive that the week it
starts working. `bin/ops-kit.py` renders what two harnesses had each built by hand for
that, into the harness's own `scripts/`. It writes files and prints commands. It never
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
| `scripts/<name>-up` | `up` / `down` / `restart` / `status` / `running` / `attach` |
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
  another name.
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
  harness stopped on purpose came back within two minutes.
- **A laptop stays awake** (macOS) while the harness runs, with `caffeinate -is -w <pid>`.

## Logs

| File | Written by | Rolled by |
|---|---|---|
| `logs/harness.jsonl` | the harness, one line per run | the harness |
| `logs/console.out` | `<name>-up` (the harness's stdout and stderr) | the watchdog |
| `logs/watchdog.log` | the watchdog | the watchdog |

Each log line from `Harness.log` starts with an ISO time and its UTC offset
(`2026-10-14T09:00:00+09:00`), so `console.out` needs no second timestamp.

Rolling is by date. The first write of a day (the first watchdog round of a day, for the
console logs) moves what came before to `<stem>.<YYYY-MM-DD><ext>.gz`, so "the log from
the 13th" is found by name. Past 10MB within one day the file is rolled early to
`<stem>.<YYYY-MM-DD>-<HHMMSS><ext>.gz`. The console logs are rolled with copy-and-truncate,
which their `O_APPEND` writers survive without a restart.

Nothing rolled is deleted by default, so `logs/` grows by about one compressed day per
day. To bound it, set `h.run_log_keep_days = 30` in `harness.py` for `harness.jsonl`, and
render with `--keep-days 30` for the console logs.

## Stopping it

| To | Run |
|---|---|
| Stop it for a while | `scripts/<name>-up down`. The watchdog leaves it down until `up`. |
| Take it down for good | Remove the watchdog first, then `down`. On macOS `launchctl bootout gui/$(id -u)/<label>`, on Linux `systemctl --user disable --now <name>-watchdog.timer`. |

`down` sends SIGTERM and waits up to 5 minutes for runs already started to finish. If one
is still going it says so and leaves the harness marked down.
