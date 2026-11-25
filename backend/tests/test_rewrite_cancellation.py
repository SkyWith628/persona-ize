"""취소된 첨삭 요청의 사용량 (2026-11-25).

요청 태스크가 취소돼도 이미 쓴 토큰은 사용량 카운터에 남아야 한다. 고치기 전에는 세 시점 모두에서
요청 수만 1 오르고 토큰은 0이었다:
(a) 모델 호출 도중 — 호출이 함께 끊기고 토큰은 기록되지 않았다
(b) 호출이 끝난 뒤 토큰 기록 도중 — 측정한 값이 버려졌다
(c) 재생성(두 번째 호출) 도중 — 끝난 첫 호출의 토큰까지 버려졌다

지금 운영 구성(uvicorn + 순수 ASGI 미들웨어)에서는 클라이언트가 연결을 끊어도 요청 태스크가 취소되지
않는다(끝까지 돌고 기록한다). 취소는 서버 종료의 강제 취소처럼 태스크를 직접 끊을 때 온다 — 그래서
이 테스트들은 태스크를 직접 취소한다. 시점은 이벤트로 맞춘다(sleep 없음).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.ai.providers.base import GenerationResult
from app.ai.providers.fake import FakeProvider
from app.api.routes.rewrites import create_rewrites_batch
from app.api.schemas import RewriteBatchCreate
from app.core.config import Settings
from app.core.errors import ModelRefusalError, ModelUnavailableError
from app.domain.rewrite import ModelOutput, RewriteInput
from app.domain.usage import estimate_tokens, usage_day
from app.main import create_app

DRAFT = "내일 오전 10시까지 보고서 보내드리겠습니다."
# 원문의 날짜·시각이 빠진 출력 — 사실 검증에 걸려 재생성으로 간다
BAD = ModelOutput(rewritten_text="모레 보내겠습니다.", change_reasons=[], evidence_ids=[], warnings=[])


class GateProvider(FakeProvider):
    """호출마다 시작을 알리고, 놓아줄 때까지 응답하지 않는다. `script`는 호출 순번별 응답 바꿔치기."""

    def __init__(self, script: dict[int, ModelOutput | Exception] | None = None) -> None:
        super().__init__()
        self.script = script or {}
        self.started = [asyncio.Event() for _ in range(4)]
        self.release = [asyncio.Event() for _ in range(4)]
        self.entered = 0  # 들어온 호출 수 (순번 — 동시에 들어와도 겹치지 않는다)
        self.cut = 0  # 응답 전에 끊긴 호출 수
        self.measured: list[dict[str, int]] = []  # 응답한 호출이 보고한 토큰
        self.prompt_chars: list[int] = []
        self.before_return: Any = None  # 응답을 돌려주기 직전에 부를 것 (취소 시점 맞추기)

    async def generate(self, *, system: str, user: str) -> GenerationResult:
        i = self.entered
        self.entered += 1
        self.prompt_chars.append(len(system) + len(user))
        self.started[i].set()
        try:
            await self.release[i].wait()
        except asyncio.CancelledError:
            self.cut += 1
            raise
        gen = await super().generate(system=system, user=user)  # calls를 올린다
        scripted = self.script.get(i)
        if isinstance(scripted, Exception):
            raise scripted
        if scripted is not None:
            gen = GenerationResult(output=scripted, model_version=gen.model_version, usage=gen.usage)
        self.measured.append(gen.usage)
        if self.before_return is not None:
            self.before_return()
        return gen

    def total(self) -> tuple[int, int]:
        return (
            sum(u["input_tokens"] for u in self.measured),
            sum(u["output_tokens"] for u in self.measured),
        )


class GatedUsage:
    """토큰 기록을 붙잡아 두는 저장소 덧씌우개 — 기록 도중의 취소를 만든다."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.added = asyncio.Event()  # 기록이 한 번 끝날 때마다 선다
        self.adds = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def add_tokens(self, *args: Any) -> Any:
        self.started.set()
        await self.release.wait()
        out = await self.inner.add_tokens(*args)
        self.adds += 1
        self.added.set()
        return out


async def _setup(provider: FakeProvider, user: str = "u", **overrides: object):  # noqa: ANN202
    settings = Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[arg-type]
    state = create_app(settings).state.container
    state.workflow.provider = provider
    contact = await state.contacts.create(user, "박지현", "friend")
    inp = RewriteInput(user_id=user, contact_id=contact.id, draft_text=DRAFT, draft_version=1, condition="A")
    return state, inp


