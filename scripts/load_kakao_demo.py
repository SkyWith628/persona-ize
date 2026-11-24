"""한 사용자의 상대·대화·그래프를 지우고 카카오톡 내보내기 파일들로 다시 채운다 (시연 계정 준비).

2026-11-19에 시연 계정 `demo`의 데이터를 카카오톡 내보내기 4개로 바꿀 때 쓴 일회용 스크립트를 다시 쓸 수
있게 옮긴 것이다. 실제 대화 파일은 저장소에 넣지 않는다 — 설정 파일(JSON)이 저장소 밖의 경로를 가리킨다.

무엇을 하나
  1. 설정의 파일을 전부 읽어 화자를 검사한다. 화자가 정확히 {내 이름, 상대}가 아닌 파일은 저장하지 않고
     이름 목록과 함께 실패로 보고한다 (단체방은 그 대화의 `skip`에 뺄 화자를 적는다).
  2. `--keep-existing`이 아니면 지운다: 그래프 group 전체 → 상대마다(대화·묶음·첨삭 이력·페르소나).
     그래프를 안 쓰는 설정이면 그래프 단계는 건너뛴다.
  3. 대화마다 상대 → 페르소나 v1(관계 유형 기본값에 `address_term`만 덮어씀) → 대화방 → 메시지·말투 묶음.
     개인정보 가림은 하지 않는다 (API 경로만 가린다).
  4. `--graph`면 그래프에 수집하고(haiku 호출, 메시지당 10~15초, 상한 없음) 상대 이름의 노드가 생겼는지
     확인한다. 없으면 경고만 찍는다 — 추출 모델이 '김상무님'을 '김상무'로 만들어 앱이 못 찾은 적이 있다.
  5. 대화별 메시지·묶음·그래프 수집 수를 표로 찍는다. 실패가 하나라도 있으면 종료 코드 1.

설정 파일: `scripts/kakao_demo.example.json` 참고. `file`이 상대 경로면 설정 파일이 있는 폴더 기준이다.

사용 예 (저장소 루트, .env의 DATABASE_URL — `--graph`면 NEO4J_*·ANTHROPIC_API_KEY도):
    .venv/Scripts/python.exe scripts/load_kakao_demo.py --config <설정.json> --dry-run   # 파일만 읽어 본다
    .venv/Scripts/python.exe scripts/load_kakao_demo.py --config <설정.json> --graph
--user 는 username 또는 user_id(기본 demo). 되돌릴 수 없으므로 --yes 가 없으면 지우기 전에 한 번 묻는다.
`--dry-run`은 DB에 붙지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pydantic import ValidationError  # noqa: E402

from app.core.errors import DomainError  # noqa: E402
from app.core.safe_exc import type_name  # noqa: E402
from app.domain.persona import PersonaSettings, RelationshipType, default_persona  # noqa: E402
from app.importers import ParsedImport, parse_import  # noqa: E402

SECONDS_PER_MESSAGE = 12  # 11-08 실측 10~15초
RELATIONSHIP_TYPES = get_args(RelationshipType)
REQUIRED = ("file", "contact", "relationship_type", "source_key")


class ConfigError(ValueError):
    pass


@dataclass
class Entry:
    """설정의 대화 하나 + 읽은 결과."""

    file: Path
    contact: str
    relationship_type: str
    source_key: str
    title: str | None
    persona: PersonaSettings
    skip: list[str]
    parsed: ParsedImport | None = None
    error: str | None = None  # 읽기·화자 검사·저장 실패 (있으면 저장하지 않는다)

    @property
    def label(self) -> str:
        return f"{self.source_key} ({self.contact})"


@dataclass
class Outcome:
    failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="카카오톡 내보내기로 한 사용자의 데이터를 다시 채운다")
    p.add_argument("--config", type=Path, required=True, help="설정 JSON (kakao_demo.example.json 참고)")
    p.add_argument("--user", default="demo", help="username 또는 user_id (기본 demo)")
    p.add_argument("--dry-run", action="store_true", help="파일만 읽어 화자·건수를 보여주고 바꾸지 않는다")
    p.add_argument("--yes", action="store_true", help="확인 질문 없이 진행")
    p.add_argument("--graph", action="store_true", help="그래프(Neo4j)에도 수집 (haiku 호출)")
    p.add_argument("--keep-existing", action="store_true", help="기존 상대를 지우지 않고 추가")
    return p.parse_args(argv)


def load_config(path: Path) -> tuple[str, list[Entry]]:
    """(내 이름, 대화 목록). 형식이 틀리면 ConfigError — 아무것도 바꾸기 전에 끝낸다."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"설정 파일을 읽을 수 없습니다: {path} ({type_name(exc)})") from exc
    me = str((data.get("me") if isinstance(data, dict) else "") or "").strip()
    convs = data.get("conversations") if isinstance(data, dict) else None
    if not me or not isinstance(convs, list) or not convs:
        raise ConfigError('설정에는 "me"(내 이름)와 "conversations"(한 개 이상)가 있어야 합니다.')
    entries: list[Entry] = []
    for n, raw in enumerate(convs, 1):
        entries.append(_entry(raw, n, me, path.parent))
    keys = [e.source_key for e in entries]
    if len(set(keys)) != len(keys):
        raise ConfigError(
            "source_key가 겹칩니다: " + ", ".join(sorted({k for k in keys if keys.count(k) > 1}))
        )
    return me, entries


