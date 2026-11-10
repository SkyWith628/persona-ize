"""2026-11-11 독립 검토(보안·정확성)에서 나온 지적의 회귀 테스트 — core 영역.

C1 비용 상한 없는 운영 설정 거부 · C2 로그에 원문 없음 + 저장할 수 없는 문자는 422 · C3 로그인 잠금 범위
· C4 관리자 이름 열거 · C5 깊은 헬스체크 캐시·요청 제한 · C6 사용량 한도의 Retry-After.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import TooManyAttemptsError, UnauthorizedError
from app.core.guard import (
    UnsafeConfigError,
    enforce_production_guard,
    has_model_credentials,
    inspect_production_settings,
)
from app.core.safe_exc import OMITTED, format_exception_safely, safe_exception_message
from app.core.text_guard import find_unstorable
from app.main import create_app
from app.repositories.auth import InMemoryAuthRepo
from app.services.auth_service import AuthService, FailureLimits

# 운영 모드의 다른 검사(헤더 스텁·쿠키·DB)를 통과하는 바탕. 127.0.0.1:1 은 곧바로 접속이 거절되는 주소.
PROD = {
    "app_env": "production",
    "auth_allow_header_stub": False,
    "session_cookie_secure": True,
    "database_url": "postgresql+asyncpg://persona:Str0ng-Pass-2026@127.0.0.1:1/persona_ize",
}
GRAPH_ON = {"neo4j_uri": "bolt://neo4j:7687", "neo4j_password": "Gr4ph-Pass-2026"}
# 검사는 값이 있는지만 본다 — 이 테스트들은 모델도 그래프도 부르지 않는다.
WITH_KEY = {"anthropic_api_key": "not-a-real-key"}
INVITE = {"register_invite_code": "come-in-2026"}


def _settings(**overrides) -> Settings:  # noqa: ANN003
    base = {"llm_provider": "fake", "embedding_provider": "fake", "anthropic_api_key": ""}
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[arg-type]


def _prod(**overrides) -> Settings:  # noqa: ANN003
    return _settings(**{**PROD, **overrides})


def _errors(**overrides) -> list[str]:  # noqa: ANN003
    return list(inspect_production_settings(_prod(**overrides)).errors)


def _warnings(**overrides) -> list[str]:  # noqa: ANN003
    return list(inspect_production_settings(_prod(**overrides)).warnings)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


# ---------- C1. 비용 상한이 꺼진 운영 설정 ----------


def test_c1_graph_without_any_cap_is_refused_when_the_model_can_be_called() -> None:
    errors = _errors(**GRAPH_ON, **WITH_KEY, **INVITE)  # 상한 기본값 = 0, 0 (무제한)
    assert len(errors) == 1
    # 무엇을 정하면 되는지 메시지에 있다
    for expected in (
        "GRAPH_INGEST_USER_MESSAGE_CAP",
        "GRAPH_INGEST_DAILY_GLOBAL_CAP",
        "ALLOW_UNCAPPED_COSTS=true",
    ):
        assert expected in errors[0]
    # LLM_PROVIDER=fake여도 그래프 수집은 실제 모델을 부른다 — 위 설정이 바로 가짜 모델이다
    assert _prod(**GRAPH_ON, **WITH_KEY).llm_provider == "fake"

    # 상한을 정하면 통과하고 경고도 없다 (운영 예시 값)
    capped = {"graph_ingest_user_message_cap": 300, "graph_ingest_daily_global_cap": 1000}
    assert _errors(**GRAPH_ON, **WITH_KEY, **INVITE, **capped) == []
    assert not [w for w in _warnings(**GRAPH_ON, **WITH_KEY, **INVITE, **capped) if "GRAPH_INGEST" in w]
    # 그래프를 쓰지 않으면(NEO4J_URI 없음) 상한이 0이어도 상관없다
    assert _errors(**WITH_KEY, **INVITE) == []


def test_c1_graph_without_cap_only_warns_while_no_credentials_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    """키가 없으면 수집이 어차피 실패한다(비용 0) → 경고만. 키가 생기는 순간(재시작) 거부로 바뀐다."""
    assert _errors(**GRAPH_ON, **INVITE) == []
    warnings = [w for w in _warnings(**GRAPH_ON, **INVITE) if "GRAPH_INGEST" in w]
    assert len(warnings) == 1 and "ANTHROPIC_API_KEY가 없어" in warnings[0]
    assert has_model_credentials(_prod()) is False

    # SDK는 ANTHROPIC_AUTH_TOKEN 환경변수로도 인증한다 — 그것도 '부를 수 있다'로 본다
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "not-a-real-token")
    assert has_model_credentials(_prod()) is True
    assert len(_errors(**GRAPH_ON, **INVITE)) == 1


def test_c1_graph_with_one_cap_depends_on_who_can_sign_up() -> None:
    user_only = {"graph_ingest_user_message_cap": 300}  # 하루 전체 상한 없음
    daily_only = {"graph_ingest_daily_global_cap": 1000}  # 사용자당 상한 없음

    # 가입이 초대 코드 없이 열려 있으면 '사용자당 상한 × 가입자 수'에 끝이 없다 → 거부
    open_signup = _errors(**GRAPH_ON, **WITH_KEY, **user_only)
    assert len(open_signup) == 1 and "REGISTER_INVITE_CODE" in open_signup[0]
    assert "GRAPH_INGEST_DAILY_GLOBAL_CAP" in open_signup[0]
    # 초대 코드가 있거나 가입이 닫혀 있으면 가입자 수가 묶인다 → 통과, 다만 알려 준다
    for bounded in (INVITE, {"auth_allow_register": False}):
        assert _errors(**GRAPH_ON, **WITH_KEY, **user_only, **bounded) == []
        notes = [w for w in _warnings(**GRAPH_ON, **WITH_KEY, **user_only, **bounded) if "GRAPH_INGEST" in w]
        assert len(notes) == 1 and "가입자 수" in notes[0]
    # 하루 전체 상한만 있으면 비용은 묶인다 → 통과 (한 사용자가 다 쓸 수 있다는 경고)
    assert [e for e in _errors(**GRAPH_ON, **WITH_KEY, **daily_only) if "그래프" in e] == []
    notes = [w for w in _warnings(**GRAPH_ON, **WITH_KEY, **daily_only, **INVITE) if "GRAPH_INGEST" in w]
    assert len(notes) == 1 and "GRAPH_INGEST_USER_MESSAGE_CAP=0" in notes[0]


def test_c1_open_signup_with_real_model_needs_a_global_breaker() -> None:
    real = {"llm_provider": "anthropic"}  # 키 유무와 무관 — 실제 모델을 쓰겠다는 설정이다
    errors = _errors(**real)
    assert len(errors) == 1
    for expected in (
        "USAGE_GLOBAL_DAILY_REWRITE_LIMIT",
        "USAGE_GLOBAL_DAILY_TOKEN_LIMIT",
        "REGISTER_INVITE_CODE",
        "ALLOW_UNCAPPED_COSTS=true",
    ):
        assert expected in errors[0]
    assert len(_errors(**real, **WITH_KEY)) == 1

    # 넷 중 하나만 있어도 끝없는 비용은 아니다
    assert _errors(**real, **INVITE) == []
    assert _errors(**real, auth_allow_register=False) == []
    assert _errors(**real, usage_global_daily_rewrite_limit=2000) == []
    assert _errors(**real, usage_global_daily_token_limit=4_000_000) == []
    # 가짜 모델이면 첨삭 비용이 없다 → 해당 없음 (가입 열림 경고는 따로 나온다)
    assert _errors() == []
    assert any("REGISTER_INVITE_CODE" in w for w in _warnings())


def test_c1_allow_uncapped_costs_turns_refusals_into_warnings(caplog: pytest.LogCaptureFixture) -> None:
    worst = {"llm_provider": "anthropic", **GRAPH_ON, **WITH_KEY}  # 그래프 무제한 + 공개 가입 + 차단기 없음
    assert len(_errors(**worst)) == 2

    with (
        caplog.at_level(logging.WARNING, logger="app.core.guard"),
        pytest.raises(UnsafeConfigError) as raised,
    ):
        enforce_production_guard(_prod(**worst))
    message = str(raised.value)
    assert "그래프 수집 비용에 상한이 없습니다" in message and "첨삭 비용에 전체 상한이 없습니다" in message
    assert [r for r in caplog.records if r.levelno == logging.CRITICAL]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.core.guard"):
        report = enforce_production_guard(_prod(**worst, allow_uncapped_costs=True))
    assert report.errors == ()
    allowed = [w for w in report.warnings if w.startswith("ALLOW_UNCAPPED_COSTS=true라서 막지 않습니다")]
    assert len(allowed) == 2 and not any("ALLOW_UNCAPPED_COSTS=true." in w for w in allowed)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "그래프 수집 비용에 상한이 없습니다" in logged and not [
        r for r in caplog.records if r.levelno >= logging.ERROR
    ]


def test_c1_rehearsal_and_example_settings_still_pass() -> None:
    """로컬 리허설(가짜 모델·키 없음·그래프 켜짐·상한 300/1000·초대 코드)과 운영 예시 값은 그대로 통과한다."""
    rehearsal = {
        **GRAPH_ON,
        **INVITE,
        "graph_ingest_user_message_cap": 300,
        "graph_ingest_daily_global_cap": 1000,
        "usage_global_daily_rewrite_limit": 2000,
        "usage_global_daily_token_limit": 4_000_000,
    }
    assert _errors(**rehearsal) == []
    assert not [w for w in _warnings(**rehearsal) if "상한" in w]
    production_example = {**rehearsal, "llm_provider": "anthropic", **WITH_KEY}
    assert _errors(**production_example) == []
    assert not [w for w in _warnings(**production_example) if "상한" in w]
    # 개발 모드에서는 어떤 조합이어도 아무 일도 하지 않는다
    dev = _settings(llm_provider="anthropic", **GRAPH_ON, **WITH_KEY)
    report = enforce_production_guard(dev)
    assert (report.errors, report.warnings) == ((), ())


# ---------- C2. 로그에 원문이 남지 않는다 · 저장할 수 없는 문자는 422 ----------

MARK = "LOGMARK"
PRIVATE_TEXT = MARK + " 사적인 대화 원문"
# JSON 이스케이프로 쓴 짝 없는 대리 문자 — 클라이언트는 이것을 ASCII 여섯 글자(\ud800)로 보낸다
LONE_SURROGATE = bytes([0x5C]) + b"ud800"
JSON_HEADERS = {"Content-Type": "application/json"}


def _stub() -> dict[str, str]:
    return {"X-User-Id": f"u-{uuid.uuid4().hex[:10]}"}


def _everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    """기록된 모든 것: 메시지, 포매터가 만들어 둔 스택, 예외 객체가 달려 있으면 원래 모양의 스택까지."""
    parts: list[str] = []
    for record in caplog.records:
        parts.append(record.getMessage())
        if record.exc_text:
            parts.append(record.exc_text)
        if record.exc_info:
            parts.append(logging.Formatter().formatException(record.exc_info))
    return "\n".join(parts)


def test_c2_unstorable_characters_are_rejected_before_storage_and_never_logged(
    pg_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """검토에서 500이 났던 8곳: NUL·짝 없는 대리 문자가 든 본문은 저장·모델 호출 전에 422로 끝난다."""
    app = create_app(_settings(database_url=pg_url))
    h = _stub()
    with TestClient(app, raise_server_exceptions=False) as c:
        made = c.post("/v1/contacts", json={"alias": "정상", "relationship_type": "friend"}, headers=h)
        contact = made.json()["id"]
        conv = c.post("/v1/conversations", json={"contact_id": contact, "source_key": "c1"}, headers=h).json()
        seed = c.post(
            "/v1/rewrites", json={"contact_id": contact, "draft_text": "안녕", "condition": "A"}, headers=h
        )
        assert seed.status_code == 201, seed.text
        message = {"id": "m1", "speaker": "me", "sent_at": "2026-10-05T09:00:00+09:00"}
        surrogate_alias = (
            b'{"alias": "' + MARK.encode() + b"-SURR" + LONE_SURROGATE + b'", "relationship_type": "friend"}'
        )
        surrogate_import = (
            b'{"messages": [{"id": "m2", "speaker": "me", "sent_at": "2026-10-05T09:01:00+09:00", "text": "'
            + MARK.encode()
            + b" private text "
            + LONE_SURROGATE
            + b'"}]}'
        )
        imports = f"/v1/conversations/{conv['id']}/imports"
        cases = {
            "상대 이름에 NUL": lambda: c.post(
                "/v1/contacts",
                json={"alias": f"{MARK}-ALIAS\u0000x", "relationship_type": "friend"},
                headers=h,
            ),
            "상대 이름에 짝 없는 대리 문자": lambda: c.post(
                "/v1/contacts", content=surrogate_alias, headers={**h, **JSON_HEADERS}
            ),
            "대화방 제목에 NUL": lambda: c.post(
                "/v1/conversations",
                json={"contact_id": contact, "title": f"{MARK}-TITLE\u0000", "source_key": "c2"},
                headers=h,
            ),
            "메시지 id에 NUL": lambda: c.post(
                imports,
                json={
                    "messages": [{**message, "id": "m\u00001", "text": f"{MARK}-MESSAGE 사적인 대화 원문"}]
                },
                headers=h,
            ),
            "메시지 본문에 짝 없는 대리 문자": lambda: c.post(
                imports, content=surrogate_import, headers={**h, **JSON_HEADERS}
            ),
            "초안에 NUL": lambda: c.post(
                "/v1/rewrites",
                json={"contact_id": contact, "draft_text": f"{MARK}-DRAFT 초안\u0000 내용", "condition": "A"},
                headers=h,
            ),
            "페르소나 메모에 NUL": lambda: c.put(
                f"/v1/contacts/{contact}/persona",
                json={"settings": {"notes": f"{MARK}-NOTES\u0000"}, "expected_version": None},
                headers=h,
            ),
            "피드백 편집본에 NUL": lambda: c.post(
                f"/v1/rewrites/{seed.json()['id']}/feedback",
                json={"accepted": False, "edited_text": f"{MARK}-EDITED\u0000 고친 문장"},
                headers=h,
            ),
        }
        with caplog.at_level(logging.DEBUG):
            for name, call in cases.items():
                r = call()
                assert r.status_code == 422, (name, r.status_code, r.text)
                assert r.json()["code"] == "invalid_input" and "쓸 수 없는 문자" in r.json()["message"], name
                assert MARK not in r.text, name  # 응답에도 값을 되돌려 주지 않는다
        assert MARK not in _everything_logged(caplog)
        # 아무것도 저장되지 않았고 모델도 부르지 않았다 (처음에 만든 것만 남아 있다)
        assert [x["alias"] for x in c.get("/v1/contacts", headers=h).json()] == ["정상"]
        assert len(c.get(f"/v1/conversations?contact_id={contact}", headers=h).json()) == 1
        assert c.get(f"/v1/conversations/{conv['id']}/messages", headers=h).json() == []
        assert c.get("/v1/usage", headers=h).json()["rewrites"] == 1


def test_c2_unstorable_check_has_no_false_positives() -> None:
    """실제 NUL이 아닌 것(이스케이프된 역슬래시, 정상 이모지, 한글)은 그대로 통과한다."""
    client = TestClient(create_app(_settings()))
    h = _stub()

    def alias_of(raw_json_string: bytes) -> str:
        body = b'{"alias": ' + raw_json_string + b', "relationship_type": "friend"}'
        r = client.post("/v1/contacts", content=body, headers={**h, **JSON_HEADERS})
        assert r.status_code == 201, (raw_json_string, r.text)
        return r.json()["alias"]

    backslash = bytes([0x5C])
    # 역슬래시 두 개 + u0000 = '\\u0000'이라는 글자 여섯 개 (NUL이 아니다)
    assert alias_of(b'"a' + backslash * 2 + b'u0000b"') == "a\\u0000b"
    # 대리 문자 쌍으로 쓴 이모지는 한 글자로 합쳐진다. UTF-8로 직접 쓴 이모지·한글도 그대로
    assert alias_of(b'"ok ' + backslash + b"ud83d" + backslash + b'ude00"') == "ok \U0001f600"
    assert (
        alias_of('"한글 이름 😀 퀀텀"'.encode()) == "한글 이름 😀 퀀텀"
    )  # '퀀'·'텀'은 UTF-8 첫 바이트가 0xED
    assert alias_of(b'"caf' + backslash + b'u00e9"') == "café"  # 다른 \u 이스케이프는 상관없다

    # 걸리는 것: 홀수 개의 역슬래시 뒤 u0000, 낮은 쪽 대리 문자만, UTF-8 꼴로 직접 적은 대리 문자, 주소의 %00
    for bad in (
        b'"a' + backslash * 3 + b'u0000"',
        b'"a' + backslash + b'udc00"',
        b'"a\xed\xa0\x80"',
    ):
        body = b'{"alias": ' + bad + b', "relationship_type": "friend"}'
        r = client.post("/v1/contacts", content=body, headers={**h, **JSON_HEADERS})
        assert r.status_code == 422 and r.json()["code"] == "invalid_input", (bad, r.text)
    key_body = b'{"alias": "x", "relationship_type": "friend", "k' + backslash + b'u0000": 1}'
    assert client.post("/v1/contacts", content=key_body, headers={**h, **JSON_HEADERS}).status_code == 422
    for address in ("/v1/contacts?limit=%00", "/v1/contacts/abc%00def"):
        r = client.get(address, headers=h)
        assert r.status_code == 422 and r.json()["message"].endswith(": 주소"), (address, r.text)
    # 로그인처럼 저장하지 않는 경로도 같다 (검사는 라우트를 가리지 않는다) — 401보다 먼저다
    r = client.post("/v1/auth/login", json={"username": "minsu_kim", "password": "pw\u0000-long-enough"})
    assert r.status_code == 422 and r.json()["message"].endswith(": password")

    assert find_unstorable({"a": [1, {"b": "ok"}, None, True, 2.5]}) is None
    assert find_unstorable({"messages": [{"text": "fine"}, {"text": "bad\x00"}]}) == "messages.1.text"
    assert find_unstorable("\ud800") == "본문"
    assert find_unstorable({"bad\x00key": 1}) == "(항목 이름)"
    deep: list = []
    for _ in range(5000):  # 깊게 겹쳐도 호출 스택을 넘기지 않는다
        deep = [deep]
    assert find_unstorable(deep) is None


def test_c2_escaped_db_errors_are_500_without_values_in_log_or_response(
    pg_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """입력 검사를 지나친 DB 데이터 오류: 드라이버·서버가 오류 메시지에 값을 넣어도 로그·응답에는 없다."""
    from sqlalchemy import text

    app = create_app(_settings(database_url=pg_url))

    async def run(statement: str, value: str) -> None:
        async with app.state.container.engine.connect() as conn:
            await conn.execute(text(statement), {"v": value})

    # 값은 변수로 넘긴다 — 스택에는 코드 줄이 찍히므로, 값을 코드에 직접 적으면 그 줄 때문에 표식이 보인다.
    async def client_side() -> None:  # asyncpg가 인코딩하다 실패: 메시지에 값의 repr이 들어간다
        await run("SELECT CAST(:v AS text)", PRIVATE_TEXT + " \ud800")

    async def server_side() -> None:  # PostgreSQL이 거절: invalid input syntax for type integer: "<값>"
        await run("SELECT CAST(CAST(:v AS text) AS integer)", PRIVATE_TEXT)

    app.add_api_route("/v1/review/client-side", client_side, methods=["GET"])
    app.add_api_route("/v1/review/server-side", server_side, methods=["GET"])
    with TestClient(app, raise_server_exceptions=False) as c, caplog.at_level(logging.DEBUG):
        responses = [c.get("/v1/review/client-side"), c.get("/v1/review/server-side")]
    for r in responses:
        assert r.status_code == 500 and r.json()["code"] == "internal_error", r.text
        assert set(r.json()) == {"code", "message", "request_id"} and MARK not in r.text
    logged = _everything_logged(caplog)
    assert MARK not in logged
    assert "원문" not in logged
    # 찾는 데 필요한 것은 남는다: 스택, 예외 종류, SQLSTATE, 문장의 앞부분, 요청 id
    errors = [
        rec for rec in caplog.records if rec.name == "app.core.middleware" and rec.levelno == logging.ERROR
    ]
    assert len(errors) == 2 and all(rec.exc_info is None for rec in errors)
    assert [rec.request_id for rec in errors] == [r.json()["request_id"] for r in responses]
    client_log, server_log = (rec.getMessage() for rec in errors)
    assert "Traceback (most recent call last)" in client_log and "in client_side" in client_log
    assert "asyncpg.exceptions.DataError" in client_log and OMITTED in client_log
    assert "sqlstate=22P02" in server_log and "InvalidTextRepresentationError" in server_log
    assert 'sql="SELECT CAST(CAST(' in server_log


def test_c2_engine_hides_parameters_in_its_own_messages(pg_url: str) -> None:
    """SQLAlchemy 오류 문자열(`[parameters: …]`)과 엔진 로그에도 값이 실리지 않는다 (`hide_parameters`)."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.db.session import create_engine

    async def provoke() -> str:
        engine = create_engine(pg_url)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT CAST(:n AS integer), :v"), {"n": "x", "v": f"{MARK}-PARAM"})
        except DBAPIError as exc:
            return str(exc)
        finally:
            await engine.dispose()
        return ""

    message = asyncio.run(provoke())
    assert message and MARK not in message and "hide_parameters" in message


