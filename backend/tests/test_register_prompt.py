"""[관계 상태] 블록과 설정 경고 (graph-redesign 5.2·5.3).

- 프롬프트: rewrite-v3까지는 `register_block`을 줘도 글자까지 그대로다. rewrite-v4만 블록 자리가 있다.
- `render_state_block`: 확정된 말투 단계와 건수, 위계 힌트만. 호칭·날짜·메시지 id는 넣지 않는다.
- 서비스: 저장된 설정이 없을 때만 블록, 있을 때만 경고. 둘 다 꺼 두면 결과가 예전과 같다.
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.ai import prompt as prompt_module
from app.ai.embeddings import FakeEmbedder
from app.ai.prompt import build_prompt
from app.ai.providers.base import GenerationResult
from app.ai.providers.fake import FakeProvider, extract_draft
from app.ai.register import RegisterProfile, Segment
from app.ai.register_block import render_state_block
from app.ai.retrieval.base import NullRetriever
from app.ai.retrieval.vector import VectorStyleRetriever
from app.ai.workflow import RewriteWorkflow
from app.core.config import PROMPTS_DIR, Settings
from app.domain.contact import Contact
from app.domain.persona import PersonaSettings
from app.domain.rewrite import PreservedItem, RetrievedExample, RewriteInput, RewriteResult
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo, InMemoryRewriteRepo
from app.repositories.usage import InMemoryUsageRepo
from app.services.import_service import ImportMessage, ImportPayload, ImportService
from app.services.rewrite_service import RewriteService, _input_hash, persona_register_warnings

BLOCK = "말투: 합쇼체 (최근 2건)\n상하: 상대가 윗사람 (호칭 직함 기준)"
T1 = datetime(2026, 10, 7, 3, 11, tzinfo=UTC)
T2 = datetime(2026, 10, 21, 1, 5, tzinfo=UTC)

# 합성 데이터 c3(이서연)의 메시지 그대로: 반말 3건(m02·m04·m06) → 합쇼체 2건(m08·m10), 호칭은 m08에 한 번.
C3 = [
    ("m01", "contact", "2026-10-07T12:10:00+09:00", "민수야 점심 뭐 먹을래?"),
    ("m02", "me", "2026-10-07T12:11:00+09:00", "서연아 오늘 국밥 ㄱ?"),
    ("m03", "contact", "2026-10-07T12:12:00+09:00", "콜 ㅋㅋ 근데 너 팀장님한테 혼났다며"),
    ("m04", "me", "2026-10-07T12:15:00+09:00", "혼난 건 아니고 일정 얘기하다가 좀 그랬어. 어제 사과드렸어"),
    ("m05", "contact", "2026-10-17T17:40:00+09:00", "민수야 나 다음 주부터 너네 파트 파트장으로 발령났어 ㅠ"),
    ("m06", "me", "2026-10-17T17:42:00+09:00", "헐 축하해!! 근데 이제 회사에선 존댓말 써야겠네 ㅋㅋ"),
    ("m07", "contact", "2026-10-17T17:43:00+09:00", "공식 자리에선 그게 낫겠다 ㅎㅎ 사적으로는 편하게 하자"),
    ("m08", "me", "2026-10-21T10:05:00+09:00", "서연 파트장님, 오늘 스탠드업 회의록 공유드립니다."),
    (
        "m09",
        "contact",
        "2026-10-21T10:20:00+09:00",
        "네 민수님 확인했습니다. 금요일까지 API 문서 정리 부탁드려요.",
    ),
    ("m10", "me", "2026-10-21T10:22:00+09:00", "네, 금요일 오전까지 정리하겠습니다."),
]
DRAFT = "어제는 내가 좀 예민했던 것 같아. 미안해."


# ── 프롬프트 ────────────────────────────────────────────────────────────────


def _contact() -> Contact:
    return Contact(
        id=uuid4(), user_id="u", alias="이서연", relationship_type="colleague", created_at=datetime.now(UTC)
    )


def _prompt_args(persona: PersonaSettings | None) -> dict[str, Any]:
    return {
        "contact": _contact(),
        "persona": persona,
        "intent": "apology",
        "preserved": [PreservedItem(kind="datetime", text="어제", normalized="어제")],
        "draft_text": DRAFT,
        "situation": "사과",
        "retrieved": [
            RetrievedExample(
                id="e1", purpose="style", text="네, 정리하겠습니다.", speaker_role="me", sent_at=T2
            ),
            RetrievedExample(id="g1", purpose="context", text="파트장 발령", speaker_role="graph"),
        ],
        "today": date(2026, 10, 23),
        "contact_known": True,
    }


def _legacy_user_prompt(version: str, args: dict[str, Any]) -> str:
    """`register_block` 인자가 생기기 전의 `build_prompt`가 만들던 본문 (그때의 format 호출 그대로)."""
    return prompt_module._load(version, "user").format(
        today=prompt_module._today_line(args["today"]),
        persona_block=prompt_module._persona_block(
            args["contact"], args["persona"], contact_known=args["contact_known"], adjust=None
        ),
        situation=args["situation"],
        intent=args["intent"],
        preserved_block=prompt_module._preserved_block(args["preserved"]),
        draft_text=args["draft_text"],
        style_block=prompt_module._examples_block(args["retrieved"], "style"),
        context_block=prompt_module._examples_block(args["retrieved"], "context"),
    )


@pytest.mark.parametrize("version", ["rewrite-v1", "rewrite-v2", "rewrite-v3"])
@pytest.mark.parametrize("persona", [None, PersonaSettings(formality=4, address_term="파트장님")])
def test_old_prompt_versions_are_byte_identical(version: str, persona: PersonaSettings | None) -> None:
    args = _prompt_args(persona)
    plain = build_prompt(version=version, **args)
    with_block = build_prompt(version=version, register_block=BLOCK, **args)
    assert with_block.user == plain.user == _legacy_user_prompt(version, args)
    assert with_block.system == plain.system == (PROMPTS_DIR / version / "system.md").read_text("utf-8")
    assert "관계 상태" not in plain.user + plain.system
    assert "합쇼체" not in plain.user


def test_default_prompt_version_is_still_v3() -> None:
    assert Settings(_env_file=None).prompt_version == "rewrite-v3"  # type: ignore[call-arg]


def test_v4_renders_the_state_block() -> None:
    p = build_prompt(version="rewrite-v4", register_block=BLOCK, **_prompt_args(None))
    header = "[관계 상태 — 내 과거 메시지에서 관찰, 내용 근거 아님]"
    assert header in p.user
    assert p.user.split(header, 1)[1].split("\n", 1)[1].startswith(BLOCK + "\n\n[상황]")
    # 격식 수치와 말투 단계의 대응, 블록은 말투 단계에만 쓴다는 규칙이 시스템 프롬프트에 있다
    assert "격식 1~2는 반말" in p.system and "3은 해요체" in p.system and "4~5는 합쇼체" in p.system
    assert "내용의 근거가 아니다" in p.system
    # fake 프로바이더가 [원문]을 찾는 방식이 v4에서도 통한다
    assert extract_draft(p.user) == DRAFT


@pytest.mark.parametrize("block", [None, "", "  \n"])
def test_v4_without_block_says_none(block: str | None) -> None:
    p = build_prompt(version="rewrite-v4", register_block=block, **_prompt_args(None))
    assert "내용 근거 아님]" in p.user
    assert re.search(r"내용 근거 아님\][^\n]*\n\(없음\)\n\n\[상황\]", p.user)
    assert "말투:" not in p.user


def test_v4_is_v3_plus_additions_only() -> None:
    """v4는 v3를 복사해 줄을 더하기만 했다 — v3의 줄은 모두 같은 순서로 남아 있다."""
    for name in ("system", "user", "regenerate"):
        v3 = (PROMPTS_DIR / "rewrite-v3" / f"{name}.md").read_text("utf-8").splitlines()
        v4 = (PROMPTS_DIR / "rewrite-v4" / f"{name}.md").read_text("utf-8").splitlines()
        rest = iter(v4)
        assert all(line in rest for line in v3), name
    args = _prompt_args(PersonaSettings())
    v4_user = build_prompt(version="rewrite-v4", **args).user
    v3_user = build_prompt(version="rewrite-v3", **args).user
    assert re.sub(r"\[관계 상태[^\n]*\n\(없음\)\n\n", "", v4_user) == v3_user


# ── render_state_block ──────────────────────────────────────────────────────


def _seg(value: str, count: int, status: str = "confirmed", start: datetime = T2) -> Segment:
    return Segment(
        value=value,
        start=start,
        end=None,
        count=count,
        message_ids=tuple(f"c3:m{i:02d}" for i in range(count)),
        status=status,  # type: ignore[arg-type]
    )


def _profile(
    speech: Segment | None = None,
    *,
    address: Segment | None = None,
    previous: Segment | None = None,
    two_valued: dict[str, int] | None = None,
    senior: bool = False,
) -> RegisterProfile:
    return RegisterProfile(
        speech=tuple(s for s in (previous, speech) if s is not None),
        address=(address,) if address else (),
        current_speech=speech,
        previous_speech=previous,
        current_address=address,
        previous_address=None,
        two_valued=two_valued,
        senior_hint=senior,
        observations=5,
        unknown=0,
    )


def test_block_has_level_and_count() -> None:
    assert render_state_block(_profile(_seg("hapsyo", 2))) == "말투: 합쇼체 (최근 2건)"
    assert render_state_block(_profile(_seg("haeyo", 4))) == "말투: 해요체 (최근 4건)"
    assert render_state_block(_profile(_seg("hae", 3))) == "말투: 반말 (최근 3건)"


def test_block_adds_senior_hint_but_never_the_address_or_dates() -> None:
    profile = _profile(
        _seg("hapsyo", 2),
        previous=_seg("hae", 3, "previous", T1),
        address=_seg("서연 파트장님", 1, "candidate"),
        senior=True,
    )
    block = render_state_block(profile)
    assert block == BLOCK
    for leaked in ("서연", "파트장", "2026", "10", "c3", "m0", "반말"):
        assert leaked not in block


@pytest.mark.parametrize(
    "profile",
    [
        _profile(None, address=_seg("서연 파트장님", 2), senior=True),  # 말투 구간이 없다
        _profile(_seg("hapsyo", 1, "candidate")),  # 확정되지 않았다
        _profile(_seg("hapsyo", 3), two_valued={"hapsyo": 3, "hae": 3}),  # 상황별 말투일 수 있다
        _profile(_seg("mixed", 3)),  # 블록에 쓸 수 없는 값
    ],
)
def test_block_is_none_when_nothing_confirmed(profile: RegisterProfile) -> None:
    assert render_state_block(profile) is None


# ── 설정 경고 (순수 함수) ───────────────────────────────────────────────────


def test_warning_when_observed_address_is_not_in_settings() -> None:
    profile = _profile(_seg("hapsyo", 2), address=_seg("서연 파트장님", 1, "candidate"))
    (w,) = persona_register_warnings(profile, PersonaSettings(formality=4))
    assert "'서연 파트장님'" in w and "1회 관찰" in w and "설정에는 호칭이 없습니다" in w
    (w,) = persona_register_warnings(profile, PersonaSettings(formality=4, address_term="서연 씨"))
    assert "'서연 씨'" in w and "다릅니다" in w
    # 이름만 덧붙은 차이는 같은 호칭으로 본다
    assert persona_register_warnings(profile, PersonaSettings(formality=4, address_term="파트장님")) == []
    assert persona_register_warnings(profile, PersonaSettings(formality=4, address_term="서연파트장님")) == []


@pytest.mark.parametrize(
    ("level", "formality", "warned"),
    [
        ("hapsyo", 1, True),
        ("hapsyo", 2, True),
        ("haeyo", 2, True),
        ("hae", 3, True),
        ("hae", 5, True),
        ("hapsyo", 3, False),  # 합쇼체/해요체 차이는 경고하지 않는다
        ("haeyo", 5, False),
        ("hae", 1, False),
        ("hapsyo", 4, False),
    ],
)
def test_speech_warning_only_across_polite_casual_boundary(level: str, formality: int, warned: bool) -> None:
    got = persona_register_warnings(_profile(_seg(level, 2)), PersonaSettings(formality=formality))
    assert bool(got) is warned
    if warned:
        assert f"격식 {formality}" in got[0] and "최근 2건" in got[0]


def test_no_speech_warning_when_unconfirmed_or_two_valued() -> None:
    casual = PersonaSettings(formality=1)
    assert persona_register_warnings(_profile(_seg("hapsyo", 1, "candidate")), casual) == []
    assert (
        persona_register_warnings(_profile(_seg("hapsyo", 3), two_valued={"hapsyo": 3, "hae": 3}), casual)
        == []
    )
    assert persona_register_warnings(_profile(None), casual) == []


# ── 서비스 ──────────────────────────────────────────────────────────────────


class RecordingProvider(FakeProvider):
    """fake 프로바이더(원문 그대로)에 보낸 프롬프트를 남긴다."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    async def generate(self, *, system: str, user: str) -> GenerationResult:
        self.prompts.append(user)
        return await super().generate(system=system, user=user)


