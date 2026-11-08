"""세션 쿠키 인증: 가입→쿠키→조회, 쓰기는 CSRF 필수, 로그아웃 뒤 401, 헤더 스텁 끄기, 비밀번호 해시."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import hash_password, token_hash, verify_password
from app.main import create_app

CRED = {"username": "minsu_kim", "password": "correct horse battery"}


def _client(**overrides) -> TestClient:  # noqa: ANN003
    s = Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[call-arg]
    return TestClient(create_app(s))


def test_password_hash_roundtrip_and_format() -> None:
    stored = hash_password("pw-1234567")
    assert stored.startswith("scrypt$16384$8$1$")
    assert verify_password("pw-1234567", stored)
    assert not verify_password("pw-1234568", stored)
    assert not verify_password("x", "garbage")
    assert hash_password("same") != hash_password("same")  # 소금
    assert len(token_hash("abc")) == 64


def test_register_login_cookie_csrf_logout() -> None:
    client = _client()
    r = client.post("/v1/auth/register", json=CRED)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["username"] == "minsu_kim" and body["csrf_token"]
    cookie = r.cookies.get("pi_session")
    assert (
        cookie
        and "HttpOnly" in r.headers["set-cookie"]
        and "SameSite=lax" in r.headers["set-cookie"].replace("Lax", "lax")
    )
    assert "pi_session" not in r.text  # 토큰 원문은 본문에 없다

    # 쿠키만으로 읽기 OK
    assert client.get("/v1/contacts").status_code == 200
    me = client.get("/v1/auth/me").json()
    assert me["user_id"] == body["user_id"] and me["csrf_token"] == body["csrf_token"]

    # 쓰기: CSRF 없으면 403, 틀리면 403, 맞으면 201
    payload = {"alias": "박지현", "relationship_type": "superior"}
    assert client.post("/v1/contacts", json=payload).status_code == 403
    assert client.post("/v1/contacts", json=payload, headers={"X-CSRF-Token": "nope"}).status_code == 403
    ok = client.post("/v1/contacts", json=payload, headers={"X-CSRF-Token": body["csrf_token"]})
    assert ok.status_code == 201, ok.text
    # 쿠키 사용자의 데이터는 쿠키 사용자 id로 격리된다
    assert client.get("/v1/contacts", headers={"X-User-Id": "someone"}).json() == [
        client.get("/v1/contacts").json()[0]
    ]  # 쿠키가 있으면 헤더는 무시

    # 중복 이름 409, 틀린 비밀번호 401 (존재 여부 노출 없음)
    assert client.post("/v1/auth/register", json=CRED).status_code == 409
    wrong = client.post("/v1/auth/login", json={**CRED, "password": "wrong-password"})
    assert wrong.status_code == 401
    assert (
        client.post("/v1/auth/login", json={"username": "nobody_here", "password": "whatever-1"}).json()[
            "message"
        ]
        == wrong.json()["message"]
    )

    # 로그아웃 → 세션 폐기, 같은 쿠키로 401
    assert client.post("/v1/auth/logout").status_code == 204
    client.cookies.set("pi_session", cookie)
    assert client.get("/v1/auth/me").status_code == 401
    assert client.get("/v1/contacts").status_code == 401

    # 다시 로그인하면 새 세션·새 CSRF
    again = client.post("/v1/auth/login", json=CRED)
    assert again.status_code == 200 and again.json()["csrf_token"] != body["csrf_token"]


def test_header_stub_can_be_disabled_and_register_closed() -> None:
    client = _client(auth_allow_header_stub=False, auth_allow_register=False)
    assert client.get("/v1/contacts", headers={"X-User-Id": "user-a"}).status_code == 401
    assert client.get("/v1/contacts").status_code == 401
    assert client.post("/v1/auth/register", json=CRED).status_code == 403


def test_register_validation() -> None:
    client = _client()
    assert client.post("/v1/auth/register", json={"username": "ab", "password": "x" * 10}).status_code == 422
    assert (
        client.post("/v1/auth/register", json={"username": "bad name!", "password": "x" * 10}).status_code
        == 422
    )
    assert (
        client.post("/v1/auth/register", json={"username": "fine_name", "password": "short"}).status_code
        == 422
    )
