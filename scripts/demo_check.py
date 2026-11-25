"""시연 흐름 회귀 검사 — `docs/guides/demo-script.md`의 장면이 끝까지 이어지는지 모델 없이 본다.

떠 있는 서버(가짜 모델)에 임시 계정 하나로 시연 장면 0~5를 순서대로 태운다. 표준 라이브러리만 쓴다.

    python3 scripts/demo_check.py --base-url http://web.localhost:8101
    python3 scripts/demo_check.py --base-url https://localhost --insecure --invite-code <코드> --assume-fake

같은 장면을 pytest도 돈다(`backend/tests/test_demo_flow.py` — 서버 없이 메모리 저장소로, CI와
`make demo-check-docker`). 장면 함수는 이 파일 하나에만 있고, 스크립트와 테스트는 전송 계층만 다르다.

장면 (실패하면 어느 장면의 어느 단계인지와 응답 코드·오류 `code`를 찍는다. 응답 본문은 찍지 않는다)
  0   헬스·화면 셸·가입(초대 코드·동의)·로그아웃 → 로그인·CSRF
  준비 상대 4명(상사·동료·영어 상대·말투가 변한 지인) → 말투 설정 → 카카오톡 PC .csv 꼴의 합성 대화
      가져오기(미리보기 → 화자 지정 → 저장 → 말투 분석 완료 대기 → 대화방 통계·화자 목록)
  1   상사에게 B → C (추천문·바꾼 이유·주의·지킨 항목 꼴, 같은 키 재요청, C의 참고 표현과 그 원문)
  2   동료에게 C → D (그래프가 꺼진 서버면 D = C)
  3   영어 상대에게 자동 조건 → D
  4   말투가 변한 지인의 제안 카드(보기만으로는 저장되지 않는다 → 저장하면 버전이 오른다), 맥락 그래프 조회
  5   피드백 저장 → 이력 → 사용량 → 내 데이터 요약 → 내려받기
  끝   계정 삭제 → 삭제 뒤 세션 무효 (중간에 실패해도 지우려고 시도한다)

**이 검사가 보지 못하는 것.** 가짜 모델은 초안을 그대로 돌려준다. 그래서 문장이 실제로 다듬어지는지, 주의
문구("이전에 57,200원으로…", "14일 오전으로 안내하신 적이…")가 뜨는지, 검증 실패 → 다시 만들기 → 원문 유지,
조건 C와 D의 차이는 **보지 못한다**. 그래프가 꺼진 서버에서는 D가 C와 같은 경로다. 화면(단추·배치·복사)도
보지 않는다 — 화면은 `scripts/demo_ui/`의 선택 도구가 본다. 여기서 보는 것은 경로뿐이다: 각 단계가 오류
없이 다음 단계로 넘어가고, 응답에 화면이 읽는 항목이 있다. 품질은 실제 모델 리허설(대본)로 확인한다.

**실제 모델 서버에는 그냥 돌리지 않는다.** 서버가 가짜 모델이라고 확인되지 않으면 시작하지 않는다(첨삭 7회와
가져온 메시지 90여 건의 그래프 수집이 실제 호출로 나간다). 운영 모드 서버는 `/healthz`가 모델을 알려 주지
않는다 — 가짜 모델로 띄운 것을 알고 있으면 `--assume-fake`, 비용을 알고도 돌리려면 `--allow-real-model`.

대화 내용·이름은 전부 합성이다(실물 대화는 저장소에 넣지 않는다). 꼴만 시연 계정과 같다.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

FAKE_MODEL = "fake-echo"
ME = "윤가람"
SCENE_ORDER = ("0", "준비", "1", "2", "3", "4", "5", "끝")
SCENE_TITLES = {
    "0": "가입·로그인",
    "준비": "상대·설정·가져오기",
    "1": "상사 B → C",
    "2": "동료 C → D",
    "3": "영어 상대",
    "4": "말투 변화 제안",
    "5": "이력·내 데이터",
    "끝": "계정 삭제",
}
LIMITS = (
    "보지 못한 것: 문장이 실제로 다듬어지는지, 주의 문구, 검증 실패 뒤 다시 만들기·원문 유지, 조건 C와 D의 "
    "차이(가짜 모델은 초안을 그대로 돌려준다). 화면은 scripts/demo_ui/, 품질은 실제 모델 리허설로 확인한다."
)

# ---- 합성 대화 -------------------------------------------------------------------------------------
# (시각, 화자, 내용). 화자는 "me" · "them" · 그 밖의 이름(단체방의 다른 사람) · "system"(카카오톡이 넣는 줄).
Turn = tuple[str, str, str]

BOSS_TURNS: list[Turn] = [
    ("10-05 09:10", "system", "정해원 상무님이 윤가람님을 초대했습니다."),
    ("10-05 09:12", "them", "가온식품 견적서는 어디까지 됐나요?"),
    ("10-05 09:15", "me", "상무님, 초안은 작성했습니다. 오늘 오후 4시까지 공유드리겠습니다."),
    ("10-05 09:16", "them", "단가는 VAT 포함 57,200원으로 적어 주세요."),
    ("10-05 09:18", "me", "네, VAT 포함 57,200원으로 반영하겠습니다."),
    ("10-05 16:02", "me", "상무님, 견적서 초안 공유드립니다. 검토 부탁드립니다."),
    ("10-05 16:03", "me", "사진"),
    ("10-05 16:40", "them", "납기 조건이 빠졌네요. 추가해 주세요."),
    ("10-05 16:45", "me", "확인했습니다. 납기 조건을 넣어 다시 올리겠습니다."),
    ("10-06 10:20", "me", "상무님, 수정본 올렸습니다.\n납기는 발주 후 3주로 적었습니다."),
    ("10-06 10:31", "them", "좋습니다. 내일 회의 전에 요약본도 주세요."),
    ("10-06 10:33", "me", "알겠습니다. 요약본은 내일 오전 9시까지 드리겠습니다."),
    ("10-07 08:52", "me", "상무님, 요약본 보내드립니다."),
    ("10-07 09:05", "them", "받았습니다. 회의는 10시 그대로입니다."),
    ("10-07 09:06", "me", "네, 10시에 회의실로 가겠습니다."),
    ("10-12 14:10", "them", "거래처 방문 일정은 정해졌나요?"),
    ("10-12 14:14", "me", "아직 답을 받지 못했습니다. 오늘 중으로 다시 확인하겠습니다."),
    ("10-12 17:30", "me", "상무님, 방문은 10월 20일 오후 2시로 확정되었습니다."),
    ("10-12 17:41", "them", "그날 저는 참석이 어렵습니다. 결과만 공유해 주세요."),
    ("10-12 17:43", "me", "알겠습니다. 방문 뒤 당일에 결과 정리해서 보고드리겠습니다."),
    ("10-20 18:05", "me", "상무님, 방문 결과 보고드립니다. 2차 견적 요청을 받았습니다."),
    ("10-20 18:20", "them", "수고했습니다. 2차 견적은 다음 주 수요일까지 준비합시다."),
    ("10-20 18:22", "me", "네, 다음 주 수요일까지 준비하겠습니다."),
]

COLLEAGUE_TURNS: list[Turn] = [
    ("10-28 13:02", "them", "가람님, 1차 입고분 검수 끝났어요?"),
    ("10-28 13:05", "me", "네, 오전에 끝났어요. 불량은 3박스였습니다."),
    ("10-28 13:06", "백도윤", "창고 자리는 제가 비워 둘게요."),
    ("10-28 13:07", "them", "3박스는 교환 요청할게요."),
    ("10-28 13:09", "me", "고맙습니다. 교환 수량은 제가 메일로 정리해 둘게요."),
    ("11-03 10:11", "them", "2차 생산 일정은 어떻게 돼요?"),
    ("11-03 10:15", "me", "공장에 물어봤는데 다음 주 금요일쯤 될 것 같아요."),
    ("11-03 10:16", "백도윤", "지게차 예약은 금요일로 잡을까요?"),
    ("11-03 10:18", "me", "확정되면 바로 알려 드릴게요."),
    ("11-07 15:30", "me", "과장님, 2차 생산 완료는 11월 14일 오전으로 안내받았습니다."),
    ("11-07 15:32", "them", "그럼 입고는 14일 오후로 잡을게요."),
    ("11-07 15:33", "me", "네, 14일 오후 입고로 진행하겠습니다."),
    ("11-07 15:34", "system", ""),  # 삭제된 메시지 행
    ("11-07 15:40", "them", "운송은 어느 쪽에서 부르죠?"),
    ("11-07 15:42", "me", "이번에는 저희 쪽에서 부를게요. 비용은 나중에 정산해요."),
    ("11-10 09:20", "them", "포장 사양이 바뀌었다던데 맞아요?"),
    ("11-10 09:24", "me", "맞아요. 박스당 24개에서 20개로 바뀌었습니다."),
    ("11-10 09:25", "백도윤", "그럼 팔레트 수도 다시 계산해야겠네요."),
    ("11-10 09:27", "me", "팔레트는 제가 다시 계산해서 공유드릴게요."),
    ("11-12 17:02", "them", "혹시 14일 일정에 변동 있어요?"),
    ("11-12 17:10", "me", "아직은 없어요. 내일 오전에 공장에 한 번 더 확인해 볼게요."),
    ("11-12 17:11", "them", "네 부탁드려요."),
    ("11-13 11:45", "me", "과장님, 공장에서 자재가 늦는다고 연락이 왔습니다. 오후에 다시 말씀드릴게요."),
    ("11-13 11:47", "them", "알겠어요. 기다릴게요."),
]

ENGLISH_TURNS: list[Turn] = [
    ("09-22 22:05", "them", "Hi Garam, did you get a chance to review the agreement draft?"),
    ("09-22 22:20", "me", "Hi Daniel, yes. I have two comments on the payment terms."),
    ("09-22 22:21", "me", "I'll send them over by tomorrow morning."),
    ("09-23 08:40", "me", "Daniel, please find my comments attached. Section 4 needs a second look."),
    ("09-23 21:15", "them", "Thanks. We can accept net 45 instead of net 30."),
    ("09-23 21:30", "me", "That works for us. I'll update the draft and share it by Thursday."),
    ("09-25 17:02", "me", "The revised draft is ready. Could you confirm the delivery schedule?"),
    ("09-25 23:48", "them", "Confirmed. First shipment of 4,500 pcs in the week of October 12."),
    ("09-26 09:05", "me", "Great, thank you. We'll prepare the warehouse for 4,500 pcs."),
    ("10-01 22:10", "them", "Are you still planning to sign in person in Chicago on October 15?"),
    ("10-01 22:25", "me", "Yes, that's the plan. I'll book my flight this week."),
    ("10-06 08:30", "me", "I've booked the flight. I arrive in Chicago on October 14."),
    ("10-06 21:55", "them", "Perfect. Let's meet at our office at 10am on the 15th."),
    ("10-06 22:03", "me", "Sounds good. See you at 10am on the 15th."),
    ("10-09 18:40", "me", "Daniel, a quick heads-up: our legal team is still reviewing clause 7."),
    ("10-09 22:12", "them", "Understood. Let me know if anything changes."),
    ("10-09 22:15", "me", "Will do. I'll keep you posted."),
]

# 해요체로 쓰다가 10/18부터 합쇼체로 바뀐다(동호회 지인이 거래 상대가 됐다).
CHANGED_TURNS: list[Turn] = [
    ("09-14 19:02", "them", "가람 씨, 지난주에 본 장비 어땠어요?"),
    ("09-14 19:05", "me", "생각보다 괜찮았어요. 가격만 맞으면 사고 싶어요."),
    ("09-14 19:06", "them", "공동구매로 하면 더 싸게 살 수 있대요."),
    ("09-14 19:08", "me", "그럼 저도 같이 할게요."),
    ("09-20 11:30", "me", "태호 씨, 이번 주 모임은 토요일 맞아요?"),
    ("09-20 11:34", "them", "네 토요일 2시예요."),
    ("09-20 11:35", "me", "알겠어요. 조금 늦을 수도 있어요."),
    ("09-27 14:12", "them", "공동구매 인원이 다 찼어요."),
    ("09-27 14:15", "me", "잘됐네요. 입금은 언제까지 하면 돼요?"),
    ("09-27 14:16", "them", "다음 주 수요일까지요."),
    ("09-27 14:18", "me", "네, 그 전에 보낼게요."),
    ("10-04 10:02", "me", "입금했어요. 확인해 주세요."),
    ("10-04 10:10", "them", "확인했어요. 고마워요."),
    ("10-11 16:20", "me", "장비는 언제쯤 도착해요?"),
    ("10-11 16:25", "them", "다음 주에 온대요. 그리고 저 이번에 총판 대표를 맡게 됐어요."),
    ("10-11 16:27", "me", "와, 축하드려요. 잘됐네요."),
    ("10-18 09:30", "me", "문 대표님, 저희 회사에서 장비 12대 견적을 요청드립니다."),
    ("10-18 09:41", "them", "네, 오늘 중으로 견적서 보내 드리겠습니다."),
    ("10-18 09:43", "me", "감사합니다. 받는 대로 검토하겠습니다."),
    ("10-19 13:05", "me", "견적서 잘 받았습니다. 납기 일정도 알려 주시면 감사하겠습니다."),
    ("10-19 13:20", "them", "납기는 발주 후 2주입니다."),
    ("10-19 13:22", "me", "확인했습니다. 내부 결재 뒤에 다시 연락드리겠습니다."),
]


@dataclass(frozen=True)
class ContactPlan:
    key: str
    alias: str
    relationship_type: str
    kakao_name: str  # 내보내기 파일에 적힌 상대 이름 (별칭과 다를 수 있다)
    title: str
    turns: list[Turn]
    persona: dict[str, Any] | None  # 기본 설정에 덮어쓸 값. None이면 기본 설정 그대로 둔다
    year: int = 2026


# 시연 계정과 같은 꼴: 상사 · 동료(단체방에서 한 사람만) · 영어 상대 · 말투가 변한 지인
PLANS: tuple[ContactPlan, ...] = (
    ContactPlan(
        "boss",
        "정상무님",
        "superior",
        "정해원 상무",
        "정해원 상무",
        BOSS_TURNS,
        {"formality": 5, "address_term": "상무님", "forbidden_phrases": ["ㅋㅋ", "ㅠ"]},
    ),
    ContactPlan(
        "colleague",
        "오과장",
        "colleague",
        "오세린 과장",
        "입고 일정방",
        COLLEAGUE_TURNS,
        {"address_term": "과장님"},
    ),
    ContactPlan(
        "english",
        "Daniel Reed",
        "acquaintance",
        "Daniel Reed",
        "Daniel Reed",
        ENGLISH_TURNS,
        {"address_term": "Daniel"},
    ),
    # 말투 설정을 손대지 않는다 — 과거 대화에서 읽은 변화가 제안으로 나와야 한다
    ContactPlan("changed", "문태호", "acquaintance", "문태호", "문태호", CHANGED_TURNS, None),
)

DRAFTS = {
    "boss": (
        "가온식품 견적 단가는 VAT 별도 52,000원으로 고쳤어요. 요약본은 내일 오전 8시 반까지 드릴게요.",
        "보고",
    ),
    "colleague": ("2차 입고는 11월 14일은 어렵고 11월 21일까지는 가능합니다.", "일정 조율"),
    "english": ("We can't sign the agreement on October 15. I'll send the revised draft by Friday.", ""),
}


def speaker_name(plan: ContactPlan, who: str) -> str:
    return {"me": ME, "them": plan.kakao_name}.get(who, who)


def kakao_csv(plan: ContactPlan) -> str:
    """카카오톡 PC '대화 내보내기'의 .csv 꼴(BOM, CRLF, 따옴표 안 줄바꿈, 초대·사진·삭제된 메시지 행)."""
    lines = ["﻿Date,User,Message"]
    for at, who, text in plan.turns:
        if who == "system" and not text:
            lines.append(',"","메시지가 삭제되었습니다."')
            continue
        moment = datetime.strptime(f"{plan.year}-{at}", "%Y-%m-%d %H:%M")
        name = plan.kakao_name if who == "system" else speaker_name(plan, who)
        body = text.replace('"', '""').replace("\n", "\r\n")
        lines.append(f'{moment:%Y-%m-%d %H:%M}:{len(lines) % 60:02d},"{name}","{body}"')
    return "\r\n".join(lines) + "\r\n"


def expected_counts(plan: ContactPlan) -> dict[str, int]:
    """파서가 메시지로 읽어야 하는 수: 화자 이름별. 초대·삭제 행과 `사진`은 메시지가 아니다."""
    counts: dict[str, int] = {}
    for _at, who, text in plan.turns:
        if who == "system" or text == "사진":
            continue
        name = speaker_name(plan, who)
        counts[name] = counts.get(name, 0) + 1
    return counts


def speaker_map(plan: ContactPlan) -> dict[str, str]:
    """화자 지정: 나 · 상대 · 그 밖의 사람은 뺀다(단체방에서 한 사람만 — 시연 계정의 `skip`과 같다)."""
    roles = {ME: "me", plan.kakao_name: "contact"}
    return {name: roles.get(name, "skip") for name in expected_counts(plan)}


def dump_data(directory: Path) -> list[Path]:
    """합성 대화를 파일로 쓴다: 상대마다 카카오톡 .csv 하나 + `manifest.json`(상대·화자 지정·초안).

    화면 검사(`scripts/demo_ui/demo_ui_check.cjs`)가 같은 자료를 화면에 올리려고 읽는다.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    contacts: list[dict[str, Any]] = []
    for plan in PLANS:
        path = directory / f"KakaoTalk_{plan.key}.csv"
        path.write_bytes(kakao_csv(plan).encode("utf-8"))  # 줄바꿈(CRLF)을 그대로 둔다
        written.append(path)
        counts = expected_counts(plan)
        roles = speaker_map(plan)
        draft, situation = DRAFTS.get(plan.key, ("", ""))
        contacts.append(
            {
                "key": plan.key,
                "alias": plan.alias,
                "relationship_type": plan.relationship_type,
                "file": path.name,
                "speakers": roles,
                "messages": sum(n for name, n in counts.items() if roles[name] != "skip"),
                "my_messages": counts[ME],
                "draft": draft,
                "situation": situation,
            }
        )
    manifest = directory / "manifest.json"
    manifest.write_text(
        json.dumps({"me": ME, "contacts": contacts}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return [*written, manifest]


# ---- 실행기 ---------------------------------------------------------------------------------------
class StepError(Exception):
    """단계 실패. 메시지는 그대로 찍힌다 — 응답 본문(대화·초안)을 넣지 않는다."""


class SkippedError(Exception):
    """앞 단계가 실패해 할 수 없는 단계."""


def describe(response: Any) -> str:
    """오류 응답을 `HTTP 422 code=invalid_input` 꼴로. 서버가 쓴 문장은 싣지 않는다(입력이 섞일 수 있다)."""
    data = response.json()
    code = None
    if isinstance(data, dict):
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else data
        code = detail.get("code")
    return f"HTTP {response.status}" + (f" code={code}" if code else "")


def need(data: Any, keys: tuple[str, ...], what: str) -> None:
    """화면이 읽는 항목이 응답에 있는가."""
    if not isinstance(data, dict):
        raise StepError(f"{what}: 객체가 아닙니다 ({type(data).__name__})")
    missing = [k for k in keys if k not in data]
    if missing:
        raise StepError(f"{what}: 빠진 항목 {', '.join(missing)}")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise StepError(message)


@dataclass
class Report:
    passed: int = 0
    failed: list[str] = field(default_factory=list)  # "장면 1 · B 첨삭 — HTTP 422 code=…"
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failed and not self.skipped

    def failed_scenes(self) -> list[str]:
        seen = [line.split(" · ")[0].removeprefix("장면 ") for line in self.failed]
        return [s for s in SCENE_ORDER if s in seen]


class DemoCheck:
    """`client`는 `request(method, path, body=None, *, csrf=True, headers=None)`와 `csrf` 속성을 가진 것.

    응답은 `status`·`json()`·`headers.get()`을 가진다(`smoke_test.Client`, 테스트의 TestClient 어댑터).
    """

    RESULT_KEYS = (
        "status",
        "result_text",
        "change_reasons",
        "warnings",
        "evidence_ids",
        "preserved_items",
        "validation",
        "attempts",
        "attempt_log",
        "model_version",
        "prompt_version",
        "retrieval_version",
        "token_usage",
        "latency_ms",
    )

    def __init__(
        self,
        client: Any,
        *,
        invite_code: str = "",
        assume_fake: bool = False,
        allow_real_model: bool = False,
        keep_account: bool = False,
        index_wait: float = 120.0,
        poll_seconds: float = 0.5,
        out: Callable[[str], None] = print,
    ) -> None:
        self.client = client
        self.invite_code = invite_code
        self.assume_fake = assume_fake
        self.allow_real_model = allow_real_model
        self.keep_account = keep_account
        self.index_wait = index_wait
        self.poll_seconds = poll_seconds
        self.out = out
        self.report = Report()
        self.username = f"democheck-{secrets.token_hex(4)}"
        self.password = secrets.token_urlsafe(18)
        self.registered = False
        self.deleted = False
        self.graph: bool | None = None  # 서버가 그래프를 쓰는가 (대화방 통계가 알려 준다)
        self.contacts: dict[str, dict[str, Any]] = {}
        self.conversations: dict[str, str] = {}
        self.ready: set[str] = set()  # 가져오기까지 끝난 상대
        self.rewrites: dict[str, dict[str, Any]] = {}  # "boss:B" → 응답
        self._scene = "0"

    # ---- 출력·단계 ----
    def _line(self, mark: str, name: str, detail: str, started: float | None = None) -> None:
        took = f"  ({int((time.perf_counter() - started) * 1000)}ms)" if started is not None else ""
        self.out(f"[{mark}] 장면 {self._scene} · {name}" + (f" — {detail}" if detail else "") + took)

    def warn(self, name: str, detail: str) -> None:
        self.report.warnings.append(f"장면 {self._scene} · {name} — {detail}")
        self._line("주의", name, detail)

    def step(self, name: str, fn: Callable[[], str | None]) -> bool:
        started = time.perf_counter()
        label = f"장면 {self._scene} · {name}"
        try:
            detail = fn() or ""
        except SkippedError as exc:
            self.report.skipped.append(f"{label} — {exc}")
            self._line("건너뜀", name, str(exc))
            return False
        except StepError as exc:
            self.report.failed.append(f"{label} — {exc}")
            self._line("실패", name, str(exc), started)
            return False
        except Exception as exc:  # noqa: BLE001 - 예상 못 한 응답 모양도 실패로 보고하고 다음 단계로
            # 예외 문장에는 응답 내용이 섞일 수 있다 — 종류만 남긴다 (빠진 항목 이름은 우리가 쓴 키라 괜찮다)
            hint = f" {exc.args[0]!r}" if isinstance(exc, KeyError) and exc.args else ""
            self.report.failed.append(f"{label} — {type(exc).__name__}{hint}")
            self._line("실패", name, f"{type(exc).__name__}{hint}", started)
            return False
        self.report.passed += 1
        self._line(" OK ", name, detail, started)
        return True

    def call(self, method: str, path: str, body: Any = None, *, expect: tuple[int, ...], **kw: Any) -> Any:
        """요청 하나. 기대한 상태 코드가 아니면 StepError. 요청 제한(429)이면 한 번 기다렸다 다시 보낸다."""
        response = self.client.request(method, path, body, **kw)
        if response.status == 429 and 429 not in expect:
            data = response.json()
            if not (isinstance(data, dict) and data.get("code") == "usage_exceeded"):
                wait = min(float(response.headers.get("Retry-After") or 5), 65.0)
                self.out(f"       요청 제한(429) — {wait:.0f}초 기다렸다 다시 보냅니다")
                time.sleep(wait)
                response = self.client.request(method, path, body, **kw)
        if response.status not in expect:
            raise StepError(describe(response))
        return response

    def get(self, path: str) -> Any:
        return self.call("GET", path, expect=(200,)).json()

    def contact(self, key: str) -> dict[str, Any]:
        if key not in self.ready:
            raise SkippedError(f"준비 단계에서 '{key}' 상대가 만들어지지 않았습니다")
        return self.contacts[key]

    # ---- 장면 0 ----
    def health(self) -> str:
        data = self.get("/healthz")
        check(isinstance(data, dict) and data.get("status") == "ok", "status가 ok가 아닙니다")
        provider = data.get("llm_provider")
        self.graph = data.get("graph") if isinstance(data.get("graph"), bool) else None
        if provider == "fake" or self.assume_fake or self.allow_real_model:
            return f"모델={provider or '알 수 없음(운영 모드)'} 저장소={data.get('storage', '?')}"
        why = f"서버의 모델이 '{provider}'입니다" if provider else "운영 모드 서버라 모델을 알 수 없습니다"
        raise StepError(
            f"{why}. 이 검사는 가짜 모델 서버용입니다 — 가짜 모델로 띄운 서버면 --assume-fake, "
            "실제 호출 비용을 알고 돌리려면 --allow-real-model"
        )

    def shell(self) -> str:
        page = self.call("GET", "/", expect=(200,), headers={"Accept": "text/html"})
        check("text/html" in (page.headers.get("Content-Type") or ""), "/ 가 HTML이 아닙니다")
        for path in ("/static/js/main.js", "/static/css/tokens.css"):
            self.call("GET", path, expect=(200,), headers={"Accept": "*/*"})
        return "/, main.js, tokens.css"

    def signup(self) -> str:
        config = self.get("/v1/auth/config")
        need(config, ("allow_register", "invite_required", "require_consent"), "가입 설정")
        check(config["allow_register"] is not False, "가입이 닫힌 서버입니다 (AUTH_ALLOW_REGISTER=false)")
        if config["invite_required"] and not self.invite_code:
            raise StepError("초대 코드가 필요한 서버입니다 (--invite-code)")
        body: dict[str, Any] = {"username": self.username, "password": self.password, "consent": True}
        if self.invite_code:
            body["invite_code"] = self.invite_code
        data = self.call("POST", "/v1/auth/register", body, expect=(201,)).json()
        self.registered = True
        need(data, ("user_id", "username", "csrf_token"), "가입 응답")
        self.client.csrf = data["csrf_token"]
        return f"임시 계정 {self.username} (초대 코드 {'필요' if config['invite_required'] else '없음'})"

    def relogin(self) -> str:
        if not self.registered:
            raise SkippedError("가입이 되지 않았습니다")
        self.call("POST", "/v1/auth/logout", expect=(204,))
        check(self.client.request("GET", "/v1/auth/me").status == 401, "로그아웃 뒤에도 세션이 살아 있습니다")
        body = {"username": self.username, "password": self.password}
        data = self.call("POST", "/v1/auth/login", body, expect=(200,)).json()
        need(data, ("csrf_token",), "로그인 응답")
        self.client.csrf = data["csrf_token"]
        me = self.get("/v1/auth/me")
        check(me.get("username") == self.username, "다른 사용자가 돌아왔습니다")
        return "로그아웃 → 로그인 → 내 정보"

    def csrf_enforced(self) -> str:
        if not self.registered:
            raise SkippedError("가입이 되지 않았습니다")
        body = {"alias": "막혀야 함", "relationship_type": "colleague"}
        status = self.client.request("POST", "/v1/contacts", body, csrf=False).status
        check(status == 403, f"CSRF 토큰 없는 쓰기가 거부되지 않았습니다: HTTP {status}")
        return "토큰 없는 쓰기는 403"

    # ---- 준비 ----
    def create_contact(self, plan: ContactPlan) -> str:
        if not self.registered:
            raise SkippedError("가입이 되지 않았습니다")
        # 화면과 같은 요청: 관계 유형 기본 설정으로 말투 설정 v1을 함께 만든다
        body = {"alias": plan.alias, "relationship_type": plan.relationship_type, "use_default_persona": True}
        data = self.call("POST", "/v1/contacts", body, expect=(201,)).json()
        need(data, ("id", "alias", "relationship_type", "persona_version"), "상대")
        check(data["persona_version"] == 1, "기본 말투 설정(v1)이 함께 만들어지지 않았습니다")
        self.contacts[plan.key] = data
        if plan.persona is None:
            return "말투 설정 v1 (기본값 그대로)"
        current = self.get(f"/v1/contacts/{data['id']}/persona")
        need(current, ("version", "settings"), "말투 설정")
        body = {"settings": {**current["settings"], **plan.persona}, "expected_version": current["version"]}
        saved = self.call("PUT", f"/v1/contacts/{data['id']}/persona", body, expect=(200,)).json()
        check(saved.get("version") == 2, "말투 설정 버전이 2가 아닙니다")
        return f"말투 설정 v2 (호칭 {plan.persona.get('address_term')})"

    def import_conversation(self, plan: ContactPlan) -> str:
        if plan.key not in self.contacts:
            raise SkippedError("상대가 만들어지지 않았습니다")
        contact_id = self.contacts[plan.key]["id"]
        text = kakao_csv(plan)
        expected = expected_counts(plan)

        # 1) 미리보기 — 저장하지 않는다
        preview = self.call(
            "POST", "/v1/imports/parse", {"format": "auto", "text": text}, expect=(200,)
        ).json()
        need(preview, ("format", "variant", "messages_total", "speakers", "sample", "problems"), "미리보기")
        check(
            (preview["format"], preview["variant"]) == ("kakao", "csv"),
            f"카카오톡 .csv로 읽지 않았습니다: {preview['format']}/{preview['variant']}",
        )
        seen = {s["name"]: s["count"] for s in preview["speakers"]}
        check(
            seen == expected,
            f"화자별 건수가 다릅니다: 읽음 {sorted(seen.values())}, 기대 {sorted(expected.values())}",
        )
        check(not preview["problems"], f"미리보기에 문제 {len(preview['problems'])}건")

        # 2) 대화방 → 화자 지정대로 저장
        body = {"contact_id": contact_id, "title": plan.title, "source": "kakao", "me_name": ME}
        conv = self.call("POST", "/v1/conversations", body, expect=(201,)).json()
        self.conversations[plan.key] = conv["id"]
        roles = speaker_map(plan)
        payload = {"format": preview["format"], "text": text, "speaker_map": roles}
        path = f"/v1/conversations/{conv['id']}/imports/raw"
        job = self.call("POST", path, payload, expect=(202,)).json()
        need(job, ("id", "status", "imported", "duplicates", "episodes", "graph_status"), "가져오기 작업")
        kept = sum(n for name, n in expected.items() if roles[name] != "skip")
        check(job["imported"] == kept, f"가져온 수가 다릅니다: {job['imported']} (기대 {kept})")

        # 3) 말투 분석(묶음 생성)이 끝날 때까지 작업 상태를 다시 읽는다
        deadline = time.monotonic() + self.index_wait
        polls = 0
        while job["status"] == "processing" and time.monotonic() < deadline:
            time.sleep(self.poll_seconds)
            polls += 1
            job = self.get(f"/v1/imports/{job['id']}")
        check(job["status"] != "processing", f"말투 분석이 {self.index_wait:.0f}초 안에 끝나지 않았습니다")
        check(job["status"] == "completed", f"말투 분석 실패: {job['status']} {job.get('error_code') or ''}")
        check(job["episodes"] >= 1, "말투 묶음이 만들어지지 않았습니다")

        # 4) 가져오기 화면·내 데이터 탭이 읽는 값
        stats = self.get(f"/v1/conversations/{conv['id']}/stats")
        need(stats, ("messages", "my_messages", "episodes", "graph_enabled", "latest_job"), "대화방 통계")
        mine = expected[ME]
        check(
            (stats["messages"], stats["my_messages"]) == (kept, mine),
            f"통계가 다릅니다: 전체 {stats['messages']}/{kept}, 내 메시지 {stats['my_messages']}/{mine}",
        )
        self.graph = bool(stats["graph_enabled"])
        speakers = self.get(f"/v1/conversations/{conv['id']}/speakers")
        got = {(s["name"], s["speaker_role"]): s["messages"] for s in speakers}
        want = {(n, roles[n]): c for n, c in expected.items() if roles[n] != "skip"}
        check(got == want, "저장된 화자·역할이 지정과 다릅니다")
        rooms = self.get(f"/v1/conversations?contact_id={contact_id}")
        check([r["id"] for r in rooms] == [conv["id"]], "상대의 대화방 목록이 다릅니다")

        self.ready.add(plan.key)
        skipped = len(expected) - len(want)
        return (
            f"메시지 {kept}건(내 {mine}건), 묶음 {job['episodes']}개, 그래프 {job['graph_status']}"
            + (f", 뺀 화자 {skipped}명" if skipped else "")
            + (f", 분석 대기 {polls}회" if polls else "")
        )

    # ---- 첨삭 ----
    def rewrite(self, key: str, condition: str, *, replay: bool = False) -> dict[str, Any]:
        contact = self.contact(key)
        draft, situation = DRAFTS[key]
        body = {
            "contact_id": contact["id"],
            "draft_text": draft,
            "situation": situation,
            "condition": condition,
        }
        headers = {"Idempotency-Key": f"democheck-{uuid.uuid4()}"}
        data = self.call("POST", "/v1/rewrites", body, expect=(201,), headers=headers).json()
        need(data, ("id", "contact_id", "condition", "created_at", "result"), "첨삭 응답")
        result = data["result"]
        need(result, self.RESULT_KEYS, "첨삭 결과")
        if not self.allow_real_model and result["model_version"] != FAKE_MODEL:
            raise StepError(f"가짜 모델이 아닙니다 (모델 {result['model_version']}) — 검사를 멈춥니다")
        check(bool(result["result_text"]), "추천 문장이 비어 있습니다")
        if result["status"] != "recommended":
            # 가짜 모델은 초안을 그대로 돌려주므로 검증에 걸릴 일이 없다
            check(self.allow_real_model, f"추천되지 않았습니다: {result['status']}")
            self.warn(f"{condition} 첨삭", "검증을 통과하지 못해 원문을 유지했습니다 (실제 모델)")
        for name in ("change_reasons", "warnings", "evidence_ids", "preserved_items", "attempt_log"):
            check(isinstance(result[name], list), f"{name}이 목록이 아닙니다")
        check(len(result["preserved_items"]) >= 2, "지킨 항목(날짜·수량·가능 여부·약속)이 뽑히지 않았습니다")
        for item in result["preserved_items"]:
            need(item, ("kind", "text", "normalized"), "지킨 항목")
        if result["model_version"] == FAKE_MODEL:
            check(bool(result["change_reasons"]), "바꾼 이유가 비어 있습니다")
        if replay:
            again = self.call("POST", "/v1/rewrites", body, expect=(201,), headers=headers).json()
            check(again["id"] == data["id"], "같은 Idempotency-Key 재요청이 다른 결과를 만들었습니다")
        self.rewrites[f"{key}:{condition}"] = data
        return data

    @staticmethod
    def _summary(data: dict[str, Any]) -> str:
        r = data["result"]
        kinds = sorted({item["kind"] for item in r["preserved_items"]})
        return (
            f"조건 {data['condition']}, 지킨 항목 {len(r['preserved_items'])}개({'·'.join(kinds)}), "
            f"참고 표현 {len(r['evidence_ids'])}개, 검색 {r['retrieval_version']}"
        )

    def plain(self, key: str, condition: str) -> str:
        """과거 표현을 쓰지 않는 조건(A·B): 참고 표현이 없어야 한다."""
        data = self.rewrite(key, condition, replay=True)
        check(data["condition"] == condition, f"적용된 조건이 {data['condition']}입니다")
        check(not data["result"]["evidence_ids"], "B 조건인데 참고한 과거 표현이 있습니다")
        return self._summary(data) + ", 같은 키 재요청은 같은 결과"

    def with_style(self, key: str, condition: str) -> str:
        """C: 같은 상대에게 쓴 내 과거 표현이 붙고, 그 원문을 다시 읽을 수 있어야 한다."""
        data = self.rewrite(key, condition)
        check(data["condition"] == condition, f"적용된 조건이 {data['condition']}입니다")
        return self._summary(data) + ", " + self._evidence(data)

    def _evidence(self, data: dict[str, Any]) -> str:
        result = data["result"]
        if result["model_version"] != FAKE_MODEL and not result["evidence_ids"]:
            return "참고 표현 없음(실제 모델은 인용하지 않을 수 있다)"
        check(len(result["evidence_ids"]) >= 1, "문체 예시가 붙지 않았습니다 (참고 표현 0개)")
        evidence = self.get(f"/v1/rewrites/{data['id']}/evidence")
        need(evidence, ("items", "missing", "refs_recorded"), "참고 표현 원문")
        check(len(evidence["items"]) >= 1 and evidence["missing"] == 0, "참고 표현의 원문을 읽지 못했습니다")
        messages = evidence["items"][0]["messages"]
        check(bool(messages), "참고 표현에 메시지가 없습니다")
        for message in messages:
            need(message, ("speaker_role", "sent_at", "text"), "참고 표현 메시지")
        check(any(m["speaker_role"] == "me" for m in messages), "참고 표현에 내 메시지가 없습니다")
        return f"원문 {len(messages)}건 읽음"

    def with_graph(self, key: str, style_key: str | None = None) -> str:
        """D: 그래프가 꺼진 서버면 C와 같은 검색, 켜진 서버면 그래프 검색이 더해진 경로."""
        data = self.rewrite(key, "D")
        check(data["condition"] == "D", f"적용된 조건이 {data['condition']}입니다")
        version = data["result"]["retrieval_version"]
        style = self.rewrites.get(style_key or "", {}).get("result", {}).get("retrieval_version")
        if self.graph:
            check(style is None or version != style, "그래프가 켜진 서버인데 D의 검색이 C와 같습니다")
            note = "그래프 검색이 더해짐"
        else:
            check(style is None or version == style, "그래프가 꺼진 서버인데 D의 검색이 C와 다릅니다")
            note = "그래프 꺼짐 → D = C"
        return self._summary(data) + f", {self._evidence(data)}, {note}"

    def auto(self, key: str) -> str:
        """화면의 기본값. 서버가 이 상대에 쓸 수 있는 가장 풍부한 조건을 고른다."""
        data = self.rewrite(key, "auto")
        check(
            data["condition"] in ("C", "D"), f"자동 조건이 {data['condition']}로 풀렸습니다 (기대 C 또는 D)"
        )
        if not self.graph:
            check(data["condition"] == "C", "그래프가 꺼진 서버인데 자동 조건이 D입니다")
        return self._summary(data) + " (자동)"

    # ---- 장면 4 ----
    def suggestion(self) -> str:
        contact = self.contact("changed")
        base = f"/v1/contacts/{contact['id']}/persona"
        before = self.get(base)
        data = self.get(f"{base}/suggestion")
        need(data, ("based_on_messages", "settings", "signals"), "제안")
        check("card" in data and data["card"] is not None, "제안 카드가 오지 않았습니다")
        card = data["card"]
        keys = ("contact_alias", "speech_previous", "speech_current", "address", "trigger", "scope_hints")
        need(card, (*keys, "ask", "ask_reason", "proposed", "two_valued"), "제안 카드")
        previous, current = card["speech_previous"], card["speech_current"]
        check(previous is not None and current is not None, "카드에 이전·지금 말투가 없습니다")
        need(current, ("level", "count", "message_ids", "since"), "지금 말투")
        check(previous["level"] != current["level"], "말투 변화를 읽지 못했습니다 (이전과 지금이 같다)")
        check(bool(current["message_ids"]), "근거 메시지가 없습니다")
        check(any(s.get("source") == "register" for s in data["signals"]), "말투 프로필 근거가 없습니다")
        # 보기만으로는 저장되지 않는다
        check(self.get(base)["version"] == before["version"], "제안을 보기만 했는데 설정이 바뀌었습니다")
        self._card = card
        self._persona = before
        return (
            f"{previous['level']}({previous['count']}건) → {current['level']}({current['count']}건, "
            f"{current['since']}부터), 묻기={card['ask']}, 제안 {card['proposed']}"
        )

    def accept_suggestion(self) -> str:
        contact = self.contact("changed")
        card = getattr(self, "_card", None)
        if card is None:
            raise SkippedError("제안 카드를 받지 못했습니다")
        before = self._persona
        # 카드가 값을 정해 주지 않고 물으면(ask) 사용자가 지금 말투를 고른 것으로 친다 — 격식 4
        proposed = {k: v for k, v in card["proposed"].items() if v is not None}
        settings = {**before["settings"], **(proposed or {"formality": 4})}
        body = {"settings": settings, "expected_version": before["version"]}
        saved = self.call("PUT", f"/v1/contacts/{contact['id']}/persona", body, expect=(200,)).json()
        check(saved["version"] == before["version"] + 1, "저장한 뒤 버전이 오르지 않았습니다")
        versions = self.get(f"/v1/contacts/{contact['id']}/persona/versions")
        check(len(versions) == saved["version"], "말투 설정 버전 목록이 다릅니다")
        return f"말투 설정 v{before['version']} → v{saved['version']}"

    def contact_graph(self) -> str:
        contact = self.contact("changed")
        data = self.get(f"/v1/contacts/{contact['id']}/graph")
        need(data, ("enabled", "center_id", "nodes", "edges"), "맥락 그래프")
        segments = (data.get("register") or {}).get("segments") or []
        check(len(segments) >= 2, "그래프 화면의 말투 구간이 둘보다 적습니다")
        return f"켜짐={data['enabled']}, 노드 {len(data['nodes'])}개, 말투 구간 {len(segments)}개"

    # ---- 장면 5 ----
    def feedback(self) -> str:
        if not self.rewrites:
            raise SkippedError("첨삭 결과가 없습니다")
        first, *rest = list(self.rewrites.values())
        body = {"accepted": True, "edited_text": None}
        self.call("POST", f"/v1/rewrites/{first['id']}/feedback", body, expect=(204,))
        if rest:  # 화면의 "마음에 안 들어요"
            body = {"accepted": False, "reason_code": "not_my_style"}
            self.call("POST", f"/v1/rewrites/{rest[-1]['id']}/feedback", body, expect=(204,))
        return f"{1 + bool(rest)}건"

    def history(self) -> str:
        if not self.rewrites:
            raise SkippedError("첨삭 결과가 없습니다")
        rows = self.get("/v1/rewrites?limit=50")
        made = {r["id"] for r in self.rewrites.values()}
        check({r["id"] for r in rows} == made, f"이력 {len(rows)}건이 만든 결과 {len(made)}건과 다릅니다")
        stamps = [r["created_at"] for r in rows]
        check(stamps == sorted(stamps, reverse=True), "이력이 최근 순이 아닙니다")
        for row in rows:
            need(
                row, ("id", "contact_id", "condition", "channel", "created_at", "result", "feedback"), "이력"
            )
            need(row["result"], ("status", "result_text", "attempts", "preserved_items"), "이력의 결과")
        first = next(iter(self.rewrites.values()))
        saved = next(r for r in rows if r["id"] == first["id"])["feedback"]
        check(saved is not None and saved["accepted"] is True, "저장한 피드백이 이력에 없습니다")
        one = self.get(f"/v1/rewrites?contact_id={first['contact_id']}&limit=50")
        check(
            bool(one) and all(r["contact_id"] == first["contact_id"] for r in one), "상대별 이력이 다릅니다"
        )
        check(self.get(f"/v1/rewrites/{first['id']}")["id"] == first["id"], "이력 한 건을 읽지 못했습니다")
        return f"{len(rows)}건, 조건 {'·'.join(sorted({r['condition'] for r in rows}))}"

    def usage(self) -> str:
        data = self.get("/v1/usage")
        keys = ("day", "rewrites", "rewrite_limit", "remaining_rewrites", "input_tokens", "output_tokens")
        need(data, keys, "사용량")
        check(
            data["rewrites"] >= len(self.rewrites),
            f"사용량 {data['rewrites']}회 < 첨삭 {len(self.rewrites)}회",
        )
        return f"오늘 {data['rewrites']}/{data['rewrite_limit']}회"

    def data_summary(self) -> str:
        data = self.get("/v1/data/summary")
        need(data, ("contacts", "totals", "graph_enabled"), "내 데이터 요약")
        rows = {row["alias"]: row for row in data["contacts"]}
        for plan in PLANS:
            if plan.key not in self.ready:
                continue
            check(plan.alias in rows, "요약에 상대가 빠졌습니다")
            row = rows[plan.alias]
            keys = (
                "persona_version",
                "conversations",
                "messages",
                "my_messages",
                "graph_ingested",
                "rewrites",
            )
            need(row, keys, "상대 요약")
            mine = expected_counts(plan)[ME]
            check(
                (row["conversations"], row["my_messages"]) == (1, mine),
                f"상대 요약이 다릅니다: 대화방 {row['conversations']}, 내 메시지 {row['my_messages']}/{mine}",
            )
        check(data["totals"]["rewrites"] == len(self.rewrites), "요약의 첨삭 수가 다릅니다")
        totals = data["totals"]
        return f"상대 {totals['contacts']}명, 메시지 {totals['messages']}건, 첨삭 {totals['rewrites']}건"

    def export(self) -> str:
        response = self.call("GET", "/v1/export", expect=(200,))
        disposition = response.headers.get("Content-Disposition") or ""
        check("attachment" in disposition and ".json" in disposition, "내려받기 파일 이름이 없습니다")
        data = response.json()
        need(data, ("exported_at", "user", "contacts"), "내려받기")
        check(data["user"].get("username") == self.username, "내려받기의 계정이 다릅니다")
        messages = rewrites = 0
        for contact in data["contacts"]:
            need(contact, ("alias", "persona_versions", "conversations", "rewrites"), "내려받기의 상대")
            messages += sum(len(c["messages"]) for c in contact["conversations"])
            rewrites += len(contact["rewrites"])
        check(rewrites == len(self.rewrites), "내려받기의 첨삭 수가 다릅니다")
        check(messages > 0 or not self.ready, "내려받기에 메시지가 없습니다")
        return f"상대 {len(data['contacts'])}명, 메시지 {messages}건, 첨삭 {rewrites}건"

    # ---- 끝 ----
    def delete_account(self) -> str:
        body = {"password": self.password}
        data = self.call("DELETE", "/v1/auth/me", body, expect=(200,)).json()
        self.deleted = True
        need(data, ("contacts", "conversations", "messages"), "계정 삭제")
        check(data["contacts"] == len(self.contacts), "지운 상대 수가 다릅니다")
        if data.get("graph_error"):
            self.warn("계정 삭제", f"그래프 삭제 오류: {data['graph_error']}")
        return f"상대 {data['contacts']}명, 대화방 {data['conversations']}개, 메시지 {data['messages']}건"

    def session_gone(self) -> str:
        status = self.client.request("GET", "/v1/auth/me").status
        check(status == 401, f"계정 삭제 뒤에도 세션이 살아 있습니다: HTTP {status}")
        return "삭제 뒤 401"

    # ---- 전체 ----
    def run(self) -> Report:
        started = time.perf_counter()
        self._scene = "0"
        if self.step("헬스체크", self.health):
            try:
                self._scenes()
            finally:
                self._scene = "끝"
                if self.keep_account:
                    self.out(f"       --keep-account: 계정 {self.username} 을 남겨 둡니다.")
                elif self.registered and not self.deleted:
                    if self.step("계정 삭제", self.delete_account):
                        self.step("삭제 뒤 세션 무효", self.session_gone)
                    else:
                        self.out(f"       남은 임시 계정: {self.username} — 직접 지워야 합니다.")
        self.report.seconds = time.perf_counter() - started
        self._finish()
        return self.report

    def _scenes(self) -> None:
        self.step("화면 셸", self.shell)
        if not self.step("가입 (초대 코드·동의)", self.signup):
            return
        self.step("다시 로그인", self.relogin)
        self.step("CSRF 검사", self.csrf_enforced)

        self._scene = "준비"
        for plan in PLANS:
            if self.step(f"상대 '{plan.alias}' + 말투 설정", lambda p=plan: self.create_contact(p)):
                self.step(f"'{plan.alias}' 대화 가져오기", lambda p=plan: self.import_conversation(p))

        self._scene = "1"
        self.step("B 첨삭", lambda: self.plain("boss", "B"))
        self.step("C 첨삭", lambda: self.with_style("boss", "C"))
        self._scene = "2"
        self.step("C 첨삭", lambda: self.with_style("colleague", "C"))
        self.step("D 첨삭", lambda: self.with_graph("colleague", "colleague:C"))
        self._scene = "3"
        self.step("자동 조건 첨삭", lambda: self.auto("english"))
        self.step("D 첨삭", lambda: self.with_graph("english"))
        self._scene = "4"
        self.step("제안 카드", self.suggestion)
        self.step("제안 저장", self.accept_suggestion)
        self.step("맥락 그래프 조회", self.contact_graph)
        self._scene = "5"
        self.step("피드백 저장", self.feedback)
        self.step("이력", self.history)
        self.step("사용량", self.usage)
        self.step("내 데이터 요약", self.data_summary)
        self.step("내려받기", self.export)

    def _finish(self) -> None:
        r = self.report
        head = "시연 흐름 통과" if r.ok else "시연 흐름 실패"
        self.out(
            f"{head} — 통과 {r.passed}, 실패 {len(r.failed)}, 건너뜀 {len(r.skipped)}, "
            f"주의 {len(r.warnings)} ({r.seconds:.1f}초)"
        )
        if r.failed:
            scenes = ", ".join(f"{s}({SCENE_TITLES[s]})" for s in r.failed_scenes())
            self.out(f"깨진 장면: {scenes}")
            for line in r.failed:
                self.out(f"  - {line}")
        self.out(LIMITS)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="시연 흐름 회귀 검사 (가짜 모델 서버용, 표준 라이브러리만)")
    p.add_argument("--base-url", help="서버 주소. 예) http://web.localhost:8101")
    p.add_argument(
        "--dump-data",
        type=Path,
        metavar="DIR",
        help="서버에 붙지 않고 합성 대화(.csv)와 manifest.json만 쓴다",
    )
    p.add_argument("--insecure", action="store_true", help="인증서 검증을 끈다 (자체 서명·localhost)")
    p.add_argument(
        "--invite-code",
        default=os.environ.get("SMOKE_INVITE_CODE") or os.environ.get("REGISTER_INVITE_CODE") or "",
        help="가입 초대 코드 (기본: 환경변수 SMOKE_INVITE_CODE 또는 REGISTER_INVITE_CODE)",
    )
    p.add_argument("--connect-to", metavar="HOST:PORT", help="TCP 연결만 이 주소로 (smoke_test.py와 같다)")
    p.add_argument("--timeout", type=float, default=90.0, help="요청 하나의 제한 시간(초)")
    p.add_argument("--index-wait", type=float, default=120.0, help="말투 분석이 끝나길 기다리는 시간(초)")
    p.add_argument(
        "--assume-fake",
        action="store_true",
        help="운영 모드 서버(모델을 알려 주지 않는다)를 가짜 모델로 본다",
    )
    p.add_argument(
        "--allow-real-model",
        action="store_true",
        help="실제 모델 서버에도 돌린다 (첨삭·그래프 수집 비용 발생)",
    )
    p.add_argument("--keep-account", action="store_true", help="끝나도 임시 계정을 지우지 않는다 (디버깅용)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.dump_data is not None:
        for path in dump_data(args.dump_data):
            print(path)
        return 0
    if not args.base_url:
        print("--base-url 이 필요합니다 (자료만 쓰려면 --dump-data DIR)")
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from smoke_test import Client  # 세션 쿠키·CSRF를 들고 다니는 HTTP 클라이언트 (표준 라이브러리)
    from smoke_test import StepError as ConnectError

    class Http(Client):
        def request(self, *a: Any, **kw: Any) -> Any:
            try:
                return super().request(*a, **kw)
            except ConnectError as exc:  # 서버에 닿지 못함 — 그 단계의 실패로 보고한다
                raise StepError(str(exc)) from exc

    client = Http(args.base_url, insecure=args.insecure, connect_to=args.connect_to, timeout=args.timeout)
    check_ = DemoCheck(
        client,
        invite_code=args.invite_code,
        assume_fake=args.assume_fake,
        allow_real_model=args.allow_real_model,
        keep_account=args.keep_account,
        index_wait=args.index_wait,
    )
    print(f"시연 흐름 검사: {args.base_url}", flush=True)
    report = check_.run()
    return 0 if report.ok else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔에서도 한글이 깨지지 않게
    raise SystemExit(main())
