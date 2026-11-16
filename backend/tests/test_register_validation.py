"""직함과 이름 구분, 검증 규칙 버전, 호칭 누출 표시, 말투 사후 점검 (graph-redesign 5.5).

모델은 부르지 않는다 — 정해 둔 출력을 순서대로 내는 `ScriptedProvider`만 쓴다. 문장은 전부 지어낸 것이다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.ai.preserve import extract_preserved_items
from app.ai.providers.fake import ScriptedProvider
from app.ai.retrieval.base import NullRetriever
from app.ai.validation import address_leaks, validate_output
from app.ai.workflow import RewriteWorkflow, _register_mismatch
from app.core.errors import ModelRefusalError, ModelUnavailableError
from app.domain.contact import Contact
from app.domain.persona import PersonaSettings
from app.domain.rewrite import ModelOutput, RewriteInput
from app.domain.titles import VALIDATOR_VERSION

DRAFT = "오늘 정리해서 내일 오전까지 보내드릴게요."
# 사실(오늘, 내일 오전, 약속)은 같고 말투만 다른 출력 셋
CASUAL = ModelOutput(rewritten_text="오늘 정리해서 내일 오전까지 전달할게.")  # 반말
POLITE = ModelOutput(rewritten_text="오늘 정리해서 내일 오전까지 보내드리겠습니다.")  # 합쇼체
HAEYO = ModelOutput(rewritten_text="오늘 정리해서 내일 오전까지 보내드릴게요.")  # 해요체
BAD_FACTS = ModelOutput(rewritten_text="내일 오후까지 보내드리겠습니다.")  # 존댓말이지만 '내일 오전'이 없다
TITLE_ADDED = ModelOutput(rewritten_text="파트장님, 오늘 정리해서 내일 오전까지 보내드리겠습니다.")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make(
    outputs: list[ModelOutput | Exception], **flags: bool
) -> tuple[RewriteWorkflow, ScriptedProvider, RewriteInput, Contact]:
    provider = ScriptedProvider(outputs)
    null = NullRetriever()
    wf = RewriteWorkflow(
        provider=provider,
        retrievers={"A": null, "B": null, "C": null, "D": null},
        prompt_version="rewrite-v3",
        **flags,
    )
    contact = Contact(
        id=uuid4(), user_id="u", alias="이서연", relationship_type="colleague", created_at=datetime.now(UTC)
    )
    inp = RewriteInput(
        user_id="u", contact_id=contact.id, draft_text=DRAFT, draft_version=1, situation="업무 보고"
    )
    return wf, provider, inp, contact


def persons(text: str) -> list[str]:
    return [i.normalized for i in extract_preserved_items(text) if i.kind == "person"]


# ── 직함은 사람 이름이 아니다 (preserve.py) ──────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "파트장님, 확인 부탁드립니다.",
        "센터장님께 보고드렸습니다.",
        "매니저님 안녕하세요.",
        "본부장님 지시사항입니다.",
        "부사장님 결재가 났습니다.",
        "팀장님, 내일 뵙겠습니다.",  # 예전부터 직함이던 것도 그대로
        "선배님 감사합니다.",
    ],
)
def test_bare_title_is_not_a_person(text: str) -> None:
    assert persons(text) == []


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("지현 팀장님, 보고드립니다.", "지현"),  # 예전과 같다
        ("서연 파트장님, 회의록 공유드립니다.", "서연"),  # 예전에는 '파트장'이 사람으로 잡혔다
        ("서연 부사장님 결재 건입니다.", "서연"),
        ("김팀장님께 전달했습니다.", "김"),  # 붙여 쓴 성+직함
        ("김파트장님께 전달했습니다.", "김"),
        ("지현선배님 감사합니다.", "지현"),
        ("서연 선배가 말한 대로 할게요.", "서연"),
        ("민수씨, 고마워요.", "민수"),
    ],
)
def test_name_before_a_title_is_the_person(text: str, name: str) -> None:
    assert persons(text) == [name]


def test_new_titles_need_nim_so_common_nouns_do_not_make_people() -> None:
    """사전에서 온 직함은 '님'이 붙었을 때만 이름 뒤 직함이다 — '책임', '의사'는 보통 명사로도 쓴다."""
    assert persons("제가 책임지고 처리하겠습니다.") == []
    assert persons("거절 의사를 전했습니다.") == []
    assert persons("서연 책임님께 전달했습니다.") == ["서연"]


def test_adding_a_bare_title_is_not_a_fact_but_a_named_title_is() -> None:
    preserved = extract_preserved_items(DRAFT)
    assert validate_output(DRAFT, TITLE_ADDED, preserved, PersonaSettings(), []).passed
    named = ModelOutput(rewritten_text="서연 파트장님, 오늘 정리해서 내일 오전까지 보내드리겠습니다.")
    report = validate_output(DRAFT, named, preserved, PersonaSettings(), [])
    assert [v.check for v in report.violations] == ["fact_added"] and "서연" in report.violations[0].detail
    # 저장된 호칭이면 이름까지 허용한다
    assert validate_output(DRAFT, named, preserved, PersonaSettings(address_term="서연 파트장님"), []).passed


# ── 검증 규칙 버전 ───────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("outputs", "status"),
    [
        ([POLITE], "recommended"),
        ([BAD_FACTS, POLITE], "recommended"),
        ([BAD_FACTS, BAD_FACTS], "fallback_original"),
        ([ModelUnavailableError("503")], "fallback_original"),
        ([ModelRefusalError("거절"), ModelRefusalError("거절")], "fallback_original"),
    ],
)
async def test_validator_version_is_stamped_on_every_result(
    outputs: list[ModelOutput | Exception], status: str
) -> None:
    wf, _, inp, contact = make(outputs)
    r = await wf.run(inp, contact, PersonaSettings(formality=5))
    assert r.status == status
    assert r.validator_version == VALIDATOR_VERSION != ""


@pytest.mark.anyio
async def test_validator_version_on_first_attempt_recommended_by_post_check() -> None:
    wf, _, inp, contact = make([CASUAL, BAD_FACTS], register_post_check=True)
    r = await wf.run(inp, contact, PersonaSettings(formality=5))
    assert r.status == "recommended" and r.validator_version == VALIDATOR_VERSION


# ── 호칭 누출 표시 (validation.address_leaks) ────────────────────────────────


@pytest.mark.parametrize(
    ("output", "persona", "alias", "leaks"),
    [
        ("파트장님, 내일 오전까지 보내드리겠습니다.", None, "", ["파트장님"]),
        ("파트장님, 내일 오전까지 보내드리겠습니다.", PersonaSettings(), "", ["파트장님"]),
        ("선배님 내일 오전까지 보내드릴게요.", PersonaSettings(), "", ["선배님"]),
        ("김 대리, 내일 오전까지 보내줄게.", PersonaSettings(), "", ["대리"]),  # 부르는 자리의 직함
        ("서연 씨, 내일 오전까지 보내드릴게요.", PersonaSettings(), "", ["서연 씨"]),
        ("고객님! 내일 오전까지 보내드리겠습니다.", PersonaSettings(), "", ["고객님"]),
        ("형, 내일 오전까지 보낼게.", PersonaSettings(), "", ["형"]),
        ("서연아, 내일 오전까지 보낼게.", PersonaSettings(), "이서연", ["서연아"]),  # 별칭이 있어야 찾는다
        ("서연아, 내일 오전까지 보낼게.", PersonaSettings(), "", []),
        # 저장된 호칭에 있는 것은 누출이 아니다 — 호칭 전체든 그 안의 직함이든
        (
            "서연 파트장님, 내일 오전까지 보내드리겠습니다.",
            PersonaSettings(address_term="서연 파트장님"),
            "",
            [],
        ),
        ("파트장님, 내일 오전까지 보내드리겠습니다.", PersonaSettings(address_term="서연 파트장님"), "", []),
        ("형, 내일 오전까지 보낼게.", PersonaSettings(address_term="형"), "", []),
        # 호칭이 아닌 말
        ("제가 책임지고 내일 오전까지 보내드리겠습니다.", PersonaSettings(), "", []),
        ("오늘 날씨, 좋네요. 내일 오전까지 보내드릴게요.", PersonaSettings(), "", []),
        ("괜찮아, 내일 오전까지 보낼게.", PersonaSettings(), "이서연", []),
        ("서연이 내일 오전까지 보낸대.", PersonaSettings(), "이서연", []),  # 주어 '서연이'
        ("형식은 내일 오전까지 정할게.", PersonaSettings(), "", []),
    ],
)
def test_address_leaks(output: str, persona: PersonaSettings | None, alias: str, leaks: list[str]) -> None:
    assert address_leaks("내일 오전까지 보내드릴게요.", output, persona, alias) == leaks


def test_address_already_in_the_draft_is_not_a_leak() -> None:
    assert address_leaks("팀장님 내일 오전까지 드릴게요", "팀장님, 내일 오전까지 드리겠습니다.", None) == []
    # 알맹이로 견준다: 초안의 직함·이름에 '님'·'씨'를 붙인 것은 말투를 바꾼 것이다
    assert address_leaks("파트장이 내일 오라고 했어", "파트장님께서 내일 오라고 하셨습니다.", None) == []
    assert address_leaks("서연아 고마워", "서연 씨, 고마워요.", PersonaSettings(), "이서연") == []


def test_address_leaks_reports_each_term_once_in_order() -> None:
    out = "파트장님, 내일 오전까지 보내드리겠습니다. 파트장님, 그리고 형, 미안."
    assert address_leaks("내일 오전까지 보내드릴게요.", out, None) == ["파트장님", "형"]


@pytest.mark.anyio
async def test_leak_warning_is_added_without_failing_or_regenerating() -> None:
    wf, provider, inp, contact = make([TITLE_ADDED], register_warnings=True)
    r = await wf.run(inp, contact, PersonaSettings(formality=5))
    assert r.status == "recommended" and r.result_text == TITLE_ADDED.rewritten_text
    assert r.validation.passed and r.attempts == 1 and provider.calls == 1
    assert len(r.warnings) == 1 and "'파트장님'" in r.warnings[0] and "호칭" in r.warnings[0]


@pytest.mark.anyio
async def test_leak_warning_keeps_model_warnings_and_respects_saved_address() -> None:
    noted = TITLE_ADDED.model_copy(update={"warnings": ["모델이 남긴 주의"]})
    wf, _, inp, contact = make([noted], register_warnings=True)
    r = await wf.run(inp, contact, PersonaSettings(formality=5))
    assert r.warnings[0] == "모델이 남긴 주의" and "'파트장님'" in r.warnings[1]
    wf, _, inp, contact = make([TITLE_ADDED], register_warnings=True)
    r = await wf.run(inp, contact, PersonaSettings(formality=5, address_term="파트장님"))
    assert r.warnings == []


@pytest.mark.anyio
async def test_leak_warning_is_off_by_default() -> None:
    wf, _, inp, contact = make([TITLE_ADDED])
    r = await wf.run(inp, contact, PersonaSettings(formality=5))
    assert r.status == "recommended" and r.warnings == []


# ── 말투 사후 점검 (workflow, register_post_check) ───────────────────────────


@pytest.mark.parametrize(
    ("formality", "text", "wanted"),
    [
        (5, CASUAL.rewritten_text, True),  # 설정은 존댓말, 출력은 반말
        (3, CASUAL.rewritten_text, True),
        (1, POLITE.rewritten_text, False),  # 설정은 반말, 출력은 존댓말
        (2, HAEYO.rewritten_text, False),
        (5, HAEYO.rewritten_text, None),  # 합쇼체 설정에 해요체 출력 — 강제하지 않는다
        (3, POLITE.rewritten_text, None),
        (1, CASUAL.rewritten_text, None),
        (5, "내일 오전 10시", None),  # 말투를 알 수 없는 출력은 판단하지 않는다
    ],
)
def test_register_mismatch_only_at_the_polite_casual_boundary(
    formality: int, text: str, wanted: bool | None
) -> None:
    assert _register_mismatch(PersonaSettings(formality=formality), text) is wanted
    assert _register_mismatch(None, text) is None  # 설정이 없으면 기준이 없다


@pytest.mark.anyio
async def test_post_check_regenerates_once_and_recommends_the_second() -> None:
    wf, provider, inp, contact = make([CASUAL, POLITE], register_post_check=True)
    r = await wf.run(inp, contact, PersonaSettings(formality=5))
    assert r.status == "recommended" and r.result_text == POLITE.rewritten_text
    assert r.attempts == 2 and provider.calls == 2
    # 재생성 지시: 말투만 고치라는 말과 이전 결과가 들어간다. 사실 검증 실패용 꼬리말은 아니다
    assert "존댓말" in provider.prompts[1] and CASUAL.rewritten_text in provider.prompts[1]
    assert provider.prompts[1].startswith(provider.prompts[0]) and "위반 항목" not in provider.prompts[1]
    # 1차도 사실 검증은 통과했다 — 기록이 그렇게 남는다
    assert [(a.attempt, a.passed, a.rewritten_text) for a in r.attempt_log] == [
        (1, True, CASUAL.rewritten_text),
        (2, True, POLITE.rewritten_text),
    ]
    assert r.validation.passed and r.warnings == []


@pytest.mark.anyio
async def test_post_check_second_fails_facts_recommends_first_with_warning() -> None:
    wf, provider, inp, contact = make([CASUAL, BAD_FACTS], register_post_check=True)
    r = await wf.run(inp, contact, PersonaSettings(formality=5))
    assert r.status == "recommended"  # 원문으로 돌아가지 않는다
    assert r.result_text == CASUAL.rewritten_text
    assert r.validation.passed and r.validation.violations == []  # 추천한 1차의 검증 결과
    assert r.attempts == 2 and provider.calls == 2
    assert [(a.attempt, a.passed) for a in r.attempt_log] == [(1, True), (2, False)]
    assert "preserve_missing" in {v.check for v in r.attempt_log[1].violations}
    assert len(r.warnings) == 1 and "첫 결과" in r.warnings[0] and "말투" in r.warnings[0]


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [ModelUnavailableError("503"), ModelRefusalError("거절")])
async def test_post_check_second_call_fails_recommends_first_with_warning(failure: Exception) -> None:
    wf, provider, inp, contact = make([CASUAL, failure], register_post_check=True)
    r = await wf.run(inp, contact, PersonaSettings(formality=5))
    assert r.status == "recommended" and r.result_text == CASUAL.rewritten_text
    assert r.attempts == 2 and provider.calls == 2
    assert r.attempt_log[1].passed is False and "첫 결과" in r.warnings[0]
    assert r.token_usage["estimated_input_tokens"] > 0  # 응답 없이 끝난 2차 호출도 센다


@pytest.mark.anyio
async def test_post_check_off_makes_no_extra_call() -> None:
    wf, provider, inp, contact = make([CASUAL, POLITE])
    r = await wf.run(inp, contact, PersonaSettings(formality=5))
    assert r.status == "recommended" and r.result_text == CASUAL.rewritten_text
    assert r.attempts == 1 and provider.calls == 1 and r.warnings == []


@pytest.mark.anyio
async def test_post_check_never_enforces_hapsyo_versus_haeyo() -> None:
    wf, provider, inp, contact = make([HAEYO, POLITE], register_post_check=True)
    r = await wf.run(inp, contact, PersonaSettings(formality=5))
    assert r.result_text == HAEYO.rewritten_text and provider.calls == 1 and r.warnings == []


@pytest.mark.anyio
async def test_post_check_skips_condition_a_and_missing_persona() -> None:
    wf, provider, inp, contact = make([CASUAL, POLITE], register_post_check=True)
    r = await wf.run(inp.model_copy(update={"condition": "A"}), contact, PersonaSettings(formality=5))
    assert r.result_text == CASUAL.rewritten_text and provider.calls == 1 and r.warnings == []
    wf, provider, inp, contact = make([CASUAL, POLITE], register_post_check=True)
    r = await wf.run(inp, contact, None)
    assert r.result_text == CASUAL.rewritten_text and provider.calls == 1 and r.warnings == []


@pytest.mark.anyio
async def test_post_check_uses_the_persona_after_adjust() -> None:
    """저장된 격식 2(반말)에 '더 격식 있게'를 누르면 3(해요체)이 기준이다 — 반말 출력은 다시 쓴다."""
    wf, provider, inp, contact = make([CASUAL, HAEYO], register_post_check=True)
    r = await wf.run(inp.model_copy(update={"adjust": "more_formal"}), contact, PersonaSettings(formality=2))
    assert r.result_text == HAEYO.rewritten_text and provider.calls == 2
    wf, provider, inp, contact = make([CASUAL, HAEYO], register_post_check=True)
    r = await wf.run(inp, contact, PersonaSettings(formality=2))
    assert r.result_text == CASUAL.rewritten_text and provider.calls == 1


@pytest.mark.anyio
async def test_post_check_has_no_third_call() -> None:
    """기회는 그대로 두 번이다. 1차가 사실 검증에 실패했으면 말투 때문에 다시 쓸 기회는 없다 — 경고만."""
    wf, provider, inp, contact = make([BAD_FACTS, CASUAL, POLITE], register_post_check=True)
    r = await wf.run(inp, contact, PersonaSettings(formality=5))
    assert r.status == "recommended" and r.result_text == CASUAL.rewritten_text
    assert r.attempts == 2 and provider.calls == 2
    assert len(r.warnings) == 1 and "말투" in r.warnings[0] and "첫 결과" not in r.warnings[0]
    # 다시 썼는데도 말투가 그대로면 2차를 추천하고 경고한다
    wf, provider, inp, contact = make([CASUAL, CASUAL, POLITE], register_post_check=True)
    r = await wf.run(inp, contact, PersonaSettings(formality=5))
    assert r.status == "recommended" and r.attempts == 2 and provider.calls == 2
    assert len(r.warnings) == 1 and "첫 결과" not in r.warnings[0]


@pytest.mark.anyio
async def test_post_check_leaves_fact_failures_as_they_were() -> None:
    wf, provider, inp, contact = make([BAD_FACTS, BAD_FACTS], register_post_check=True)
    r = await wf.run(inp, contact, PersonaSettings(formality=5))
    assert r.status == "fallback_original" and r.result_text == DRAFT and provider.calls == 2
    wf, provider, inp, contact = make([BAD_FACTS, POLITE], register_post_check=True)
    r = await wf.run(inp, contact, PersonaSettings(formality=5))
    assert r.status == "recommended" and "위반 항목" in provider.prompts[1] and r.warnings == []


def test_flags_do_not_change_the_graph_shape() -> None:
    plain, *_ = make([])
    flagged, *_ = make([], register_warnings=True, register_post_check=True)
    a, b = plain._graph.get_graph(), flagged._graph.get_graph()
    assert set(a.nodes) == set(b.nodes)
    assert {(e.source, e.target, e.conditional) for e in a.edges} == {
        (e.source, e.target, e.conditional) for e in b.edges
    }


# ── 띄어 쓴 직함 앞의 낱말이 이름이 아닌 경우 (리뷰 2026-11-16) ─────────────────────────────


@pytest.mark.parametrize(
    ("draft", "rewritten"),
    [
        ("어제 파트장님이 승인하셨어요.", "파트장님께서 어제 승인하셨습니다."),
        ("오늘 매니저님이 휴가래요.", "매니저님께서 오늘 휴가라고 합니다."),
        ("회의록은 센터장님께 공유했어요.", "센터장님께 회의록을 공유했습니다."),
        ("우리 파트장님께 내일 보고드릴게요.", "파트장님께 내일 보고드리겠습니다."),
        ("이번 건은 새로 오신 매니저님께 여쭤볼게요.", "이번 건은 매니저님께 여쭤보겠습니다."),
        ("어제 파트장님이 말씀하신 건 확인했습니다", "파트장님께서 어제 말씀하신 건은 확인했습니다."),
        # 예전 목록의 직함도 같다 (main에서는 '어제'가 사람으로 잡혔다)
        ("어제 팀장님이 말씀하신 건 확인했습니다", "팀장님께서 어제 말씀하신 건은 확인했습니다."),
    ],
)
def test_reordering_around_a_title_is_not_a_missing_person(draft: str, rewritten: str) -> None:
    assert persons(draft) == []
    report = validate_output(
        draft, ModelOutput(rewritten_text=rewritten), extract_preserved_items(draft), None, []
    )
    assert report.passed, [(v.check, v.detail) for v in report.violations]


@pytest.mark.parametrize(
    "text",
    ["담당 매니저님", "저희 센터장님", "오늘 책임님", "옆팀 파트장님", "우리 팀장님", "자료는 부장님께"],
)
def test_common_words_before_a_title_are_not_names(text: str) -> None:
    assert persons(text) == []


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("지현 팀장님께 전달했습니다.", "지현"),
        ("서연 파트장님, 회의록 공유드립니다.", "서연"),
        ("김지은 팀장님께 보고했습니다.", "김지은"),  # '은'으로 끝나는 이름을 조사로 잘라 내지 않는다
        ("지은 매니저님이 승인했습니다.", "지은"),
        ("우리 김팀장님이 오셨어요.", "김"),  # 붙여 쓴 꼴은 예전 그대로
    ],
)
def test_real_names_before_a_title_are_still_persons(text: str, name: str) -> None:
    assert persons(text) == [name]


def test_dropping_a_real_name_before_a_title_still_fails() -> None:
    draft = "서연 파트장님께 내일 보고드릴게요."
    report = validate_output(
        draft,
        ModelOutput(rewritten_text="파트장님께 내일 보고드리겠습니다."),
        extract_preserved_items(draft),
        None,
        [],
    )
    assert "preserve_missing" in {v.check for v in report.violations}


def test_post_check_reads_polite_output_with_mid_sentence_connectives() -> None:
    """공손한 출력이 말줄임·ㅠㅠ에서 나뉘어도 존댓말로 읽힌다 — 예전에는 혼용이 되어 점검이 조용히 빠졌다."""
    casual = PersonaSettings(formality=1)
    for polite_text in ("죄송한데... 내일 가능할까요?", "늦어서ㅠㅠ 죄송합니다", "확인했습니다. 내일 봬요!"):
        assert _register_mismatch(casual, polite_text) is False  # 설정은 반말을 바란다
        assert _register_mismatch(PersonaSettings(formality=4), polite_text) is None
