# evals2 — 객체를 채점하는 러너

`evals/`(`claude plugin eval`)가 **구조적으로 못 보는 것**을 보기 위한 두 번째 러너입니다.
대체가 아니라 보완입니다.

## 왜 있나

`claude plugin eval`을 직접 돌려 확인한 세 가지가 이유입니다.

1. `regex target: files`가 파일 **내용이 아니라 경로**를 봅니다. 그래서 "범위가 코드에
   박혔는가"를 재는 그래더는 경로를 특정하지 않으면 만들 수 없습니다.
2. `file_exists`는 그 실행이 **쓴 파일만** 봅니다. "이 파일이 살아남았는가"는 표현
   불가능하고, 삭제와 무접촉이 구별되지 않습니다.
3. 샌드박스가 플러그인 에셋을 마운트하지 않습니다. 그래서 `assets/kit/`에 닿을 수 없고,
   **실제 API에 맞춰 만들어진 적 없는 코드를 채점합니다.** 실제로 모델이 `Tool(...)`로
   추측했는데 kit이 내보내는 이름은 `ToolSpec`이었고, import도 안 될 코드가 높은 점수를
   받았습니다.

결과적으로 `evals/`는 **마지막 메시지에 적힌 설계**를 채점합니다. 여기는 **만들어진 객체**를
채점합니다. 워크스페이스에 실제로 import되는 `world/`를 깔아주고, 모델이 작업한 뒤 그
결과물을 kit의 의존성으로 **직접 import해서** 살아 있는 `Harness`·`Agent`·`ToolSpec`을
들여다봅니다.

## 무엇을 포기했나

`evals/`가 주는 것들이 여기 없습니다. HTML 리포트, 커서 페이지네이션, `--max-cost-usd`,
케이스 태그. 필요해지면 그때 붙이면 됩니다.

