"""비용 통제 (2026-11-11 보안·정확성 검토 D3).

- 프롬프트 크기: 내 연속 발화가 아주 긴 대화(단체방에서 다른 사람을 건너뜀, 텔레그램 '저장한 메시지',
  4,000자짜리 메시지 수백 건)에서도 묶음 하나·예시 하나·프롬프트 전체가 상한 안에 있다.
- 토큰 한도 경합: 토큰은 호출이 끝나야 기록되므로, 동시에 보낸 요청은 "진행 중 요청 × 추정"으로 센다.
- 실패한 호출: 응답 없이 끝나도 보낸 프롬프트만큼의 추정 입력 토큰을 사용량에 더한다.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.ai.episodes import MAX_RUN_CHARS, MAX_RUN_MESSAGES, build_episodes, split_run
from app.ai.prompt import BLOCK_MAX_CHARS, EXAMPLE_MAX_CHARS, TRUNCATED_MARK, fit_examples
from app.ai.providers.fake import FakeProvider
from app.core.config import Settings
from app.core.errors import DomainError, ModelRefusalError, ModelUnavailableError
from app.domain.conversation import Message
from app.domain.rewrite import RetrievedExample, RewriteInput
from app.domain.usage import estimate_tokens, usage_day
from app.main import create_app
from app.services.import_service import ImportMessage, ImportPayload
from app.services.rewrite_service import DEFAULT_REQUEST_TOKENS, MAX_REQUEST_TOKENS, MIN_REQUEST_TOKENS

PROMPT_BUDGET_CHARS = 12_000  # 설계의 입력 예산 6,000토큰 ≈ 12,000자
T0 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
CONV = uuid.uuid4()


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[arg-type]


def _msg(i: int, role: str, text: str) -> Message:
    return Message(
        id=uuid.uuid4(),
        user_id="u",
        conversation_id=CONV,
        source_key=f"m{i:04d}",
        speaker_role=role,  # type: ignore[arg-type]
        sent_at=T0 + timedelta(seconds=i),
        text=text,
    )


class SlowProvider(FakeProvider):
    """실제 모델처럼 응답에 시간이 걸린다 — 그 사이 다른 요청이 한도 검사에 들어온다."""

    def __init__(self, delay: float = 0.05) -> None:
        super().__init__()
        self.delay = delay
        self.prompt_chars: list[int] = []

    async def generate(self, *, system: str, user: str):  # noqa: ANN201
        self.prompt_chars.append(len(system) + len(user))
        await asyncio.sleep(self.delay)
        return await super().generate(system=system, user=user)


class FailingProvider(FakeProvider):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error
        self.prompt_chars: list[int] = []

    async def generate(self, *, system: str, user: str):  # noqa: ANN201
        self.calls += 1
        self.prompt_chars.append(len(system) + len(user))
        raise self.error


def _state(settings: Settings, provider: FakeProvider):  # noqa: ANN202
    state = create_app(settings).state.container
    state.workflow.provider = provider
    return state


async def _try(coro) -> str:  # noqa: ANN001
    try:
        await coro
        return "ok"
    except DomainError as exc:
        return exc.code


async def _input(state, user_id: str, draft: str = "내일 오전까지 보내드리겠습니다.") -> RewriteInput:  # noqa: ANN001
    contact = await state.contacts.create(user_id, "박지현", "friend")
    return RewriteInput(
        user_id=user_id, contact_id=contact.id, draft_text=draft, draft_version=1, condition="A"
    )


# ---- 묶음 나누기 ----------------------------------------------------------------------------------


def test_split_run_cuts_by_message_count_and_by_characters() -> None:
    short = [_msg(i, "me", f"짧은 말 {i}") for i in range(14)]
    assert [len(chunk) for chunk in split_run(short)] == [MAX_RUN_MESSAGES, MAX_RUN_MESSAGES, 2]

    long_text = "가" * 250
    by_chars = split_run([_msg(i, "me", long_text) for i in range(5)])
    assert [len(chunk) for chunk in by_chars] == [2, 2, 1]  # 250 + 1 + 250 = 501 ≤ 600, 세 건은 넘는다
    assert all(len("\n".join(m.text for m in chunk)) <= MAX_RUN_CHARS for chunk in by_chars)

    # 한 건이 상한보다 길면 그 한 건이 묶음 하나다 (메시지를 자르지는 않는다)
    huge = split_run([_msg(1, "me", "나" * 3000), _msg(2, "me", "짧게")])
    assert [len(chunk) for chunk in huge] == [1, 1]
    assert split_run([]) == []


def test_ordinary_conversations_are_grouped_exactly_as_before() -> None:
    msgs = [
        _msg(1, "contact", "언제까지 가능해요?"),
        _msg(2, "me", "금요일은 어렵고"),
        _msg(3, "me", "다음 주 수요일까지는 가능합니다."),
        _msg(4, "contact", "목요일은요?"),
        _msg(5, "me", "목요일이면 테스트를 못 돌립니다."),
    ]
    eps = build_episodes(msgs, "c1", window=4)
    assert [e.style_text for e in eps] == [
        "금요일은 어렵고\n다음 주 수요일까지는 가능합니다.",
        "목요일이면 테스트를 못 돌립니다.",
    ]
    assert eps[0].source_message_ids == ["c1:m0001", "c1:m0002", "c1:m0003"]
    assert eps[1].source_message_ids == ["c1:m0002", "c1:m0003", "c1:m0004", "c1:m0005"]


def test_a_long_run_of_my_messages_becomes_several_bounded_episodes() -> None:
    msgs = [_msg(0, "contact", "다들 의견 주세요")] + [
        _msg(i, "me", f"제 생각에는 {i}번 안이 가장 현실적이라고 봅니다") for i in range(1, 401)
    ]
    eps = build_episodes(msgs, "c1", window=4)
    assert len(eps) == 67  # 400건을 6건씩
    assert max(len(e.style_text) for e in eps) <= MAX_RUN_CHARS
    # 나뉜 묶음은 서로 겹치지 않고 내 발화를 빠짐없이 한 번씩 담는다
    mine = [line for e in eps for line in e.style_text.split("\n")]
    assert mine == [m.text for m in msgs[1:]]
    # 두 번째 묶음부터는 구간이 창을 채워 앞 맥락이 붙지 않는다. 첫 묶음도 6건이라 마찬가지다.
    assert eps[0].source_message_ids[0] == "c1:m0001"
    assert eps[1].source_message_ids[0] == "c1:m0007"
    assert eps[1].start_at == msgs[7].sent_at and eps[1].end_at == msgs[12].sent_at


# ---- 프롬프트에 넣는 예시의 상한 ------------------------------------------------------------------


def _example(i: int, text: str, purpose: str = "style") -> RetrievedExample:
    return RetrievedExample(
        id=f"ep-{i}",
        text=text,
        source_message_ids=[f"c1:m{i}"],
        purpose=purpose,  # type: ignore[arg-type]
        speaker_role="me" if purpose == "style" else "graph",
        sent_at=T0,
    )


def test_fit_examples_leaves_ordinary_results_untouched() -> None:
    examples = [_example(i, f"팀장님, {i}번 건은 내일 오전까지 보내드리겠습니다.") for i in range(4)]
    fitted = fit_examples(examples)
    assert fitted == examples
    assert all(a is b for a, b in zip(fitted, examples, strict=True))  # 사본도 만들지 않는다


def test_fit_examples_truncates_long_examples_and_drops_what_does_not_fit() -> None:
    long_one = _example(1, "가" * 5000)
    fitted = fit_examples([long_one])
    assert len(fitted) == 1 and fitted[0].id == "ep-1"
    assert fitted[0].text == "가" * EXAMPLE_MAX_CHARS + TRUNCATED_MARK
    assert long_one.text == "가" * 5000  # 받은 객체는 바꾸지 않는다

    # 용도별 합계: 문체 2,400자, 사실 맥락 1,200자 — 넘기는 뒤쪽 예시는 뺀다 (순서는 검색기가 준 대로)
    many = [_example(i, "나" * 590) for i in range(10)]
    kept = fit_examples(many)
    assert [e.id for e in kept] == ["ep-0", "ep-1", "ep-2", "ep-3"]
    assert sum(len(e.text) for e in kept) <= BLOCK_MAX_CHARS["style"]
    context = [_example(i, "다" * 500, purpose="context") for i in range(5)]
    assert len(fit_examples(context)) == 2
    assert len(fit_examples(many + context)) == 6  # 용도별로 따로 센다


# ---- 실제 흐름: 가져오기 → 첨삭 프롬프트 크기 ------------------------------------------------------


def test_group_chat_with_skipped_speakers_keeps_the_prompt_within_budget(
    client: TestClient, user_a: dict[str, str]
) -> None:
    provider = SlowProvider(delay=0)
    client.app.state.container.workflow.provider = provider
    cid = client.post(
        "/v1/contacts", json={"alias": "박지현", "relationship_type": "colleague"}, headers=user_a
    ).json()["id"]
    conv = client.post("/v1/conversations", json={"contact_id": cid}, headers=user_a).json()["id"]
    # 단체방: 나와 '다른사람'이 번갈아 400번. 다른사람을 건너뛰면 내 말이 400건 연달아 남는다.
    lines = [
        "--------------- 2026년 10월 5일 월요일 ---------------",
        "[박지현] [오전 9:00] 다들 의견 주세요",
    ]
    for i in range(400):
        lines.append(f"[김민수] [오전 9:{i % 60:02d}] 제 생각에는 {i}번 안이 일정상 가장 현실적이라고 봅니다")
        lines.append(f"[다른사람] [오전 9:{i % 60:02d}] 저는 반대예요 {i}")
    job = client.post(
        f"/v1/conversations/{conv}/imports/raw",
        json={
            "text": "\n".join(lines),
            "speaker_map": {"김민수": "me", "박지현": "contact", "다른사람": "skip"},
        },
        headers=user_a,
    )
    assert job.status_code == 202, job.text
    assert job.json()["imported"] == 401
    assert job.json()["episodes"] == 67  # 예전에는 1개(= 400건이 통째로 예시 하나)

    out = client.post(
        "/v1/rewrites",
        json={"contact_id": cid, "draft_text": "내일 오전까지 보내드릴게요.", "condition": "auto"},
        headers=user_a,
    )
    assert out.status_code == 201, out.text
    assert out.json()["condition"] == "C"
    assert max(provider.prompt_chars) < PROMPT_BUDGET_CHARS


@pytest.mark.anyio
async def test_hundreds_of_maximum_length_messages_do_not_blow_up_the_prompt() -> None:
    provider = SlowProvider(delay=0)
    state = _state(_settings(), provider)
    contact = await state.contacts.create("u", "박지현", "friend")
    conv = await state.import_service.create_conversation("u", contact.id, None, "json", "c1")
    body = "가나다라마바사아자차" * 390  # 3,900자
    msgs = [
        ImportMessage(
            id=f"m{i}", speaker="me", sent_at=f"2026-10-05T09:{i % 60:02d}:{i // 60:02d}+09:00", text=body
        )
        for i in range(300)
    ]
    job = await state.import_service.import_messages("u", conv.id, ImportPayload(messages=msgs))
    assert (job.imported, job.episodes) == (300, 300)  # 한 건이 묶음 하나

    inp = RewriteInput(
        user_id="u", contact_id=contact.id, draft_text="내일 보내드릴게요", draft_version=1, condition="auto"
    )
    rec = await state.rewrite_service.rewrite(inp, None)
    used = await state.rewrite_service.usage("u")
    assert rec.condition == "C"
    assert max(provider.prompt_chars) < PROMPT_BUDGET_CHARS  # 예전에는 117만 자
    assert used.input_tokens < PROMPT_BUDGET_CHARS // 2 < used.token_limit


def test_preview_warns_when_only_one_person_speaks(client: TestClient, user_a: dict[str, str]) -> None:
    saved = {
        "name": None,
        "type": "saved_messages",
        "id": 1,
        "messages": [
            {
                "id": i,
                "type": "message",
                "date": "2026-10-05T09:00:00",
                "date_unixtime": str(1791159900 + i * 60),
                "from": "민수 김",
                "text": f"메모 {i}: 회의 내용 정리",
            }
            for i in range(1, 31)
        ],
    }
    prev = client.post("/v1/imports/parse", json={"text": json.dumps(saved)}, headers=user_a)
    assert prev.status_code == 200, prev.text
    body = prev.json()
    assert [s["suggested_role"] for s in body["speakers"]] == ["me"]
    assert any("한 명(민수 김)뿐" in p for p in body["problems"]), body["problems"]

    # 주고받은 대화, 그리고 몇 줄짜리 붙여넣기에는 경고가 없다
    two = (
        "2026-10-05 09:25 | 박지현 | 언제까지 가능해요?\n2026-10-05 09:31 | 김민수 | 수요일까지 가능합니다.\n"
    )
    assert client.post("/v1/imports/parse", json={"text": two}, headers=user_a).json()["problems"] == []
    short = "2026-10-05 09:31 | 김민수 | 수요일까지 가능합니다.\n"
    assert client.post("/v1/imports/parse", json={"text": short}, headers=user_a).json()["problems"] == []


# ---- 토큰 한도와 동시 요청 ------------------------------------------------------------------------


@pytest.mark.anyio
async def test_concurrent_requests_cannot_run_past_the_user_token_limit() -> None:
    provider = SlowProvider()
    state = _state(_settings(usage_daily_token_limit=200), provider)
    inp = await _input(state, "par")
    results = await asyncio.gather(*[_try(state.rewrite_service.rewrite(inp, None)) for _ in range(30)])
    # 첫 요청만 모델까지 간다. 나머지는 "진행 중 1건 × 추정"이 한도를 채워 막힌다 (예전에는 30건 전부).
    assert (results.count("ok"), results.count("usage_exceeded")) == (1, 29)
    assert provider.calls == 1
    used = await state.rewrite_service.usage("par")
    assert used.rewrites == 1  # 막힌 요청은 요청 수도 쓰지 않는다
    # 끝난 뒤에는 기록된 토큰이 한도를 넘었으므로 계속 막힌다
    assert await _try(state.rewrite_service.rewrite(inp, None)) == "usage_exceeded"
    assert state.rewrite_service._in_flight == {} and state.rewrite_service._in_flight_total == 0


@pytest.mark.anyio
async def test_requests_in_flight_only_count_while_they_run() -> None:
    """한도가 넉넉하면 동시 요청이 모두 통과하고, 끝나면 진행 중 집계가 0으로 돌아온다."""
    provider = SlowProvider()
    state = _state(_settings(), provider)
    inp = await _input(state, "u")
    results = await asyncio.gather(*[_try(state.rewrite_service.rewrite(inp, None)) for _ in range(10)])
    assert results == ["ok"] * 10 and provider.calls == 10
    service = state.rewrite_service
    assert service._in_flight == {} and service._in_flight_total == 0
    assert MIN_REQUEST_TOKENS <= service._request_tokens <= DEFAULT_REQUEST_TOKENS  # 실제 사용량 쪽으로

    # 한도 직전(3,500토큰 남음, 요청당 추정 1,000): 진행 중 4건이 남은 양을 채우면 더 받지 않는다
    limit = state.settings.usage_daily_token_limit
    day = usage_day(offset_hours=state.settings.usage_day_offset_hours)
    spent = (await state.usage.get("u", day)).total_tokens
    await state.usage.add_tokens("u", day, limit - 3_500 - spent, 0)
    service._request_tokens = 1_000
    near = await asyncio.gather(*[_try(service.rewrite(inp, None)) for _ in range(10)])
    assert (near.count("ok"), near.count("usage_exceeded")) == (4, 6)


@pytest.mark.anyio
async def test_global_token_breaker_counts_requests_in_flight() -> None:
    provider = SlowProvider()
    state = _state(_settings(usage_global_daily_token_limit=200), provider)
    inputs = [await _input(state, f"user{i}", "안녕하세요") for i in range(25)]
    results = await asyncio.gather(*[_try(state.rewrite_service.rewrite(i, None)) for i in inputs])
    # 25명이 동시에 1건씩: 예전에는 전부 모델까지 갔다 (합계 토큰은 호출이 끝나야 더해지므로)
    assert (results.count("ok"), results.count("budget_exceeded")) == (1, 24)
    assert provider.calls == 1
    assert await _try(state.rewrite_service.rewrite(inputs[3], None)) == "budget_exceeded"


def test_global_rewrite_breaker_is_exact_under_concurrency_on_postgres(pg_url: str) -> None:
    """요청 수 차단기: 확인과 예약 사이에 다른 요청이 끼어들지 못한다 (PostgreSQL은 그 사이에 양보가 있다)."""
    day_limit = 3

    async def scenario() -> tuple[list[str], int]:
        provider = SlowProvider()
        state = _state(_settings(database_url=pg_url, usage_global_daily_rewrite_limit=10**9), provider)
        try:
            day = usage_day(offset_hours=state.settings.usage_day_offset_hours)
            already = (await state.usage.totals(day)).rewrites  # 다른 테스트가 오늘 쓴 요청 수
            state.settings.usage_global_daily_rewrite_limit = already + day_limit
            inputs = [await _input(state, f"u-{uuid.uuid4().hex[:8]}", "안녕하세요") for _ in range(12)]
            results = await asyncio.gather(*[_try(state.rewrite_service.rewrite(i, None)) for i in inputs])
            return list(results), provider.calls
        finally:
            await state.close()

    results, calls = asyncio.run(scenario())
    assert (results.count("ok"), results.count("budget_exceeded")) == (day_limit, 12 - day_limit)
    assert calls == day_limit


# ---- 실패한 호출도 사용량에 남는다 -----------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error", [ModelUnavailableError("모델 응답 시간 초과."), ModelRefusalError("모델이 요청을 거절했습니다.")]
)
async def test_failed_model_calls_count_estimated_input_tokens(error: Exception) -> None:
    provider = FailingProvider(error)
    state = _state(_settings(usage_daily_token_limit=10), provider)
    inp = await _input(state, "u", "안녕하세요")
    first = await state.rewrite_service.rewrite(inp, None)
    assert first.result.status == "fallback_original"
    # 응답이 없어 측정값은 없다. 보낸 프롬프트 글자 수로 어림한 값이 따로 남는다.
    expected = sum(estimate_tokens(n) for n in provider.prompt_chars)
    assert first.result.token_usage == {"estimated_input_tokens": expected}
    used = await state.rewrite_service.usage("u")
    assert (used.input_tokens, used.output_tokens) == (expected, 0)
    # 예전에는 0으로 기록돼 실패하는 호출을 요청 수 한도(200회)까지 되풀이할 수 있었다
    calls = provider.calls
    results = [await _try(state.rewrite_service.rewrite(inp, None)) for _ in range(5)]
    assert results == ["usage_exceeded"] * 5 and provider.calls == calls


@pytest.mark.anyio
async def test_in_flight_count_is_released_when_the_workflow_raises() -> None:
    class Broken(FakeProvider):
        async def generate(self, *, system: str, user: str):  # noqa: ANN201
            raise RuntimeError("예상하지 못한 오류")

    state = _state(_settings(), Broken())
    inp = await _input(state, "u")
    with pytest.raises(RuntimeError):
        await state.rewrite_service.rewrite(inp, None)
    service = state.rewrite_service
    assert service._in_flight == {} and service._in_flight_total == 0
    assert len(service._locks) == 0
    assert service._request_tokens <= MAX_REQUEST_TOKENS
