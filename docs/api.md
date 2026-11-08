# API 안내 (팀원용, 2026-11-09 기준)

서버: `make dev` → http://localhost:8000 (화면 `/`, Swagger `/docs`). 모든 경로는 `/v1/`로 시작한다.
자동 전송은 없다 — 추천 결과는 사용자가 직접 복사해 보낸다 (project-design §3).

## 인증

| 방법 | 언제 |
|---|---|
| **세션 쿠키** `pi_session` (HttpOnly) + 쓰기 요청의 `X-CSRF-Token` 헤더 | 화면·실제 사용 |
| `X-User-Id: <아무 문자열>` 헤더 | 개발·테스트·평가 스크립트. `.env`의 `AUTH_ALLOW_HEADER_STUB=false`면 거부 |

```bash
curl -s -c cookie.txt -X POST localhost:8000/v1/auth/register -H "Content-Type: application/json" -d '{"username":"minsu","password":"long-enough-pw"}'
```
응답의 `csrf_token`을 쓰기 요청에 `-H "X-CSRF-Token: ..."`로 보낸다. `POST /v1/auth/login`, `POST /v1/auth/logout`, `GET /v1/auth/me`(CSRF 토큰 재조회).
`POST /v1/auth/password` `{current_password, new_password}`(CSRF 필수) — 바꾸면 다른 세션은 모두 로그아웃. 로그인 실패가 이름·IP별 5회(15분)에 닿으면 429 `too_many_attempts`.
`DELETE /v1/auth/me` `{password}`(CSRF 필수) — **계정 삭제**. 상대·대화·묶음·첨삭 이력·그래프 group·사용량·세션을 모두 지우고 쿠키를 지운다. 되돌릴 수 없다. 응답 `{contacts, conversations, messages, graph_nodes_removed, graph_error}`.

## 상대·페르소나

| 메서드·경로 | 내용 |
|---|---|
| `POST /v1/contacts` `{alias, relationship_type}` | 상대 생성. relationship_type: superior·colleague·subordinate·friend·family·partner·acquaintance |
| `GET /v1/contacts`, `GET /v1/contacts/{id}` | 목록·조회 (`persona_version` 포함) |
| `PUT /v1/contacts/{id}/persona` `{settings, expected_version}` | 새 페르소나 버전. 현재 버전과 다르면 409 |
| `GET /v1/contacts/{id}/persona` | 최신 버전 |
| `DELETE /v1/contacts/{id}` | **상대 삭제**. 그 상대의 대화방·메시지·묶음·그래프 에피소드·페르소나·첨삭 이력을 모두 지운다. 응답 `{conversations, messages, episodes, graph_episodes, rewrites, graph_errors}` (그래프 삭제가 실패해도 DB는 지우고 사유를 돌려준다) |

settings: `formality·friendliness·directness`(1~5), `length`(shorter/same/longer), `emoji`(none/rare/often), `address_term`, `preferred_phrases[]`, `forbidden_phrases[]`, `notes`.

## 과거 대화 (C·D 검색 재료)

| 메서드·경로 | 내용 |
|---|---|
| `POST /v1/conversations` `{contact_id, title?, source_key?, me_name?}` | 대화방. `source_key`는 데이터셋의 방 id(c1), 사용자 안에서 유일. `me_name`은 그래프 수집에 쓰는 내 이름 |
| `GET /v1/conversations?contact_id=` | 목록 |
| `POST /v1/conversations/{id}/imports` `{messages[], speaker_map?}` | 메시지 가져오기. 응답 202 + 작업. 문체 묶음은 즉시, **그래프 수집은 백그라운드**(`graph_status` pending→completed, 메시지당 10~15초) |
| `GET /v1/imports/{job_id}` | 작업 상태 |
| `GET /v1/conversations/{id}/messages` | 메시지 목록 |
| `DELETE /v1/conversations/{id}` | 원문·묶음·그래프 에피소드 삭제. 이후 검색에 나오지 않는다 |

메시지: `{"id":"m01","speaker":"김민수","sent_at":"2026-10-05T09:31:00+09:00","text":"..."}`. `speaker`가 `me`/`contact`가 아니면 `speaker_map`으로 지정한다 (`{"김민수":"me","박지현":"contact"}`). 같은 내용을 다시 올리면 같은 작업을 돌려주고, 겹친 메시지 id는 건너뛴다.

## 첨삭

| 메서드·경로 | 내용 |
|---|---|
| `POST /v1/rewrites` `{contact_id, draft_text, situation?, condition?, persona_version?}` + `Idempotency-Key` 헤더 | 추천 생성. condition A/B/C/D (기본 B) |
| `GET /v1/rewrites?contact_id=&limit=` | 첨삭 이력 (최근 순, 최대 100) |
| `GET /v1/rewrites/{id}` | 결과 조회 |
| `POST /v1/rewrites/{id}/feedback` `{accepted, edited_text?, reason?, consent_to_reuse?}` | 적용·수정·거절 피드백 |
| `GET /v1/usage` | 오늘(KST) 요청 수·토큰과 한도. 넘으면 첨삭이 429 `usage_exceeded` |

결과(`result`): `status`(recommended / fallback_original), `result_text`, `change_reasons[]`, `warnings[]`(모델이 적은 주의점 — 화면에 보여 준다), `evidence_ids[]`(참고한 과거 표현 id), `preserved_items[]`, `validation`, `attempt_log[]`(시도별 위반), `retrieval_version`, `token_usage`, `latency_ms`.

## 오류 모양

`{"code": "...", "message": "..."}` — 401 unauthorized, 403 forbidden(CSRF), 404 not_found(남의 것도 404), 409 conflict, 422 invalid_input, 429 usage_exceeded, 503 model_unavailable.

## 그래프 담당용 Cypher (Neo4j Browser http://localhost:7474)

```cypher
MATCH (n:Entity {group_id: "demo"}) RETURN n.name, labels(n) ORDER BY n.name;
MATCH (a:Entity {group_id: "demo"})-[r:RELATES_TO]->(b:Entity) RETURN a.name, r.name, r.fact, r.valid_at, r.invalid_at, b.name LIMIT 50;
MATCH (e:Episodic {group_id: "demo"}) RETURN e.name, e.valid_at, left(e.content, 80) ORDER BY e.valid_at;
```
`group_id`는 사용자 id다. 합성 대화를 넣은 사용자는 `scripts/import_synthetic.py --user demo --graph`의 `demo`.
