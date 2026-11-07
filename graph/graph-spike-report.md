# 그래프 스파이크 점검 보고 (Graphiti P0)

D 경로(지식그래프) 사전 검증. 판정 기준과 실행법은 [graphiti_p0/README.md](graphiti_p0/README.md), 판정은 `docs/decisions.md`에 기록한다.

## 2차: 36개 전체 수집 (2026-11-08, Aura Free)

| 항목 | 값 |
|---|---|
| 그래프 DB | Neo4j Aura Free (`5.27-aura`, `neo4j+s://`), `--reset` 후 새로 수집 |
| 추출 LLM | `claude-haiku-4-5` (graphiti 0.31.2 `AnthropicClient`, 강제 tool_choice) |
| 임베딩 | 로컬 `intfloat/multilingual-e5-small` 384차원 |
| 데이터 | `data/synthetic/sample_conversations.json` 36개 에피소드 (대화방 4개, 인물 5명) |
| 결과 | 36/36 수집, 엔티티 18(Person 6·Event 7·무타입 5), 사실 엣지 37(무효화 8) |
| 시간 | 합계 401초, **에피소드당 중앙값 11.1초**(최소 5.1, 최대 28.1). 짧은 응답("일단 알겠어요")은 5초대 |
| 실행 기록 | `graphiti_p0/runs/ingest-20261108-071424.json`, `runs/queries-20261108-071529.json` (git 제외) |

### 점검표 (`inspect_graph.py`)

| 질문 | 결과 | 근거 |
|---|---|---|
| 호칭이 달라도 한 사람으로 병합되나? | **PASS 5/5** | "팀장님"·"지현 팀장님"·"민수야"·"서연 파트장님"·"유진아"·"형" 전부 각 1노드. "김준호의 어머니"는 파생 인물이라 분열이 아님(검사에서 소유격 파생을 제외하도록 수정) |
| 관계가 바뀌면 옛 사실이 무효화되나? | **PASS (기전) / 설계 장면은 부분** | 무효화 8건. 박지현 관계는 colleague → subordinate로 바뀌며 옛 엣지가 `invalid_at`됨. **이서연(동기 반말 → 파트장 존댓말)은 옛 엣지 무효화 대신 새 엣지(`BECOMES_SUPERVISOR_OF`, `speech_style=혼용/존댓말`) 추가**로 표현됨. c3:m01~m04의 반말 자체가 `speech_style`로 추출되지 않아 모순이 성립하지 않았다 |
| 커스텀 속성이 채워지나? | **PASS 28/37** | 나머지 9개는 graphiti가 자유 엣지명(`RELATES_TO`, `WORKS_FOR`, `PLANS_TO_MEET`, `REQUESTS`…)으로 빠짐. 엔티티도 5개(주간보고·국밥·케이크·API 문서·스탠드업 회의록)가 무타입 |
| 상대 시간이 절대 시각으로 바뀌나? | **WARN** | "다음 주 수요일"이 한 사실에서는 `2026년 10월 14일`(정답), 다른 사실에서는 `10월 13일`(오답)로 적힘. `valid_at`은 KST(`09:52`)와 UTC(`00:52`)가 섞임 — c1:m05, c1:m09, c4:m07에서 UTC 변환 |
| 한국어 검색이 되나? | **부분** | 아래 Recall@5. 질의어와 사실 문장이 겹치는 경우(워크숍 발표)는 1.0, 사건을 돌려 말한 질의(갈등, 불참)는 낮음 |
| 비용·시간은? | 시간은 위 표. **토큰·금액은 Anthropic Console 사용량 페이지에서 확인**(graphiti가 토큰 수를 돌려주지 않음). 36개에 haiku 호출 ≈ 에피소드당 3~5회 |

### `graph_queries.csv` Recall@5 (`eval_queries.py`, 검색 엣지의 출처 메시지 기준)

| 질의 | needs_graph | Recall@5 | 정답 | 검색된 메시지(상위 5) | 메모 |
|---|---|---|---|---|---|
| q01 팀장님과 일정 조율에서 갈등이 있었던 일 | Y | 0.40 | c1:m03;m04;m05;m07;m08 | c2:m04, c1:m07, c1:m01, c1:m08, c3:m04 | 사과(m07)·합의(m08)는 잡았고 갈등 당일(m03~m05)은 놓침. c3:m04("혼난 건 아니고…")가 걸린 것은 그래프가 방 넘어 사건을 연결한 긍정 신호 |
| q02 워크숍 발표 자료 약속 | N | 1.00 | c1:m10;m11 | c1:m10, c1:m11, … | 어휘가 겹쳐 쉬움 |
| q03 서연과 관계가 바뀐 시점 | Y | 0.67 | c3:m05;m06;m08 | c3:m05, c3:m03, c3:m08, c1:m01, c3:m09 | 발령(m05)·첫 존댓말(m08) 잡음. m06("존댓말 써야겠네")은 엣지 0개로 추출돼 검색 불가 |
| q04 홈커밍 불참과 다음 약속 | N | 0.00 | c4:m04;m06;m07 | c4:m03, c4:m02, c4:m01, c1:m10, c1:m11 | **c4:m04 "못 갈 것 같아"가 사실 엣지로 남지 않았다**(기존 '참석 고려' 엣지도 무효화되지 않음). m06·m07 사실은 있으나 홈커밍 사실들이 먼저 랭크됨 |
| 평균 | | **0.52** | | | |