def test_c2_safe_exception_text_keeps_the_stack_and_drops_values() -> None:
    def fail() -> None:
        try:
            raise ValueError(f"invalid literal: '{MARK} 사용자가 쓴 글'")
        except ValueError as inner:
            raise RuntimeError("configuration is broken") from inner

    try:
        fail()
    except RuntimeError as exc:
        text = format_exception_safely(exc)
        outer = exc
    # 스택(파일·줄·함수)과 예외 종류, 코드·시스템 상태를 말하는 메시지는 남는다
    assert text.count("Traceback (most recent call last):") == 2 and "in fail" in text
    assert "RuntimeError: configuration is broken" in text
    assert "The above exception was the direct cause of the following exception:" in text
    # 값을 옮기는 종류(ValueError)의 메시지는 버린다
    assert f"ValueError: {OMITTED}" in text and MARK not in text
    assert safe_exception_message(outer) == "RuntimeError: configuration is broken"
    assert safe_exception_message(KeyError(f"{MARK}-key")) == f"KeyError: {OMITTED}"
    assert safe_exception_message(ConnectionRefusedError(111, "Connection refused")).endswith(
        "Connection refused"
    )
    assert len(safe_exception_message(RuntimeError("x" * 5000))) < 400  # 남기는 메시지도 길이를 자른다

    # 다른 패키지의 예외는 종류가 RuntimeError 계열이어도 믿지 않는다. 이름·코드인 속성만 적는다
    class DriverError(RuntimeError):
        sqlstate = "22021"
        table_name = "contacts"
        detail = f"Key (alias)=({MARK}) already exists."

    DriverError.__module__ = "asyncpg.exceptions"
    summary = safe_exception_message(DriverError(f"invalid byte sequence near {MARK}"))
    assert (
        summary
        == f"asyncpg.exceptions.{DriverError.__qualname__}: {OMITTED} sqlstate=22021 table_name=contacts"
    )

    # pydantic 검증 오류는 입력 값을 메시지에 넣는다
    from pydantic import BaseModel, ValidationError

    class Model(BaseModel):
        count: int

    with pytest.raises(ValidationError) as raised:
        Model(count=f"{MARK}-not-a-number")  # type: ignore[arg-type]
    assert MARK in str(raised.value) and MARK not in format_exception_safely(raised.value)

    # 묶음 예외(ExceptionGroup)도 구성원의 값을 옮기지 않는다
    group = ExceptionGroup("several", [ValueError(MARK), TimeoutError("timed out")])
    grouped = format_exception_safely(group)
    assert MARK not in grouped and "TimeoutError: timed out" in grouped


