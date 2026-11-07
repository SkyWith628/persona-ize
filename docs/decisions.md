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
