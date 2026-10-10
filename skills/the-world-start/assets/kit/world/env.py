"""Load the harness's one env file, <harness>/env/harness.env.

The loader follows these rules:

  Never override — an operator who exported something in the launching shell meant
  it. The file is a default, not an authority. Because these names carry no harness
  prefix, that deference is also a hazard (a stray `SLACK_BOT_TOKEN` outranks the
  harness's own), so an override is reported instead of applied silently.

  Provider authentication policy does not live here. In particular, the Claude adapter
  refuses ANTHROPIC_API_KEY to preserve subscription-login behavior, while a Codex-only
  process must not inherit an unrelated Claude restriction from the common env loader.

  Missing file is not an error. A harness with no Slack tokens degrades to
  poll-only by design, so a missing harness.env looks identical to "Slack intentionally
  disabled" — which is why Harness logs which path it loaded, or that it loaded
  none. Never let this fail silently AND invisibly; one or the other.

The path is resolved relative to this module, not to $HOME: `world/` sits at the root
of the harness checkout, so `parents[1]` IS that checkout, wherever it happens to sit.
This holds for an editable install, which is the only supported way a harness gets
world. A non-editable wheel would land at `site-packages/world/`, whose `parents[1]` is
not a harness checkout — the file simply won't be found, `load_harness_env()` returns None,
and the harness logs `env=none`. That is the intended failure: visible, not silent.
$WORLD_ENV_FILE overrides for tests and one-off runs.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

# world/env.py -> world/ -> harness root.  (Was parents[2] under the old src/world layout.)
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = REPO_ROOT / "env" / "harness.env"
# What this loader itself put into os.environ, and which keys it has already
# reported as shadowed. See load_harness_env() — both exist to keep the shadow warning
# from firing on our own earlier call.
_LOADED: dict[str, str] = {}
_WARNED: set[str] = set()
# Every key the env file declares, whether or not this loader was the one that set it
# (a shadowed key is still a harness secret). child_env() strips all of them.
_FILE_KEYS: set[str] = set()
_PERM_WARNED: set[str] = set()


def env_path() -> pathlib.Path:
    override = os.environ.get("WORLD_ENV_FILE")
    return pathlib.Path(override) if override else DEFAULT_ENV_PATH


_INLINE_COMMENT = re.compile(r"\s+#.*$")


def parse_value(raw: str) -> str:
    """One env-file value: trailing comment removed, surrounding quotes dropped.

    A `#` only starts a comment when whitespace precedes it, which is the dotenv
    convention and the only rule that lets a value legitimately contain one (a URL
    fragment, a channel name). A quoted value is taken whole — quote it when the value
    really does end in ` #something`.

    Inline comments must be stripped here. Otherwise comments become part of IDs or
    tokens and silently break rendering and equality checks downstream.
    """
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return _INLINE_COMMENT.sub("", value).strip()


def load_harness_env() -> pathlib.Path | None:
    """Populate os.environ from the harness env file. Returns the path it read, or
    None if there was nothing to read. Idempotent — a second call is a no-op.

    An already-exported shell variable WINS over the file. That is deliberate (it is
    how one-off overrides work — the file is a default, not an authority), but it
    is easy to trip over when these names carry no harness prefix: a stray
    `SLACK_BOT_TOKEN` left in a shell silently outranks the harness's own.
    Silent is the part that hurts — a harness reading someone else's token looks
    exactly like a harness reading its own. So say it, on the stderr the caller is
    already watching, naming the file (with no prefix, the key alone doesn't tell you
    which config it lost to).

    _LOADED is what keeps that honest. This function runs at least twice per startup
    (`import world`, then every `Harness(...)`), so "key already in os.environ" does NOT
    mean "shadowed" — the first call is usually the one that put it there. Compare
    against what WE set: same value, we set it, stay quiet. Anything else is a real
    outside value. Without this the warning fires on every key of every harness
    startup, which is worse than not warning at all: a wall of false positives is how
    you teach an operator to ignore the one true one. _WARNED keeps it to once per key
    per process for the same reason.
    """
    path = env_path()
    if not path.exists():
        return None
    _warn_if_readable(path)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = parse_value(value)
        _FILE_KEYS.add(key)
        if key not in os.environ:
            os.environ[key] = value
            _LOADED[key] = value
            continue
        if _LOADED.get(key) == os.environ[key]:
            continue  # our own earlier call — idempotent, nothing to report
        if key not in _WARNED:
            _WARNED.add(key)
            print(
                f"[world] {key} in {path} shadowed by the environment — using the "
                f"exported value. `unset {key}` to use the file's.",
                file=sys.stderr,
            )
    return path


def _warn_if_readable(path: pathlib.Path) -> None:
    """One line on stderr when the token file is readable by group or other. Names the
    file and the fix, never a value. Once per path per process."""
    if os.name == "nt":
        try:
            import win32api
            import win32con
            import win32security
            token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
            try:
                sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
            finally:
                token.Close()
            descriptor = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
            acl = descriptor.GetSecurityDescriptorDacl()
            readable = acl is None or any(
                acl.GetAce(i)[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE
                and acl.GetAce(i)[2] != sid
                and acl.GetAce(i)[1] & (0x1 | 0x80000000 | 0x10000000)
                for i in range(acl.GetAceCount())
            )
            message = ("has Windows ACL entries letting other accounts read its tokens — in PowerShell: "
                       f'icacls "{path}" /inheritance:r /grant:r "${{env:USERNAME}}:F"')
        except Exception:
            readable = True
            message = "has token-file Windows permissions that could not be verified."
        if readable and str(path) not in _PERM_WARNED:
            _PERM_WARNED.add(str(path))
            print(f"[world] {path} {message}", file=sys.stderr)
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077 and str(path) not in _PERM_WARNED:
        _PERM_WARNED.add(str(path))
        print(
            f"[world] {path} is readable by other users (mode {mode & 0o777:o}) and holds "
            f"tokens — fix with `chmod 600 env/harness.env`.",
            file=sys.stderr,
        )


# ── agent subprocess environment ─────────────────────────────────────────────
# An agent's CLI (claude / codex) — and whatever it runs: Bash, plugin MCP servers —
# must not see the harness's own secrets. The harness process keeps them in os.environ
# because its in-process tools (mention_tools, integration recipes) read them there;
# the child gets an explicit, allowlisted copy instead.

_BASE_KEYS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "TZ", "LANG",
    "LANGUAGE", "COLORTERM", "NO_COLOR",
    # corporate proxies / private CAs: without these a CLI cannot reach its API
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
    "TEMP", "TMP",
    # Windows: a process cannot start or find its profile without these
    "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA",
    "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "HOMEDRIVE",
    "HOMEPATH", "USERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
})
_BASE_PREFIXES = ("LC_", "XDG_", "WORLD_")
# Per provider, what that CLI needs to find its login. Claude: its config dir
# (CLAUDE_CONFIG_DIR; the macOS keychain via HOME/USER) or CLAUDE_CODE_OAUTH_TOKEN.
# Codex: $CODEX_HOME/auth.json (`codex login`), or an OPENAI_API_KEY / CODEX_API_KEY.
_PROVIDER_PREFIXES = {"claude": ("CLAUDE_", "ANTHROPIC_"), "codex": ("CODEX_",)}
# Claude through a cloud provider logs in with that cloud's credentials. They are handed
# down only when the operator switched Claude to that cloud, and then they count as the
# provider's own (so AWS_SECRET_ACCESS_KEY survives the *_KEY rule).
_CLOUD_PREFIXES = {
    "CLAUDE_CODE_USE_BEDROCK": ("AWS_",),
    "CLAUDE_CODE_USE_VERTEX": ("GOOGLE_", "GCLOUD_", "CLOUDSDK_", "CLOUD_ML_", "VERTEX_"),
}
_PROVIDER_KEYS = {"claude": frozenset(), "codex": frozenset({"OPENAI_API_KEY", "OPENAI_BASE_URL"})}
# Login variables that stay even when harness.env is where the operator put them.
_PROVIDER_LOGIN = frozenset({
    "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY",
})
# Never handed down, whatever the prefix. The Claude runner refuses ANTHROPIC_API_KEY
# (subscription billing), so no child needs it either.
_NEVER = frozenset({"ANTHROPIC_API_KEY", "RELAY_SECRET", "GITHUB_TOKEN", "GH_TOKEN"})
_SECRET_SUFFIXES = ("_TOKEN", "_SECRET", "_KEY", "_PASSWORD")
_SECRET_PREFIXES = ("SLACK_", "DISCORD_", "LINEAR_", "JIRA_")


def child_env(
    provider: str,
    extra: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    """The complete environment for a `provider` ("claude" | "codex") agent subprocess.

    Allowlist first (what a CLI needs to run and log in), then a denylist on top: every
    key harness.env declares, and anything named like a token/secret/key/password or
    belonging to a chat or tracker integration. The provider's own login variables
    (CLAUDE_CODE_OAUTH_TOKEN, CODEX_API_KEY, ...) survive the name-based rule — the CLI
    cannot authenticate without them — but ANTHROPIC_API_KEY never does. `extra` (the
    WORLD_* values a runner sets for this run) is applied last.
    """
    source = os.environ if environ is None else environ
    provider_prefixes = _PROVIDER_PREFIXES.get(provider, ())
    if provider == "claude":
        for switch, prefixes in _CLOUD_PREFIXES.items():
            if source.get(switch, "").strip().lower() not in ("", "0", "false"):
                provider_prefixes += prefixes
    provider_keys = _PROVIDER_KEYS.get(provider, frozenset())
    env: dict[str, str] = {}
    for key, value in source.items():
        provider_owned = key in provider_keys or key.startswith(provider_prefixes)
        if not (key in _BASE_KEYS or key.startswith(_BASE_PREFIXES) or provider_owned):
            continue
        if key in _NEVER:
            continue
        if key in _FILE_KEYS and key not in _PROVIDER_LOGIN:
            continue
        if not provider_owned and (
            key.endswith(_SECRET_SUFFIXES) or key.startswith(_SECRET_PREFIXES)
        ):
            continue
        env[key] = value
    env.update(extra or {})
    return env


def sdk_env(provider: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """child_env() for an SDK that MERGES its `env` option onto os.environ instead of
    replacing it (claude_agent_sdk does: `{**os.environ, **options.env}`). Every
    os.environ key child_env() leaves out is overridden with an empty string, so the
    child sees it unset-equivalent rather than inheriting the value. Kept keys are NOT
    repeated: they arrive through the merge, which leaves the SDK's own values (it sets
    CLAUDE_CODE_ENTRYPOINT between the inherited env and options.env) in charge."""
    keep = child_env(provider, extra)
    # CLAUDECODE: the SDK already drops it, and an empty value must not reintroduce it.
    blanks = {key: "" for key in os.environ if key not in keep and key != "CLAUDECODE"}
    return {**blanks, **(extra or {})}


_TOKEN_SHAPES = re.compile(
    r"\b(?:xox[abposr]|xapp)-[A-Za-z0-9-]+"       # Slack bot/user/app-level tokens
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"              # GitHub classic/OAuth/app tokens
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"            # GitHub fine-grained PATs
    r"|(?i:\bbearer)\s+[A-Za-z0-9._~+/=-]{8,}"    # Authorization: Bearer <anything>
)


def redact(text: str) -> str:
    """Mask token-shaped substrings before error text leaves the harness (a failed run's
    output posted to Slack/Discord). Keeps the first few characters so the kind of token
    stays recognizable; the secret part never ships."""
    return _TOKEN_SHAPES.sub(lambda m: m.group(0)[:5] + "…[redacted]", text)
