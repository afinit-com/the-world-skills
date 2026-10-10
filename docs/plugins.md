# Codex · Claude Code · Cursor plugin

*Read this in [한국어](plugins.ko.md).*

The three apps use the same three `skills/` and the same bundled harness. No app gets its
own copy of the skills or its own harness. The `npx skills` install is still supported,
but in any one app use **either the skills install or the plugin install, not both**.

| Use | Supported in |
|---|---|
| Read the plugin and build a harness | Codex, Claude Code, Cursor |
| Run real agents inside the Slack bot | Claude Code or Codex |
| World map and tool lessons | All three; the output is standalone HTML |
| Limit what a running agent may write | The chosen runtime's hooks and scoped tools |

Picking a Claude model in Cursor does not install or sign in to the Claude Code runtime.
Nor does this plugin claim that the Claude/Codex hooks protect Cursor's own editing
session. `Agent.allow_plugins` (+ `PluginMarketplace`) is a **separate feature that lets a
running agent load outside plugins**; it is still Claude-only and full-trust, and it is not
the plugin this document describes.

## Install

The repository root is the marketplace (`.claude-plugin/marketplace.json`,
`.agents/plugins/marketplace.json`, `.cursor-plugin/marketplace.json`, name `the-world`,
plugin location `./`). The three files are generated from `plugin.json` with
`python3 scripts/build-plugin.py --sync`; do not edit them by hand. Install straight from
the GitHub address — no clone needed.

### Claude Code

```bash
claude plugin marketplace add afinit-com/the-world-skills
claude plugin install the-world-skills@the-world
```

In a new session, invoke `/the-world-skills:the-world-start`.

### Codex

```bash
codex plugin marketplace add afinit-com/the-world-skills
codex plugin add the-world-skills@the-world
```

In a new session, type `$the-world-start` followed by what you want done.

### Cursor

Cursor has no command to add a GitHub marketplace.

- **Team:** Dashboard → Plugins & MCPs → Team Marketplaces → Add Marketplace →
  **Import from Repo**, and paste `https://github.com/afinit-com/the-world-skills`.
- **Personal:** put it in the local plugin folder.

  ```bash
  mkdir -p ~/.cursor/plugins/local
  git clone https://github.com/afinit-com/the-world-skills ~/.cursor/plugins/local/the-world-skills
  ```

  Check the three skills under **Developer: Reload Window → Customize**.

