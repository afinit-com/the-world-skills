# the-world-skills — 캐릭터와 함께 배우는 하네스 키트

**[the-world.gon.quest](https://the-world.gon.quest)** · *이 문서의 [English](README.md) 버전이 원본이며, 이 번역이 늦을 수 있습니다.
이슈와 PR 모두 환영하지만 응답이 느릴 수 있습니다 — [CONTRIBUTING.md](CONTRIBUTING.md).*

세계관 속 여러 캐릭터에게 `tool`을 붙이고, 서로 협력하게 하면서 하네스를 배웁니다.
일정이나 변화에 따라 Claude Code 또는 Codex 에이전트를 실행하고 Slack에서 캐릭터와
대화할 수 있습니다. Claude Code · Codex용 플러그인으로 제공합니다.

## 빠르게 시작하기

**1. 설치.** 쓰는 앱을 고르세요.

<details open>
<summary><b>Claude Code</b></summary>

```bash
claude plugin marketplace add afinit-com/the-world-skills
claude plugin install the-world@the-world
```

</details>

<details>
<summary><b>Codex</b></summary>

```bash
codex plugin marketplace add afinit-com/the-world-skills
codex plugin add the-world@the-world
```

</details>

> **0.5.0 이전에 설치했다면:** 이름이 짧아졌습니다. `/the-world-skills:the-world-start`(0.4.0 이전)와
> `/the-world:the-world-start`(0.4.0)는 이제 `/the-world:start`이고, `audit`과 `util`도 같습니다.
> 0.4.0이라면 평소처럼 플러그인을 업데이트하면 됩니다. 0.4.0 이전에는 플러그인 이름 자체가 달라서
> 한 번만 바꿔 주세요:
>
> ```bash
> claude plugin marketplace update the-world && claude plugin uninstall the-world-skills@the-world && claude plugin install the-world@the-world
> codex plugin marketplace upgrade && codex plugin remove the-world-skills@the-world && codex plugin add the-world@the-world
> ```

<details>
<summary><b>다른 에이전트 (스킬만)</b></summary>

```bash
npx skills add afinit-com/the-world-skills
```

이때 스킬 이름은 그냥 `/start`입니다. 자세한 내용은 [플러그인 문서](docs/plugins.ko.md).

</details>

**2. 맡길 일을 말하세요.** 봇이 일할 프로젝트 폴더에서:

```text
/the-world:start 매일 오전 9시에 이 레포 변경 사항 요약해줘
```

Codex에서는 `$the-world:start …`입니다. 봇은 프로젝트 옆에 자기 폴더를 갖습니다
(`~/code/my-app` → `~/code/my-app-world`). 빈 폴더에서 시작하면 그 폴더에 만들고, 프로젝트는
문장에 경로로 알려주면 됩니다. 에이전트는 캐릭터들이 사는 세계관을 묻고("해적단
테마로"나 좋아하는 애니메이션 세계관처럼 미리 말하면 생략), 캐릭터마다 말투를 보여 주며 괜찮은지
확인합니다. 그다음 봇을 만들고 로컬에서 확인한 뒤 무엇을 만들었는지 알려 줍니다.

**3. Slack에서 대화하기** (선택): "Slack 연결해줘"라고 하세요. 에이전트가 쓸 채널과
승인할 사람을 묻고 Slack 앱을 만들어 줍니다. 채널에서 봇에게 한 번 인사하면 연결됩니다.

필요한 것: **Python 3.11+와 [uv](https://docs.astral.sh/uv/)**, 3단계에는
[Slack CLI](https://docs.slack.dev/tools/slack-cli)와 한 번의 `slack login`.
macOS, Linux, 네이티브 Windows에서 동작합니다. 막히면 `/the-world:help-my-world`.

<details>
<summary>각 단계에서 일어나는 일</summary>

- 에이전트가 하네스를 돌릴 OS, 봇 실행기, 채널을 묻고 그것만 설치하며, 선택은
  `project-profile.json`에 기록됩니다([프로필](docs/harness-guide.ko.md#프로필-os--실행기--채널)).
- 첫 확인에는 Slack 토큰이나 모델 로그인이 필요 없습니다. 봇 등록과 실행 준비를
  확인하며, 실제 AI 실행과 Slack 응답은 연결한 뒤에 확인합니다.
- Slack: `bin/slack-provision.py`가 Slack CLI로 하네스의 Slack 앱 하나를 만들고 토큰을
  채웁니다. 캐릭터들은 그 앱으로 각자 이름을 달고 말합니다. `bin/slack-pair.py`가 인사
  메시지에서 채널과 내 멤버 ID를 받아 둡니다. CLI가 없으면 api.slack.com에 매니페스트를
  붙여 넣는 방식으로 진행합니다.
- 기존 프로젝트에서는 하던 설정을 이어갑니다.

</details>

## 다섯 개의 스킬

| 스킬 | 하는 일 |
|---|---|
| `start` | 맡길 프로젝트나 할 일을 알려줌(프로젝트만 주면 프로젝트와 로컬 Claude Code·Codex 세션 기록을 읽고 할 일을 제안) → 기본값으로 워커 하나 생성 → 로컬 검증 → 준비되면 Slack 연결과 커스터마이즈. 중단된 설정도 이어서 진행합니다. 키트를 `assets/kit/`에 담고 있습니다. |
| `audit` | 이미 만든 하네스의 위험을 읽기만 하고 점검합니다. 봇 권한, tool 범위, 바깥으로 나가는 행동과 승인, 콘텐츠를 통한 조작, 자기 재설정, 비밀값, 조용한 실패를 항목마다 closed·open·not verified로 표시하고, open 항목에는 파일·줄과 재현 방법을 붙입니다. 고치지는 않습니다. |
| `dig-my-sessions` | 이 컴퓨터에 남은 Claude Code·Codex·opencode·Cursor·Antigravity CLI(`agy`)·구 Gemini CLI 세션 기록을 원하는 기간과 프로젝트로 골라 뒤집니다. 티켓·브랜치·주제를 다룬 세션을 이어서 열 명령과 함께 찾아 주고, 손으로 반복하는 잡무를 찾아 월드·스킬·한 줄 스크립트 중 어디로 넘길지 제안합니다. 프로그램이 띄운 세션은 뺍니다. 오프라인에서 읽기만 하고, 선택 모드 `--jev`는 동의를 받은 뒤에만 가린 첫 요청문을 TypeSafe Jev로 보내 분류합니다. |
| `util` | 하네스 전제 조건이 없는 독립 유틸리티를 서브커맨드로 실행합니다. 현재는 `slack icon generate`. |
| `help-my-world` | 개발자가 아니어도 메인테이너에게 도움을 청할 수 있습니다. 하려던 일과 실제로 일어난 일만 말하면, 나머지(재현 과정, 가려서 모은 환경 정보, 내 월드의 캐릭터·트리거·도구 구성)는 에이전트가 저장소의 이슈 양식에 채웁니다. 필요하면 GitHub CLI 설치와 로그인을 한 단계씩 안내하고, 초안을 승인하기 전에는 아무것도 올리지 않습니다. |

`start`는 `disable-model-invocation: true`입니다. git 저장소에 파일을 쓰므로,
사람이 직접 입력해야만 실행됩니다. `help-my-world`도 바깥에 글을 올리므로 같습니다. `audit`과 `dig-my-sessions`는 읽기만 하므로 모델이
호출할 수도 있습니다.

그 밖에 하네스에 필요한 것들 — 봇 추가, 범위가 정해진 능력 연결, 새 봇 검증, 플러그인
레지스트리 연결 — 은 `/the-world:start`가 **하네스 안에 직접 설치**합니다. 그래서 하네스를
다른 곳에 클론해도 그대로 동작합니다.

## 두 가지 철칙

**1. 하네스는 자신의 생성 명령어를 직접 들고 다닌다.** 빠른 시작은 `recruit`와 `equip`을
쓰는 탐험대 설정을 사용합니다. 준비가 되면 원하는 세계관과 단어를 직접 고르거나, 평범한
이름을 달라고 해도 됩니다. 해적단이라면 동료를 모집(recruit)하고 무기를 벼릴(forge) 수
있고, 강아지를 좋아한다면 입양(adopt)하고 훈련(train)시킬 수 있습니다. 각 단어는
프로젝트 로컬 스킬 명령어가 됩니다. 물론 그냥 평범한 말로 할 일을 설명해도 됩니다.

**2. 모든 에이전트와 tool은 명시적 범위를 코드에 고정해 선언한다.** `write_scope`가 없는
에이전트는 PreToolUse 훅이 모든 곳에서 쓰기를 막습니다. 범위는 서류가 아니라 집행 그
자체입니다. 능력(capability)은 선택한 프로바이더의 MCP 어댑터로 노출되는 전용
**`ToolSpec`**이며, 그 경계(cwd, 호스트, 저장소, "merge 동사 금지")는 생성 시점에
묶입니다. 철학을 한 줄로 하면(모든 하네스에 설치되는 [`docs/philosophy.md`](skills/start/assets/kit/docs/philosophy.md)):
*프롬프트에 적힌 약속은 집행이 아니다.*

## 캐릭터 관계도 보기

```bash
uv run python harness.py --world-map
```

`WORLD.html`로 열립니다. 캐릭터별 tool, 역할, 쓰기 범위와 서로 어떻게 협력하는지 볼 수
있습니다. [관계도 만들기](skills/start/assets/kit/docs/world-map.md).

## 하네스 운영하기

> **업그레이드는 키트를 다시 설치하며, 키트 트리를 먼저 지웁니다.** 설치 스크립트를 다시
> 실행하기 전에 커밋하고, 실행 후 `git diff`를 읽으세요. `world/` 안에 추가한 것은 사라집니다.

설치 스크립트가 무엇을 어디에 쓰는지, 업그레이드 방법, 프로바이더 모드, 런타임이 지키는
규칙은 [하네스 가이드](docs/harness-guide.ko.md)에 있습니다. GitHub·Linear·Jira 연동은
[레시피](skills/start/assets/kit/docs/integrations.md), Discord(실험적)는
[문서](skills/start/assets/kit/docs/discord.md)를 보세요.

## 라이선스

[MIT](LICENSE). Copyright 2026 Afinit.

`/the-world:start`가 하네스에 풀어놓는 키트(`world/`, `.claude/the-world/`, `templates/`, 가드 훅)는
이 라이선스를 함께 가지고 갑니다. 그 위에 당신이 만든 것 — `harness.py`, abilities,
tools, 세계관 — 은 당신 것입니다.
