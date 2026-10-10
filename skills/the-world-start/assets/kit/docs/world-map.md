# 캐릭터 관계도

하네스의 캐릭터들이 어떤 관계인지, 각자 어떤 **tool**을 가졌는지 한 화면에서
살펴봅니다. 캐릭터를 누르면 역할과 도구 설명이 나오고, 관계를 선택하면 누가 누구에게
어떤 도움을 주는지 화살표와 설명으로 볼 수 있습니다.

## 먼저 열어보기

하네스 폴더에서:

```bash
uv run python harness.py --world-map
```

생성된 `WORLD.html`을 브라우저로 엽니다. 웹 서버, Slack 연결, 모델 호출 없이
사용합니다. 캐릭터·tool 검색과 ‘관계 둘러보기’를 지원합니다. 화면 너비에 맞춰 전체
관계도를 보여주고, ‘확대 보기’를 누르면 원래 크기로 스크롤하며 살펴볼 수 있습니다.
휴대폰에서는 상세 설명이 관계도 아래에 나옵니다.

설정 변경 후 같은 명령으로 다시 생성합니다. 생성할 때의 설정을 담은 파일이며
실시간 대화 기록이 아닙니다. 기본 출력은 gitignored `WORLD.html`이고 다른 경로는
`--world-map <path>`로 지정합니다. 생성 과정은 tool, 동적 tool factory, 봇을 실행하지
않습니다. 다만 `harness.py`와 그 파일이 import하는 사용자 코드는 평소처럼 로드됩니다.

## 캐릭터의 관계 만들기

“핀은 단서를 모아 노바에게 전달하고, 노바는 해석해서 루멘에게 알려줘.”처럼 설명하면
설정을 담당하는 코딩 에이전트가 `harness.py`에 관계를 추가합니다. 직접 해보려면:

```python
from world import Harness, Persona, Relationship

lumen = Persona(key="lumen", display="루멘", flavor="탐험대를 이끄는 대장")
pin = Persona(key="pin", display="핀", flavor="단서를 찾아 나서는 정찰가")
nova = Persona(key="nova", display="노바", flavor="단서의 의미를 해석하는 분석가")

harness = Harness(
    name="별빛 탐험대",
    main_persona=lumen,
    extra_personas=[pin, nova],
    relationships=[
        Relationship("pin", "nova", "단서 전달", "핀이 찾은 단서를 노바가 해석합니다."),
        Relationship("nova", "lumen", "분석 보고", "노바의 분석을 듣고 루멘이 다음 탐험을 제안합니다."),
    ],
)

if __name__ == "__main__":
    harness.main()
```

양 끝은 `Persona.key`입니다. 화살표는 첫 캐릭터에서 두 번째 캐릭터로 향합니다.
양방향에 다른 의미가 있다면 관계를 두 개 만듭니다. 캐릭터가 없는 관계는 생성을
중단하고 알려줍니다. 관계를 선언하지 않으면 연결선을 임의로 추측하지 않습니다.

관계도는 **세계관에서 정한 관계**를 표현합니다. 관계 추가 자체가 자동 위임이나
권한을 만들지는 않습니다. 실제 협력은 캐릭터 지침과 Slack의 멘션, 필요한 tool을
함께 연결하고 별도로 확인합니다. 설명에 대화 예시를 넣을 때는 예시라고 표시합니다.

## 얼굴과 tool 붙이기

캐릭터의 기존 그림을 하네스 안에 두고 `Persona(..., portrait="portraits/pin.png")`를
설정하면 관계도에 표시합니다. PNG/JPEG/WebP, 2 MB 이하를 지원하고 이미지 자체를
HTML에 담으므로 파일 하나로 열 수 있습니다. 그림이 없으면 이름으로 만든 카드를
보여줍니다. 새 이미지 생성은 별도의 요청이 있을 때 진행합니다.

tool 목록은 별도로 작성하지 않습니다. 실제 등록한 코드에서 가져옵니다:

- `Agent.tools`: 해당 캐릭터의 작업에 등록된 tool.
- `Harness.mention_tools`: 대화할 때 공통으로 받는 tool.
- `Persona.mention_tools`: 대화할 때 캐릭터가 추가로 받는 승인 대상 tool.
- `Persona.thread_tools`: 대화 스레드에 맞춰 제공하는 tool.

대화용 목록은 전용 채널 기준입니다. 멘션 주소를 확인하는 하네스의 연결용 tool은
실행 시 자동으로 추가됩니다. 외부 채널에서 전달된 대화에는 공통 tool만 제공됩니다.

대화 때 생성하는 factory는 실행하지 않고 “대화가 시작될 때 만들어짐”으로 표시합니다.
플러그인과 Bash 같은 추가 기능은 별도로 표시하며, 이 목록만으로 모든 권한을
검증했다고 말하지 않습니다. 쓰기 경로가 없는 tool도 외부 서비스를 변경할 수 있습니다.
관계도에는 토큰 값이나 대화 내용이 포함되지 않지만, 역할·tool 설명과 경로는 들어갑니다.

학습할 때는 캐릭터를 골라 **“이 친구에게 tool 하나를 더 주면 무엇을 할 수 있을까?”**를
시작점으로 삼으세요. 붙인 뒤 관계도를 다시 열면 달라진 능력을 확인할 수 있습니다.

## 예제 세계 둘러보기

```bash
PYTHONPATH=. .venv/bin/python bin/world-map-demo.py --world-map
```

여섯 캐릭터와 일곱 관계가 있는 별빛 탐험대 예제입니다. 예제 tool은 설명을 돌려주는
학습용이며 실제 외부 서비스를 조작하지 않습니다. 내 `WORLD.html`을 유지하려면
`--world-map demo-world.html`처럼 다른 출력 경로를 지정하세요.
