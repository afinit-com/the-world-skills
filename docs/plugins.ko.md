# Claude Code · Codex 플러그인

*[English](plugins.md)로 읽기.*

같은 `skills/` 네 개와 번들 하네스를 두 앱에서 사용합니다. 앱마다 스킬을 복제하거나
별도의 하네스를 유지하지 않습니다. 기존 `npx skills` 설치도 계속 지원하지만,
같은 앱에는 **스킬 설치와 플러그인 설치 중 하나만** 사용하세요.

| 용도 | 지원 범위 |
|---|---|
| 플러그인을 읽고 하네스 만들기 | Claude Code, Codex |
| Slack 봇 안에서 실제 에이전트 실행 | Claude Code 또는 Codex |
| 관계도와 tool 학습 | 두 앱 공통, 생성 결과는 독립 HTML |
| 실행 에이전트의 쓰기 범위 제한 | 선택한 런타임의 훅과 scoped tool |

`Agent.allow_plugins`(+ `PluginMarketplace`)는 **실행 에이전트에 외부 플러그인을 허용하는 별도 기능**이며,
여전히 Claude 전용/full-trust입니다. 이 문서의 배포 플러그인과 다릅니다.

## 설치

저장소 루트가 곧 마켓플레이스입니다(`.claude-plugin/marketplace.json`,
`.agents/plugins/marketplace.json`, 이름 `the-world`, 플러그인 위치 `./`). 두 파일은 `plugin.json`에서 `python3 scripts/build-plugin.py --sync`로
만들며 손으로 고치지 않습니다. clone 없이 GitHub 주소로 바로 설치합니다.

### Claude Code

```bash
claude plugin marketplace add afinit-com/the-world-skills
claude plugin install the-world-skills@the-world
```

새 세션에서 `/the-world-skills:the-world-start`을 호출합니다.

### Codex

```bash
codex plugin marketplace add afinit-com/the-world-skills
codex plugin add the-world-skills@the-world
```

새 세션에서 `$the-world-start`과 원하는 일을 입력합니다.

루트 마켓플레이스로 Claude Code와 Codex 모두 등록·설치·활성화까지 격리된 설정
폴더(`CLAUDE_CONFIG_DIR`, `CODEX_HOME`)에서 검증했습니다. 설치되는 것은 저장소
전체입니다(`./`).

## 개발 중 로컬 빌드

아래는 저장소를 받아 둔 상태에서 추린 사본으로 설치하거나 시험할 때 씁니다.
빌드 자체는 개인 설정을 바꾸거나 로그인·봇 실행·공개 등록을 하지 않습니다.
로컬 빌드의 마켓플레이스 이름은 `the-world-local`이라 위의 `the-world`와 함께
등록해도 겹치지 않습니다.

### Codex (로컬 빌드)

```bash
python3 scripts/build-plugin.py dist/the-world-marketplace
codex plugin marketplace add ./dist/the-world-marketplace
codex plugin add the-world-skills@the-world-local
```

새 세션에서 스킬 목록을 열어 `the-world-start`을 선택하거나 `$the-world-start`과 원하는
일을 입력합니다. CLI 명령 이름은 `plugin add`입니다.
빌드는 기존 경로를 덮어쓰지 않으므로 다시 만들 때는 새 출력 경로를 사용하세요.

### Claude Code (로컬 빌드)

개발 중에는 별도 등록 없이 현재 저장소를 직접 로드할 수 있습니다.

```bash
claude --plugin-dir "$PWD"
```

`/the-world-skills:the-world-start` 또는 `/the-world-skills:the-world-util`을 호출합니다.
지속 설치하려면 위에서 생성한 마켓플레이스를 등록합니다.

```bash
claude plugin marketplace add ./dist/the-world-marketplace
claude plugin install the-world-skills@the-world-local
```

## 패키지와 프로젝트의 경계

라이선스는 MIT입니다. [LICENSE](../LICENSE)를 보세요.

