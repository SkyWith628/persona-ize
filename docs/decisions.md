# 결정 기록

## 2026-11-02 팀 운영·범위 확정
- 결정: 팀장 단독 개발 + 비전공자는 "평가 시트·프롬프트 문서·스키마 설계" 중심(방향 B+C 혼합). 전원이 GitHub(PR 기반)와 Docker를 쓰되, 비전공자는 GitHub Desktop·`make` 명령 수준으로 제한한다(2026-11-02 수정: 비전공자도 코드 작업 가능성 있음).
- 결정: 지식그래프(D)를 필수 과제로 승격. MVP 이후가 아니라 3주차부터 본 구현하고 C와 나란히 비교한다.
- 이유: 공모전·포폴에서 "그래프가 단순 검색보다 나은가"를 실측으로 보여주는 것이 차별점. 비전공자가 평가와 스키마에서 직접 기여할 수 있다.
- 리스크: 그래프 수집 비용·한국어 추출 품질 미검증 → 1주차에 스파이크 `--limit 5`로 확인, 실패하면 D 범위를 축소(관계·사건 질의 사례 한정).
- 리스크: 공모전 공고 미정 → 3주차 말(11/22)에 시연 가능한 체크포인트를 만든다.
- 예산: 인당 구독 $20/월은 API와 별개. 팀 공용 API 한도를 정해(초안 $50~100, 미확정) 사용량 알림을 건다.

## 2026-11-04 모델 제공자: Claude + Voyage
- 결정: LLM은 Anthropic Claude, 임베딩은 Voyage AI(`voyage-3.5`, 1024차원). Graphiti 리랭커는 패스스루(OpenAI 기본값 제거). OpenAI 키는 쓰지 않는다.
- 이유: Anthropic은 임베딩 모델이 없고 Voyage를 권장한다. 키 두 개(Anthropic, Voyage)로 통일.
- 제약: graphiti `AnthropicClient`는 강제 `tool_choice`를 쓰는데 Sonnet/Opus 5.5는 이를 400으로 거부한다. 그래프 수집은 `claude-haiku-4-5`로 시작하고, 5.5 계열은 소규모 검증 후에만 쓴다. 첨삭·검증 코드는 직접 작성하므로 구조화 출력을 쓴다.
- 미검증: Voyage 모델명·한국어 검색 품질, Claude 한국어 그래프 추출 품질. 임베딩 차원이 바뀌었으므로 기존 Aura 데이터는 `--reset` 필요.

## 2026-11-08 LLM을 Claude로, 임베딩은 로컬 모델로 (11-04 수정)
- 결정: 첨삭 LLM은 **Claude**(`anthropic` SDK, `messages.parse` 구조화 출력), 기본 `claude-opus-5-5`. 그래프 추출은 11-04 제약대로 `claude-haiku-4-5` 유지(graphiti 강제 tool_choice). OpenAI 코드는 넣지 않는다.
- 결정: 임베딩(C 경로 pgvector, D 경로 Graphiti) 기본을 Voyage에서 **로컬 sentence-transformers** `intfloat/multilingual-e5-small`(384차원)로 바꾼다. Voyage는 대안(`EMBEDDING_PROVIDER=voyage`)으로 남긴다.
- 이유: 팀 API 키가 Anthropic 하나라 Voyage 키를 추가로 관리하지 않기 위해. 로컬 모델은 키·비용 없이 돌아간다. 모델·차원은 결과에 기록해 재색인 시 혼합을 막는다.
- 리스크: 로컬 모델은 torch 의존(설치 용량 큼), e5-small의 한국어 검색 품질은 2주차 Recall@5로 확인 → 낮으면 `bge-m3` 또는 Voyage로 교체. Opus 5.5 비용은 120사례×4조건×3회 기준으로 1주차 말 `--repeat 1` 실측 후 Sonnet/Haiku와 비교해 조정.
- 결정: 모델이 거절(`stop_reason=refusal`)하거나 장애면 **원문 유지**로 축소 동작한다. 서버측 fallback 모델 전환은 쓰지 않는다 (설계 원칙: 검증 못 한 결과는 내지 않음).