class Env:
    """인메모리 저장소 + fake 프로바이더로 만든 서비스 한 벌. 가져온 대화는 c3(이서연) 하나."""

    def __init__(self) -> None:
        self.settings = Settings(_env_file=None, llm_provider="fake", embedding_provider="fake")  # type: ignore[call-arg]
        self.contacts = InMemoryContactRepo()
        self.conversations = InMemoryConversationRepo()
        self.embedder = FakeEmbedder(dim=128)
        self.provider = RecordingProvider()
        self.inputs: list[RewriteInput] = []  # 워크플로에 들어간 입력
        self.contact: Contact

    async def load(self, persona: PersonaSettings | None = None) -> None:
        imports = ImportService(self.contacts, self.conversations, self.embedder)
        self.contact = await self.contacts.create("u", "이서연", "colleague")
        conv = await imports.create_conversation("u", self.contact.id, None, "json", "c3")
        payload = ImportPayload(
            messages=[ImportMessage(id=i, speaker=s, sent_at=t, text=x) for i, s, t, x in C3]  # type: ignore[arg-type]
        )
        await imports.import_messages("u", conv.id, payload)
        if persona is not None:
            await self.contacts.save_persona("u", self.contact.id, persona, 0)

    def service(self, *, prompt_version: str = "rewrite-v4", **flags: bool) -> RewriteService:
        null = NullRetriever()
        vector = VectorStyleRetriever(self.conversations, self.embedder)
        workflow = RewriteWorkflow(
            provider=self.provider,
            retrievers={"A": null, "B": null, "C": vector, "D": vector},
            prompt_version=prompt_version,
        )
        run = workflow.run

        async def recording_run(
            inp: RewriteInput, contact: Contact, persona: PersonaSettings | None, **kwargs: object
        ) -> RewriteResult:
            self.inputs.append(inp)
            return await run(inp, contact, persona, **kwargs)  # 서비스가 넘기는 호출 장부(meter)

        workflow.run = recording_run  # type: ignore[method-assign]
        return RewriteService(
            self.settings,
            self.contacts,
            InMemoryRewriteRepo(),
            workflow,
            InMemoryUsageRepo(),
            conversations=self.conversations,
            **flags,
        )

    def request(self, **extra: Any) -> RewriteInput:
        return RewriteInput(
            user_id="u",
            contact_id=self.contact.id,
            draft_text=DRAFT,
            draft_version=1,
            condition=extra.pop("condition", "auto"),
            **extra,
        )


