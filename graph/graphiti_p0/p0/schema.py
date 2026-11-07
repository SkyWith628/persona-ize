"""Graphiti 커스텀 엔티티·엣지 타입.

클래스 docstring과 Field description이 그대로 추출 프롬프트에 들어가므로 한국어로 구체적으로 쓴다.
필드 이름은 Graphiti 예약 필드(uuid, name, summary, fact, valid_at 등)와 겹치지 않게 한다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Person(BaseModel):
    """대화에 등장하는 사람. 서비스 사용자 본인과 대화 상대를 모두 포함한다."""

    role: str | None = Field(None, description="직업·직책·가족 관계 등 사회적 역할 (예: 팀장, 동생)")


class Event(BaseModel):
    """시점이 있는 사건·약속·상태 변화 (예: 일정 조율, 사과, 발표 약속, 발령)."""

    event_type: (
        Literal[
            "request",
            "refusal",
            "apology",
            "schedule",
            "promise",
            "conflict",
            "status",
            "other",
        ]
        | None
    ) = Field(None, description="사건 유형")
    status: Literal["open", "resolved"] | None = Field(
        None, description="아직 진행 중이면 open, 마무리되었으면 resolved"
    )
    importance: int | None = Field(
        None,
        ge=1,
        le=10,
        description="사용자 관계에 미치는 중요도 1~10 (일상 대화 1, 갈등·약속 7 이상)",
    )


class Relationship(BaseModel):
    """한 사람이 다른 사람을 대하는 관계. 방향이 있다 (source가 target을 어떻게 대하는가).

    한 쌍(source→target)에 유효한 Relationship 사실은 한 번에 하나다. 관계 유형이 바뀌면(동기→상사)
    새 사실을 만들고 이전 사실은 끝난 것으로 본다. 바뀌지 않았으면 이전 사실의 relation_type을 그대로 쓴다.
    """

    relation_type: Literal["family", "friend", "colleague", "partner", "acquaintance"] | None = Field(
        None,
        description=(
            "두 사람이 어떤 사이인지(방향 없음). 같은 회사 사람은 상사·부하·동기 모두 colleague, "
            "가족은 family, 친구는 friend, 연인·배우자는 partner, 어디에도 속하지 않을 때만 acquaintance. "
            "상하 관계는 hierarchy에 따로 적는다. 이전에 colleague였던 두 사람을 "
            "한 번의 다툼으로 acquaintance로 바꾸지 않는다"
        ),
    )
    # 3차 수집에서 superior/subordinate의 방향(target 기준)을 모델이 섞어 썼다 → 상하를 분리한다.
    hierarchy: Literal["target_higher", "target_lower", "equal"] | None = Field(
        None,
        description=(
            "source 입장에서 target의 위치. target_higher = target이 source의 상사·선배·형/누나 "
            "(예: 김민수→박지현 팀장), target_lower = target이 source의 부하·후배·동생 "
            "(예: 박지현→김민수), equal = 동기·친구·연인. 대화에서 확인되지 않으면 비운다"
        ),
    )
    speech_style: Literal["반말", "존댓말", "혼용"] | None = Field(
        None,
        description=(
            "source가 target에게 쓰는 말투. source의 문장 어미로만 판단한다: "
            "'~습니다/~요/~드립니다'면 존댓말, '~야/~어/~ㄱ?/~해'면 반말. "
            "사과·갈등 같은 내용과는 무관하다"
        ),
    )
    address_term: str | None = Field(
        None,
        description=(
            "source가 target을 직접 부를 때 쓴 말 (예: 팀장님, 형, 서연아). "
            "대화에 등장하는 제3자 호칭('팀장님한테 혼났다며'의 팀장님)은 아니다. "
            "직접 부른 말이 없으면 비운다"
        ),
    )
    closeness: int | None = Field(None, ge=1, le=5, description="친밀도 1(거리감)~5(매우 가까움)")


ParticipantRole = Literal["당사자", "상대", "요청자", "수행자", "목격자", "관련자"]


class Involved(BaseModel):
    """사람이 사건에 관여함. 취소·거절·불참·연기('못 갈 것 같아', '어렵습니다')도 관여 사실로 기록한다."""

    participant_role: ParticipantRole | None = Field(
        None,
        description=(
            "당사자(사건의 주체) / 상대(사건이 향하는 사람) / 요청자(부탁·지시한 사람) / "
            "수행자(부탁받아 해내기로 한 사람) / 목격자 / 관련자(그 외). 이 여섯 값 중 하나만 쓴다. "
            "예: '발표를 맡아달라고 부탁했다' → 부탁한 박지현은 요청자, 부탁받은 김민수는 수행자. "
            "'못 갈 것 같아' → 불참하는 김민수는 당사자, 듣는 최유진은 상대"
        ),
    )
    feeling: str | None = Field(None, description="이 사람이 사건에 대해 드러낸 감정 (드러나지 않으면 비움)")


ENTITY_TYPES: dict[str, type[BaseModel]] = {"Person": Person, "Event": Event}
EDGE_TYPES: dict[str, type[BaseModel]] = {
    "Relationship": Relationship,
    "Involved": Involved,
}
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {
    ("Person", "Person"): ["Relationship"],
    ("Person", "Event"): ["Involved"],
}

# 2026-11-08 36개 수집 결과로 보강 (graph/graph-spike-report.md): 관계 유형 흔들림, 호칭 오귀속,
# participant_role 값 혼재, 취소·불참 사실 누락, 시간대 혼재.
EXTRACTION_INSTRUCTIONS = """\
한국어 메신저 대화다. 다음 규칙을 따른다.