### 품질에서 눈에 띈 것 (사람 검토 필요)

- **말투 오판**: c1:m07 "죄송합니다"(존댓말)인데 엣지 `speech_style=반말`. 박지현 관계에 `acquaintance, closeness=1`도 붙음. 같은 두 사람의 relation_type이 colleague/acquaintance/subordinate로 흔들린다 → 스키마 설명에 "관계 유형은 한 쌍에 하나, 바뀌면 이전 사실을 무효화"를 명시해야 한다.
- **`address_term` 오귀속**: c3:m03 "너 팀장님한테 혼났다며"에서 이서연→김민수 엣지에 `address_term=팀장님`. 호칭 필드가 "상대를 부르는 말"인지 "대화 속 등장 호칭"인지 추출 지시가 모호하다.
- **`participant_role` 값 혼재**: 당사자/관련자/참여자/참석자/담당자/파트장 + 영어 requester. 허용값 목록이 필요하다.
- **부정·취소 사실 누락**: "못 갈 것 같아"(c4:m04)처럼 기존 계획을 뒤집는 짧은 메시지가 엣지가 되지 않았다. q04가 0인 직접 원인. 추출 지시에 "취소·거절·불참도 사건으로 남긴다"를 넣고 재측정.

### 환경에서 걸린 것 (코드로 해결, 커밋됨)

- **Aura TLS 검증 실패** `self-signed certificate in certificate chain`: Aura 인증서가 SSL.com 루트인데 새 Windows PC의 루트 저장소에 그 루트가 없었다. `load_settings()`가 `SSL_CERT_FILE` 미설정 시 certifi 번들을 지정해 해결.
- **`temperature` TypeError**: anthropic SDK 2.6.0 `messages.create`에 `temperature`가 없는데 graphiti 0.31.2가 넘긴다. `GraphAnthropicClient`가 호출 직전에 떼어낸다.
- `ingest.py`가 스파이크 폴더의 `.env`만 보던 것을 상위 폴더(루트 `.env`)까지 찾도록 수정.
- 소음: graphiti가 인덱스 생성을 두 번 병렬로 시도해 Aura가 `EquivalentSchemaRuleAlreadyExists`를 20여 줄 찍는다. 실패가 아니다. "Materialized 1 dangling edge endpoint node" 9건도 결과에 영향 없음.

### 판정 제안 (팀 결정은 `docs/decisions.md`)

- 병합·bi-temporal 무효화·커스텀 속성·한국어 하이브리드 검색이 **기전으로는 전부 동작**한다. 비용·시간(에피소드당 ~11초, haiku)은 36개 규모에서 문제 없다.
- 품질은 추출 지시 보강이 필요하고(위 4건), **D 채택 조건(needs_graph=Y에서 C 대비 개선)은 C가 생기는 2주차에 같은 질의로 판정**한다. 지금 수치(q01 0.40, q03 0.67)는 그때의 기준선이다.

### 다음

- [ ] `p0/schema.py` 추출 지시 보강: relation_type 일관성·무효화 규칙, address_term 정의, participant_role 허용값, 취소·거절 사건, 시간대(KST 고정). 보강 후 `--reset` → 36개 재수집 → 이 표 갱신.
- [ ] Console에서 36건 수집의 토큰·금액을 읽어 전체 데이터셋(대화방 수 × 메시지 수) 기준으로 환산해 적는다.
- [ ] 2주차: C(pgvector) 검색기에 같은 `graph_queries.csv`를 돌려 q01·q03 비교 → D 진행/축소 판정.
- [ ] `graph_queries.csv`에 needs_graph=Y 질의를 늘린다(현재 2개). 시나리오 담당에게 "여러 메시지에 흩어진 같은 사건" 장면 추가 요청.

## 1차: `--limit 5` (2026-11-08) 요약

c1:m01~m05만 수집. 5/5 성공, 첫 건 84.9초(임베딩 모델 다운로드 포함) 이후 10.8~13.8초. 김민수·박지현 병합 PASS, colleague→superior 무효화 1건, 커스텀 속성 6/6, "팀장님과 대시보드 일정 조율" 검색 상위 3개 적중. 2차에서 전부 재검증했으므로 세부 표는 생략한다.