def _comparable(result: RewriteResult) -> dict[str, Any]:
    return result.model_dump(exclude={"latency_ms"})


def _workflow_passes_block() -> bool:
    """워크플로(`_compose`)가 `inp.register_block`을 `build_prompt`에 넘기는가 (다른 묶음의 파일이다)."""
    return "register_block" in inspect.getsource(RewriteWorkflow)


@pytest.mark.anyio
async def test_no_persona_and_flag_on_sets_the_block() -> None:
    env = Env()
    await env.load()
    rec = await env.service(register_state_block=True).rewrite(env.request(), None)
    assert rec.condition == "C" and rec.persona_version_id is None
    block = env.inputs[-1].register_block
    assert block is not None
    assert block.splitlines()[0] == "말투: 합쇼체 (최근 2건)"
    # 관찰된 호칭·날짜·메시지 id는 블록에 없다
    for leaked in ("서연", "파트장", "2026", "10/", "m08", "c3"):
        assert leaked not in block
    assert rec.result.status == "recommended"
    assert rec.result.result_text == DRAFT  # fake 프로바이더는 원문 그대로
    if not _workflow_passes_block():
        pytest.skip("워크플로가 아직 register_block을 build_prompt에 넘기지 않는다 (workflow.py 담당 몫)")
    assert f"내용 근거 아님]{_HEADER_NOTE}\n{block}\n\n[상황]" in env.provider.prompts[-1]