async def _cancel(task: asyncio.Task) -> None:
    """태스크를 취소하고, 취소가 삼켜지지 않고 그대로 올라오는지 본다."""
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _drain(service) -> None:  # noqa: ANN001
    """서비스가 뒤로 넘긴 일이 끝날 때까지 기다린다."""
    while service._tasks:
        await asyncio.wait(set(service._tasks))
        await asyncio.sleep(0)


async def _used(state, user: str = "u") -> tuple[int, int, int]:  # noqa: ANN001
    day = usage_day(offset_hours=state.settings.usage_day_offset_hours)
    u = await state.usage.get(user, day)
    return u.rewrites, u.input_tokens, u.output_tokens


# ---- (a) 모델 호출 도중 ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_call_in_flight_is_left_to_finish_and_its_tokens_are_recorded() -> None:
    provider = GateProvider()
    state, inp = await _setup(provider)
    service = state.rewrite_service
    task = asyncio.create_task(service.rewrite(inp, "k1"))
    await provider.started[0].wait()
    await _cancel(task)
    # 호출은 끊기지 않았고, 토큰이 기록되기 전까지는 "진행 중"으로 한도 계산에 잡혀 있다
    assert provider.cut == 0
    assert await _used(state) == (1, 0, 0)
    assert service._in_flight == {"u": 1} and service._in_flight_total == 1

    provider.release[0].set()
    await _drain(service)
    assert await _used(state) == (1, *provider.total())
    assert provider.total()[0] > 0 and provider.calls == 1
    assert service._in_flight == {} and service._in_flight_total == 0
    assert len(service._locks) == 0  # 멱등 잠금도 남지 않는다
    assert await state.rewrites.list("u", None, 10, None) == []  # 취소된 요청은 이력에 남지 않는다


@pytest.mark.anyio
async def test_unsettled_call_still_counts_against_the_token_limit() -> None:
    """취소로 "진행 중" 집계를 비울 수 있으면, 요청을 보내고 끊기를 되풀이해 한도를 넘길 수 있다."""
    provider = GateProvider()
    state, inp = await _setup(provider, usage_daily_token_limit=200)
    service = state.rewrite_service
    task = asyncio.create_task(service.rewrite(inp, None))
    await provider.started[0].wait()
    await _cancel(task)
    with pytest.raises(Exception) as blocked:  # noqa: PT011 - 아래에서 코드를 본다
        await service.rewrite(inp, None)
    assert getattr(blocked.value, "code", None) == "usage_exceeded"
    provider.release[0].set()
    await _drain(service)
    assert (await _used(state))[0] == 1 and provider.calls == 1


@pytest.mark.anyio
async def test_shutdown_cuts_a_call_that_never_ends_and_records_the_estimate() -> None:
    provider = GateProvider()
    state, inp = await _setup(provider, shutdown_grace_seconds=0)
    service = state.rewrite_service
    task = asyncio.create_task(service.rewrite(inp, None))
    await provider.started[0].wait()
    await _cancel(task)

    await asyncio.wait_for(state.close(), timeout=5)  # 끝나지 않는 호출이 종료를 붙잡지 않는다
    assert provider.cut == 1 and service._tasks == set()
    # 응답을 못 받았다 — 실패한 호출과 같이 보낸 프롬프트만큼의 추정치를 남긴다
    assert await _used(state) == (1, estimate_tokens(provider.prompt_chars[0]), 0)
    assert service._in_flight == {} and service._in_flight_total == 0


@pytest.mark.anyio
async def test_shutdown_waits_for_a_call_that_ends_within_the_grace() -> None:
    provider = GateProvider()
    state, inp = await _setup(provider)
    service = state.rewrite_service
    task = asyncio.create_task(service.rewrite(inp, None))
    await provider.started[0].wait()
    await _cancel(task)
    closing = asyncio.create_task(service.shutdown(30))
    provider.release[0].set()
    assert await asyncio.wait_for(closing, timeout=5) == 0  # 끊은 것 없음
    assert provider.cut == 0 and await _used(state) == (1, *provider.total())


@pytest.mark.anyio
async def test_shutdown_with_nothing_left_returns_at_once() -> None:
    state, inp = await _setup(FakeProvider())
    await state.rewrite_service.rewrite(inp, None)
    assert await asyncio.wait_for(state.rewrite_service.shutdown(30), timeout=1) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("error", [ModelUnavailableError("모델 응답 시간 초과."), ModelRefusalError("거절")])
