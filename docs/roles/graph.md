# 지식그래프 담당 플레이북 (2026-11-09)

**한 문장**: 대화에서 뽑은 **사람·사건·관계의 시간 그래프**가 제대로 서는지 보고, 그래프가 있어야만 풀리는 질의를 모아 "D가 C보다 나은가"를 판정할 근거를 만든다.

현황: 1주차 스파이크(`graph/graph-spike-report.md`)로 병합·무효화·날짜 해석·검색은 검증됐고, 11/08~09에 백엔드에 연동됐다(`backend/app/graph/`). 그래프는 **사용자별**로 Neo4j에 들어가며, 화면에서 대화를 가져오면 자동으로 수집된다. 이제 필요한 것은 **질의와 정답**, 그리고 **추출 품질 점검**이다.

## 내가 만드는 파일

| 파일 | 내용 | 검사 |
|---|---|---|
| `graph/graph_queries.csv` | 질의(사용자가 첨삭할 때 필요한 과거 사실) + 정답 메시지 id + `needs_graph` | `python scripts/validate_data_files.py` |
| `graph/alias_cases.csv` | 호칭 병합 사례(같은 사람이 다르게 불린 경우)와 기대 결과 | 같은 스크립트 |
| `graph/graph_schema.md` | 엔티티·관계 스키마 v1 — **11/10 확정** | 팀장 검토 |
| `graph/inspection-YYYY-MM-DD.md` | 그래프 점검 기록(Neo4j Browser에서 본 것: 병합 실패·엉뚱한 사실·빠진 사건) | — |

## 매주 할 일

| 주 | 할 일 | 완료 기준 |
|---|---|---|
| 11/10~15 | **스키마 v1 확정**(relation_type 5종 + hierarchy, participant_role 닫힌 집합). 시나리오 담당의 흩어진 장면 5개를 **질의 5개 이상**으로 `graph_queries.csv`에 추가(`needs_graph=Y`), 정답 메시지 id까지 | PR 병합. 팀장이 그 질의로 D recall을 잴 수 있음 |
| 11/16~22 | 합성 60건 재수집 뒤 **점검 기록 1차**: 인물 병합(5/5인가), 관계 변화 무효화(동기→파트장), 엉뚱한 사실(예: "형 또 그랬냐고 묻는 관계") 목록, 사건 `importance` 누락 비율 | `graph/inspection-11-2x.md`. 시연에서 Neo4j Browser 화면 1장 |
| 11/23~29 | C vs D 비교표에서 **D가 이긴/진 사례** 분석 — 그래프 사실이 어떻게 쓰였는지(결과 jsonl의 `retrieved` context) | 분석 메모 |
| 11/30~12/6 | 추출 지시문·스키마 개선 제안(팀장이 코드 반영), 재수집·재측정 | decisions에 결과 |
| 12/7~13 | 한계 정리(어떤 사건이 안 잡히나, 비용) | 최종 보고서의 그래프 절 |
| 12/14~20 | 발표의 "지식그래프" 부분 + 논문 1편 발표(Graphiti/Zep) | 슬라이드 초안 |

## 어떻게 하나

1. **질의 쓰기**: "첨삭하려는 순간 과거의 무엇을 알아야 하나"를 한 문장으로. 예) `팀장님과 일정 조율에서 갈등이 있었던 일`. 정답은 그 사실이 들어 있는 메시지 id들(`c1:m03;c1:m04`). **여러 방·여러 날에 흩어진** 것이 `needs_graph=Y`, 인접 메시지 2개로 충분하면 `N`. 둘 다 있어야 D의 이득을 잴 수 있다.
2. **그래프 보기**: `make up` 뒤 http://localhost:7474 (계정은 `.env`의 NEO4J_USERNAME/PASSWORD). Cypher는 [docs/api.md](../api.md) 맨 아래 — `group_id`는 사용자 id(시연 계정은 `scripts/prepare_demo.py`가 출력). 보는 순서: Entity 이름 목록 → 사람별 RELATES_TO 사실 → Episodic(원문) 시간 순.
   - 점검 질문: 같은 사람이 둘로 서 있나? 바뀐 관계의 옛 사실에 `invalid_at`이 찍혔나? 사실 문장이 원문과 맞나? Event의 `event_type`·`importance`가 채워졌나(재정렬 가중치에 쓴다).
3. **재수집**: 데이터가 바뀌면 팀장이 `scripts/prepare_demo.py --graph` 또는 `import_synthetic.py --graph`로 다시 넣는다(메시지당 10~15초, haiku 비용). 당신은 "언제, 어느 데이터셋으로" 요청하고 결과를 점검한다.
4. **올리기**: GitHub Desktop → 브랜치 `graph/…` → PR.

## 반드시 지킬 것
- 합성 데이터만. 그래프는 Neo4j(지금은 로컬 컨테이너, 1주차 데이터는 Aura)에 간다.
- 스키마 열 이름(`relation_type` 값 등)을 바꾸면 백엔드 `schema.py`와 평가 코드가 같이 바뀌어야 한다 → 바꾸기 전에 PR로 제안.
- Cypher로 **쓰기(DELETE·SET)는 하지 않는다.** 읽기만. 지울 때는 팀장에게.

## 팀장에게 요청할 때
- 새 질의 추가 뒤 "D recall 재측정 부탁" — `run_eval.py --conditions C,D`로 recall 열을 받는다.
- 추출이 이상한 사례 → `graph/inspection-*.md`에 (원문 메시지 id, 그래프에 들어간 사실, 기대) 세 줄로 적고 PR. 지시문(`schema.py EXTRACTION_INSTRUCTIONS`) 수정은 팀장이.
