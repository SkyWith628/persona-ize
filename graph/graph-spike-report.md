# 그래프 스파이크 점검 보고 (Graphiti P0)

D 경로(지식그래프) 사전 검증. 판정 기준과 실행법은 [graphiti_p0/README.md](graphiti_p0/README.md), 판정은 `docs/decisions.md`에 기록한다.

공통 환경: Neo4j Aura Free(`5.27-aura`), 추출 LLM `claude-haiku-4-5`(graphiti 0.31.2), 임베딩 로컬 `intfloat/multilingual-e5-small` 384차원, 데이터 `data/synthetic/sample_conversations.json` 36개 에피소드(대화방 4, 인물 5). 실행 기록은 `graphiti_p0/runs/`(git 제외).

## 3차: 추출 지시 보강 후 재수집 (2026-11-08)

보강 내용(`p0/schema.py`): 사물은 엔티티가 아니라 사건으로, 엣지 이름은 Relationship·Involved만, 말투는 어미로 판단, 호칭은 직접 부른 말만, 한 쌍에 relation_type 하나(바뀌면 무효화), 취소·거절·불참도 사건으로, 상대 날짜는 KST로 계산해 `YYYY-MM-DD`를 사실 문장에 적기, `participant_role`은 6개 값 Literal. `ingest.py`는 `reference_time`을 UTC로 바꿔 넘긴다.

### 2차(보강 전) → 3차(보강 후)

| 항목 | 2차 | 3차 | 비고 |
|---|---|---|---|
| 수집 | 36/36, 401초, 중앙값 11.1초 | 36/36, 407초, 중앙값 11.6초 | 지시문이 길어져도 시간 차이 없음 |
| 엔티티 | 18 (Person 6·Event 7·무타입 5) | 18 (Person 6·Event 11·무타입 1) | 국밥·케이크·회의록·API 문서가 사건으로 바뀜. 남은 무타입은 '꽃' |
| 사실 엣지 | 37 (무효화 8) | 38 (무효화 10) | |
| 커스텀 속성 채워진 엣지 | 28/37 | **35/38** | 남은 3개: `INVOLVED`(대문자 변형) 2, `RELATES_TO` 자기참조 1 |
| 인물 병합 | 5/5 | 5/5 | "김준호의 엄마"는 파생 인물 |
| 이서연 관계 변화(설계 장면) | 새 엣지 추가만 | **옛 엣지(acquaintance) `invalid_at`=발령 알림 시각** + superior·존댓말/혼용 새 엣지 | 설계한 bi-temporal 장면이 그대로 재현됨 |
| 시간대 | KST/UTC 혼재 | **전부 UTC 저장, 표시는 +09** | `inspect_graph.py`가 KST로 보여 줌 |
| participant_role 값 | 7종 혼재(영어 포함) | 당사자·요청자·수행자만 | Literal 효과 |
| c1:m07 말투 | 반말(오판) | 존댓말 | 어미 규칙 효과 |
| c3:m03 제3자 호칭 오귀속 | 있음 | 없음 | |
| c4:m04 불참 사실 | 누락 | 기록됨 + 이전 '참석 계획' 엣지 무효화 | q04 0 → 0.67의 원인 |

### `graph_queries.csv` Recall@5 (`eval_queries.py`)

| 질의 | needs_graph | 2차 | 3차 | 3차 검색 메시지(상위 5) | 메모 |
|---|---|---|---|---|---|
| q01 팀장님과 일정 조율에서 갈등이 있었던 일 | Y | 0.40 | 0.40 | c1:m07, c3:m04, c2:m04, c3:m03, c1:m05 | 사과(m07)·절충(m05)은 잡고 갈등 당일 m03·m04·합의 m08은 놓침. 다른 방(c3:m03·m04 "혼났다며")을 끌어온 것은 그래프 쪽 장점 |
| q02 워크숍 발표 자료 약속 | N | 1.00 | 1.00 | c4:m04, c1:m10, c1:m11, … | c4:m04("발표 준비해야 돼서")가 1위로 올라온 것도 맞는 연결 |
| q03 서연과 관계가 바뀐 시점 | Y | 0.67 | 0.67 | c3:m06, c3:m05, c3:m09, c3:m03, c1:m07 | 발령(m05)·존댓말 전환(m06) 잡음. 첫 존댓말 실사용(m08)은 5위 밖 |
| q04 홈커밍 불참과 다음 약속 | N | 0.00 | **0.67** | c4:m04, c4:m07, c4:m03, c4:m02, c4:m01 | 불참(m04)·날짜 잡기(m07) 잡음. 밥 약속 제안(m06)은 5위 밖 |
| 평균 | | 0.52 | **0.68** | | |

### 남은 품질 문제 (3차에서 새로 보이거나 그대로인 것)

