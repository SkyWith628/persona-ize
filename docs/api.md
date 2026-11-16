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

### 회원가입·상태·소셜 로그인 (11-16)

| 요청 | 설명 |
|---|---|
| `GET /v1/auth/providers` | 로그인 없이. `{signup_open, requires_approval, google, kakao}` — google·kakao는 서버에 클라이언트 설정이 있을 때만 true |
| `POST /v1/auth/signup` `{email, password, display_name?}` | 이메일 가입 201(세션 쿠키 + `csrf_token`). 아이디는 이메일 앞부분에서 만든다. 중복 409 `conflict`, 닫힘 403 `signup_closed`, 형식·길이 422 |
| `POST /v1/auth/register` `{username, password}` | 아이디 가입(하위 호환). 가입 정책은 같다. `ADMIN_USERNAMES`의 이름은 409 |
| `POST /v1/auth/login` `{username, password}` | `username`에 아이디 **또는 이메일**. 없는 계정·틀린 비밀번호는 같은 401, 정지 계정은 403 `account_suspended` |
| `GET /v1/auth/oauth/{google\|kakao}/start?next=/경로` | 302 → 제공자 로그인. `next`는 `/`로 시작하는 같은 출처 경로만(아니면 `/`). 설정 안 된 제공자는 404 |
| `GET /v1/auth/oauth/{provider}/callback?code&state` | 성공: 세션 쿠키 + 302 → `next`. 실패: 302 → `<next>?auth_error=<code>`(상태 쿠키가 없으면 `/?auth_error=`). code: `oauth_denied` `oauth_failed` `signup_closed` `account_suspended` |

- 응답 `AuthOut` = `{user_id, username, csrf_token, expires_at, email, display_name, role, status, is_admin}`, `GET /v1/auth/me`는 `expires_at` 대신 `providers`(`password`·`google`·`kakao`).
- 상태: `active` / `pending`(승인 대기 — 로그인과 `/v1/auth/*`만 되고 그 밖은 403 `account_pending`) / `suspended`(로그인 403, 정지 때 세션 전부 폐기).
- 정책: `AUTH_ALLOW_REGISTER=false`면 아이디·이메일·소셜 신규 가입 모두 403 `signup_closed`(기존 계정의 소셜 로그인은 된다). `AUTH_SIGNUP_REQUIRES_APPROVAL`(기본 true)이면 새 계정은 pending.
- 소셜 계정 연결: 이미 연결된 계정 → 그 사용자. 아니면 제공자가 **확인한** 이메일이 같은 기존 계정에 연결. 아니면 새 계정(확인 안 된 이메일은 연결도 저장도 하지 않는다). 카카오는 이메일 동의 항목이 없으면 이메일 없는 계정.
- 소셜 전용 계정(비밀번호 없음)의 `DELETE /v1/auth/me`는 `{"password": ""}`.
- 설정: `GOOGLE_CLIENT_ID`·`GOOGLE_CLIENT_SECRET`, `KAKAO_CLIENT_ID`(REST API 키)·`KAKAO_CLIENT_SECRET`(선택), `PUBLIC_BASE_URL`. 제공자 콘솔에 등록할 Redirect URI는 `{PUBLIC_BASE_URL}/v1/auth/oauth/google/callback`, `.../kakao/callback`(로컬은 `http://localhost:8000/...`).
- 계정 도구: `python -m app.tools.create_user <아이디> [--admin] [--reset-password]` — 바로 active, `--admin`은 role=admin.

## 상대·페르소나

| 메서드·경로 | 내용 |
|---|---|
| `POST /v1/contacts` `{alias, relationship_type}` | 상대 생성. relationship_type: superior·colleague·subordinate·friend·family·partner·acquaintance |
| `GET /v1/contacts`, `GET /v1/contacts/{id}` | 목록·조회 (`persona_version` 포함) |
| `PUT /v1/contacts/{id}/persona` `{settings, expected_version}` | 새 페르소나 버전. 현재 버전과 다르면 409 |
| `GET /v1/contacts/{id}/persona` | 최신 버전 |
| `GET /v1/contacts/{id}/graph` | 상대 주변 맥락 그래프 `{enabled, center_id, nodes[{id,name,kind,summary,event_type,importance,is_contact}], edges[{id,source,target,relation,fact,valid_at,invalid_at,current}]}`. 상대 노드와 그 상대가 관여한 사건에 닿은 사실만(D 검색 범위와 같음), 유효한 사실 먼저·최근 순. 그래프가 꺼져 있으면 `enabled=false` |
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

