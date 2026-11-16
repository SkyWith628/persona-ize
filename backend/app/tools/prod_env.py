"""배포 설정(.env.prod) 점검 규칙. 실행은 `scripts/check_prod_env.py`."""

from __future__ import annotations

import re

DEV_PASSWORDS = {"persona-dev-only", "neo4j-dev-only", "password", "changeme", "neo4j"}
MIN_SECRET_LENGTH = 16


def parse_env(text: str) -> dict[str, str]:
    """KEY=VALUE 줄만 읽는다. 주석·빈 줄은 건너뛰고 값의 양끝 따옴표는 뗀다."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def find_problems(env: dict[str, str]) -> list[str]:
    problems: list[str] = []

    def flag(key: str, expected: str, why: str) -> None:
        if env.get(key, "").lower() != expected:
            problems.append(f"{key}는 {expected}여야 합니다 — {why}")

    flag("AUTH_ALLOW_HEADER_STUB", "false", "켜 두면 X-User-Id 헤더만으로 누구의 데이터든 읽습니다")
    flag("SESSION_COOKIE_SECURE", "true", "HTTPS에서만 세션 쿠키를 보내야 합니다")
    if env.get("AUTH_ALLOW_REGISTER", "").lower() != "false":
        problems.append("AUTH_ALLOW_REGISTER가 false가 아닙니다 — 누구나 가입해 API 비용을 쓸 수 있습니다")

    site = env.get("SITE_ADDRESS", "")
    if not site or site.startswith(("http://", "localhost", "127.")) or re.fullmatch(r"[\d.:]+", site):
        problems.append("SITE_ADDRESS에는 인증서를 받을 도메인이 필요합니다 (IP·localhost·http:// 불가)")

    for key in ("POSTGRES_PASSWORD", "NEO4J_PASSWORD"):
        value = env.get(key, "")
        if value in DEV_PASSWORDS or len(value) < MIN_SECRET_LENGTH:
            problems.append(
                f"{key}는 {MIN_SECRET_LENGTH}자 이상의 새 무작위 값이어야 합니다 (개발용 기본값 금지)"
            )
        elif not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            problems.append(f"{key}에는 영문·숫자·-·_만 쓰세요 (접속 주소에 그대로 들어갑니다)")
    if env.get("POSTGRES_PASSWORD") and env.get("POSTGRES_PASSWORD") == env.get("NEO4J_PASSWORD"):
        problems.append("POSTGRES_PASSWORD와 NEO4J_PASSWORD는 서로 달라야 합니다")

    provider = env.get("LLM_PROVIDER", "")
    if provider == "anthropic" and not env.get("ANTHROPIC_API_KEY"):
        problems.append("LLM_PROVIDER=anthropic인데 ANTHROPIC_API_KEY가 비어 있습니다")
    if provider not in ("anthropic", "fake"):
        problems.append("LLM_PROVIDER는 anthropic 또는 fake입니다")

    for key in ("USAGE_DAILY_REWRITE_LIMIT", "USAGE_DAILY_TOKEN_LIMIT"):
        if not env.get(key, "").isdigit() or int(env[key]) <= 0:
            problems.append(f"{key}에 0보다 큰 한도를 넣으세요 — 한도가 없으면 비용이 막히지 않습니다")

    if not [name for name in env.get("ADMIN_USERNAMES", "").split(",") if name.strip()]:
        problems.append("ADMIN_USERNAMES가 비어 있습니다 — 운영 대시보드(/admin)를 아무도 볼 수 없습니다")
    return problems