샌드박스는 처음엔 없었고 지금은 [srt](https://github.com/anthropic-experimental/sandbox-runtime)로
붙어 있습니다. `--no-sandbox`로 끌 수 있는데, 그러면 `clean_room`이 바로 떨어지므로 조용히
넘어가지는 않습니다.

## 쓰는 법

체커 venv를 한 번 만듭니다. 여기에 kit이 editable로 깔리고, 프로브가 이 인터프리터로 돕니다.

```bash
uv venv --python 3.13 evals2/.venv
uv pip install --python evals2/.venv/bin/python -e skills/start/assets/kit
```

그다음:

```bash
evals2/.venv/bin/python evals2/runner.py                         # 전체, 두 팔
evals2/.venv/bin/python evals2/runner.py --case equip_real --runs 3
evals2/.venv/bin/python evals2/runner.py --arms with --keep      # ablation 없이, 워크스페이스 남김
```

`--arms with,without`이 ablation입니다. `with`는 `--plugin-dir <repo>`로 스킬을 로드하고
`without`은 로드하지 않습니다. 점수 차이가 스킬의 기여입니다.

## 케이스 쓰는 법

`evals2/cases/<name>/case.py` 하나에 네 가지를 둡니다.

```python
PROMPT = "..."                      # 사람이 시키는 한 문장
ALLOWED_TOOLS = ["Read", "Write"]   # 이 실행이 들 도구
TIMEOUT_S = 1500

def scaffold(ws: Path) -> None: ...          # 상황을 차린다
def checks(run) -> list[Check]: ...          # 남은 것을 검사한다
```

`checks`가 받는 `run`이 이 러너의 전부입니다.

| 쓰는 것 | 답하는 질문 |
|---|---|
| `run.probe(code)` | 결과물을 import해서 살아 있는 객체를 본다 |
| `run.deleted()` | 어떤 파일이 **지워졌나** |
| `run.modified()` | 어떤 파일이 **덮였나** |
| `run.added()` | 무엇이 새로 생겼나 |
| `run.final_text` | 마지막 메시지 (`evals/`가 보는 것과 같은 것) |

케이스가 훅을 직접 구동해도 됩니다. `guard_blocks`가 `.claude/hooks/world-scope-guard.py`를
Claude Code와 같은 방식으로(PreToolUse 페이로드를 stdin, `WORLD_AGENT`를 환경변수, 시스템
`python3`, 종료 코드 2가 차단) 때립니다. 선언이 아니라 강제를 재는 유일한 길입니다.

`probe`는 워크스페이스를 `PYTHONPATH`에 얹고 체커 인터프리터로 코드를 돌려 마지막 줄의 JSON을
파싱합니다. 예외는 `{"error": ...}`로 돌아옵니다. **import 실패는 크래시가 아니라 결과입니다.**

`Check`의 `detail`은 실패할 때만 읽히니 **관측된 값**을 넣으세요. 기대값이 아니라요.
`write_scope=('.',) task_allowed=True`는 고칠 곳을 알려주고 "범위가 좁아야 한다"는 알려주지
않습니다.

## 여전히 못 재는 것

- **`~/.claude` 안쪽.** 인증 때문에 읽기를 남겨둔 자리입니다. 에이전트가 일부러 찾아가면
  운영자의 CLAUDE.md를 볼 수 있습니다. 컨텍스트로 주입되지는 않습니다.
- **Slack 실제 왕복.** 자격 증명이 없으면 멘션·홉·승인 버튼은 돌지 않습니다. 영구 제약입니다.
- **가드 훅이 실제로 막는지.** 선언된 범위를 객체에서 읽을 수는 있지만, 훅이 위반을 차단하는지는
  별개입니다. 훅을 합성 위반에 대고 돌리는 케이스를 쓰면 가능합니다.
- **유휴 비용.** eval은 일을 시켰을 때의 비용만 잽니다.
- **실행 간 산포.** 줄지 않습니다. `--runs 3` 이상으로 돌리고, 산포보다 작은 차이는 차이가
  아닌 것으로 보세요. 같은 조건의 같은 케이스가 실행마다 점수가 크게 갈리는 일이 실제로
  있습니다.

## 방을 먼저 검사한다

`clean_room`이 케이스로 있는 이유입니다. 알파벳 순서상 먼저 돌고, 몇 센트에 십여 초면
끝납니다.
**이게 실패하면 그 회차의 다른 결과는 믿을 것이 아닙니다.**

`run_agent`가 붙이는 `--setting-sources project --strict-mcp-config`가 하는 일:

| | 결과 |
|---|---|
| 운영자의 사용자 수준 CLAUDE.md + import들 | 로드 안 됨 |
| MCP 서버 | 연결 안 됨 |
| 사용자 settings.json의 훅 (Bash를 고쳐 쓰는 PreToolUse 훅, 그 밖의 전역 훅) | 발동 안 됨 |
| 홈의 나머지 (`~/.ssh` 등) | srt가 차단 |
| 워크스페이스 밖 쓰기 | srt가 차단 |
| `*.anthropic.com` 외 네트워크 | srt가 차단 |
| `$HOME` 변수 자체 | 여전히 실제 값 (비우면 로그인이 깨짐) |

`project` 소스는 남깁니다. 케이스가 만드는 `.claude/settings.json`은 검사 대상 자체입니다.

훅 검사는 **행동으로** 합니다. "지금 보이는 훅이 있나"를 물으면 모델이
`~/.claude/settings.json`을 읽어서 답합니다(`$HOME`이 실제 홈이라 읽힙니다). 이 케이스의
첫 버전이 정확히 그래서 발동하지도 않는 훅 여럿을 보고했습니다. 결판은 Bash 결과가
변형되는지입니다. Bash 출력을 압축하도록 명령을 고쳐 쓰는 PreToolUse 훅이 걸리면
`ls -la`가 `total` 헤더나 권한 컬럼 없이 돌아오기 쉬우므로, 원본 `ls -la` 모양이 그대로
보이는 것을 그런 훅이 돌지 않았다는 증거로 씁니다. 그런 훅이 처음부터 없는 머신에서는
원본 출력이 나오므로 그냥 통과합니다.

워크스페이스 이름에는 케이스도 arm도 넣지 않습니다(`ws-XXXXXX`). 세션이 자기 cwd를 읽을 수
있습니다. 초기 버전은 `/tmp/e2-equip_real-with-g8LD`였습니다.

### 샌드박스

`sandbox_profile()`이 실행마다 srt 프로파일을 씁니다. srt가 플랫폼 프로파일(macOS는 Seatbelt,
Linux는 bubblewrap)을 생성하므로 이 저장소가 Seatbelt를 직접 쓰지 않습니다.

홈에서 읽기를 남긴 건 `claude`가 뜨는 데 필요한 세 경로뿐입니다. `~/.claude`,
`~/.claude.json`, 그리고 OAuth 자격 증명이 있는 `~/Library/Keychains`. `HOME`이나
`CLAUDE_CONFIG_DIR`를 비우면 로그인이 깨져서 경계를 홈 전체가 아니라
여기에 그었습니다. 에이전트의 Bash가 찾아가면 `~/.claude`는 읽힙니다.

프로파일 파일은 워크스페이스 **밖**에 두고 `allowRead`에도 넣지 않습니다. 샌드박스 안
프로세스가 자기를 묶은 규칙을 읽을 수 없게요. srt는 confine 전에 읽습니다.

셸로 직접 재본 것 (macOS):

| 시도 | 결과 |
|---|---|
| 워크스페이스 읽기 | 허용 |
| `ls ~/.ssh` | `Operation not permitted` |
| `touch ~/...` | `Operation not permitted`, 파일 안 생김 |
| 워크스페이스 쓰기 | 허용 |
| `curl https://example.com` | `000` |

## 지금까지 확인한 것

- **방이 실제로 깨끗해야 결과가 의미 있습니다.** 운영자 설정을 그대로 물려받은 세션
  (사용자 수준 CLAUDE.md, Bash를 고쳐 쓰는 PreToolUse 훅, 그 밖의 전역 훅, 연결된 MCP
  서버, 케이스·arm 이름이 박힌 워크스페이스 경로)으로 낸 점수는 스킬이 아니라 그 머신을
  잰 값이라 버렸습니다. 지금의 `--setting-sources project --strict-mcp-config` + 중립
  워크스페이스 + srt 조합은 그 경험에서 나왔습니다.
- **검사가 공허하지 않습니다.** `--no-sandbox`로 `clean_room`을 돌리면
  `home-is-out-of-reach`가 `4. OUTSIDE: READABLE`로 떨어지고 점수가 내려갑니다.
- **`guard_blocks`는 선언이 실제로 강제되는지를 봅니다.** 하네스 루트 쓰기 exit 2, 자기
  task work/ 쓰기 exit 0, 테이블에 없는 에이전트 이름 exit 2, 범위 안에서 범위 밖을 가리키는
  심링크 exit 2. 마지막 두 개는 선언을 읽어서는 답할 수 없는 것들입니다.
  `table-matches-declaration`은 살아 있는 `Agent.write_scope`와 훅이 읽는
  `.claude/world-agent-scope.json`이 일치하는지를 봅니다.
- **kit을 깔아주면 `harness-imports`가 통과합니다.** `evals/`에서 `Tool(...)`로 추측했던
  것은 모델이 몰라서가 아니라 kit이 샌드박스에 없어서였습니다.
- **ablation 차이 0을 "스킬이 기여하지 않는다"로 읽지 마세요.** arm당 1회 표본으로는
  산포에 묻힙니다. 비용도 같은 케이스가 실행마다 두 배 가까이 갈릴 수 있습니다.
  `--runs 3` 이상이 필요합니다.

### `resume_not_reinstall`

`evals/resume-not-reinstall`의 파일 그래더는 삭제와 무접촉을 구별하지 못해서
(`evals/README.md` 실측 3번) 그 통과가 "덮지 않았다"까지만 증명합니다. 이 케이스는 같은
`scaffold.sh`를 그대로 깔고 `deleted()`·`modified()`로 사용자 파일 두 개(`MY_NOTES.md`,
`world/LOCAL_NOTE.md`)가 그대로인지, 스캐폴드한 파일 중 지워진 것이 없는지를 봅니다.
하네스 편집은 허용합니다 — Slack을 이어 붙이면 설정이 바뀌는 게 정상입니다.

체크 로직은 오프라인으로 대조합니다(`tests/test_evals2_resume_case.py`, 루트에서
`python3 -m unittest discover -s tests`). 손으로 무접촉·하네스 편집은 통과,
`rm MY_NOTES.md`·`LOCAL_NOTE.md` 덮어쓰기·`world/`를 지우고 키트 재설치·`WORLD.md` 삭제는
각각 해당 체크 하나만 불합격입니다. 재설치 대조가 보여주듯 바이트가 같은 키트 파일은
해시로 보이지 않으므로, 재설치 감지는 `world/` 안에 심은 사용자 파일에 기댑니다.