- 루트 `plugin.json`: Agent Plugins 공통 형식.
- `.codex-plugin/plugin.json`, `.claude-plugin/plugin.json`: 앱별 매니페스트. 둘 다 같은
  이름·버전과 `skills/`를 사용합니다. 세 파일 중 내용이 같은 쌍은 없어서(루트는 `skills`가
  없고, Codex는 `interface`를 더 답니다) 한 파일이나 심볼릭 링크로 합칠 수 없습니다. 대신 공유하는
  6개 필드(`name` `version` `description` `author` `repository` `license`)만 맞춥니다:

  ```bash
  python3 scripts/build-plugin.py --sync
  ```

  루트 `plugin.json`을 고친 뒤 실행하면 두 앱 매니페스트의 그 6개 필드를 덮어쓰고,
  앱 전용 필드와 키 순서는 그대로 둡니다. 이미 일치하면 파일을 건드리지 않습니다.
  빌드는 소스 파일을 몰래 고치지 않으므로 `--sync`는 목적지를 받지 않는 독립 동작이며,
  어긋난 채로 빌드하면 `--sync`를 실행하라는 메시지와 함께 실패합니다.
- 빌드 출력: `plugins/the-world-skills/`에 완전한 사본과 두 앱의 마켓플레이스 목록을
  만듭니다. 원본 저장소 밖으로 이동해도 설치 스크립트와 관계도 자산을 찾을 수 있습니다.
- 번들 안의 `.claude/hooks`, `.codex/hooks`는 **설치 자료**입니다. 플러그인 자체에
  전역 훅이나 MCP 서버를 등록하지 않습니다.
- 생성된 프로젝트의 단어 명령은 `bin/scaffold-world-skill.py --host claude|codex`로
  `.claude/skills/` 또는 `.agents/skills/`에 만들고, `--host all`은 둘 다 만듭니다.
  런타임 선택 `--provider claude|codex|both`와는 별개입니다.
- Codex 기본 샌드박스(`workspace-write`)는 `.codex/`와 `.agents/` 쓰기를 막습니다.
  `install-kit.py`와 `scaffold-world-skill.py`는 쓰기 전에 모든 목적지를 확인하고,
  막히면 아무것도 쓰지 않은 채 `cannot write to ...`로 실패합니다. 사용자에게 그 경로
  쓰기 승인(escalation)을 요청해 다시 실행하세요. 나머지만 손으로 복사하지 말고,
  승인되지 않으면 가드 훅과 단어 명령이 설치되지 않았다고 보고합니다.

## 검증과 공개 배포

패키지 테스트는 매니페스트 일치, 이동한 패키지에서 하네스 설치, 두 호스트의 단어
명령 생성, 기존 편집 보존을 확인합니다. Claude의 네이티브 검증과 Codex 플러그인
검증도 별도로 실행할 수 있습니다. **정적 검증은 각 앱의 로그인·GUI 설치·Slack
실행 검증과 다릅니다.**

테스트 수는 계속 바뀌므로 직접 돌려보세요.

```bash
python3 -m unittest discover -s tests
cd skills/the-world-start/assets/kit && uv run --extra dev pytest -q
```

공통 Agent Plugins JSON Schema와 Claude Code 매니페스트 검증을 통과합니다. 각 앱
GUI에서의 실제 설치·호출은 검증하지 않았습니다.

`start`와 `help-my-world`의 명시 호출 전용 정책은 유지합니다. Claude에는
`disable-model-invocation: true`, Codex에는 `agents/openai.yaml`의
`policy.allow_implicit_invocation: false`를 제공합니다.
Codex plugin-creator 검증 도우미는 frontmatter의 `disable-model-invocation: true`를
거부하므로 이 도우미의 전체 통과를 주장하지 않으며, 통과시키려고 명시 호출 정책을
빼지 않습니다.
[Codex 호출 정책](https://learn.chatgpt.com/docs/build-skills).

`the-world-start`는 할 일 제안도 맡습니다. 프로젝트 경로만 받으면 그 경로의 Claude
Code·Codex 세션 기록을 읽고 근거를 붙여 제안합니다.
`the-world-audit`은 하네스를 읽기만 하는 위험 점검이라 명시 호출 전용 정책을 두지
않습니다. 네 스킬은 두 앱에 같은 `skills/` 경로로 노출되며, `scan.py`는 표준
라이브러리만 씁니다.

저장소 자체가 마켓플레이스이며, 각 앱의 공식 마켓플레이스에 등록했다는 뜻은 아닙니다.
라이선스는 MIT이고 매니페스트와 배포 패키지에 반영돼 있습니다.

공식 사양:
[OpenAI 패키징](https://developers.openai.com/plugins/build/plugins),
[Claude 플러그인](https://code.claude.com/docs/en/plugins-reference),
[Claude 마켓플레이스](https://code.claude.com/docs/en/plugin-marketplaces).
