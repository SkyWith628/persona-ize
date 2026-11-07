# graph/ — 지식그래프(D 경로) 담당 폴더

담당: 지식그래프 팀원. 접점 파일을 채워 PR을 올리면 팀장이 코드에 연결한다.

| 파일 | 내용 | 쓰는 곳 |
|---|---|---|
| [graph_schema.md](graph_schema.md) | 엔티티·관계 스키마와 속성 정의, 추출 규칙 | `graphiti_p0/p0/schema.py` 갱신 근거 |
| [alias_cases.csv](alias_cases.csv) | 호칭 병합 정답 (같은 사람을 가리키는 표현들) | 병합 정확도 측정 |
| [graph_queries.csv](graph_queries.csv) | 사건·관계 질의와 관련 과거 메시지 정답 | Recall@5, D 채택 조건 판정 |
| `graph-spike-report.md` | 스파이크 점검 보고 (병합·무효화·비용) | 1주차 말 D 판정 |
| [graphiti_p0/](graphiti_p0/README.md) | Graphiti 스파이크 코드 | 팀장 |

## D 채택 조건 (team-roles-and-schedule.md)
"여러 메시지에 흩어진 같은 사건을 연결해야 하는 사례"에서 C(벡터 검색) 대비 개선이 있어야 한다.
그런 사례는 `graph_queries.csv`의 `needs_graph` 열을 `Y`로 표시한다. 시나리오 담당에게 그런 장면을 합성 대화에 넣어 달라고 요청한다.