## 운영 (관리자)

| 요청 | 설명 |
|---|---|
| `GET /v1/admin/overview?days=7` | 운영 대시보드 집계(1~90일). 추천 성공률, 원문 유지 사유, 응답 시간 p50·p95, 피드백 수락률, 토큰, 조건별 건수, 최근 거절(가린 사용자·사유 입력 여부만), 오늘 한도 도달 사용자, 그래프 대기·실패, `users: {total, pending, suspended, new_in_range}`. 화면은 `/admin` |
| `GET /v1/admin/users?query=&status=&role=&limit=50&offset=0` | 회원 목록(최신 가입순). `query`는 아이디·이메일·표시 이름 부분 일치. `{total, items: [AdminUser]}` — `total`은 조건에 맞는 전체 수 |
| `PATCH /v1/admin/users/{user_id}` `{status?: active\|suspended, role?: user\|admin}` | AdminUser. pending → active가 승인. 정지하면 그 사용자의 세션 전부 폐기. 자기 자신의 정지·권한 내리기·빈 본문은 400 `invalid_input` |
| `POST /v1/admin/users/{user_id}/revoke-sessions` | 강제 로그아웃. `{revoked}` |
| `DELETE /v1/admin/users/{user_id}` | 계정과 모든 데이터 삭제(`DELETE /v1/auth/me`와 같은 응답). 자기 자신은 400 |
| `GET /v1/admin/audit?limit=100` | 관리자 작업 기록(최근 순). `{items: [{id, at, admin_username, action, target_username, detail}]}` — action: `user.approve` `user.suspend` `user.activate` `user.role` `user.revoke_sessions` `user.delete` `users.view` |

- 권한: `role == admin`이거나 아이디가 `ADMIN_USERNAMES`에 있는 세션만. 아니면 403 `forbidden`, 로그인 전·헤더 스텁은 401. 쓰기 요청은 `X-CSRF-Token` 필수.
- `AdminUser` = `{user_id, username, email, display_name, role, status, is_admin, providers[], created_at, last_login_at, rewrites_today, contacts}`. 대화 원문·첨삭 문장·피드백 사유 글은 없다.
- 기록: 상태·권한 변경, 세션 폐기, 삭제는 항상 남는다. `users.view`는 목록 첫 쪽(`offset=0`, `limit>1`) 조회만 — 화면이 대기 수를 세는 `limit=1` 조회는 남기지 않는다.

화면 경로: `/` 모바일 웹 · `/manage` 실험용 관리 화면 · `/admin` 운영 대시보드. 배포는 `docs/deploy.md`.

## 오류 모양

`{"code": "...", "message": "..."}` — 400 invalid_input(관리자 자기 보호), 401 unauthorized, 403 forbidden(CSRF·권한)·signup_closed·account_pending·account_suspended, 404 not_found(남의 것도 404), 409 conflict, 422 invalid_input, 429 usage_exceeded·too_many_attempts, 503 model_unavailable.

## 그래프 담당용 Cypher (Neo4j Browser http://localhost:7474)

```cypher
MATCH (n:Entity {group_id: "demo"}) RETURN n.name, labels(n) ORDER BY n.name;
MATCH (a:Entity {group_id: "demo"})-[r:RELATES_TO]->(b:Entity) RETURN a.name, r.name, r.fact, r.valid_at, r.invalid_at, b.name LIMIT 50;
MATCH (e:Episodic {group_id: "demo"}) RETURN e.name, e.valid_at, left(e.content, 80) ORDER BY e.valid_at;
```
`group_id`는 사용자 id다. 합성 대화를 넣은 사용자는 `scripts/import_synthetic.py --user demo --graph`의 `demo`.
