"""A fictional crew for exploring the character map without accounts or agent runs."""

from pathlib import Path

from world import Harness, Persona, Relationship, result, tool


def example_tool(name: str, description: str):
    @tool(name, description, {})
    async def respond(_args: dict):
        return result(f"학습 예제 응답: {description}")

    return respond


captain = Persona(
    key="captain", display="루멘", flavor="별빛 탐험대의 대장. 당신의 아이디어를 듣고 동료들과 모험을 시작해요.",
    thread_tools=[example_tool("plan_mission", "학습용 · 탐험 계획을 세우는 tool의 예시입니다.")],
)
scout = Persona(
    key="scout", display="핀", flavor="먼저 길을 나서는 정찰가. 새로운 단서를 발견하면 노바에게 가져갑니다.",
    thread_tools=[example_tool("read_map", "학습용 · 지도를 읽고 단서를 찾는 tool의 예시입니다.")],
)
analyst = Persona(
    key="analyst", display="노바", flavor="흩어진 단서에서 의미를 찾는 분석가. 핀의 발견을 구체적인 제안으로 바꿔요.",
    thread_tools=[example_tool("compare_routes", "학습용 · 두 경로를 비교하는 tool의 예시입니다.")],
)
maker = Persona(
    key="maker", display="모스", flavor="아이디어를 형태로 만드는 제작자. 세라의 꼼꼼한 검토를 믿고 맡깁니다.",
    mention_tools=[example_tool(
        "build_model", "학습용 · 모형 제작을 흉내 내는 tool입니다. 실제 파일을 바꾸지 않습니다."
    )],
)
reviewer = Persona(
    key="reviewer", display="세라", flavor="동료의 작업을 살피는 검토자. 잘된 점도, 고칠 점도 솔직하게 이야기해요.",
    thread_tools=[example_tool("inspect_model", "학습용 · 모형을 검토하는 tool의 예시입니다.")],
)
archivist = Persona(
    key="archivist", display="에코", flavor="모험의 이야기를 모으는 기록가. 다음 탐험이 더 쉬워지도록 경험을 남겨요.",
    thread_tools=[example_tool("recall_story", "학습용 · 지난 이야기를 떠올리는 tool의 예시입니다.")],
)
harness = Harness(
    name="별빛 탐험대", home=Path(__file__).resolve().parents[1], provider="codex",
    main_persona=captain, extra_personas=[scout, analyst, maker, reviewer, archivist],
    relationships=[
        Relationship(
            "captain", "scout", "탐험 부탁",
            "루멘: 핀, 북쪽 길에 무엇이 있는지 살펴봐 줄래? 핀은 read_map으로 단서를 찾는 역할입니다. (설정 예시)",
        ),
        Relationship(
            "scout", "analyst", "단서 전달",
            "핀: 노바, 두 갈래 길을 찾았어! 노바는 compare_routes로 차이를 살펴봅니다. (설정 예시)",
        ),
        Relationship(
            "analyst", "maker", "제작 제안",
            "노바: 모스, 이 길을 건널 다리를 만들어보자. "
            "모스의 build_model은 대화에서 승인 후 사용하는 tool입니다. (설정 예시)",
        ),
        Relationship(
            "maker", "reviewer", "검토 부탁",
            "모스: 세라, 내가 만든 모형을 확인해 줄래? 세라는 inspect_model을 가지고 있습니다. (설정 예시)",
        ),
        Relationship(
            "reviewer", "captain", "검토 보고",
            "세라: 루멘, 모형을 검토했어. 이제 다음 탐험을 함께 정하자! (설정 예시)",
        ),
        Relationship(
            "captain", "archivist", "추억 공유",
            "루멘: 에코, 오늘의 모험도 기억해 줘. 다음에 recall_story로 함께 돌아보자. (설정 예시)",
        ),
        Relationship(
            "archivist", "scout", "경험 나눔",
            "에코: 핀, 지난 탐험에서 찾은 길도 참고해 봐! 캐릭터들의 역할이 하나의 이야기로 이어집니다. (설정 예시)",
        ),
    ],
)

if __name__ == "__main__":
    harness.main()
