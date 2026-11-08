"""D 경로: Graphiti(Neo4j) 시간 지식 그래프. 1주차 스파이크 `graph/graphiti_p0`를 백엔드로 옮긴 것.

- `schema.py`, `dates.py`: 스파이크 6차와 같은 내용 (스파이크 폴더는 참고용으로 동결).
- `client.py`: Settings → Graphiti (Claude haiku-4-5, 로컬 임베더, 패스스루 리랭커).
- `store.py`: 그래프 접근 Protocol. 테스트는 가짜 구현을 꽂는다.
- `episodes.py`, `ingest.py`: 메시지 → 에피소드 본문 → add_episode (사용자별 group_id).
- `retriever.py`: 사건·관계 사실을 `purpose=context`로 돌려주는 검색기. D = C(문체) + 이것.
"""