def test_c2_log_formatter_and_server_filter_never_print_raw_exception_messages() -> None:
    """다른 모듈이 `log.exception`을 쓰거나 서버(uvicorn)가 예외를 남겨도 값은 찍히지 않는다."""
    import io

    from app.core.logs import LOG_FORMAT, RequestIdFilter, SafeFormatter, configure_logging, mark_reported

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeFormatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())
    module_log = logging.getLogger("app.services.some_module")
    module_log.addHandler(handler)
    try:
        try:
            raise ValueError(f"{MARK} 대화 원문")
        except ValueError:
            module_log.exception("가져오기 실패")
    finally:
        module_log.removeHandler(handler)
    printed = stream.getvalue()
    assert "가져오기 실패" in printed and "Traceback (most recent call last):" in printed
    assert f"ValueError: {OMITTED}" in printed and MARK not in printed

    # 서버 로거: 앱이 이미 남긴 예외는 버리고, 나머지는 값 없는 모양으로 바꾼다
    configure_logging("INFO")
    server_log = logging.getLogger("uvicorn.error")
    seen: list[logging.LogRecord] = []
    collector = logging.Handler()
    collector.emit = seen.append  # type: ignore[method-assign]
    server_log.addHandler(collector)
    try:
        reported, fresh = RuntimeError("already logged by the app"), ValueError(f"{MARK} in lifespan")
        mark_reported(reported)
        server_log.error("Exception in ASGI application\n", exc_info=reported)
        server_log.error("Exception in ASGI application\n", exc_info=fresh)
        server_log.error("plain line")
    finally:
        server_log.removeHandler(collector)
    assert len(seen) == 2 and seen[1].getMessage() == "plain line"
    assert seen[0].exc_info is None and seen[0].exc_text is None
    assert seen[0].getMessage().startswith("Exception in ASGI application\nValueError: ")
    assert MARK not in seen[0].getMessage()


