# Persona_ize

상대·상황에 맞는 **사용자 본인의 말투**로 메시지를 첨삭하되, **원문의 사실과 의도를 보존**하는 서비스.
공모전(2026-12) + 포트폴리오. 설계: [docs/project-design.md](docs/project-design.md) · 그래프 실험: [docs/design.md](docs/design.md) · 결정: [docs/decisions.md](docs/decisions.md)

```
상대 선택 + 초안 + 상황
  → 의도·보존 항목 추출 → (검색: 페르소나 / 본인 과거 표현 / 그래프 맥락)
  → Claude 첨삭(구조화 출력) → 규칙 검증 → 실패 시 재생성 1회 → 재실패 시 원문 유지
  → 추천 표시 → 사용자가 적용·수정·취소 (자동 전송 없음)
```

실험 조건: **A** 일반 첨삭 / **B** +상대별 페르소나 / **C** +벡터 검색한 본인 과거 표현 / **D** +그래프 검색 맥락.

## 빠른 시작

```bash
cp .env.example .env            # LLM_PROVIDER=fake면 키 없이 동작
py -3.14 -m venv .venv          # 맥: python3 -m venv .venv
.venv/Scripts/python.exe -m pip install -e "backend[dev]"
make test                        # 백엔드 테스트
make dev                         # http://localhost:8000/docs
```

실제 Claude를 쓰려면 `.env`에 `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY`, `LLM_MODEL`을 채운다. Console에서 사용량 한도를 먼저 건다.

Docker로 전체(PostgreSQL+pgvector, Neo4j, 백엔드): `make up` → http://localhost:8000/docs, Neo4j Browser http://localhost:7474

### 수직 기능 시험 (상대 1 · 페르소나 1 · 원문 1)

```bash
curl -s -X POST localhost:8000/v1/contacts -H "X-User-Id: me" -H "Content-Type: application/json" -d '{"alias":"박지현","relationship_type":"superior"}'
```
```bash
curl -s -X PUT localhost:8000/v1/contacts/<id>/persona -H "X-User-Id: me" -H "Content-Type: application/json" -d '{"settings":{"formality":5,"address_term":"팀장님","forbidden_phrases":["ㅋㅋ"]},"expected_version":0}'
```
```bash
curl -s -X POST localhost:8000/v1/rewrites -H "X-User-Id: me" -H "Idempotency-Key: t1" -H "Content-Type: application/json" -d '{"contact_id":"<id>","draft_text":"오늘 정리해서 내일 오전까지 보내드릴게요.","situation":"업무 보고","condition":"B"}'
```

## 폴더 (담당)

| 폴더 | 담당 | 내용 |
|---|---|---|
| `backend/` | 팀장 | FastAPI · 워크플로 · 검증 · 저장소. [app/prompts/](backend/app/prompts) 는 비전공자도 수정 |
| `evals/` | 평가·품질 | 사례 CSV, 보존 규칙, 루브릭, 실행 하니스 |
| `data/synthetic/` | 사용자·시나리오 | 합성 과거 대화 JSON |
| `graph/` | 지식그래프 | 스키마, 호칭 병합·질의 정답, Graphiti 스파이크 |
| `docs/` | 전원 | 설계·결정·일정 |
| `apps/web/` | 팀장 (5주차) | React UI |

협업 규칙(GitHub Desktop, PR, 폴더 소유): [CONTRIBUTING.md](CONTRIBUTING.md) · 역할·일정: [docs/team-roles-and-schedule.md](docs/team-roles-and-schedule.md) · **평가 사례·합성 대화 쓰는 법: [docs/dataset-guide.md](docs/dataset-guide.md)**

## 명령어

| 명령 | 내용 |
|---|---|
| `make test` | 백엔드 테스트 |
| `make lint` | ruff |
| `make eval` | 평가 사례 A/B 실행 → `evals/results/` |
| `make graph-dry-run` | 그래프 스파이크 변환만 확인 |
| `python scripts/validate_data_files.py` | CSV/JSON 접점 파일 형식 검사 |

Windows에 `make`가 없으면 Makefile의 명령을 그대로 실행한다.

## 상태 (2026-11-08)
- 저장소: `github.com/SkyWith628/persona-ize` (비공개). 11/04까지의 초기 뼈대(`spikes/`)는 `graph/graphiti_p0/`로 대체.
- 1주차 수직 기능: 상대·페르소나 CRUD, 첨삭 워크플로(의도·보존 추출 → 프롬프트 → 검증 → 재생성 → 원문 유지), 멱등성, 사용자 격리. 인메모리 저장소. **Claude 실제 호출 검증 완료** (Opus 5.5·Sonnet 5.5, 5사례×A/B, [첫 실측 메모](evals/results/2026-11-08-first-real-run.md)).
- 2주차 예정: 대화 업로드·화자 지정·2~6턴 묶음·로컬 임베딩·pgvector 검색(C).
- 3주차 예정: Graphiti 수집·검색(D), 피드백·삭제·사용량 제어.