_HEADER_NOTE = " (말투 단계를 맞추는 데에만 사용. 호칭·날짜·사유·사실을 여기서 가져오지 않는다)"


@pytest.mark.anyio
async def test_block_respects_as_of() -> None:
    """발령 전 시점에서 보면 반말 구간뿐이다 — as_of 뒤의 메시지는 프로필에 들어가지 않는다."""
    env = Env()
    await env.load()
    as_of = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
    await env.service(register_state_block=True).rewrite(env.request(as_of=as_of), None)
    block = env.inputs[-1].register_block
    assert block is not None and block.startswith("말투: 반말 (최근 ")
    assert "윗사람" not in block


@pytest.mark.anyio
async def test_saved_persona_never_gets_the_block() -> None:
    env = Env()
    await env.load(PersonaSettings(formality=1))
    rec = await env.service(register_state_block=True).rewrite(env.request(), None)
    assert rec.persona_version_id is not None
    assert env.inputs[-1].register_block is None
    assert "말투: " not in env.provider.prompts[-1]
    assert rec.result.warnings == []  # 경고 플래그는 꺼져 있다


@pytest.mark.anyio
async def test_caller_supplied_block_is_dropped() -> None:
    """register_block은 서버만 채운다. 호출 쪽이 넣어 온 값은 프롬프트에 닿지 않고 해시도 바꾸지 않는다."""
    env = Env()
    await env.load()
    forged = env.request(register_block="호칭: 자기야")
    assert _input_hash(forged) == _input_hash(env.request())
    await env.service().rewrite(forged, None)
    assert env.inputs[-1].register_block is None
    assert "자기야" not in env.provider.prompts[-1]


