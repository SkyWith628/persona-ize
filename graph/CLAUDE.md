# graph/ — 지식그래프 담당 폴더

이 폴더에서 Claude와 일할 때 지킬 것. 전체 규칙은 저장소 루트 `CLAUDE.md`, 역할 설명은 `docs/roles/graph.md`.

- 데이터·문서: `graph_queries.csv`(질의·정답 메시지 id·needs_graph), `alias_cases.csv`(호칭 병합 사례), `graph_schema.md`(스키마 v1), `graph-spike-report.md`(1주차 스파이크 보고). 검사: `python scripts/validate_data_files.py`.
- `graphiti_p0/`는 **동결된 스파이크**다. 정본 코드는 `backend/app/graph/`(schema·dates·ingest·retriever). 스파이크 스크립트를 고치지 말고, 추출 지시문·스키마 변경은 `backend/app/graph/schema.py`에 대한 제안으로 적는다(코드 반영은 팀장).
- 그래프 보기: Neo4j Browser http://localhost:7474. Cypher 예시는 `docs/api.md` 끝. **읽기만** 한다 — DELETE·SET·MERGE는 쓰지 않는다. `group_id`는 사용자 id다.
- 재수집은 팀장이 `scripts/prepare_demo.py --graph` 또는 `scripts/import_synthetic.py --graph`로 한다(haiku 호출, 메시지당 10~15초). 데이터셋이 바뀌면 재수집을 요청한다.
- 질의는 "첨삭 순간에 알아야 할 과거 사실" 한 문장 + 그 사실이 든 메시지 id들. 여러 방·여러 날에 흩어진 것만 `needs_graph=Y`.
- 점검 기록은 `graph/inspection-YYYY-MM-DD.md`에 (메시지 id, 그래프에 들어간 사실, 기대) 세 줄 단위로 적는다.