# ---------- C3. 로그인 잠금: (이름, IP) 쌍은 원래 한도, IP 단독·이름 단독은 느슨한 한도 ----------

VICTIM = ("victim01", "correct-horse-battery")
ATTACKER_IP, VICTIM_IP, NAT_IP = "203.0.113.9", "198.51.100.7", "192.0.2.10"


def _auth(**overrides) -> AuthService:  # noqa: ANN003
    return AuthService(_settings(**overrides), InMemoryAuthRepo())


async def _fail(service: AuthService, name: str, ip: str | None, times: int = 1) -> None:
    for _ in range(times):
        with pytest.raises(UnauthorizedError):
            await service.login(name, "wrong-password-1", client_ip=ip)


@pytest.mark.anyio
async def test_c3_failures_from_one_address_do_not_lock_the_account_for_everyone() -> None:
    """검토 재현(P2): 공격자가 자기 IP에서 5번 틀려도 피해자는 다른 IP에서 올바른 비밀번호로 들어온다."""
    service = _auth()
    await service.register(*VICTIM)
    await _fail(service, "victim01", ATTACKER_IP, 5)
    with pytest.raises(TooManyAttemptsError) as raised:  # 틀리던 쪽은 맞는 비밀번호여도 잠긴다
        await service.login(*VICTIM, client_ip=ATTACKER_IP)
    assert 0 < int(raised.value.headers["Retry-After"]) <= 15 * 60
    assert (await service.login(*VICTIM, client_ip=VICTIM_IP)).user.username == "victim01"
    with pytest.raises(TooManyAttemptsError):  # 피해자의 성공이 공격자 쪽 잠금을 풀어 주지는 않는다
        await service.login(*VICTIM, client_ip=ATTACKER_IP)
    # 이름의 대소문자·앞뒤 공백을 바꿔도 같은 기록이다
    with pytest.raises(TooManyAttemptsError):
        await service.login(" VICTIM01 ", "whatever-123", client_ip=ATTACKER_IP)