async def test_left_over_call_that_fails_counts_the_estimate(error: Exception) -> None:
    provider = GateProvider({0: error})
    state, inp = await _setup(provider)
    task = asyncio.create_task(state.rewrite_service.rewrite(inp, None))
    await provider.started[0].wait()
    await _cancel(task)
    provider.release[0].set()
    await _drain(state.rewrite_service)
    # 취소된 뒤에는 거절이어도 다시 부르지 않는다 — 새로 나가는 비용이 없다
    assert provider.calls == 1
    assert await _used(state) == (1, estimate_tokens(provider.prompt_chars[0]), 0)
    assert state.rewrite_service._in_flight_total == 0


@pytest.mark.anyio
async def test_cancel_before_any_model_call_leaves_nothing_behind() -> None:
    provider = GateProvider()
    state, inp = await _setup(provider)
    reached, hold = asyncio.Event(), asyncio.Event()

    class SlowRetriever:
        version = "slow"

        async def retrieve(self, **_: object) -> list:
            reached.set()
            await hold.wait()
            return []

    state.workflow.retrievers["A"] = SlowRetriever()  # type: ignore[assignment]
    service = state.rewrite_service
    task = asyncio.create_task(service.rewrite(inp, None))
    await reached.wait()
    await _cancel(task)
    assert service._tasks == set() and service._in_flight_total == 0
    assert await _used(state) == (1, 0, 0) and provider.calls == 0  # 요청 수 예약은 그대로다


# ---- (b) 호출이 끝난 뒤, 토큰 기록 전 ---------------------------------------------------------------


@pytest.mark.anyio
async def test_cancel_during_the_token_write_does_not_lose_it() -> None:
    provider = GateProvider()
    state, inp = await _setup(provider)
    service = state.rewrite_service
    gated = GatedUsage(state.usage)
    service._usage = gated
    provider.release[0].set()
    task = asyncio.create_task(service.rewrite(inp, "k1"))
    await gated.started.wait()
    await _cancel(task)
    assert await _used(state) == (1, 0, 0)

    gated.release.set()
    await _drain(service)
    assert gated.adds == 1  # 한 번만 기록한다 (뒷정리가 같은 토큰을 다시 넘기지 않는다)
    assert await _used(state) == (1, *provider.total())
    assert service._in_flight_total == 0


@pytest.mark.anyio
async def test_cancel_arriving_just_as_the_call_returns_counts_it_once() -> None:
    provider = GateProvider()
    state, inp = await _setup(provider)
    service = state.rewrite_service
    task = asyncio.create_task(service.rewrite(inp, None))
    provider.before_return = task.cancel  # 호출이 응답을 돌려주는 바로 그때 요청이 취소된다
    provider.release[0].set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _drain(service)
    assert provider.calls == 1 and provider.cut == 0
    assert await _used(state) == (1, *provider.total())
    assert service._in_flight_total == 0


# ---- (c) 재생성 도중 ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cancel_during_regeneration_records_both_calls() -> None:
    provider = GateProvider({0: BAD})
    state, inp = await _setup(provider)
    service = state.rewrite_service
    gated = GatedUsage(state.usage)
    gated.release.set()  # 붙잡지 않고, 기록이 끝난 것만 알린다
    service._usage = gated
    provider.release[0].set()
    task = asyncio.create_task(service.rewrite(inp, None))
    await provider.started[1].wait()
    await _cancel(task)

    # 끝난 첫 호출의 토큰은 두 번째 호출을 기다리지 않고 바로 기록된다
    await gated.added.wait()
    first = provider.measured[0]
    assert await _used(state) == (1, first["input_tokens"], first["output_tokens"])
    assert service._in_flight_total == 1 and provider.cut == 0

    provider.release[1].set()
    await _drain(service)
    assert provider.calls == 2 and len(provider.measured) == 2
    assert await _used(state) == (1, *provider.total())
    assert service._in_flight_total == 0


@pytest.mark.anyio
async def test_cancel_during_regeneration_after_a_refusal_keeps_its_estimate() -> None:
    provider = GateProvider({0: ModelRefusalError("거절")})
    state, inp = await _setup(provider)
    provider.release[0].set()
    task = asyncio.create_task(state.rewrite_service.rewrite(inp, None))
    await provider.started[1].wait()
    await _cancel(task)
    provider.release[1].set()
    await _drain(state.rewrite_service)
    second = provider.measured[0]  # 첫 호출은 거절이라 측정값이 없다
    expected_in = estimate_tokens(provider.prompt_chars[0]) + second["input_tokens"]
    assert await _used(state) == (1, expected_in, second["output_tokens"])


