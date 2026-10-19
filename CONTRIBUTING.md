# Contributing

Issues and pull requests are both welcome. **What I can't promise is a timely reply.**

One person maintains this, alongside other work. A PR may sit for weeks before I look at
it properly. That's not a verdict on your patch and it isn't me ignoring you — it's the
honest capacity of the project, and you deserve to know it before you spend a weekend
here rather than after.

Everything below exists to make that slowness cost you as little as possible.

## Open an issue first for anything non-trivial

This is the one request worth honoring. A typo, a broken link, an obviously wrong path —
just send the PR. But for a new flow, a change to the runtime in `world/`, a new template,
a different provider behavior, or anything touching the guard hooks: **describe it in an
issue before you write it.**

Not for permission. It's that I may already know the change conflicts with something
that isn't visible from the outside — a constraint seen in a running harness, a decision
recorded in [`docs/philosophy.md`](skills/start/assets/kit/docs/philosophy.md), a path another flow depends on. Finding that out in a two-line
issue reply costs you nothing. Finding it out after the patch is written is how
contributors end up bitter, and fairly so.

## What makes a PR easy to say yes to

- **One thing.** A patch doing three things waits for me to have time for the largest of
  them; a patch doing one thing gets reviewed and merged quickly.
- **It fixes `skills/start/assets/kit/`, not an installed harness.** The kit is
  the source; a harness is an unpacked copy that a re-run overwrites. A fix made in the
  wrong place looks correct and disappears on the next upgrade.
- **Tests pass.** Package tests from the repo root, kit tests from inside the kit:

  ```bash
  python3 -m unittest discover -s tests
  cd skills/start/assets/kit && python3 -m unittest discover -s tests
  ```

- **No new runtime dependency** unless the issue thread already agreed it's worth it. The
  kit installs into other people's machines, and every dependency is a thing their harness
  now has to carry.
- **It says what would have caught the bug.** A regression test, or a sentence explaining
  why one isn't practical here.

## What I will push back on, whoever sends it

These aren't bureaucracy — they're the point of the kit, and they're written up in
[`docs/philosophy.md`](skills/start/assets/kit/docs/philosophy.md) under the line the design hangs off: *a promise written in a prompt is
not enforcement.*

- **A second way to give an agent a capability.** There is exactly one: a provider-neutral
  `ToolSpec` whose boundary is bound at construction. External MCP servers, plugin
  surfaces, per-subagent tool sets — each one hands over a surface the harness didn't
  bound. Plugin loading (`Agent.allow_plugins` with a `PluginMarketplace`, Claude runner
  only) is the single documented full-trust exception and stays the only one.
- **Loosening a scope boundary for convenience.** An agent with no `write_scope` is
  write-blocked everywhere, spawned agents start with an empty base tool set, and a bot
  may only relay an HMAC-signed human decision, never originate one.
- **Making a degradation quiet.** Missing config, Slack off, absent cost — all of it
  degrades loudly. A missing field is omitted, never zeroed, because an unknown cost and a
  free call must not look alike.
- **Moving flow or runtime logic outside the harness.** A harness carries its own commands
  so it still works after you clone it somewhere else.

If your change genuinely needs one of these relaxed, that's an issue thread, not a PR.
The reasoning behind each is a real incident, and I'd rather explain it than re-litigate it
in a diff.

## If it goes quiet

Ping the thread. A nudge after a couple of weeks is welcome, not rude.

And if waiting isn't worth it — **fork it.** MIT means you don't need my agreement that
your idea is good. If your harness wants a different rule than mine, it should have one;
that's the correct outcome, not a failure. An issue telling me where you took it is
welcome too.

## Security

Don't open a public issue for a vulnerability — see [SECURITY.md](SECURITY.md).

---

## 한국어

이슈도 PR도 환영합니다. **다만 빠른 응답은 약속드릴 수 없습니다.** 혼자서 다른 일과
병행하며 관리하고 있어서 PR이 몇 주 열려 있을 수 있습니다. 패치에 대한 평가가 아니라
이 프로젝트의 솔직한 여력이고, 주말을 쓰시기 전에 아셔야 할 정보라 앞에 적습니다.

- **큰 변경은 이슈를 먼저 열어주세요.** 오타·깨진 링크는 그냥 PR 주셔도 됩니다. 하지만
  새 플로우, `world/` 런타임 수정, 가드 훅과 관련된 변경은 코드를 쓰기 전에 이슈로
  설명해 주세요. 허락을 받으라는 뜻이 아니라, 밖에서는 보이지 않는 제약(운영 중인 하네스에서
  드러난 제약, [`docs/philosophy.md`](skills/start/assets/kit/docs/philosophy.md)의 결정, 다른 플로우가 의존하는 경로)과 충돌하는 걸 두 줄
  답변으로 알 수 있기 때문입니다. 패치를 다 쓴 뒤에 알게 되면 서로 손해입니다.
- **머지하기 쉬운 PR** — 한 가지만 다루고, 설치된 하네스가 아니라
  `skills/start/assets/kit/`을 고치고(하네스는 재설치 때 덮어쓰이는 사본입니다),
  테스트가 통과하고(루트에서 `python3 -m unittest discover -s tests`, 키트 디렉터리에서 한
  번 더), 런타임 의존성을 새로 늘리지 않고, 무엇이 이 버그를 잡아줬을지를 밝힌 것.
- **누가 보내든 제동을 걸 변경** — 에이전트에게 능력을 주는 두 번째 경로를 만드는 것,
  편의를 위해 범위 경계를 푸는 것, 기능 저하를 조용하게 만드는 것, 플로우나 런타임
  로직을 하네스 바깥으로 옮기는 것. 각 항목은 실제 사고에서 나왔고
  [`docs/philosophy.md`](skills/start/assets/kit/docs/philosophy.md)에 근거가 있습니다. 정말 필요하다면 PR이 아니라 이슈에서
  이야기해 주세요.
- **조용해지면 스레드에 한 번 찔러주세요.** 2주쯤 뒤의 재촉은 무례하지 않습니다.
  기다릴 가치가 없다고 판단되면 **포크하셔도 됩니다.** MIT이고, 규칙이 달라야 한다면
  하네스가 달라지는 게 맞는 결말입니다.
- 보안 문제는 공개 이슈 대신 [SECURITY.md](SECURITY.md)를 따라주세요.