def _entry(raw: Any, n: int, me: str, base: Path) -> Entry:
    if not isinstance(raw, dict):
        raise ConfigError(f"conversations[{n}]: 객체여야 합니다.")
    missing = [k for k in REQUIRED if not str(raw.get(k) or "").strip()]
    if missing:
        raise ConfigError(f"conversations[{n}]: 빠진 값 — {', '.join(missing)}")
    rel = str(raw["relationship_type"]).strip()
    if rel not in RELATIONSHIP_TYPES:
        raise ConfigError(
            f"conversations[{n}]: relationship_type '{rel}' — {', '.join(RELATIONSHIP_TYPES)} 중 하나"
        )
    contact = str(raw["contact"]).strip()
    if contact == me:
        raise ConfigError(f"conversations[{n}]: contact가 내 이름과 같습니다.")
    skip = raw.get("skip") or []
    if not isinstance(skip, list) or me in skip or contact in skip:
        raise ConfigError(f"conversations[{n}]: skip은 내 이름·상대를 뺀 화자 이름의 목록이어야 합니다.")
    settings = default_persona(rel).model_dump()
    address = str(raw.get("address_term") or "").strip()
    if address:
        settings["address_term"] = address
    try:
        persona = PersonaSettings.model_validate(settings)
    except ValidationError as exc:
        raise ConfigError(f"conversations[{n}]: address_term을 쓸 수 없습니다 (30자 이내).") from exc
    file = Path(str(raw["file"]).strip()).expanduser()
    return Entry(
        file=file if file.is_absolute() else base / file,
        contact=contact,
        relationship_type=rel,
        source_key=str(raw["source_key"]).strip(),
        title=str(raw.get("title") or "").strip() or None,
        persona=persona,
        skip=[str(s).strip() for s in skip],
    )