# ---- 이력과 멱등 재요청 ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_retry_with_the_same_key_after_a_cancel_is_a_new_request() -> None:
    provider = GateProvider()
    state, inp = await _setup(provider)
    service = state.rewrite_service
    task = asyncio.create_task(service.rewrite(inp, "k1"))
    await provider.started[0].wait()
    await _cancel(task)
    assert await state.rewrites.find_by_idempotency("u", "k1") is None

    # 남은 호출이 아직 도는 중에 같은 키로 다시 보내도 막히지 않는다 (잠금은 취소 때 풀렸다)
    provider.release[1].set()
    record = await service.rewrite(inp, "k1")
    assert record.result.status == "recommended"
    provider.release[0].set()
    await _drain(service)
    # 취소된 요청과 재요청은 각각 한 건이고, 두 호출의 토큰이 모두 남는다
    assert await _used(state) == (2, *provider.total())
    assert provider.calls == 2

    # 저장된 뒤의 재요청은 예전과 같다: 저장된 결과를 돌려주고 세지 않는다
    again = await service.rewrite(inp, "k1")
    assert again.id == record.id and provider.calls == 2
    assert (await _used(state))[0] == 2
    assert len(await state.rewrites.list("u", None, 10, None)) == 1


# ---- 묶음 첨삭 ------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cancelled_batch_records_every_contacts_tokens() -> None:
    provider = GateProvider()
    state, inp = await _setup(provider)
    other = await state.contacts.create("u", "김서연", "friend")
    body = RewriteBatchCreate(
        contact_ids=[inp.contact_id, other.id], draft_text=DRAFT, draft_version=1, condition="A"
    )
    task = asyncio.create_task(create_rewrites_batch(body, "u", state, "batch-1"))
    await provider.started[0].wait()
    await provider.started[1].wait()
    await _cancel(task)  # 묶음 라우트도 취소를 항목 오류로 바꾸지 않고 그대로 올린다
    service = state.rewrite_service
    assert service._in_flight == {"u": 2} and provider.cut == 0

    provider.release[0].set()
    provider.release[1].set()
    await _drain(service)
    assert provider.calls == 2
    assert await _used(state) == (2, *provider.total())
    assert service._in_flight == {} and len(service._locks) == 0


# ---- 정상 경로 회귀 -------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_normal_requests_record_exactly_what_the_result_reports() -> None:
    provider = GateProvider({0: BAD})
    for release in provider.release:
        release.set()
    state, inp = await _setup(provider)
    service = state.rewrite_service
    record = await service.rewrite(inp, "k1")
    assert record.result.status == "recommended" and record.result.attempts == 2
    assert record.result.token_usage == dict(
        zip(("input_tokens", "output_tokens"), provider.total(), strict=True)
    )
    assert await _used(state) == (1, *provider.total())
    assert service._tasks == set() and service._in_flight_total == 0


@pytest.mark.anyio
async def test_workflow_without_a_meter_still_cuts_the_call_when_cancelled() -> None:
    """평가 스크립트처럼 워크플로를 직접 돌리면 예전 그대로다 — 취소되면 호출도 끊긴다."""
    provider = GateProvider()
    state, inp = await _setup(provider)
    contact = await state.contacts.get("u", inp.contact_id)
    task = asyncio.create_task(state.workflow.run(inp, contact, None))
    await provider.started[0].wait()
    await _cancel(task)
    assert provider.cut == 1


# ---- PostgreSQL -----------------------------------------------------------------------------------


def test_cancelled_requests_record_tokens_on_postgres(pg_url: str) -> None:
    """실제 저장소는 기록에 양보가 있다(UPSERT) — 호출 도중·재생성 도중의 취소를 같은 DB로 본다."""

    async def scenario() -> None:
        import uuid

        user = f"u-{uuid.uuid4().hex[:8]}"
        provider = GateProvider({0: BAD})
        state, inp = await _setup(provider, user=user, database_url=pg_url)
        try:
            service = state.rewrite_service
            provider.release[0].set()
            task = asyncio.create_task(service.rewrite(inp, "k1"))
            await provider.started[1].wait()
            await _cancel(task)
            provider.release[1].set()
            await _drain(service)
            assert await _used(state, user) == (1, *provider.total())
            assert await state.rewrites.find_by_idempotency(user, "k1") is None

            provider.release[2].set()
            record = await service.rewrite(inp, "k1")  # 같은 키의 재요청은 새 요청으로 저장된다
            assert record.result.status == "recommended"
            assert await _used(state, user) == (2, *provider.total())
            assert service._in_flight_total == 0
        finally:
            await state.close()

    asyncio.run(scenario())