- **relation_type 방향 혼동**: 김민수→박지현 `superior`(정의대로: target이 상사)인데 박지현→김민수도 `superior`, 이서연→김민수 `superior`, 김민수→이서연 `subordinate`. 모델이 "target의 지위"와 "source의 지위"를 섞는다. 다음 보강: `relation_type`은 대칭 유형(family/friend/colleague/…)만 두고 상하는 별도 필드 `hierarchy: target_higher | target_lower | equal`로 분리.
- **날짜 계산 오류**: 2026-10-05(월) 기준 "다음 주 수요일"을 10-15, 10-08로 적음(정답 10-14). "금요일까지"를 10-11(일요일)로 적음. haiku의 요일 산술이 불안정하다. 다음 보강: 에피소드 본문 앞에 `[2026-10-05 월 09:31]`처럼 요일을 넣어 주거나, 날짜 해석을 코드로 후처리.
- **자기참조 엣지 2건**: `최유진 -RELATES_TO-> 최유진`, `김민수 -INVOLVED-> 김민수`. 짧은 답("좋아 내가 날짜 잡을게", "네 금요일 오전까지 하겠습니다")에서 상대 엔티티를 못 세워 생긴 것. `Materialized dangling edge endpoint` 로그 6건과 같은 원인으로 보임. 다음 보강: 답장 에피소드에 직전 메시지 발화자를 명시(`source_description`에 "박지현에게 답함").
- **participant_role 의미 오류**: "부탁받았다"에 `요청자`(정답 수행자/상대). 값은 닫혔지만 선택은 틀린다. 설명에 예문을 넣어 재시도.
- q01은 두 번 다 0.40. 갈등 당일 메시지(m03·m04)가 "갈등"이라는 말과 어휘가 겹치지 않는다. 사건 엔티티에 `event_type=conflict`가 붙으면 그 사건의 모든 관여 메시지를 끌어오는 식의 **엔티티 중심 검색**(사실 검색 + 해당 사건 노드의 에피소드 전개)이 D 검색기 설계에 필요하다.

### 비용

토큰·금액은 graphiti가 돌려주지 않아 **Anthropic Console 사용량 페이지에서 확인**한다. 오늘 haiku-4-5 호출은 수집 4회(5개 + 36개 + 1개 스모크 + 36개) = 78 에피소드분. 에피소드당 haiku 호출은 3~5회(노드 추출·엣지 추출·속성·중복 판정·요약).

### 환경에서 걸린 것 (코드로 해결, 커밋됨)

- **Aura TLS 검증 실패** `self-signed certificate in certificate chain`: Aura 인증서가 SSL.com 루트인데 새 Windows PC의 루트 저장소에 그 루트가 없었다. `load_settings()`가 `SSL_CERT_FILE` 미설정 시 certifi 번들을 지정해 해결.
- **`temperature` TypeError**: anthropic SDK 2.6.0 `messages.create`에 `temperature`가 없는데 graphiti 0.31.2가 넘긴다. `GraphAnthropicClient`가 호출 직전에 떼어낸다.
- `ingest.py`가 스파이크 폴더의 `.env`만 보던 것을 상위 폴더(루트 `.env`)까지 찾도록 수정.
- `strict_edge_types`는 `add_episode_bulk`에만 있어 순차 수집에서는 못 쓴다. 지시문으로 대체.
- 소음: graphiti가 인덱스 생성을 두 번 병렬로 시도해 Aura가 `EquivalentSchemaRuleAlreadyExists`를 20여 줄 찍는다. 실패가 아니다. `LLM returned invalid duplicate_facts idx values` 1건은 graphiti가 자체 처리.

### 판정 제안 (팀 결정은 `docs/decisions.md`)

- 병합·bi-temporal 무효화(설계 장면 포함)·커스텀 속성·한국어 하이브리드 검색이 **기전으로 전부 동작**하고, 지시문 보강만으로 Recall@5 평균이 0.52 → 0.68로 올랐다. 비용·시간(에피소드당 ~11초, haiku)은 문제 없다.
- **D 채택 조건(needs_graph=Y에서 C 대비 개선)은 C가 생기는 2주차에 같은 질의로 판정**한다. 3차 수치(q01 0.40, q03 0.67)가 그때의 D 기준선이다.

### 다음

- [ ] 4차 보강: `hierarchy` 필드 분리, 본문에 요일 표기, 답장 에피소드에 수신자 명시, participant_role 예문. 보강 후 `--reset` → 재수집 → 이 표에 4차 열 추가.
- [ ] Console에서 오늘 haiku 토큰·금액을 읽어 에피소드당 비용과 전체 데이터셋 환산치를 적는다.
- [ ] 2주차: C(pgvector) 검색기에 같은 `graph_queries.csv`를 돌려 q01·q03 비교 → D 진행/축소 판정.
- [ ] `graph_queries.csv`에 needs_graph=Y 질의를 늘린다(현재 2개). 시나리오 담당에게 "여러 메시지에 흩어진 같은 사건" 장면 추가 요청.
- [ ] D 검색기 설계에 엔티티 중심 전개(사건 노드 → 관여 에피소드) 포함.

## 2차: 36개 전체 수집, 보강 전 (2026-11-08) 요약

36/36, 401초. 병합 5/5, 무효화 8(이서연 장면은 새 엣지 추가만), 커스텀 속성 28/37, 무타입 엔티티 5, 시간대 KST/UTC 혼재, Recall@5 평균 0.52(q04 0: 불참 사실 누락). 품질 과제 5건이 3차 보강의 근거가 됐다. 세부 비교는 3차 표.

## 1차: `--limit 5` (2026-11-08) 요약

c1:m01~m05만 수집. 5/5 성공, 첫 건 84.9초(임베딩 모델 다운로드 포함) 이후 10.8~13.8초. 김민수·박지현 병합 PASS, colleague→superior 무효화 1건, 커스텀 속성 6/6, "팀장님과 대시보드 일정 조율" 검색 상위 3개 적중.
