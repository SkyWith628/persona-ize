# 그래프 스파이크 점검 보고 (Graphiti P0)

D 경로(지식그래프) 사전 검증. 판정 기준과 실행법은 [graphiti_p0/README.md](graphiti_p0/README.md), 판정은 `docs/decisions.md`에 기록한다.

## 1차: `--limit 5` (2026-11-08, Aura Free)

| 항목 | 값 |
|---|---|
| 그래프 DB | Neo4j Aura Free (`5.27-aura`, `neo4j+s://`) |
| 추출 LLM | `claude-haiku-4-5` (graphiti 0.31.2 `AnthropicClient`, 강제 tool_choice) |
| 임베딩 | 로컬 `intfloat/multilingual-e5-small` 384차원 |
| 데이터 | `data/synthetic/sample_conversations.json` 앞 5개 에피소드 = c1:m01~m05 (김민수 ↔ 박지현 팀장, 대시보드 일정 조율) |
| 결과 | 5/5 수집, 엔티티 5(Person 2·Event 3), 사실 엣지 6(무효화 1) |
| 시간 | 합계 133.6초. 첫 건 84.9초는 임베딩 모델 다운로드 포함, 이후 **에피소드당 10.8~13.8초** |
| 실행 기록 | `graphiti_p0/runs/ingest-20261108-065523.json` (git 제외) |

### 점검표 결과

| 질문 | 결과 | 근거 |
|---|---|---|
| 호칭이 달라도 한 사람으로 병합되나? | **PASS** | "팀장님"·"민수씨"로 불려도 김민수·박지현 각 1노드. 5개 안에 다른 인물은 등장하지 않아 김준호·이서연·최유진은 미판정 |
| 관계가 바뀌면 옛 사실이 무효화되나? | **PASS (기전 확인)** | c1:m03의 `Relationship(colleague, 존댓말)` 엣지가 c1:m05 시점에 `invalid_at` 설정되고 `Relationship(superior, 호칭 '팀장')`으로 대체됨. 설계한 이서연(동기→파트장) 장면은 뒤쪽 에피소드라 아직 미검증 |
| 커스텀 속성이 채워지나? | **PASS** | 6/6 엣지. `relation_type`, `speech_style`, `address_term`, `participant_role` 채워짐. 단 `participant_role` 값이 한국어('작성자','요청자','당사자')와 영어('performer')로 섞임 → 스키마 설명에 허용값을 명시해야 한다 |
| 상대 시간이 절대 시각으로 바뀌나? | **WARN** | `valid_at`은 대체로 메시지 시각으로 들어갔지만 한 건(c1:m03)은 `00:31`로 UTC 변환돼 다른 건(`09:xx`, KST 그대로)과 섞였다. "다음 주 수요일"은 사실 문장에 그대로 남고 날짜로 바뀌지 않았다. → 수집 전 reference_time을 UTC로 통일하거나 수집 후 정규화가 필요 |
| 한국어 검색이 되나? | **PASS** | `--search "팀장님과 대시보드 일정 조율"` 상위 3개가 모두 대시보드 일정 관련 사실. 5개 규모라 BM25 토큰화 리스크는 36개·질의 4개(`graph_queries.csv`)에서 다시 본다 |
| 비용·시간은? | 시간은 위 표. **토큰·금액은 Anthropic Console 사용량 페이지에서 확인** (graphiti가 토큰 수를 돌려주지 않아 보고서에 없음) |

### 환경에서 걸린 것 (코드로 해결, 커밋됨)

- **Aura TLS 검증 실패** `self-signed certificate in certificate chain`: Aura 인증서가 SSL.com 루트인데 새 Windows PC의 루트 저장소에 그 루트가 없었다. `load_settings()`가 `SSL_CERT_FILE` 미설정 시 certifi 번들을 지정해 해결.
- **`temperature` TypeError**: anthropic SDK 2.6.0 `messages.create`에 `temperature`가 없는데 graphiti 0.31.2가 넘긴다. `GraphAnthropicClient`가 호출 직전에 떼어낸다.
- `ingest.py`가 스파이크 폴더의 `.env`만 보던 것을 상위 폴더(루트 `.env`)까지 찾도록 수정.
- 소음: graphiti가 인덱스 생성을 두 번 병렬로 시도해 Aura가 `EquivalentSchemaRuleAlreadyExists`를 20여 줄 찍는다. 실패가 아니며 무시한다. "Materialized 1 dangling edge endpoint node" 1건도 결과에 영향 없음.

### 다음

- [ ] `inspect_graph.py --reset --yes` 후 36개 전체 수집 (예상 36×~12초 ≈ 7~8분, 모델 다운로드 없음) → 김준호·이서연·최유진 병합, 이서연 관계 변화 무효화, `graph_queries.csv` 4개 질의 Recall 확인.
- [ ] `participant_role` 허용값과 시간대(UTC 통일) 규칙을 `p0/schema.py` 추출 지시에 추가.
- [ ] Console에서 5건 수집의 토큰·금액을 읽어 36개·전체 데이터셋 기준으로 환산해 여기에 적는다.
- [ ] 36개 결과로 D 진행/축소 판정 → `docs/decisions.md`.
