# 하네스 가이드

*[English](harness-guide.md) · [README](../README.ko.md)로 돌아가기*

하네스를 운영하는 사람을 위한 문서입니다. 설치 스크립트가 무엇을 어디에 두는지, 작업을
잃지 않고 업그레이드하는 방법, 런타임이 지키는 규칙을 담았습니다. 시작만 하려면
[README](../README.ko.md)로 충분합니다.

## 런타임과 명령어가 하네스에 도달하는 방식

`npx skills`는 저장소가 아니라 스킬 디렉터리를 설치합니다. 그래서 런타임이 프로젝트에
"이미 있는" 상태가 될 수 없고, 런타임은 `start` 안에 실려 갑니다.

```
skills/start/
  SKILL.md
  scripts/install-kit.py     키트를 대상 디렉터리에 멱등하게 풀어놓음
  assets/kit/
    world/                   런타임 (pyproject.toml 외 의존성 없음)
    templates/               플로우가 채워 넣는 코드 생성 템플릿
    .claude/the-world/       Claude 전용 agent · tool · verify · plugins 호환 스텁
    .claude/hooks/           scope · git · integrity · durable-config 가드
    .claude/settings.json    훅 등록 (병합하며, 덮어쓰지 않음)
    .codex/hooks/            Codex apply_patch/Bash 경계 어댑터
    .codex/hooks.json        Codex 훅 등록 (병합하며, 덮어쓰지 않음)
    env/harness.env.example  pyproject.toml  bin/smoke.py  bin/codex-spike.py
    bin/scaffold-world-skill.py
    bin/world-map-demo.py    캐릭터 여섯 명짜리 학습 예제
    bin/slack-provision.py   Slack CLI로 하네스의 Slack 앱 생성
    bin/slack-pair.py        @멘션 한 번으로 채널·승인자 ID 받기
    docs/world-map.md        초상화 · 관계 · tool 맵 가이드
    docs/integrations.md     GitHub · Linear · Jira 연동 레시피 (폴링 check + 승인 게이트 쓰기)
skills/audit/
  SKILL.md                   감사가 지킬 것과 보고 형식
  scan.py                    읽기 전용 근거 수집기 (하네스를 파싱만 하고 import하지 않음)
skills/util/
  SKILL.md                   서브커맨드 라우터
  references/slack-icon.md
skills/help-my-world/
  SKILL.md                   초안 → 승인 → 이 저장소에 이슈 등록
  collect_env.py             가린 환경 보고서 (표준 라이브러리만)
```

`/the-world:start`의 Phase 3 step 1이 설치 스크립트를 실행합니다. 하네스 디렉터리에
일어나는 일은 다음과 같습니다.