@pytest.mark.anyio
async def test_c3_one_persons_typos_do_not_lock_everyone_behind_the_same_address() -> None:
    """같은 공유기(사무실·학교 NAT) 뒤: 한 사람이 5번 틀려도 같은 IP의 다른 사람은 로그인한다."""
    service = _auth()
    await service.register("student_a", "password-of-a-1")
    await service.register("student_b", "password-of-b-2")
    await _fail(service, "student_a", NAT_IP, 5)
    with pytest.raises(TooManyAttemptsError):
        await service.login("student_a", "password-of-a-1", client_ip=NAT_IP)
    assert (
        await service.login("student_b", "password-of-b-2", client_ip=NAT_IP)
    ).user.username == "student_b"
    await _fail(service, "student_b", NAT_IP, 1)  # 다른 사람의 오타도 따로 센다 (1/5)
    assert (
        await service.login("student_b", "password-of-b-2", client_ip=NAT_IP)
    ).user.username == "student_b"


@pytest.mark.anyio
async def test_c3_guessing_many_accounts_from_one_address_hits_the_address_limit() -> None:
    service = _auth(auth_max_failures=2)  # 쌍 2, IP 2×6=12, 이름 2×4=8
    mine = ("own_account", "password-of-mine-1")
    await service.register(*mine)
    for i in range(6):  # 계정 6개를 2번씩 = 실패 12번
        # 사이사이 자기 계정으로 로그인해도 IP의 실패 기록은 지워지지 않는다
        assert (await service.login(*mine, client_ip=ATTACKER_IP)).user.username == "own_account"
        await _fail(service, f"target_{i}", ATTACKER_IP, 2)
    for name, password in (("target_new", "whatever-123"), mine):  # 처음 보는 이름도, 자기 계정도 막힌다
        with pytest.raises(TooManyAttemptsError):
            await service.login(name, password, client_ip=ATTACKER_IP)
    assert (
        await service.login(*mine, client_ip=VICTIM_IP)
    ).user.username == "own_account"  # 다른 IP는 그대로

    # 배수 0이면 IP 단독 잠금을 끈다 (큰 NAT 뒤에서 쓰는 배포용)
    lenient = _auth(auth_max_failures=2, auth_ip_failure_factor=0)
    await lenient.register(*mine)
    for i in range(10):
        await _fail(lenient, f"target_{i}", ATTACKER_IP, 2)
    assert (await lenient.login(*mine, client_ip=ATTACKER_IP)).user.username == "own_account"