[메시지 형식]
- 메시지는 `[YYYY-MM-DD 요일 HH:MM] 발화자 → 수신자: 내용` 형식이다. 대괄호 안이 한국 시간 발화 시각과
  요일이고, 화살표 뒤가 이 말을 듣는 사람이다. 1:1 대화라 수신자는 항상 발화자가 아닌 다른 사람이다.
- 짧은 답('네', '좋아 내가 날짜 잡을게')도 발화자가 수신자에게 한 말이다. 발화자와 수신자 사이의
  관계·사건 관여 사실로 적고, 발화자 혼자를 양 끝으로 하는 사실(자기참조)은 만들지 않는다.

[엔티티]
- 같은 사람을 가리키는 호칭(예: '박지현', '지현 팀장님', '팀장님')은 하나의 Person으로 본다.
  엔티티 이름은 성과 이름이 드러난 가장 완전한 실명을 쓴다.
- 사람(Person)과 사건(Event)만 엔티티로 만든다. 음식·문서·물건(국밥, 케이크, 회의록, API 문서)은
  엔티티로 만들지 않고, 그것이 등장하는 사건(점심 약속, 회의록 공유, 문서 정리 요청)을 Event로 만든다.
- 사건 이름은 '대시보드 개편', '홈커밍 참석', '파트장 발령'처럼 짧은 명사구로 쓴다.

[관계·말투·호칭]
- 존댓말/반말, 호칭은 실제 발화에서 확인될 때만 기록하고 추측하지 않는다. 말투는 발화자의 문장 어미로
  판단한다('죄송합니다'는 존댓말, '미안'은 반말).
- 호칭(address_term)은 source가 target을 직접 부른 말만 적는다. 대화 속 제3자 호칭은 적지 않는다.
- 엣지 이름은 사람↔사람이면 Relationship, 사람↔사건이면 Involved만 쓴다. RELATES_TO, WORKS_FOR,
  PLANS_TO_MEET 같은 다른 이름을 만들지 않는다. 사람↔사물 관계는 만들지 않는다.
- 한 쌍의 relation_type은 하나다. 관계가 바뀌면(예: 동기 → 상사, 반말 → 존댓말) 새 사실로 기록하고
  이전 관계 사실은 더 이상 유효하지 않다. 바뀌지 않았으면 이전 사실의 relation_type을 유지한다.
- 상하 관계는 relation_type이 아니라 hierarchy에 적는다. 기준은 항상 source에서 본 target의 위치다:
  김민수→박지현(팀장)은 target_higher, 박지현→김민수는 target_lower, 동기·친구는 equal.

[사건]
- 취소·거절·불참·연기('못 갈 것 같아', '이번 주는 어렵습니다')도 반드시 사건 관여 사실로 기록한다.
  이전 계획(참석 예정, 금요일 완료)과 모순되면 이전 사실은 끝난 것으로 본다.
- 부탁·약속·일정 확정도 각각 사실로 남긴다. 짧은 답('일단 알겠어요', '좋아 내가 날짜 잡을게')도
  앞 메시지의 사건에 대한 관여 사실이다.

[시간]
- 상대 시간('어제', '다음 주 수요일', '금요일까지')은 메시지 앞 대괄호의 날짜·요일을 기준으로 계산해
  사실 문장 안에 'YYYY-MM-DD'로 함께 적는다. '다음 주'는 메시지가 속한 주(월~일)의 바로 다음 주다.
  예: [2026-10-05 월] 기준 '다음 주 수요일' = 2026-10-14, '이번 주 금요일' = 2026-10-09,
  [2026-10-09 금] 기준 '다음 주 토요일' = 2026-10-17.
- valid_at·invalid_at 출력은 REFERENCE_TIME과 같은 UTC(Z) 형식으로 쓴다. 메시지 시각을 그대로
  옮기지 말고 REFERENCE_TIME 값을 쓴다.

[금지]
- 성격·건강·정치 성향 등 민감한 특성은 추정하지 않는다.
"""