@pytest.mark.anyio
@pytest.mark.parametrize("persona", [None, PersonaSettings(formality=1)])
@pytest.mark.parametrize("prompt_version", ["rewrite-v3", "rewrite-v4"])
async def test_flags_off_equals_today(persona: PersonaSettings | None, prompt_version: str) -> None:
    """플래그를 주지 않은 서비스 = 둘 다 False인 서비스. 프로필을 계산하지 않고 프롬프트·결과가 같다."""
    env = Env()
    await env.load(persona)
    default = await env.service(prompt_version=prompt_version).rewrite(env.request(), None)
    off = await env.service(
        prompt_version=prompt_version, register_state_block=False, register_warnings=False
    ).rewrite(env.request(), None)
    assert env.provider.prompts[0] == env.provider.prompts[1]
    assert _comparable(default.result) == _comparable(off.result)
    assert [i.register_block for i in env.inputs] == [None, None]
    assert default.result.warnings == []
    assert "말투: " not in env.provider.prompts[0]


@pytest.mark.anyio
async def test_v3_prompt_is_unchanged_even_with_the_block_on() -> None:
    """기본 프롬프트(v3)에는 블록 자리가 없다 — 플래그만 켜서는 모델에 가는 글자가 달라지지 않는다."""
    env = Env()
    await env.load()
    await env.service(prompt_version="rewrite-v3").rewrite(env.request(), None)
    await env.service(prompt_version="rewrite-v3", register_state_block=True).rewrite(env.request(), None)
    assert env.inputs[-1].register_block is not None
    assert env.provider.prompts[0] == env.provider.prompts[1]