@pytest.mark.anyio
async def test_c3_many_addresses_guessing_one_account_hits_the_name_limit() -> None:
    """남은 위험(문서화): 서로 다른 IP 여러 개로 한도의 4배를 틀리면 그 계정은 어디서든 잠시 막힌다."""
    service = _auth(auth_max_failures=2)  # 이름 한도 8 = IP 4개 × 2번
    await service.register(*VICTIM)
    for i in range(3):
        await _fail(service, "victim01", f"203.0.113.{i}", 2)
    assert (
        await service.login(*VICTIM, client_ip=VICTIM_IP)
    ).user.username == "victim01"  # 6번까지는 들어온다
    for i in range(4):  # 피해자의 성공으로 이름 기록이 지워졌다 → 다시 8번이 필요하다
        await _fail(service, "victim01", f"203.0.113.{10 + i}", 2)
    with pytest.raises(TooManyAttemptsError):
        await service.login(*VICTIM, client_ip=VICTIM_IP)

    lenient = _auth(auth_max_failures=2, auth_name_failure_factor=0)  # 이름 단독 잠금 끔
    await lenient.register(*VICTIM)
    for i in range(10):
        await _fail(lenient, "victim01", f"203.0.113.{i}", 2)
    assert (await lenient.login(*VICTIM, client_ip=VICTIM_IP)).user.username == "victim01"


def test_c3_limits_expire_reset_and_prune() -> None:
    limits = FailureLimits(2, timedelta(minutes=15), ip_factor=3, name_factor=2)  # 쌍 2, IP 6, 이름 4
    t0 = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)

    def minutes(n: float) -> datetime:
        return t0 + timedelta(minutes=n)

    assert limits.retry_after("a", "ip1", t0) is None
    limits.record_failure("a", "ip1", t0)
    limits.record_failure("a", "ip1", minutes(1))
    assert limits.retry_after("a", "ip1", minutes(2)) == 13 * 60  # 첫 실패가 창을 벗어날 때까지
    assert limits.retry_after("a", "ip2", minutes(2)) is None  # 같은 이름, 다른 IP
    assert limits.retry_after("b", "ip1", minutes(2)) is None  # 같은 IP, 다른 이름

    # 이름 단독(4): 다른 IP 둘이 더 틀리면 어디서든 막힌다. IP 단독(6)은 아직 아니다
    limits.record_failure("a", "ip2", minutes(2))
    limits.record_failure("a", "ip3", minutes(2))
    assert limits.retry_after("a", "ip9", minutes(3)) == 12 * 60
    assert limits.retry_after("b", "ip1", minutes(3)) is None
    # 성공: 그 쌍과 이름 기록만 지운다. IP 기록은 남는다
    limits.record_success("a", "ip9")
    assert limits.retry_after("a", "ip9", minutes(3)) is None
    assert limits.retry_after("a", "ip1", minutes(3)) == 12 * 60  # 다른 쌍의 잠금은 그대로
    for name in ("c", "d", "e", "f"):  # ip1의 실패 합계: a 2번 + 4번 = 6
        limits.record_failure(name, "ip1", minutes(4))
    assert limits.retry_after("fresh", "ip1", minutes(5)) == 10 * 60
    limits.record_success("c", "ip1")
    assert limits.retry_after("fresh", "ip1", minutes(5)) == 10 * 60

    # 운영자가 비밀번호를 재설정하면 그 이름의 기록을 모든 IP에서 지운다
    limits.forget_name("a")
    assert limits.pair.retry_after(("a", "ip1"), minutes(5)) is None
    # IP를 모르는 호출은 쌍 (이름, None)과 이름만 본다
    limits.record_failure("z", None, minutes(5))
    limits.record_failure("z", None, minutes(5))
    assert limits.retry_after("z", None, minutes(6)) == 14 * 60
    assert limits.retry_after("z", "ip7", minutes(6)) is None
    # 창이 지나면 풀리고, 다 지나면 기록이 전부 비워진다
    assert limits.retry_after("z", None, minutes(19.9)) == 6
    assert limits.retry_after("z", None, minutes(20.1)) is None
    assert limits.prune_all(minutes(60)) > 0
    assert (len(limits.pair._failures), len(limits.by_ip._failures), len(limits.by_name._failures)) == (
        0,
        0,
        0,
    )


def test_c3_http_login_and_invite_follow_the_same_rule() -> None:
    app = create_app(_settings(register_invite_code="come-in-2026", auth_max_failures=2))  # 쌍 2, IP 12
    here = TestClient(app, client=(NAT_IP, 40000))
    elsewhere = TestClient(app, client=(VICTIM_IP, 40000))

    def signup(client: TestClient, name: str, invite: str) -> int:
        body = {"username": name, "password": "long-enough-pw", "invite_code": invite}
        return client.post("/v1/auth/register", json=body).status_code

    # 초대 코드: 같은 이름으로 계속 틀리면 그 쌍만 잠기고, 같은 IP의 다른 사람은 가입된다
    assert [signup(here, "student_a", "guess") for _ in range(3)] == [403, 403, 429]
    assert signup(here, "student_a", "come-in-2026") == 429
    assert signup(here, "student_b", "come-in-2026") == 201
    # 로그인: 같은 규칙 (쿠키 로그인과 토큰 발급이 같은 기록)
    wrong = {"username": "student_b", "password": "wrong-password-1"}
    assert here.post("/v1/auth/login", json=wrong).status_code == 401
    assert here.post("/v1/auth/token", json=wrong).status_code == 401
    locked = here.post("/v1/auth/login", json={"username": "student_b", "password": "long-enough-pw"})
    assert locked.status_code == 429 and locked.json()["code"] == "too_many_attempts"
    assert int(locked.headers["retry-after"]) > 0
    ok = elsewhere.post("/v1/auth/login", json={"username": "student_b", "password": "long-enough-pw"})
    assert ok.status_code == 200  # 다른 IP의 올바른 로그인은 막히지 않는다

    # 이름을 바꿔 가며 초대 코드를 추측하면 IP 한도(12)에 걸린다: 지금까지 실패 2번 + 10번
    assert [signup(here, f"guess_{i}", "guess") for i in range(10)] == [403] * 10
    blocked = here.post(
        "/v1/auth/register",
        json={"username": "student_c", "password": "long-enough-pw", "invite_code": "come-in-2026"},
    )
    assert blocked.status_code == 429 and blocked.json()["code"] == "too_many_attempts"
    assert signup(elsewhere, "student_c", "come-in-2026") == 201