| | 파일 | 재실행 시 |
|---|---|---|
| 키트 소유 | `world/` `.claude/the-world/`(Claude) `templates/` 래퍼·매니페스트 선택한 프로바이더 훅 `bin/smoke.py` `bin/codex-spike.py` `bin/slack-provision.py` `bin/slack-pair.py` `docs/philosophy.md` | **교체됨** — 재실행은 업그레이드이며, 키트 소유 트리는 먼저 삭제됩니다 ([꼭 읽으세요](#upgrading)) |
| 하네스 소유 | `pyproject.toml` `env/harness.env.example` `.gitignore` | 없으면 생성, 있으면 유지 |
| 프로필 | `project-profile.json` (OS · 실행기 · 채널) | 플래그대로 다시 씀. 생략한 플래그는 기록된 값 유지 |
| 병합 | `.claude/settings.json` 및/또는 `.codex/hooks.json` | 가드 훅 추가, 기존 훅 유지 |
| 당신 것 | `harness.py` `WORLD.md` 있으면 `AGENTS.md` `abilities/` `tools/` `slack-app/` `env/harness.env` 프로바이더 스킬 래퍼 | 건드리지 않음 |

**결과적으로 하네스는 자급자족합니다.** Phase 3 step 7이 세계관 단어마다
`.claude/skills/<word>/SKILL.md`와 `.agents/skills/<word>/SKILL.md`를 만들고, 그 래퍼는 하네스 안의 `WORLD.md`와
`docs/philosophy.md`를 가리킵니다. 다른 머신에 클론해도 전역 설치 없이 `/recruit`가 그대로
동작합니다. 그 지침을 여기 별도 스킬로 두지 않고 키트 페이로드에 넣은 이유도
같습니다. 로직이 하네스 바깥에 있는 명령어는, 하네스가 옮겨지는 순간 깨지는 명령어입니다.

그러니 하네스 안의 `world/`나 `.claude/the-world/`를 고치지 마세요. 여기 `assets/kit/`을 고치고
설치 스크립트를 다시 실행하세요. 다음 절이 중요한 이유가 바로 이것입니다. 이 지침을
지킨 하네스는 안전하게 업그레이드되지만, 지키지 않은 하네스는 그 수정들을 조용히
잃습니다.

키트가 그 머신에서 아예 돌지 않는다면(네이티브 Windows 지원이 그렇게 시작됐습니다),
`world/`에서 최소한만 고치고, 바꾼 내용을 `world/` **바깥** 메모에 적고, 여기에 이슈를
여세요. `kit-source/` 체크아웃, `patches/` 디렉터리, 패치 내보내기 스크립트처럼 하네스 안에
두 번째 파이프라인을 만들지 마세요. 런타임 사본이 셋이 되어 셋을 함께 맞춰야 하고,
하네스를 연 사람은 어느 것이 진짜인지 알 수 없습니다.

<a id="upgrading"></a>
`npx skills update` 후 기존 하네스를 업그레이드하려면:

```bash
cd <harness> && git status --short          # 반드시 clean이어야 합니다 — 아래 참고
python3 <installed-skill>/scripts/install-kit.py .   # project-profile.json 유지. Windows에서는 `python`
uv pip install -e .
uv run python bin/smoke.py
git diff                                    # 설치 스크립트가 출력하지 않는 유일한 보고서
```

> **재실행은 파괴적이며, 무엇을 파괴했는지 말해주지 않습니다.** 키트 소유 트리는 복사
> 전에 삭제되므로, `world/` · `.claude/the-world/` 안에 당신이 추가한 파일은 이름조차
> 남기지 않고 사라지고, 직접 수정한 키트 파일도 같은 방식으로 덮어써집니다. 로그는
> 어느 쪽이든 `replaced world/`라고만 적습니다.
>
> 이는 의도된 계약입니다. 런타임은 하나이고 절대 포크하지 않는다는 것이 요점이니까요.
> 하지만 그래서 재실행은 그 트리들을 건드리지 않은 하네스에서만 안전하고, 오래 살아남은
> 하네스는 대개 그렇지 않습니다. 한 번은 한 하네스의 `world/`에서 로컬 수정 네
> 건과 **그 수정들을 적어둔 문서**까지 재실행이 날려버렸습니다. 그 문서도 `world/` 안에
> 있었기 때문입니다. 되돌릴 수 있었던 건 사전에 트리가 깨끗해서 `git checkout`이
> 통했기 때문입니다.
>
> 그러니 먼저 커밋하거나 stash하고, 그다음 `git diff`를 읽으세요. 그 diff가 업그레이드가
> 무엇을 가져갔는지 알려주는 유일한 보고서입니다. 그리고 하네스가 의도적으로 자기
> `world/`를 소유한다면, 그 메모는 위 트리 **바깥**(`docs/`나 `harness.py` 맨 위)에
> 두세요. 아니면 업그레이드가 영토와 함께 지도까지 지웁니다.

### 프로필: OS · 실행기 · 채널

설치 스크립트는 세 가지를 선택받아 `project-profile.json`에 기록합니다.

| 플래그 | 값 | 바뀌는 것 |
|---|---|---|
| `--os` | `windows` `macos` `linux` (기본: 지금 머신) | Windows 도구 브리지 복사 여부, 가드 훅 인터프리터(Windows는 `python`, 그 외 `python3`), 안내하는 검증 명령 |
| `--provider` | `claude` `codex` `both` | 복사할 실행기·훅·문서, `Harness()`의 기본 provider |
| `--channel` | `slack` `discord` `both` `none` | Discord 게이트웨이 복사 여부. `none`은 poll·schedule만 |

프로필이 고른 것만 복사합니다. 그래서 macOS 하네스에는 Windows 파이프 코드가 없고, Windows · Codex · Slack 하네스에는 넘겨 읽어야 할
Claude 실행기, `.claude/` 훅, Discord 코드가 없습니다. `world/profile.py`가 실행 시 같은 파일을
읽어서, 이런 하네스에 `provider="claude"`나 `DiscordConfig`를 주면 나중에 ImportError로
터지지 않고 생성 시점에 재설치 플래그를 담은 오류로 실패합니다. 플래그 없이 다시
실행하면 기록된 프로필을 유지합니다. 플래그를 주면 프로필이 바뀌고, 새 프로필에서
빠지는 파일은 지운 뒤 로그에 이름을 남깁니다. 프로필이 생기기 전에 설치한 하네스에는 이
파일이 없고 전부 유지합니다.

### 네이티브 Windows

Windows에서 WSL 없이 그대로 돌아갑니다. Codex의 실행별 도구 브리지는 다른 OS에서는
0600 unix socket이고, Windows에서는 named pipe입니다. 이 파이프의 DACL은 현재 사용자만
허용하고, 원격 클라이언트를 거부하고, 실행마다 임의 이름을 쓰며 그 이름을 다른 인스턴스와
공유하지 않습니다. `chmod 600`이 아무 효과가 없으므로 `env/harness.env`에도 같은 사용자 전용
ACL을 겁니다. `pywin32`와 `tzdata`(Windows에는 `Schedule`이 쓸 시간대 DB가 없음)는
`pyproject.toml`에서 `sys_platform` 마커로 설치됩니다. 파일·파이프·훅 입력은 콘솔 코드
페이지와 상관없이 모두 UTF-8로 읽고 쓰며, 인코딩을 지정하지 않은 텍스트 입출력이 있으면
테스트가 실패합니다. `uv`는 PowerShell에서 실행하세요. `uv run`을 쓰지 않으면 인터프리터는
`.venv\Scripts\python.exe`입니다.

프로바이더 모드는 `claude`, `codex`, `both`이며 기본값은 여전히 `claude`입니다. 기존
하네스를 업그레이드할 때 예상치 못한 플랫폼 파일이 생기지 않도록 하기 위해서입니다.
Codex 하네스는 `Harness(provider="codex")`로 설정합니다. `bin/codex-spike.py`는 저장된
Codex CLI 로그인으로 실제 읽기 전용 + 범위 지정 MCP 검증을 수행합니다.
Codex 전용 설치는 `claude-agent-sdk`를 제외하며, 이를 `both`로 바꿀 때는 프로젝트가
소유한 `pyproject.toml`을 보존하고 해당 의존성을 추가하라는 조치를 명시적으로
출력합니다. 구현 결정, 매트릭스, 남은 위험은
[`docs/codex-compatibility.md`](../skills/start/assets/kit/docs/codex-compatibility.md)에
기록되어 있습니다.

## 능력을 주는 길은 하나

> The World는 에이전트에게 능력을 주는 길을 의도적으로 **하나**만 둡니다. 프로바이더
> 중립적인 `ToolSpec`입니다. Claude는 이를 인프로세스 SDK MCP tool로 변환하고, Codex는
> 실행마다 사설 stdio MCP 브리지로 노출하되 범위가 걸린 핸들러는 부모 프로세스에
> 남겨 둡니다. 프로바이더들은 더 넓은 표면 — stdio/SSE/HTTP 외부 MCP 서버, 플러그인,
> `skills`, 서브에이전트별 tool 세트 — 을 제공하지만, 그 전부가 하네스가 경계를 긋지
> 않은 표면을 넘겨줍니다. 플러그인 로딩(`Agent.allow_plugins` + `PluginMarketplace`,
> `world/process/plugins.py` 참고. Claude 러너 전용이며 Codex 러너는 거부합니다)이
> 명시적으로 완전 신뢰를 인정한 유일한 예외입니다. 두 번째 길을 만들지 마세요.

## 물려받은 불변식 (풀려면 의식적으로 결정하세요)

- **바깥으로 나가는 모든 일은 사람 승인자 한 명.** 봇 공동 승인자는 사람의 결정을
  (HMAC 서명으로) *전달*만 할 수 있고, 결코 스스로 결정을 만들지 않습니다.
- **Claude 구독 과금만.** `ClaudeRunner`는 `ANTHROPIC_API_KEY`를 거부합니다. Codex 인증은
  별개이며 저장된 Codex CLI 로그인을 씁니다.
- **exit-0 ≠ 완료.** `writes_report` 에이전트의 진짜 완료는 해당 태스크의 `work/` 아래에
  생긴 파일입니다. 파일 없이 exit-0이면 blocked로 기록되고 다시 발사됩니다. 일의 종류마다
  "끝났다"가 무엇인지는 누가 지정하는 상태가 아니라 `TaskType`의 완료 술어입니다.
- **경계는 프롬프트가 아니라 코드에.** 스폰된 에이전트는 빈 기본 tool 세트(Bash 없음,
  Write 없음)를 받고, 당신이 넘긴 범위 지정 tool만 갖습니다.
- **조용한 상한도, 보이지 않는 데몬도 없다.** 기능을 낮출 땐 시끄럽게(`env=none`,
  `slack=off`) 알리고, 로그가 보이는 곳에서 실행합니다. 게시되는 모든 메시지는 그 실행의
  비용 — 지연 시간, 과금된 모델, 토큰, 캐시 적중률, tool 수, USD — 를 함께 싣습니다
  (`Harness(run_footer=False)`로 숨길 수 있음). 없는 항목은 0이 아니라 생략합니다.
  Codex 푸터가 달러 대신 `USD 미제공`이라고 적는 것도 그래서입니다. 비용을 알 수 없는
  것과 정말 공짜인 것이 같아 보여서는 안 되기 때문입니다.

각 항목은 실제 사고에서 비롯됐습니다. 이유는
[`docs/philosophy.md`](../skills/start/assets/kit/docs/philosophy.md)에 있습니다.

> **환경 변수 이름 규칙.** 하네스의 설정 변수에는 키트 전역 접두사를 붙이지 않습니다.
> 하네스 하나가 소유하는 것은 `<PROJECT>_THING`, 여러 하네스가 진짜로 공유하는 것은
> 접두사 없는 `THING`입니다(승인자는 사람 한 명 — 정체성 하나, 변수 하나). 접두사 없는
> 변수는 셸에 이미 export된 것과 충돌할 수 있고 그땐 셸이 이깁니다. 로더는 하네스가 남의
> 토큰을 조용히 읽게 두는 대신 stderr로 그 사실을 알립니다.
>
> `WORLD_*`는 다른 네임스페이스이며 접두사를 유지합니다. `WORLD_AGENT`와 `WORLD_HEADLESS`는
> 스폰 시점에 런타임이 주입하고 가드 훅이 읽습니다. 특히 `WORLD_AGENT`는 범위 가드가
> 신뢰하는 통로라서, 여기에 접두사 없는 이름을 쓰면 충돌하기 쉬운 동시에 위조당하기도
> 쉬워집니다.
>
> `world` 패키지 이름과 `env/harness.env` 파일 이름은 브랜딩이 아니라 메커니즘입니다.
> 플로우와 훅이 그 이름을 전제하므로, 바꾸려면 find-replace가 아니라 연동된 편집이
> 필요합니다.

## 출처

여기서 키트는 `start` 스킬 안에 실려 있습니다. 대상 디렉터리에 스캐폴딩하며,
가드 훅과 `settings.json`은 이미 있다고 가정하지 않고 복사·병합합니다.