## 2026-11-08 앱 저장소 생성 (`~/dev/persona-ize`, iCloud 밖)
- 결정: 1주차 수직 기능은 **인메모리 저장소 + `X-User-Id` 헤더 인증 스텁**으로 간다. 저장소 인터페이스는 유지하고 2주차에 PostgreSQL, 3주차에 세션 인증으로 교체.
- 결정: 검색기는 조건별 교체형(`Retriever`). 지금은 `NullRetriever`만 있어 A/B 실험만 가능하다.
- 이유: 검증·재생성·원문 유지 흐름을 먼저 고정하고, 저장소·인증은 그 뒤에 붙여도 흐름이 바뀌지 않는다.
- 이 폴더(iCloud)의 `docs/`가 원본이고 저장소 `docs/`는 복사본. 앱 코드는 저장소에만 있다.

## 2026-11-08 GitHub 원격 연결과 11-04 뼈대 병합
- 상황: `github.com/SkyWith628/persona-ize`(비공개, 협업자 3명)에 11/02~11/04 뼈대(health 전용 backend, `spikes/graphiti-p0`, `docs/dataset-guide.md`)가 이미 있었다. 로컬 11/08 뼈대와 이력이 달랐다.
- 결정: 11/08 뼈대가 코드를 대체하고(`spikes/` → `graph/graphiti_p0/`), 원격 이력은 `-s ours` 병합으로 보존한다. 원격에만 있던 리랭커 패스스루·haiku-4-5 제약·PR 템플릿·데이터셋 가이드는 이식한다.
- 결정: **평가셋 열은 `docs/dataset-guide.md`(비전공자용) 기준**으로 통일한다 — `case_id, relation, situation, original_text, preserve_items, must_not_change, expected_notes, needs_graph, relevant_msg_ids, as_of, split`. 등장인물 표는 `evals/characters.csv`로 두고 `relation`→`relationship_type` 매핑은 거기서 한다. 메시지 참조는 `대화방id:메시지id`(예 `c1:m03`), `graph/graph_queries.csv`와 같다.
- 이유: 팀원이 이미 가이드를 보고 작성할 수 있으므로 코드가 가이드를 따르는 쪽이 맞다.

## 2026-11-08 그래프 스파이크 첫 수집: Aura Free + haiku-4-5, 판정은 36개 후
- 상황: 이 Windows PC에 Docker가 없어 Neo4j는 **Aura Free**(`neo4j+s://`)로 돌렸다. 앞 5개 에피소드 수집 성공, 에피소드당 10.8~13.8초. 호칭 병합·관계 무효화·커스텀 속성·한국어 검색은 5개 규모에서 PASS (`graph/graph-spike-report.md`).
- 결정: D 진행/축소 판정은 36개 전체 수집 뒤로 미룬다. 5개에는 이서연 관계 변화 장면과 다른 인물이 없어 핵심 기준(5명 병합, 이서연 무효화)을 아직 못 봤다.
- 결정: 로컬 Neo4j 컨테이너 기본은 유지하되 Docker 없는 PC는 Aura Free를 공식 대안으로 둔다. Aura TLS 루트 문제는 certifi 번들 지정으로 코드에서 해결했다.
- 리스크: `valid_at` 시간대가 KST·UTC로 섞여 들어오고 "다음 주 수요일"이 날짜로 바뀌지 않는다 → 36개 수집 전 추출 지시에 시간대 규칙을 넣을지 결정. graphiti 0.31.2와 anthropic SDK 2.x 사이 호환 패치(`temperature` 제거)가 필요했으므로 graphiti 버전을 고정한다.

## 2026-11-08 그래프 스파이크 36개 수집 결과: 기전 OK, 판정은 2주차 C 비교로
- 결과: Aura Free + `claude-haiku-4-5`로 36/36 수집, 에피소드당 중앙값 11.1초. 인물 5명 병합 PASS, 관계 변화 무효화 8건, 커스텀 속성 28/37. `graph_queries.csv` Recall@5 평균 0.52 (q01 0.40, q02 1.00, q03 0.67, q04 0.00). 상세 `graph/graph-spike-report.md`.
- 결정: **D는 계획대로 진행**한다. 채택 조건(needs_graph=Y에서 C 대비 개선)은 2주차에 C(pgvector)가 생기면 같은 질의로 판정하고, 지금 수치를 기준선으로 둔다.
- 결정: 재수집 전에 `p0/schema.py` 추출 지시를 보강한다 — 한 쌍에 relation_type 하나(바뀌면 무효화), address_term은 "상대를 부르는 말"로 한정, participant_role 허용값, 취소·거절·불참도 사건으로, 시간은 KST 고정. 이서연 장면이 "무효화"가 아니라 "새 엣지 추가"로 나온 것과 q04=0(불참 사실 누락)이 근거.
- 결정: `graph_queries.csv`의 needs_graph=Y 질의를 2개에서 늘린다(시나리오 담당에게 장면 요청). 2개로는 D 판정이 흔들린다.

