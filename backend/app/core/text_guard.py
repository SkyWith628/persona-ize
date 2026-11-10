"""저장할 수 없는 문자가 든 입력을 저장 계층에 닿기 전에 422 `invalid_input`으로 돌려보낸다.

- NUL(U+0000): PostgreSQL의 text·varchar는 담지 못한다.
- 짝 없는 대리 문자(U+D800~U+DFFF): UTF-8로 쓸 수 없다. 파이썬 `json.loads`는 이스케이프(`"\\ud800"`)로 온
  것도, UTF-8 꼴 바이트(ED A0 80)로 직접 적힌 것도 그대로 문자열에 넣는다.

둘 다 예전에는 DB에 넣다가 500이 났다 — 첨삭은 모델을 부른 뒤에 터졌고, 그 스택에 입력 원문이 실렸다
(2026-11-11 검토). 어느 라우트에 어떤 필드가 생기든 같게 막으려고 FastAPI 전역 의존성으로 건다
(`main.create_app`). 본문은 FastAPI가 이미 읽어 파싱한 것을 다시 쓴다(`Request.body()`·`.json()`은 캐시된다).

대부분의 요청은 바이트 검사 한 번으로 끝난다. `\\u0000`·대리 문자 이스케이프도, 대리 문자 꼴 바이트도,
0 바이트(UTF-16·32 본문)도 없으면 파싱한 문자열에 저런 문자가 생길 수 없다. 의심스러울 때만 파싱 결과를
훑는다 — 그래서 `"\\\\u0000"`(역슬래시 뒤에 u0000이라는 글자 여섯 개)처럼 실제 NUL이 아닌 것은 걸리지 않는다.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import Request

from app.core.errors import InvalidInputError

MESSAGE = "입력에 쓸 수 없는 문자(NUL 또는 깨진 유니코드)가 있습니다"
MAX_LOC_CHARS = 120
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")
# 본문 바이트에 이 가운데 하나도 없으면 파싱한 문자열에 NUL·대리 문자가 있을 수 없다.
_SUSPECT_BYTES = re.compile(
    rb"\\u(?:0000|[dD][89a-fA-F][0-9a-fA-F]{2})"  # JSON 이스케이프로 쓴 NUL·대리 문자
    rb"|\x00"  # 0 바이트: UTF-16·32로 인코딩한 본문
    rb"|\xed[\xa0-\xbf]"  # 대리 문자를 UTF-8 꼴로 직접 적은 바이트 (json.loads가 받아들인다)
)


def find_unstorable(value: Any) -> str | None:
    """JSON에서 온 값(dict·list·str…)을 훑어 문제 문자가 든 곳의 위치를 돌려준다. 없으면 None.

    위치는 `messages.3.text` 꼴이다. 재귀 없이 돈다 — 깊게 겹친 본문이 호출 스택을 넘기지 않게.
    """
    stack: list[tuple[Any, str]] = [(value, "")]
    while stack:
        current, path = stack.pop()
        if isinstance(current, str):
            if _UNSTORABLE.search(current):
                return path or "본문"
        elif isinstance(current, dict):
            for key, item in current.items():
                if isinstance(key, str) and _UNSTORABLE.search(key):
                    return f"{path}.(항목 이름)" if path else "(항목 이름)"  # 이름 자체는 되돌려 주지 않는다
                stack.append((item, f"{path}.{key}" if path else str(key)))
        elif isinstance(current, list):
            for index, item in enumerate(current):
                stack.append((item, f"{path}.{index}" if path else str(index)))
    return None


async def reject_unstorable_text(request: Request) -> None:
    """모든 API 라우트 앞에서 돈다. 주소(경로·쿼리)와 JSON 본문의 문자열을 본다."""
    if "\x00" in request.scope.get("path", "") or b"%00" in request.scope.get("query_string", b""):
        raise InvalidInputError(f"{MESSAGE}: 주소")
    if request.method not in BODY_METHODS:
        return
    body = await request.body()
    if not body or _SUSPECT_BYTES.search(body) is None:
        return
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 - JSON이 아니거나 깨진 본문은 라우트의 본문 검증이 거절한다
        return
    location = find_unstorable(data)
    if location is not None:
        raise InvalidInputError(f"{MESSAGE}: {location[:MAX_LOC_CHARS]}")