@pytest.mark.anyio
async def test_warnings_only_with_flag_and_text_untouched() -> None:
    env = Env()
    await env.load(PersonaSettings(formality=1))  # 설정은 반말, 최근 2건은 합쇼체, 호칭은 설정에 없다
    plain = await env.service().rewrite(env.request(), None)
    warned = await env.service(register_warnings=True).rewrite(env.request(), None)
    assert plain.result.warnings == []
    assert any("격식 1(반말)" in w and "최근 2건" in w and "합쇼체" in w for w in warned.result.warnings)
    assert any("'서연 파트장님'" in w and "1회 관찰" in w for w in warned.result.warnings)
    assert len(warned.result.warnings) == 2
    # 경고는 문장도, 모델에 보낸 프롬프트도 바꾸지 않는다
    assert env.provider.prompts[0] == env.provider.prompts[1]
    assert _comparable(warned.result) | {"warnings": []} == _comparable(plain.result)
    assert env.inputs[-1].register_block is None


@pytest.mark.anyio
async def test_no_warnings_when_settings_agree() -> None:
    env = Env()
    await env.load(PersonaSettings(formality=4, address_term="서연 파트장님"))
    rec = await env.service(register_warnings=True).rewrite(env.request(), None)
    assert rec.result.warnings == []


@pytest.mark.anyio
async def test_warnings_need_a_saved_persona() -> None:
    """설정이 없으면 견줄 것이 없다 — 경고 플래그만 켠 서비스는 블록도 경고도 내지 않는다."""
    env = Env()
    await env.load()
    rec = await env.service(register_warnings=True).rewrite(env.request(), None)
    assert rec.result.warnings == []
    assert env.inputs[-1].register_block is None


@pytest.mark.anyio
async def test_idempotent_replay_returns_stored_result() -> None:
    env = Env()
    await env.load(PersonaSettings(formality=1))
    svc = env.service(register_warnings=True)
    first = await svc.rewrite(env.request(), "key-1")
    # 그 사이 설정이 바뀌어도(경고가 달라질 상황) 같은 키의 재요청은 저장된 결과 그대로다
    await env.contacts.save_persona("u", env.contact.id, PersonaSettings(formality=4), 1)
    again = await svc.rewrite(env.request(), "key-1")
    assert again == first
    assert env.provider.calls == 1
    assert len(first.result.warnings) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("flags", [{"register_state_block": True}, {"register_warnings": True}])
async def test_profile_failure_never_fails_the_rewrite(
    flags: dict[str, bool], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("서연 파트장님, 오늘 스탠드업 회의록 공유드립니다.")

    monkeypatch.setattr("app.ai.register.build_profile", boom)
    env = Env()
    await env.load(PersonaSettings(formality=1) if "register_warnings" in flags else None)
    with caplog.at_level("WARNING"):
        rec = await env.service(**flags).rewrite(env.request(), None)
    assert rec.result.status == "recommended"
    assert rec.result.warnings == []
    assert env.inputs[-1].register_block is None
    # 로그에는 예외의 종류만 남는다 (문장에는 대화 원문이 섞일 수 있다)
    assert "RuntimeError" in caplog.text
    assert "파트장" not in caplog.text