## 2026-11-08 추출 지시 보강 후 재수집: Recall@5 0.52 → 0.68
- 결과: 지시 보강(사물→사건, 엣지 이름 제한, 어미로 말투 판단, 직접 호칭만, 한 쌍 relation_type 하나, 취소·불참도 사건, KST 날짜 계산, participant_role Literal) + reference_time UTC 통일. 커스텀 속성 28/37 → 35/38, 무타입 엔티티 5 → 1, 이서연 장면의 옛 관계가 실제로 무효화됨, q04 0 → 0.67. 시간은 변화 없음(에피소드당 ~11초).
- 결정: **시각은 UTC로 저장하고 표시만 KST**. graphiti 프롬프트가 UTC를 전제하므로 그에 맞춘다.
- 결정: 4차 보강 3건을 다음 재수집 전에 한다 — relation_type에서 상하 관계를 `hierarchy` 필드로 분리(방향 혼동), 본문에 요일 표기(haiku 날짜 산술 오류), 답장 에피소드에 수신자 명시(자기참조 엣지).
- 유지: D 채택 판정은 2주차 C 비교로. 3차 수치가 D 기준선. q01(0.40)은 지시문으로 안 오르므로 D 검색기에 사건 노드 중심 전개를 넣어 푼다.

## 2026-11-08 4차 수집: 구조는 닫혔고, 날짜·말투·관계 유형은 지시문 대신 코드로
- 결과: `hierarchy` 분리 + 본문 `[날짜 요일 시각] 발화자 → 수신자:` 표기로 엣지 0개 에피소드 5 → 0, 자기참조 엣지 0, 속성 채움 43/44, 상하 방향 혼동 해소. 그러나 요일을 보여 줘도 haiku-4-5의 날짜 산술은 틀리고(다음 주 수요일 → 10-15, 정답 10-14), 말투·relation_type은 실행마다 흔들린다. Recall@5 0.68 → 0.60은 1회 측정이라 비결정성 범위.
- 결정: **그래프 스키마·지시문은 4차 상태로 고정**한다. 더 바꾸지 않는다.
- 결정: 상대 날짜는 코드의 규칙 기반 해석기로 본문에 미리 풀어 준다. 쌍별 relation_type·speech_style은 추출값 다수결 또는 `characters.csv` 등록값 우선 후처리로 고정하고, 그래프 말투는 참고값으로만 쓴다(설계 원칙 유지).
- 결정: 회차 비교 전에 같은 설정 2~3회 반복으로 Recall@5 분산을 잰다. D 채택 판정은 2주차 C 비교로(변경 없음).

## 2026-11-08 5차 수집: 상대 날짜는 코드 해석기로 확정
- 결과: `p0/dates.py`가 '다음 주 수요일(=2026-10-14)'처럼 미리 계산해 주자 명확한 표현 17/17이 정확해졌다(4차는 거의 전부 오답). 수식어 없는 요일은 두 후보를 적어 줬는데 모델이 문맥으로 고르지 못해 7건 중 1건만 정답. Recall@5 0.60으로 4차와 같음.
- 결정: 날짜 산술은 LLM에 맡기지 않는다. 그래프 수집뿐 아니라 첨삭(보존 항목 검증)에서도 같은 해석기를 재사용할 후보로 둔다.
- 결정: 수식어 없는 요일의 담화 선택(앞 메시지의 '다음 주 수요일'을 이어받는 '수요일')은 해석기에 대화방 단위 규칙으로 넣는다. 지시문은 고정 유지.

## 2026-11-08 6차 수집: 담화 규칙으로 날짜 24/25, 스파이크 종료
- 결과: `DiscourseContext`(같은 대화방 앞 메시지가 특정한 요일 날짜를 이어받기)로 "수요일로 조정" → 10-14가 맞게 들어갔다. 날짜 든 사실 25건 중 24건 정확. Recall@5 0.77(q04 1.00, 1회 측정).
- 결정: **1주차 그래프 스파이크는 여기서 종료**한다. 기전 검증(병합·무효화·속성·날짜·한국어 검색)은 끝났고, 남은 품질 문제(말투·관계 유형 일관성, q01 갈등 당일 메시지 미검색)는 후처리와 D 검색기 설계로 넘긴다.
- 결정: `p0/dates.py`는 그래프 전용이 아니다. 2주차에 `backend/app/ai/preserve.py`(보존 항목 날짜 정규화)에서 재사용을 검토한다.
- 유지: D 채택 판정은 2주차 C 비교로. 비교 전 같은 설정 2~3회 반복으로 분산 측정.

