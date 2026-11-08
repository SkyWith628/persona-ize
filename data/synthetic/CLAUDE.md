# data/synthetic/ — 사용자·시나리오 담당 폴더

이 폴더에서 Claude와 일할 때 지킬 것. 전체 규칙은 저장소 루트 `CLAUDE.md`, 역할 설명은 `docs/roles/scenario.md`, 형식은 이 폴더의 `README.md`.

- 여기 JSON은 서비스가 읽는 **가짜 과거 대화**다. 외부 서비스(Claude API, Neo4j)로 전송되므로 **실제 대화·실존 인물은 절대 넣지 않는다.**
- 형식: `conversations[].messages[]`의 `id`(방 안에서 유일, m01…), `speaker`(`me`/`contact`), `sent_at`(시간대 포함 ISO, `+09:00`), `text`. 대화방 `conversation_id`는 c1, c2… 순서. `contact`는 `evals/characters.csv`의 이름과 같아야 한다.
- 시트/CSV로 쓴 것을 JSON으로 바꿀 때는 팀장의 `scripts/csv_to_synthetic.py`(준비 중)를 쓴다. 손으로 바꾸면 `sent_at` 시간대와 따옴표를 틀리기 쉽다.
- 저장 뒤 `python scripts/validate_data_files.py`로 검사한다(중복 id, 시간대 누락, 모르는 인물을 잡는다).
- 좋은 대화의 조건: 상대마다 말투가 확 다르고, 날짜·약속·부탁·사과가 자연스럽게 들어가며, **한 사건이 여러 방에 흩어진 장면**이 있다(그래프 실험의 재료). 그런 장면을 만들면 그래프 담당에게 메시지 id를 알린다.
- 메시지를 가리킬 때는 `대화방id:메시지id`(`c1:m03`). 다른 파일(`evals/eval_cases.csv`, `graph/graph_queries.csv`)이 이 형식으로 참조한다 — 기존 id를 바꾸면 그 파일들이 깨진다.
