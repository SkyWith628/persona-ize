"""말투 프로필 → 프롬프트의 [관계 상태] 블록 (docs/design/graph-redesign-2026-11-16.md 5.3).

저장된 설정이 없는 상대에게만 쓰는 블록이다. 모델이 여기서 가져가도 되는 것은 **말투 단계 하나**뿐이라
블록에 넣는 것도 그만큼만이다.
- 넣는 것: 확정된 현재 말투 단계와 그 건수("말투: 합쇼체 (최근 2건)"), 호칭 직함에서 얻은 위계 힌트.
- 넣지 않는 것: 관찰된 호칭 문자열, 날짜, 메시지 id, 이전 구간, 그래프에서 온 어떤 것도.
  호칭은 승인된 설정을 통해서만 첨삭에 들어가고(P8), 날짜·사건은 내용으로 새어 들어갈 수 있다.
- 블록을 만들지 않는 경우(None): 확정된 현재 말투 구간이 없을 때, 최근 메시지에 두 단계가 섞여 있을 때
  (`two_valued` — 상황별 말투일 수 있어 하나로 정해 주지 않는다).

순수 함수다. 입출력도 모델 호출도 없다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.ai.register import RegisterProfile

# 블록에 쓸 수 있는 말투 단계. 프로필의 값이 이 밖이면(혼용·알 수 없음) 블록을 만들지 않는다.
# 이름은 `app.ai.register.LEVEL_KO`와 같다 — 프롬프트 글자가 그쪽 표기 변경에 따라 바뀌지 않게 여기 고정한다.
_LEVEL_LINE = {"hapsyo": "합쇼체", "haeyo": "해요체", "hae": "반말"}


def render_state_block(profile: RegisterProfile) -> str | None:
    """[관계 상태] 블록의 본문. 넣을 것이 없으면 None (프롬프트에는 '(없음)'으로 나간다)."""
    current = profile.current_speech
    if current is None or current.status != "confirmed" or profile.two_valued:
        return None
    level = _LEVEL_LINE.get(current.value)
    if level is None or current.count < 1:
        return None
    lines = [f"말투: {level} (최근 {current.count}건)"]
    if profile.senior_hint:
        lines.append("상하: 상대가 윗사람 (호칭 직함 기준)")
    return "\n".join(lines)