## 2026-11-08 Docker compose 검증 완료: 2주차 DB는 로컬 컨테이너로
- 결과: Docker Desktop 4.98 + WSL 3.0.3로 `docker compose up -d postgres neo4j` 성공. PostgreSQL 16.15 + pgvector 0.8.7, Neo4j 5.26.32 + APOC, 호스트에서 bolt·5432 접속 확인. 선행 조건은 BIOS 가상화(SVM)·VirtualMachinePlatform·WSL 설치였다(`docker/README.md`).
- 결정: 2주차 C 경로(pgvector)와 3주차 D 연동은 로컬 컨테이너를 쓴다. Aura Free는 1주차 스파이크 결과 보관용으로 남기고 `.env`의 `NEO4J_URI`는 당장 바꾸지 않는다(스파이크 반복 측정 때 로컬로 전환).
- 결정: backend 컨테이너 이미지는 당분간 빌드하지 않는다. `db` extra의 sentence-transformers가 torch를 끌어와 이미지가 수 GB라, 팀원 온보딩 전까지는 `make dev`(호스트 venv)로 백엔드를 띄운다.

## 2026-11-08 저장소를 PostgreSQL로 교체: 비동기 Protocol, 결과는 JSONB
- 결과: `repositories/base.py` Protocol 아래 `postgres.py`(SQLAlchemy 2 + asyncpg)와 `memory.py` 두 구현. Alembic 0001로 contacts·persona_versions·rewrite_requests·rewrite_results·feedback 생성. 통합 테스트 4개가 같은 서버의 `persona_ize_test` DB에서 돌고(39 passed), 접속이 없으면 건너뛴다.
- 결정: 저장소 인터페이스는 **비동기**로 통일한다(asyncpg가 기준, 인메모리도 같은 모양). 라우트는 전부 `async def`.
- 결정: `RewriteResult`는 `rewrite_results.payload` JSONB에 통째로 저장하고 집계용 열(result_text·validation_status·model_version·prompt_version·retrieval_version·token_usage·latency_ms)만 따로 둔다. 설계서 §6의 `rewrite_evidence`·`users`·`import_jobs`는 필요해지는 주에 추가.
- 결정: 소유권은 모든 쿼리의 WHERE에 user_id를 함께 걸어 "없음"과 "남의 것"을 같은 404로 만든다. 동시성 충돌(페르소나 버전, Idempotency-Key)은 유일 제약 → 409로 번역.

## 2026-11-08 C 경로(업로드·화자 지정·pgvector 검색) 구현과 묶음 규칙
- 결과: `POST /v1/conversations/{id}/imports`(speaker_map으로 화자 지정, 해시로 재업로드 멱등, 겹친 메시지 id는 건너뛰고 수 보고) → 묶음 → e5-small 임베딩 → `episodes`(vector 384). `VectorStyleRetriever`가 같은 상대·as_of 이전 묶음을 kNN으로 고른다. 합성 36건 → 묶음 18개, 검색 25ms 안팎. `run_eval.py --conditions A,B,C`가 recall 열을 채운다(E002 1.0, E004 1.0, E005 0.5, fake 모델 기준이라 검색만 평가).
- 결정: **묶음 = 본인 발화 연속 구간 1개 + 그 앞 메시지 최대 window-1(=3)개.** 임베딩은 묶음 전문(상대/나 표기), 문체 예시 본문은 본인 발화만(상대 말투 혼입 방지), 출처 id는 묶음 전체(맥락 메시지도 recall에 인정). 설계서의 "2~6턴 묶음"을 이렇게 고정한다.
- 결정: 검색 기준 시각은 사례 `as_of` 날짜의 당일 끝(KST). 평가에서 미래 대화가 새지 않게 `RewriteInput.as_of`를 추가했다. API 요청은 지금 시각.
- 결정: 가져오기는 요청 안에서 동기로 끝내되 응답은 설계대로 202 + job. 비동기 작업 큐는 데이터가 커질 때. D 조건은 3주차 그래프 맥락을 붙이기 전까지 C와 같으므로 지금 D 수치는 비교에 쓰지 않는다.
