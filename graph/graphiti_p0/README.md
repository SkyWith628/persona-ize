# Graphiti P0 스파이크 (D 경로 사전 검증)

**목적**: 한국어 메신저 대화에서 Graphiti(Zep 논문 구현체)가 인물·관계·사건을 제대로 뽑는지 **소규모 합성 데이터로** 확인한다.
본 앱(`backend/`)과 분리된 실험이며, [project-design.md](../../docs/project-design.md) §5.4 / [design.md](../../docs/design.md) §4-②의 사전 검증이다.
결과로 **D를 계획대로 진행할지, 질의 유형을 축소할지**를 판정한다 (`docs/decisions.md`).

> 합성 데이터(`data/synthetic/`)만 사용한다. 실제 대화는 넣지 않는다.

## 구성 (2026-11-08 변경)

| 역할 | 선택 | 이유 |
|---|---|---|
| 추출 LLM | **Claude** (`GRAPH_LLM_MODEL`, 기본 `claude-haiku-4-5`) | 팀 API 키가 Anthropic. graphiti의 강제 `tool_choice`를 5.5 계열이 400으로 거부해 4.5 Haiku로 시작 |
| 임베딩 | **로컬 sentence-transformers** (`intfloat/multilingual-e5-small`) 또는 Voyage AI | Anthropic에 임베딩 API가 없음. 로컬은 키·비용 없음, Voyage는 품질 비교용 |
| 리랭커 | **패스스루** (`PassthroughReranker`) | graphiti 기본 리랭커는 OpenAI 호출. 필요해지면 로컬 cross-encoder로 교체 |
| 그래프 DB | **로컬 Neo4j 컨테이너** (`docker compose up neo4j`) | 비용 없음, Neo4j Browser로 바로 확인. Aura는 선택 |

## 확인할 것

| 질문 | 확인 방법 | 데이터 속 장치 |
|---|---|---|
| 호칭이 달라도 한 사람으로 병합되나? | 점검표 `PASS 엔티티가 하나로 병합됨` | 박지현 / 지현 팀장님 / 팀장님 |
| 관계가 바뀌면 옛 사실이 무효화되나? | 점검표 `무효화된 엣지`, 사실 목록의 ✗ | 이서연: 동기(반말) → 파트장(존댓말) |
| 말투·호칭 같은 커스텀 속성이 채워지나? | 점검표 `커스텀 속성이 채워진 엣지` | 존댓말/반말, "팀장님", "형" |
| 상대 시간이 절대 시각으로 바뀌나? | 사실 목록의 `valid=` | "어제", "다음 주 수요일" |
| 한국어 검색이 되나? | `--search` 결과 | BM25 한국어 토큰화 리스크 |
| 비용·시간은? | `runs/ingest-*.json`의 에피소드당 초, Anthropic Console 사용량 | 36개 메시지 |

## 1. 준비

1. 저장소 루트에서 `docker compose up -d neo4j` (Neo4j Browser: http://localhost:7474).
2. 루트 `.env`에 `ANTHROPIC_API_KEY`를 채운다 (또는 `ant auth login`). Console에서 월 사용량 한도를 먼저 건다.
3. 이 폴더의 `.env.example`은 참고용이다. 루트 `.env`를 그대로 읽는다 (`load_dotenv`가 상위 폴더를 찾지는 않으므로 실행 시 `--env ../../.env` 또는 복사).

## 2. 설치

저장소 루트의 `.venv`에 그래프 의존성을 추가한다 (sentence-transformers는 torch를 끌어와 용량이 크다):

```bash
.venv/Scripts/python.exe -m pip install -e "backend[graph]"
```

## 3. 실행

```bash
cd graph/graphiti_p0
../../.venv/Scripts/python.exe -m pytest -q            # 네트워크 없이 변환·설정 테스트
../../.venv/Scripts/python.exe ingest.py --dry-run     # 에피소드 변환만 출력
../../.venv/Scripts/python.exe ingest.py --limit 5     # 앞 5개만 수집 → 비용·시간 측정
../../.venv/Scripts/python.exe inspect_graph.py        # 요약 + 점검표
../../.venv/Scripts/python.exe inspect_graph.py --search "팀장님과 일정 조율"
../../.venv/Scripts/python.exe inspect_graph.py --reset --yes   # 이 group_id 데이터 삭제
```

맥/리눅스는 `.venv/bin/python`.

## 4. 판정 기준 (1주차 말)

- 병합: `checks.people`의 5명이 각각 하나의 Person으로 병합 → D 진행.
- 무효화: 이서연 관계 변화에서 옛 엣지 `invalid_at` 설정 → bi-temporal 사용 가능.
- 비용: 에피소드당 평균 초·토큰을 `graph/graph-spike-report.md`에 기록. 36개 기준 금액이 과하면 D 범위를 "관계·사건 질의 사례"로 축소.

## 파일

| 파일 | 역할 |
|---|---|
| `p0/schema.py` | 커스텀 타입 Person·Event·Relationship·Involved + 한국어 추출 지시 |
| `p0/episodes.py` | `data/synthetic/*.json` → 시간 순 에피소드 (형식 오류는 위치와 함께 예외) |
| `p0/config.py` | `.env` 로딩 (Claude, 임베딩 제공자, Neo4j) |
| `p0/graph.py` | Graphiti 클라이언트 (AnthropicClient + 로컬/Voyage 임베더) |
| `p0/embedder_local.py` | sentence-transformers 기반 `EmbedderClient` |
| `ingest.py` | 수집 + `runs/` 보고서 |
| `inspect_graph.py` | 점검표·검색·초기화 |

## 알려진 사항

- **실제 API 호출은 아직 검증하지 않았다.** 테스트·dry-run·import까지만 확인했다. `--limit 5`가 1주차 말 과제다.
- graphiti-core 0.31.2의 `AnthropicClient`는 구조화 출력을 **강제 tool_choice**로 구현한다. Opus/Sonnet/Haiku 5.5는 이를 400으로 거부하므로 기본 모델은 `claude-haiku-4-5`다. 모델별 `max_tokens` 매핑도 구버전 기준이라 새 모델명은 기본값(8192)을 쓴다 — 추출 결과가 잘리면 확인한다.
- Voyage를 쓰면 임베딩 차원이 1024로 바뀐다. 제공자를 바꾸면 `inspect_graph.py --reset --yes`로 지우고 다시 넣는다 (차원 혼합 금지).
- Neo4j 전문검색(BM25)의 한국어 토큰화가 약할 수 있다. 검색이 나쁘면 임베딩 비중을 높인다.
