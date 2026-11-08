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
| 그래프 DB | **로컬 Neo4j 컨테이너** (`docker compose up neo4j`) 또는 **Aura Free** | 로컬은 비용 없음. Docker가 없는 PC는 Aura Free(`neo4j+s://`)로 — 2026-11-08 첫 수집은 Aura에서 했다 |

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

1. Neo4j: 로컬이면 저장소 루트에서 `docker compose up -d neo4j` (Browser: http://localhost:7474). Docker가 없으면 **Aura Free**(https://console.neo4j.io) 인스턴스를 만들고, 생성 직후 내려받는 접속 파일의 값을 루트 `.env`에 넣는다 — `NEO4J_URI=neo4j+s://xxxx.databases.neo4j.io`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`(이때만 보여준다).
2. 루트 `.env`에 `ANTHROPIC_API_KEY`를 채운다 (또는 `ant auth login`). Console에서 월 사용량 한도를 먼저 건다. `GRAPH_LLM_MODEL`은 비우거나 `claude-haiku-4-5`.
3. 이 폴더의 `.env.example`은 참고용이다. 어느 폴더에서 실행해도 `load_dotenv`가 상위 폴더를 거슬러 루트 `.env`를 찾는다 (2026-11-08 확인).

## 2. 설치

저장소 루트의 `.venv`에 그래프 의존성을 추가한다 (sentence-transformers는 torch를 끌어와 용량이 크다):

```bash
.venv/Scripts/python.exe -m pip install -e "backend[db,embed,graph]"
```

## 3. 실행

```bash
cd graph/graphiti_p0
../../.venv/Scripts/python.exe -m pytest -q            # 네트워크 없이 변환·설정 테스트
../../.venv/Scripts/python.exe ingest.py --dry-run     # 에피소드 변환만 출력
../../.venv/Scripts/python.exe ingest.py --limit 5     # 앞 5개만 수집 → 비용·시간 측정
../../.venv/Scripts/python.exe inspect_graph.py        # 요약 + 점검표
../../.venv/Scripts/python.exe inspect_graph.py --search "팀장님과 일정 조율"
../../.venv/Scripts/python.exe eval_queries.py                   # graph_queries.csv 4개 질의 Recall@5
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
| `p0/episodes.py` | `data/synthetic/*.json` → 시간 순 에피소드 (형식 오류는 위치와 함께 예외). 본문에 `[날짜 요일 시각] 발화자 → 수신자:` 접두 |
| `p0/dates.py` | 한국어 상대 날짜('다음 주 수요일', '어제', '11월 5일') → `(=YYYY-MM-DD)` 주석. 수식어 없는 요일은 같은 대화방 앞 메시지가 특정한 날짜(`DiscourseContext`)를 이어받고, 없으면 두 후보 |
| `p0/config.py` | `.env` 로딩 (Claude, 임베딩 제공자, Neo4j) |
| `p0/graph.py` | Graphiti 클라이언트 (AnthropicClient + 로컬/Voyage 임베더) |
| `p0/embedder_local.py` | sentence-transformers 기반 `EmbedderClient` |
| `ingest.py` | 수집 + `runs/` 보고서 |
| `inspect_graph.py` | 점검표·검색·초기화 |
| `p0/queries.py`, `eval_queries.py` | `../graph_queries.csv` 질의를 검색해 출처 메시지 기준 Recall@k (`runs/queries-*.json`) |

## 알려진 사항

- **2026-11-08 밤부터 이 스파이크는 동결.** `p0/schema.py`·`p0/dates.py`·에피소드 본문 규칙은 `backend/app/graph/`로 옮겨졌고 그쪽이 정본이다. 앱의 그래프 수집·검색(D)은 `backend/app/graph/ingest.py`·`retriever.py`, 실행은 `scripts/import_synthetic.py --graph`와 `evals/run_eval.py --conditions D`. 여기 스크립트는 1주차 결과 재현용으로만 남긴다(`.env`의 NEO4J_URI를 Aura로 되돌려야 그 데이터가 보인다).

- **36개 전체 수집을 2026-11-08 Aura에서 다섯 번 끝냈다** (2차→6차: 커스텀 속성 28/37 → 44/49, 엣지 0개 에피소드 6 → 0, 사실 속 날짜 정확도 ≈0 → 24/25, Recall@5 평균 0.52 → 0.77은 1회 측정). 날짜는 `p0/dates.py`가 담화 규칙까지 코드로 풀고, 말투·관계 유형 일관성은 후처리로 넘긴다. 비교표·판정 제안은 [../graph-spike-report.md](../graph-spike-report.md).
- graphiti-core 0.31.2의 `AnthropicClient`는 `messages.create`에 `temperature`를 넘기는데 anthropic SDK 2.x에는 그 인자가 없어 TypeError가 난다. `p0/graph.py`의 `GraphAnthropicClient`가 호출 직전에 `temperature`를 떼어낸다 (2026-11-08). graphiti를 올릴 때 이 래퍼가 여전히 필요한지 확인한다.
- graphiti-core 0.31.2의 `AnthropicClient`는 구조화 출력을 **강제 tool_choice**로 구현한다. Opus/Sonnet/Haiku 5.5는 이를 400으로 거부하므로 기본 모델은 `claude-haiku-4-5`다. 모델별 `max_tokens` 매핑도 구버전 기준이라 새 모델명은 기본값(8192)을 쓴다 — 추출 결과가 잘리면 확인한다.
- **Aura 접속 시 `self-signed certificate in certificate chain`**: Aura 인증서는 SSL.com 루트로 서명돼 있고, 새 Windows PC의 루트 저장소에는 그 루트가 아직 없을 수 있다(Windows는 처음 쓸 때 내려받는데 Python 드라이버는 로컬 저장소만 읽는다). `load_settings()`가 `SSL_CERT_FILE`이 비어 있으면 certifi 번들을 지정해 해결한다 (2026-11-08). 직접 지정한 `SSL_CERT_FILE`은 그대로 쓴다.
- Voyage를 쓰면 임베딩 차원이 1024로 바뀐다. 제공자를 바꾸면 `inspect_graph.py --reset --yes`로 지우고 다시 넣는다 (차원 혼합 금지).
- **에피소드 본문은 `[2026-10-05 월 09:31] 김민수 → 박지현: 다음 주 수요일(=2026-10-14)까지 가능합니다` 형식이다** (`p0.episodes.format_body`, 11-08 4·5차). 수신자는 짧은 답장에서 자기참조 엣지가 생기는 것을 막고, `(=YYYY-MM-DD)`는 `p0/dates.py`의 규칙 기반 해석기가 계산한 절대 날짜다(요일만 보여 주면 haiku가 산술을 틀린다). 검색 결과의 fact 문장에는 들어가지 않는다.
- **시각은 UTC로 저장한다.** graphiti 프롬프트가 REFERENCE_TIME을 UTC로 전제하므로 `ingest.py`가 `reference_time`을 UTC로 바꿔 넘긴다(+09:00 그대로 넘기면 KST/UTC가 섞여 들어왔다, 11-08). `inspect_graph.py`는 보여 줄 때만 한국 시간(+09)으로 바꾼다.
- 엣지 이름은 `Relationship`(사람↔사람)·`Involved`(사람↔사건)만 쓰라고 지시문에 넣었다. 자유 엣지명(`RELATES_TO`, `WORKS_FOR`…)으로 빠지면 커스텀 속성이 비기 때문이다(11-08 1차 9/37). graphiti 0.31.2의 `strict_edge_types`는 `add_episode_bulk`에만 있어 순차 수집에서는 못 쓴다. 사물(국밥·회의록)은 엔티티가 아니라 사건으로 만들라는 지시가 같이 들어 있다.
- Neo4j 전문검색(BM25)의 한국어 토큰화가 약할 수 있다. 검색이 나쁘면 임베딩 비중을 높인다.