Registering, installing, and enabling from the root marketplace is verified for Claude Code
and Codex in isolated config directories (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`). What gets
installed is the whole repository (`./`). Cursor's team import is not verified.

## Local build for development

Use this to install or try a trimmed copy from a checkout. The build itself does not
change personal settings, sign in, run a bot, or publish anything. The local build's
marketplace is named `the-world-local`, so it can be registered next to `the-world` above
without clashing.

### Codex (local build)

```bash
python3 scripts/build-plugin.py dist/the-world-marketplace
codex plugin marketplace add ./dist/the-world-marketplace
codex plugin add the-world-skills@the-world-local
```

In a new session, pick `the-world-start` from the skill list or type `$the-world-start`
followed by what you want done. The CLI command is `plugin add`. The build never
overwrites an existing path, so use a new output path when you rebuild.

### Claude Code (local build)

During development you can load the current repository directly, with no registration.

```bash
claude --plugin-dir "$PWD"
```

Invoke `/the-world-skills:the-world-start` or `/the-world-skills:the-world-util`.
For a lasting install, register the marketplace built above.

```bash
claude plugin marketplace add ./dist/the-world-marketplace
claude plugin install the-world-skills@the-world-local
```

### Cursor (local build)

Link the repository root as a local plugin. If something with the same name is already
there, check that install instead of overwriting it.

```bash
mkdir -p ~/.cursor/plugins/local
ln -s "$PWD" ~/.cursor/plugins/local/the-world-skills
```

In Cursor, open **Developer: Reload Window → Customize** and check the three skills, then
pick `the-world-start` from the `/` list in chat. If the policy allowing local plugins is
off, or a marketplace install of the same name exists, the local copy may not load.
`cursor --install-extension` is for VS Code extensions; it is not how this plugin installs.

## Package and project boundary

The Cursor layout follows the single-plugin arrangement of the
[official plugin-template](https://github.com/cursor/plugin-template). The build output
follows the template's marketplace arrangement, so the official
`scripts/validate-template.mjs` can be run against the build output directory. The
template's sample logo and MIT notice are not copied. The license is MIT; see
[LICENSE](../LICENSE).

- Root `plugin.json`: the common Agent Plugins format.
- `.codex-plugin/plugin.json`, `.claude-plugin/plugin.json`, `.cursor-plugin/plugin.json`:
  per-app manifests. All use the same name, version, and `skills/`. No two of the four
  files are identical (the root has no `skills`, Codex adds `interface`, Cursor adds
  `displayName` and `keywords`), so they cannot be merged into one file or a symlink.
  Instead, only the six shared fields (`name` `version` `description` `author`
  `repository` `license`) are kept in step:

  ```bash
  python3 scripts/build-plugin.py --sync
  ```

  Run it after editing the root `plugin.json`: it overwrites those six fields in the three
  app manifests and leaves app-only fields and key order alone. If they already match, it
  touches nothing. The build never quietly edits source files, so `--sync` is a standalone
  action that takes no destination, and building while they disagree fails with a message
  telling you to run `--sync`.
- Build output: a complete copy in `plugins/the-world-skills/` plus marketplace listings
  for the three apps. Moved outside the source repository, it can still find the install
  scripts and the world-map assets.
- The bundled `.claude/hooks` and `.codex/hooks` are **install material**. The plugin
  itself registers no global hooks and no MCP servers.
- A generated project's word commands go to `.cursor/skills/` with
  `bin/scaffold-world-skill.py --host cursor`. `--host all` writes the same content to all
  three apps' paths. This is separate from the runtime choice
  `--provider claude|codex|both`, and the existing commands keep working.
- Codex's default sandbox (`workspace-write`) blocks writes to `.codex/` and `.agents/`.
  `install-kit.py` and `scaffold-world-skill.py` check every destination before writing,
  and if one is blocked they write nothing and fail with `cannot write to ...`. Ask the
  user to approve writing that path (escalation) and run again. Do not copy the rest by
  hand; if it is not approved, report that the guard hooks and word commands were not
  installed.

## Verification and distribution

The package tests check that the manifests agree, that a moved package installs a harness,
that word commands are generated for all three hosts, and that existing edits are
preserved. Claude's native validation and Codex plugin validation can be run separately.
**Static checks are not the same as signing in, installing through each app's GUI, or
running in Slack.** Whether Cursor actually loads it is checked in Customize.

Test counts change, so run them yourself:

```bash
python3 -m unittest discover -s tests
cd skills/the-world-start/assets/kit && uv run --extra dev pytest -q
```

The common Agent Plugins JSON Schema and Claude Code manifest validation pass, as does
Cursor's official `validate-template.mjs` on the build output; its "no hooks / no MCP"
warning is the intended skills-only setup. Installing and invoking through the three
apps' GUIs, and Cursor's team import, are not verified.

`start` stays explicit-invocation only: Claude/Cursor get
`disable-model-invocation: true`, and Codex gets `policy.allow_implicit_invocation: false`
in `agents/openai.yaml`. The Codex plugin-creator validation helper rejects
`disable-model-invocation: true` in frontmatter, so this plugin does not claim a full pass
of that helper, and keeps the explicit-invocation policy rather than dropping it to pass.
See [Codex invocation policy](https://learn.chatgpt.com/docs/build-skills) and
[Cursor skills](https://cursor.com/docs/skills).

`the-world-start` also proposes work: given only a project path, it reads that project's
Claude Code and Codex session records and proposes jobs with the evidence attached. It does
not read Cursor records. `the-world-audit` only reads a harness to check it for risks, so
it has no explicit-invocation policy. All three skills are exposed to the three apps under
the same `skills/` path, and `scan.py` uses only the standard library.

The repository is itself a marketplace; that does not mean it is listed in any app's
official marketplace. The license is MIT and is reflected in the manifests and the
distributed package.

Official specs:
[OpenAI packaging](https://developers.openai.com/plugins/build/plugins),
[Claude plugins](https://code.claude.com/docs/en/plugins-reference),
[Claude marketplaces](https://code.claude.com/docs/en/plugin-marketplaces),
[Cursor plugin spec](https://cursor.com/docs/reference/plugins),
[Cursor local install](https://cursor.com/docs/plugins#test-plugins-locally).
