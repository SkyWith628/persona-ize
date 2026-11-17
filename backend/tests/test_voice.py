"""내 말투 지문(`app.ai.voice`)과 rewrite-v5 프롬프트 연결.

- 지문: 버릇을 건수와 함께 적고, 대화 내용(이름·호칭·날짜)은 싣지 않는다. 근거가 얇으면 만들지 않는다.
- 프롬프트: rewrite-v4까지는 `voice_block`을 줘도 글자까지 그대로다. rewrite-v5만 자리가 있다.
- 서비스: 플래그를 켠 C·D 요청에만 붙인다. 꺼 두면 메시지를 읽지 않는다. 계산이 실패해도 첨삭은 끝난다.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.ai import voice as voice_module
from app.ai.prompt import build_prompt
from app.ai.voice import MIN_MESSAGES, render_voice_block
from app.core.config import PROMPTS_DIR, Settings
from app.domain.persona import PersonaSettings

from .test_register_prompt import Env, _prompt_args

CASUAL = [
    "서연아 오늘 국밥 ㄱ?",
    "헐 진짜? 축하해!! ㅋㅋ",
    "근데 나 좀 늦을 것 같아 ㅠ",
    "ㅇㅋ 그럼 거기서 봐 ㅋㅋㅋ",
    "헐 대박 ㅋㅋ 진짜 고마워",
]
FORMAL = [
    "팀장님, 주간보고 초안 공유드립니다.",
    "네, 수요일까지 정리하겠습니다.",
    "확인했습니다. 금요일 오전에 다시 확인하겠습니다.",
    "말씀하신 부분 반영했습니다.",
]


def test_casual_habits_are_counted() -> None:
    block = render_voice_block(CASUAL)
    assert block is not None
    assert block.startswith("(이 상대에게 보낸 내 메시지 5건에서 센 것)")
    assert "웃음 표시: 'ㅋㅋ'" in block and "(5건 중 3건)" in block
    assert "마침표: 거의 찍지 않음 (5건 중 0건)" in block
    assert "말머리: '헐'" in block
    assert "자주 쓰는 말: '진짜'" in block
    assert "자음 줄임말:" in block and "'ㅇㅋ'" in block
    assert "느낌표" not in block, "한 건뿐인 것은 버릇으로 치지 않는다"


def test_formal_habits_are_counted() -> None:
    block = render_voice_block(FORMAL)
    assert block is not None
    assert "마침표: 문장 끝에 찍음 (4건 중 4건)" in block
    assert "'~겠습니다' 2번" in block
    assert "웃음 표시" not in block


def test_block_never_carries_names_addresses_or_content() -> None:
    """블록의 글자는 닫힌 목록과 문장부호뿐이다 — 호칭이 설정을 거치지 않고 프롬프트에 들어가면 안 된다."""
    for texts in (CASUAL, FORMAL):
        block = render_voice_block(texts) or ""
        for leaked in ("서연", "팀장님", "국밥", "주간보고", "수요일", "금요일", "축하"):
            assert leaked not in block


@pytest.mark.parametrize(
    "texts",
    [[], ["네"], CASUAL[: MIN_MESSAGES - 1], ["   ", "", "\n"]],
)
def test_no_block_without_enough_evidence(texts: list[str]) -> None:
    assert render_voice_block(texts) is None


def test_v5_renders_the_voice_block_and_older_versions_ignore_it() -> None:
    args = _prompt_args(PersonaSettings())
    block = render_voice_block(CASUAL)
    v5 = build_prompt(version="rewrite-v5", voice_block=block, **args)
    assert "[내 말투 지문" in v5.user and "웃음 표시: 'ㅋㅋ'" in v5.user
    assert "[내 말투 지문" in build_prompt(version="rewrite-v5", **args).user
    assert (
        "(없음)"
        in build_prompt(version="rewrite-v5", **args).user.split("[내 말투 지문")[1].split("[상황]")[0]
    )
    for version in ("rewrite-v3", "rewrite-v4"):
        assert (
            build_prompt(version=version, voice_block=block, **args).user
            == build_prompt(version=version, **args).user
        )


def test_v5_keeps_the_fact_and_address_rules() -> None:
    """개성을 살리되 사실·호칭 규칙은 그대로다 (v4의 규칙 번호가 v5에 남아 있다)."""
    system = (PROMPTS_DIR / "rewrite-v5" / "system.md").read_text("utf-8")
    for kept in (
        "사실과 의도를 바꾸지 않는다",
        "**호칭 규칙.**",
        "**날짜 대조.**",
        "**격식 수치와 말투 단계.**",
    ):
        assert kept in system
    assert "**내 말투.**" in system
    assert "없는 버릇을 지어내지 않는다" in system
    assert Settings(_env_file=None).prompt_version == "rewrite-v3", "기본 버전은 바꾸지 않는다"  # type: ignore[call-arg]
    assert Settings(_env_file=None).voice_fingerprint is False  # type: ignore[call-arg]


@pytest.mark.anyio
async def test_flag_on_sets_the_block_for_past_expression_conditions() -> None:
    env = Env()
    await env.load(PersonaSettings())
    service = env.service(prompt_version="rewrite-v5", voice_fingerprint=True)
    await service.rewrite(env.request(), None)  # auto → C (가져온 대화가 있다)
    assert env.inputs[-1].condition == "C"
    assert env.inputs[-1].voice_block and "내 메시지" in env.inputs[-1].voice_block
    assert "[내 말투 지문" in env.provider.prompts[-1]
    assert env.inputs[-1].voice_block in env.provider.prompts[-1]
    # B는 과거 대화를 쓰지 않는 조건이다
    await service.rewrite(env.request(condition="B"), None)
    assert env.inputs[-1].voice_block is None


@pytest.mark.anyio
async def test_flag_off_reads_nothing_and_caller_value_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env()
    await env.load(PersonaSettings())

    def boom(*_: Any) -> str:
        raise AssertionError("플래그가 꺼져 있으면 지문을 계산하지 않는다")

    monkeypatch.setattr(voice_module, "render_voice_block", boom)
    await env.service(prompt_version="rewrite-v5").rewrite(env.request(voice_block="호칭은 형님"), None)
    assert env.inputs[-1].voice_block is None
    assert "형님" not in env.provider.prompts[-1]


@pytest.mark.anyio
async def test_fingerprint_failure_never_fails_the_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env()
    await env.load(PersonaSettings())

    def boom(*_: Any) -> str:
        raise RuntimeError("대화 원문이 섞인 예외 문장")

    monkeypatch.setattr(voice_module, "render_voice_block", boom)
    result = await env.service(prompt_version="rewrite-v5", voice_fingerprint=True).rewrite(
        env.request(), None
    )
    assert result.result.result_text
    assert env.inputs[-1].voice_block is None
