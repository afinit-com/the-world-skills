# Claude compatibility wrapper

This path remains for existing Claude commands. The kit no longer ships a step-by-step
flow for adding an agent. Read `WORLD.md` for the cast and vocabulary and `docs/philosophy.md`
for why each boundary exists, then keep what already holds for every agent here:
authority declared in code and narrow, `write_scope` never empty, a scoped tool rather
than a wider Bash allowlist, outward actions behind human approval, and a report that
says what is still unverified.