@pytest.mark.anyio
async def test_c3_operator_reset_clears_locks_from_every_address() -> None:
    service = _auth(auth_max_failures=2)
    await service.register(*VICTIM)
    for ip in (ATTACKER_IP, NAT_IP):
        await _fail(service, "victim01", ip, 2)
    with pytest.raises(TooManyAttemptsError):
        await service.login(*VICTIM, client_ip=NAT_IP)
    assert await service.reset_password("victim01", "brand-new-password-9") == 1  # 가입 때 열린 세션 1개
    for ip in (ATTACKER_IP, NAT_IP):
        fresh = await service.login("victim01", "brand-new-password-9", client_ip=ip)
        assert fresh.user.username == "victim01"
    assert service.prune_throttle(datetime.now(UTC) + timedelta(hours=1)) > 0


# ---------- C4. 운영 모드 가입: 관리자 이름을 검증 순서로 가려낼 수 없다 ----------


def test_c4_admin_name_looks_like_any_other_name_whatever_is_sent() -> None:
    """검토 재현(P1): 짧은 비밀번호를 보내면 관리자 이름만 409, 나머지는 422로 갈렸다."""
    settings = _settings(admin_usernames="opsadmin")
    app = create_app(settings)
    settings.app_env = "production"  # 가입 정책의 운영 분기만 켠다 (시작 검사는 개발 모드로 지나간다)
    client = TestClient(app)
    taken = {"username": "takenname", "password": "long-enough-pw"}
    assert client.post("/v1/auth/register", json=taken).status_code == 201
    client.cookies.clear()

    def register(name: str, password: str):  # noqa: ANN202
        return client.post("/v1/auth/register", json={"username": name, "password": password})

    # 짧은 비밀번호면 어떤 이름이든 같은 422 — 관리자 이름, 대소문자만 다른 것, 이미 있는 이름, 없는 이름
    short = {name: register(name, "a") for name in ("opsadmin", "OpsAdmin", "takenname", "nobody123")}
    assert {r.status_code for r in short.values()} == {422}
    assert len({r.text for r in short.values()}) == 1
    # 형식이 맞으면 관리자 이름은 이미 있는 이름과 같은 409 (본문도 같다)
    reserved, existing = register("opsadmin", "long-enough-pw"), register("takenname", "long-enough-pw")
    assert reserved.status_code == existing.status_code == 409 and reserved.json() == existing.json()
    assert register("OPSADMIN", "long-enough-pw").status_code == 409
    assert register("nobody123", "long-enough-pw").status_code == 201


@pytest.mark.anyio
async def test_c4_reserved_name_takes_the_same_path_as_a_taken_name() -> None:
    """응답 시간으로도 갈리지 않게: 관리자 이름도 형식 검사 → 이름 조회를 똑같이 거친 뒤 409가 된다."""
    from app.core.errors import InvalidInputError, VersionConflictError

    class CountingRepo(InMemoryAuthRepo):
        lookups = 0

        async def username_exists(self, username: str) -> bool:
            type(self).lookups += 1
            return await super().username_exists(username)

    repo = CountingRepo()
    prod = AuthService(_settings(app_env="production", admin_usernames="boss"), repo)
    await prod.signup("existing_user", "long-enough-pw")
    before = CountingRepo.lookups
    for name in ("boss", "Boss", "existing_user"):
        with pytest.raises(VersionConflictError):
            await prod.signup(name, "long-enough-pw")
    assert CountingRepo.lookups - before == 3  # 셋 다 조회를 한 번씩 했다
    for name in ("boss", "existing_user", "new_user_1"):  # 형식 검사가 먼저다 — 조회조차 하지 않는다
        with pytest.raises(InvalidInputError):
            await prod.signup(name, "short")
    with pytest.raises(InvalidInputError):
        await prod.signup("bo ss", "long-enough-pw")
    assert CountingRepo.lookups - before == 3
    # 운영자가 서버에서 만드는 길과 개발 모드의 가입은 그대로다
    assert prod.is_admin(await prod.create_account("boss", "long-enough-pw"))
    dev = AuthService(_settings(admin_usernames="boss"), InMemoryAuthRepo())
    assert dev.is_admin((await dev.signup("boss", "long-enough-pw")).user)


