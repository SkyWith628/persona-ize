# Graphiti P0 스파이크

**목적**: 한국어 메신저 대화에서 Graphiti(Zep 논문 구현체)가 인물·관계·사건을 제대로 뽑는지 **소규모 합성 데이터로** 확인한다.
본 앱과 분리된 실험이며, [project-design.md](../../docs/project-design.md) §5.4 "GraphRAG 확장 실험"의 사전 검증에 해당한다.

> 합성 데이터만 사용한다. 실제 대화는 넣지 않는다 (Aura는 외부 클라우드).

## 확인할 것

| 질문 | 확인 방법 | 데이터 속 장치 |
|---|---|---|
| 호칭이 달라도 한 사람으로 병합되나? | 점검표 `PASS 엔티티가 하나로 병합됨` | 박지현 / 지현 팀장님 / 팀장님 |
| 관계가 바뀌면 옛 사실이 무효화되나? | 점검표 `무효화된 엣지`, 사실 목록의 ✗ | 이서연: 동기(반말) → 파트장(존댓말) |
| 말투·호칭 같은 커스텀 속성이 채워지나? | 점검표 `커스텀 속성이 채워진 엣지` | 존댓말/반말, "팀장님", "형" |
| 상대 시간이 절대 시각으로 바뀌나? | 사실 목록의 `valid=` | "어제", "다음 주 수요일" |
| 한국어 검색이 되나? | `--search` 결과 | BM25 한국어 토큰화 리스크 |
| 비용·시간은? | `runs/ingest-*.json`의 에피소드당 초 | 36개 메시지 |

## 1. 준비 (사용자가 직접)

1. **Neo4j Aura Free 인스턴스 생성**: https://console.neo4j.io 에서 가입 → New Instance → Free.
   생성 직후 내려받는 접속 파일(`Neo4j-xxxx.txt`)에 URI·사용자·비밀번호가 있다. 비밀번호는 이때만 보여준다.
2. **Anthropic API 키**(https://console.anthropic.com, 월 사용 한도 먼저 설정)와 **Voyage API 키**(https://dash.voyageai.com) 발급
3. `.env.example`을 `.env`로 복사하고 위 값을 채운다. `.env`는 `.gitignore`에 포함되어 있다.

```bash
cp .env.example .env
```

## 2. 설치

이 폴더에는 이미 `.venv`(Python 3.14)가 만들어져 있다. 새로 만들 때:

```bash
py -3.14 -m venv .venv
```

```bash
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

## 3. 실행 순서

네트워크 없이 테스트와 변환 결과를 확인한다.

```bash
.venv/Scripts/python.exe -m pytest -q
```

```bash
.venv/Scripts/python.exe ingest.py --dry-run
```

**먼저 5개만** 넣어 비용과 시간을 본다. Anthropic·Voyage 사용량 페이지에서 비용을 확인한 뒤 전체를 넣는다.

```bash
.venv/Scripts/python.exe ingest.py --limit 5
```

```bash
.venv/Scripts/python.exe inspect_graph.py
```

전체 수집 전에 앞의 5개를 지우고 처음부터 넣는다 (순서가 바뀌면 무효화 판단이 달라진다).

```bash
.venv/Scripts/python.exe inspect_graph.py --reset --yes
```

```bash
.venv/Scripts/python.exe ingest.py
```

```bash
.venv/Scripts/python.exe inspect_graph.py --search "팀장님과 일정 조율"
```

`inspect_graph.py`는 점검표에 WARN이 있으면 종료 코드 1을 반환한다.

## 4. Neo4j Browser로 보기

Aura 콘솔 → 인스턴스 → Query. 아래 쿼리로 그래프를 그린다.

```cypher
MATCH p=(a:Entity {group_id: 'p0-sample'})-[r:RELATES_TO]->(b:Entity)
RETURN p
```

무효화된 사실만 보기:

```cypher
MATCH (a:Entity {group_id: 'p0-sample'})-[r:RELATES_TO]->(b)
WHERE r.invalid_at IS NOT NULL OR r.expired_at IS NOT NULL
RETURN a.name, r.fact, r.valid_at, r.invalid_at, b.name
```

## 5. 결과 기록

실행 후 아래를 채워 팀과 공유한다. 이 결과로 D 실험(그래프 검색) 진행 여부를 판단한다.

| 항목 | 결과 |
|---|---|
| 사용 모델 (LLM / 임베딩) | |
| 에피소드당 평균 시간 | |
| 36개 수집 총비용 (Anthropic+Voyage 사용량 기준) | |
| 인물 병합 PASS 수 / 5 | |
| 무효화 엣지 수 (이서연 관계 변화 반영?) | |
| 커스텀 속성이 채워진 엣지 비율 | |
| 한국어 검색 상위 5개 중 관련 결과 수 | |
| 판단: D 실험 진행 / 보류, 이유 | |

## 파일

| 파일 | 역할 |
|---|---|
| `p0/schema.py` | 커스텀 타입 `Person`·`Event`, 엣지 `Relationship`·`Involved`, 한국어 추출 규칙 |
| `p0/episodes.py` | 데이터셋 JSON → 시간 순 에피소드 (잘못된 메시지는 건너뛰지 않고 오류) |
| `p0/config.py` | `.env` 로딩 (Aura의 `NEO4J_USERNAME` 지원) |
| `p0/graph.py` | Claude LLM·Voyage 임베딩 + Neo4j로 Graphiti 구성 (리랭커는 패스스루) |
| `ingest.py` | 순차 수집, 실패 시 중단, `runs/` 보고서 |
| `inspect_graph.py` | 엔티티·사실 출력, 점검표, 검색, group 단위 삭제 |
| `data/sample_conversations.json` | 합성 대화 4개방 36메시지 + 점검 기준 |

## 알려진 사항

- `graphiti-core==0.31.2`는 `httpx`를 import하지만 의존성으로 선언하지 않아 `requirements.txt`에 따로 고정했다.
  openai 3.x SDK는 `httpx2`를 쓴다. 두 패키지가 함께 설치되어도 import는 정상 동작함을 확인했다.
- **실제 API 호출은 아직 검증하지 않았다.** Aura와 API 키가 없어 테스트·dry-run·import까지만 확인했다.
  첫 `--limit 5` 실행에서 오류가 나면 `runs/` 보고서의 `failed` 항목을 확인한다.
- `SEMAPHORE_LIMIT`는 graphiti 모듈 import 시점에 읽히므로, 스크립트는 `.env`를 먼저 읽고 graphiti를 import한다.
- 에피소드는 메시지 1개 단위다. 팀 설계의 "2~6턴 묶음"과 비교하려면 `p0/episodes.py`에서 묶음 단위로 바꿔 다시 실행한다.