def read_text(path: Path) -> str:
    """카카오톡 내보내기는 UTF-8(BOM 있을 수 있음)이다. 옛 PC판을 위해 UTF-16·CP949도 읽어 본다."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp949")


def read_entry(entry: Entry, me: str) -> None:
    """파일을 읽고 화자를 검사한다. 문제가 있으면 `entry.error`에 적는다 (오류 문장에 대화 본문은 없다)."""
    try:
        entry.parsed = parse_import(read_text(entry.file), "kakao")
    except OSError as exc:
        entry.error = f"파일을 읽을 수 없습니다: {entry.file} ({type_name(exc)})"
        return
    except UnicodeDecodeError:
        entry.error = f"인코딩을 알 수 없습니다: {entry.file}"
        return
    except DomainError as exc:  # 형식 오류 — 파서의 안내 문장 (본문이 아니라 줄 번호·형식 설명)
        entry.error = str(exc)
        return
    names = {name for name, _ in entry.parsed.speakers()}
    kept = names - set(entry.skip)
    if kept != {me, entry.contact}:
        entry.error = (
            f"화자가 맞지 않습니다 — 기대: {me}, {entry.contact} / 파일: {', '.join(sorted(names))}"
            + (f" (skip: {', '.join(entry.skip)})" if entry.skip else "")
        )


def print_plan(entries: list[Entry]) -> None:
    for e in entries:
        print(f"- {e.label}: {e.file.name}")
        if e.parsed is not None:
            p = e.parsed
            speakers = ", ".join(f"{name} {count}건" for name, count in p.speakers())
            print(
                f"    형식 {p.variant or p.format}, 메시지 {len(p.messages)}건 ({speakers}), "
                f"못 읽은 줄 {p.skipped_lines}, 시스템 줄 {p.system_lines}"
            )
            for problem in p.problems:
                print(f"    주의: {problem}")
        if e.error:
            print(f"    실패: {e.error}")


async def _resolve_user(state, raw: str) -> str | None:  # noqa: ANN001
    """username이면 그 id, 아니면 UUID 꼴일 때만 id로 본다.

    이름을 id로 넘기면 오류 없이 아무 계정에도 안 보이는 행이 생긴다.
    """
    found = await state.auth.find_user_by_username(raw)
    if found:
        return str(found[0].id)
    try:
        return str(UUID(raw))
    except ValueError:
        return None


async def _wipe(state, user_id: str) -> str | None:  # noqa: ANN001
    """그래프 group 전체 → 상대마다. 그래프 삭제가 실패하면 DB는 건드리지 않고 그 이유를 돌려준다."""
    if state.graph_enabled:
        try:
            removed, _ = await state.graph_ingest.reset(user_id, None)
        except Exception as exc:  # noqa: BLE001 - 보고하고 멈춘다
            return f"그래프 삭제 실패 ({type_name(exc)}) — DB는 그대로 두었습니다."
        print(f"지움: 그래프 노드 {removed}")
    else:
        print("그래프를 쓰지 않는 설정입니다 — 그래프 삭제는 건너뜁니다.")
    contacts = conversations = messages = 0
    for contact in await state.contacts.list(user_id):
        d = await state.deletion_service.delete_contact(user_id, contact.id, remove_graph=False)
        contacts += 1
        conversations += d.conversations
        messages += d.messages
    print(f"지움: 상대 {contacts}명, 대화방 {conversations}개, 메시지 {messages}건")
    return None


async def _load_entry(state, user_id: str, me: str, entry: Entry) -> None:  # noqa: ANN001
    """상대 → 페르소나 v1 → 대화방 → 메시지. 있는 것은 그대로 쓴다(--keep-existing, 같은 상대의 방 둘)."""
    assert entry.parsed is not None
    by_alias = {c.alias: c for c in await state.contacts.list(user_id)}
    contact = by_alias.get(entry.contact)
    if contact is None:
        contact = await state.contacts.create(user_id, entry.contact, entry.relationship_type)
    if await state.contacts.latest_persona(user_id, contact.id) is None:
        await state.contacts.save_persona(user_id, contact.id, entry.persona, 0)
    by_key = {c.source_key: c for c in await state.conversations.list(user_id)}
    conv = by_key.get(entry.source_key)
    if conv is None:
        conv = await state.import_service.create_conversation(
            user_id, contact.id, entry.title, f"kakao:{entry.file.name}", entry.source_key, me
        )
    elif conv.contact_id != contact.id:
        entry.error = f"source_key '{entry.source_key}'가 이미 다른 상대의 대화방입니다."
        return
    speaker_map = {me: "me", entry.contact: "contact", **dict.fromkeys(entry.skip, "skip")}
    job = await state.import_service.import_parsed(user_id, conv.id, entry.parsed, speaker_map)
    if job.status != "completed":
        entry.error = f"가져오기 {job.status} {job.error_code or ''}".strip()


async def _load_all(state, user_id: str, me: str, entries: list[Entry]) -> None:  # noqa: ANN001
    # 가져오기 중에는 백그라운드 그래프 수집을 끄고 묶음 생성을 끝까지 기다린다 — 이유는
    # `synthetic_loader.load_synthetic` 머리말 (스크립트가 끝나면 태스크가 죽는다).
    svc = state.import_service
    previous = (svc.graph_mode, svc.index_wait)
    svc.graph_mode, svc.index_wait = "off", None
    try:
        for entry in entries:
            if entry.error:
                continue
            try:
                await _load_entry(state, user_id, me, entry)
            except DomainError as exc:  # 한도 초과 등 — 안내 문장
                entry.error = str(exc)
            except Exception as exc:  # noqa: BLE001 - 종류만 남기고 다음 파일로
                entry.error = f"저장 실패 ({type_name(exc)})"
    finally:
        svc.graph_mode, svc.index_wait = previous


async def _ingest_graph(state, user_id: str, entries: list[Entry], out: Outcome) -> None:  # noqa: ANN001
    started = time.monotonic()
    try:
        n = await state.graph_ingest.ingest_user(user_id, enforce_caps=False)
        print(f"그래프 수집: 에피소드 {n}개 ({time.monotonic() - started:.0f}초)")
    except Exception as exc:  # noqa: BLE001 - 보고하고 종료 코드로 알린다
        out.failed.append(
            f"그래프 수집 중단 ({type_name(exc)}) — API 크레딧·키·Neo4j를 확인한 뒤 "
            "scripts/graph_reset.py로 다시 넣는다."
        )
        return
    # 상대 이름의 노드가 있어야 앱이 그 상대의 그래프 맥락을 찾는다. 고치지는 않는다.
    wanted = {e.contact for e in entries if not e.error}
    for contact in await state.contacts.list(user_id):
        if contact.alias not in wanted:
            continue
        try:
            found = await state.graph_store.find_entity_uuid(user_id, contact.name_in_graph)
        except Exception as exc:  # noqa: BLE001
            out.warnings.append(f"'{contact.name_in_graph}' 노드 확인 실패 ({type_name(exc)})")
            continue
        if found is None:
            out.warnings.append(
                f"그래프에 '{contact.name_in_graph}' 노드가 없습니다 — 추출 모델이 이름을 다르게 "
                "만들었을 수 있습니다(예: '김상무님' → '김상무'). 이 상대는 그래프 맥락이 비어 보입니다."
            )


async def _print_table(state, user_id: str, graph: bool, out: Outcome) -> None:  # noqa: ANN001
    aliases = {c.id: c.alias for c in await state.contacts.list(user_id)}
    print(f"\n{'상대':<12}{'대화방':<12}{'메시지':>8}{'묶음':>8}{'그래프':>8}")
    for conv in await state.conversations.list(user_id):
        s = await state.conversations.conversation_stats(user_id, conv.id)
        alias = aliases.get(conv.contact_id, "?")
        print(f"{alias:<12}{conv.source_key:<12}{s.messages:>8}{s.episodes:>8}{s.graph_ingested:>8}")
        left = s.messages - s.graph_ingested
        if graph and left:
            out.failed.append(f"{conv.source_key}: 그래프에 안 들어간 메시지 {left}건 (보류 {s.graph_held})")


async def run(args: argparse.Namespace, state=None) -> int:  # noqa: ANN001
    """종료 코드: 0 성공, 1 실패(파일·저장·그래프), 2 설정·사용법 오류. `state`는 테스트가 넣는다."""
    try:
        me, entries = load_config(args.config)
    except ConfigError as exc:
        print(exc)
        return 2
    for entry in entries:
        read_entry(entry, me)
    print(f"내 이름: {me}, 대화 {len(entries)}개")
    print_plan(entries)
    bad = [e for e in entries if e.error]
    if args.dry_run:
        print(f"\n--dry-run: 바꾼 것 없음. 실패 {len(bad)}개.")
        return 1 if bad else 0
    if len(bad) == len(entries):
        print("\n읽을 수 있는 파일이 없습니다 — 아무것도 바꾸지 않았습니다.")
        return 1

    owned = state is None
    if owned:
        from app.api.deps import build_state
        from app.core.config import Settings

        settings = Settings()
        if not settings.database_url:
            print("DATABASE_URL이 비어 있습니다. 시연 데이터는 PostgreSQL에 있어야 합니다.")
            return 2
        state = build_state(settings)
    try:
        return await _apply(state, args, me, entries)
    finally:
        if owned:
            await state.close()


async def _apply(state, args: argparse.Namespace, me: str, entries: list[Entry]) -> int:  # noqa: ANN001
    if args.graph and not state.graph_enabled:
        print("--graph에는 .env의 NEO4J_URI가 필요합니다.")
        return 2
    user_id = await _resolve_user(state, args.user)
    if user_id is None:
        print(f"사용자 '{args.user}'가 없습니다 (prepare_demo.py·reset_password.py --create로 만든다).")
        return 2
    out = Outcome()
    total = sum(len(e.parsed.messages) for e in entries if e.parsed is not None and not e.error)
    if args.graph:
        print(f"그래프 수집 예상: 약 {total * SECONDS_PER_MESSAGE // 60}분, 모델 호출 {total}건 이상")
    if not args.keep_existing:
        existing = await state.contacts.list(user_id)
        owned_convs, owned_msgs = await state.conversations.count_owned(user_id)
        print(
            f"\n지울 대상: 사용자 {user_id} — 상대 {len(existing)}명, 대화방 {owned_convs}개, "
            f"메시지 {owned_msgs}건" + (", 그래프 전체" if state.graph_enabled else "")
        )
        if not args.yes:
            answer = input("지우고 다시 채웁니다. 되돌릴 수 없습니다. 계속할까요? [y/N] ").strip().lower()
            if answer != "y":
                print("취소.")
                return 1
        problem = await _wipe(state, user_id)
        if problem:
            print(problem)
            return 1

    await _load_all(state, user_id, me, entries)
    if args.graph:
        await _ingest_graph(state, user_id, entries, out)
    elif state.graph_enabled:
        print(
            "그래프 수집은 하지 않았습니다(--graph 없음). 서버가 뜨면 GRAPH_RESUME_ON_START로 "
            "미수집분을 이어받습니다(haiku 비용)."
        )
    await _print_table(state, user_id, args.graph, out)
    out.failed[:0] = [f"{e.label}: {e.error}" for e in entries if e.error]
    for line in out.warnings:
        print("경고:", line)
    for line in out.failed:
        print("실패:", line)
    return 1 if out.failed else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