# ---------- C5. 깊은 헬스체크: 결과를 몇 초 다시 쓰고, 요청 제한에 들어간다 ----------


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_c5_deep_health_check_is_cached_for_a_few_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import health

    clock = _Clock()
    probes: list[str] = []
    database_up = True

    async def probe(_state) -> None:  # noqa: ANN001
        probes.append("postgres")
        if not database_up:
            raise ConnectionRefusedError("database is down")

    monkeypatch.setattr(health, "_now", clock)
    monkeypatch.setattr(health, "_check_postgres", probe)
    app = create_app(_settings())
    app.state.container.engine = object()  # DB가 있는 구성처럼 — 실제 확인은 위의 probe가 대신한다
    client = TestClient(app)

    assert [client.get("/healthz").status_code for _ in range(3)] == [200, 200, 200]
    assert probes == []  # 쿼리 없는 헬스체크(컨테이너·프록시)는 DB를 건드리지 않는다
    first = client.get("/healthz?deep=true")
    assert first.status_code == 200 and first.json()["checks"] == {"postgres": "ok", "neo4j": "skipped"}
    for _ in range(20):  # 연달아 불러도 실제 확인은 한 번뿐이다
        assert client.get("/healthz?deep=true").json() == first.json()
    assert probes == ["postgres"]

    clock.now += health.DEEP_CACHE_SECONDS + 0.1  # 캐시가 지나면 다시 확인한다
    database_up = False
    down = client.get("/healthz?deep=true")
    assert down.status_code == 503 and down.json()["status"] == "degraded"
    assert down.json()["checks"]["postgres"] == "error: ConnectionRefusedError"
    assert client.get("/healthz?deep=true").status_code == 503  # 실패도 같은 시간 동안 다시 쓴다
    assert len(probes) == 2 and client.get("/healthz").status_code == 200

    clock.now += health.DEEP_CACHE_SECONDS + 0.1
    database_up = True
    assert client.get("/healthz?deep=true").status_code == 200 and len(probes) == 3
    # 다른 앱은 캐시를 나눠 쓰지 않는다
    other = create_app(_settings())
    other.state.container.engine = object()
    assert TestClient(other).get("/healthz?deep=true").status_code == 200 and len(probes) == 4


@pytest.mark.anyio
async def test_c5_concurrent_deep_checks_share_one_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """동시에 몰려와도 DB 확인은 한 번만 돌고 나머지는 그 결과를 받는다."""
    import asyncio

    import httpx

    from app.api.routes import health

    running = 0
    peak = 0
    probes = 0

    async def slow_probe(_state) -> None:  # noqa: ANN001
        nonlocal running, peak, probes
        probes += 1
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1

    monkeypatch.setattr(health, "_check_postgres", slow_probe)
    app = create_app(_settings())
    app.state.container.engine = object()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(*(client.get("/healthz?deep=true") for _ in range(25)))
    assert {r.status_code for r in responses} == {200}
    assert probes == 1 and peak == 1


def test_c5_deep_health_check_counts_against_the_rate_limit() -> None:
    app = create_app(_settings(rate_limit_per_minute=3, trusted_hosts="persona.example.com"))
    app.state.rate_limiter.clock = _Clock()
    inside = TestClient(app, client=("10.0.0.5", 40000))  # 컨테이너·프록시가 내부 주소로 부르는 헬스체크
    internal = {"Host": "backend:8000"}
    assert {inside.get("/healthz", headers=internal).status_code for _ in range(20)} == {200}

    outside = TestClient(app, client=("203.0.113.7", 40000))
    host = {"Host": "persona.example.com"}
    # 배포 스크립트의 스모크 테스트처럼 한 번 부르는 것은 그대로 된다 — Host 검사는 여전히 받지 않는다
    assert outside.get("/healthz?deep=true", headers=internal).status_code == 200
    deep = [outside.get("/healthz?deep=true", headers=host).status_code for _ in range(4)]
    assert deep == [200, 200, 429, 429]  # /v1 요청과 같은 한도(분당 3번)로 센다
    blocked = outside.get("/healthz?deep=true", headers=host)
    assert blocked.json()["code"] == "rate_limited" and int(blocked.headers["retry-after"]) > 0
    assert outside.get("/v1/auth/config", headers=host).status_code == 429  # 같은 한도를 나눠 쓴다
    assert outside.get("/healthz", headers=host).status_code == 200  # 쿼리 없는 헬스체크는 막히지 않는다
    assert inside.get("/healthz?deep=true", headers=internal).status_code == 200  # 다른 IP는 따로


# ---------- C6. 하루 한도(usage_exceeded·budget_exceeded)에도 Retry-After ----------


def test_c6_daily_limits_say_when_to_retry() -> None:
    from app.core.http_errors import seconds_until_usage_day_ends

    # 한국 시간 23:59:30 → 30초 뒤 자정. UTC로는 14:59:30
    late = datetime(2026, 11, 11, 14, 59, 30, tzinfo=UTC)
    assert seconds_until_usage_day_ends(9, late) == 30
    assert seconds_until_usage_day_ends(9, late + timedelta(seconds=30)) == 24 * 3600  # 막 자정이 된 순간
    assert seconds_until_usage_day_ends(9, late + timedelta(seconds=29, microseconds=500_000)) == 1
    assert seconds_until_usage_day_ends(0, late) == 9 * 3600 + 30  # 사용량 날짜를 UTC로 세는 설정

    def rewrite_twice(**limits) -> tuple:  # noqa: ANN003
        client = TestClient(create_app(_settings(**limits)))
        h = _stub()
        made = client.post("/v1/contacts", json={"alias": "박지현", "relationship_type": "friend"}, headers=h)
        body = {
            "contact_id": made.json()["id"],
            "draft_text": "내일 오전까지 보내드릴게요.",
            "condition": "A",
        }
        first = client.post("/v1/rewrites", json=body, headers=h)
        assert first.status_code == 201, first.text
        return client.post("/v1/rewrites", json=body, headers=h), client

    expected = seconds_until_usage_day_ends(9)
    for limits, code in (
        ({"usage_daily_rewrite_limit": 1}, "usage_exceeded"),
        ({"usage_daily_token_limit": 1}, "usage_exceeded"),
        ({"usage_global_daily_rewrite_limit": 1}, "budget_exceeded"),
    ):
        blocked, client = rewrite_twice(**limits)
        assert blocked.status_code == 429 and blocked.json()["code"] == code, (limits, blocked.text)
        assert set(blocked.json()) == {"code", "message"}  # 본문 모양은 그대로
        retry_after = int(blocked.headers["retry-after"])
        assert 1 <= retry_after <= 24 * 3600, limits
        drift = abs(retry_after - expected)  # 테스트가 자정을 걸쳐 돌면 하루만큼 차이 난다
        assert min(drift, 24 * 3600 - drift) <= 120, limits
        # 다른 도메인 오류에는 붙지 않는다
        assert "retry-after" not in client.get("/v1/contacts/00000000-0000-0000-0000-000000000000").headers
