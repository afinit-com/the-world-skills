# Claude compatibility wrapper

This path remains for existing Claude commands. Adding a tool is now equipping the cast,
and the flow for it is the tool word's: read `templates/skill-wrapper-tool.md.tmpl` and
follow it from "Split the request, place each piece, then build". Split the request into
pieces, place each one as a tool, code, prompt procedure, skill, knowledge, setting or
world sentence, propose the placement as a table and wait for the user's confirmation,
then build only what was confirmed.

Read `WORLD.md` for the cast and vocabulary and `docs/philosophy.md` for why each boundary
exists. Keep what already holds for every agent here: authority declared in code and
narrow, a scoped tool rather than a wider Bash allowlist, outward actions behind human
approval, and a report that says what is still unverified. A piece that only reads leaves
`write_scope` empty, which blocks writes everywhere; a piece that must write gets a narrow
`write_scope`, a reason for it, and a write tool bound to the same roots.

A tool word scaffolded before this flow still carries the old text, because
`bin/scaffold-world-skill.py` keeps a wrapper that exists. Move its `SKILL.md` aside,
re-run `bin/scaffold-world-skill.py <word> tool --universe ... --description ...`, and
carry over any edits the user made to the old one.
