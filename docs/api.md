# API 안내 (2026-11-11 기준)

서버: `make dev`(또는 `make up-all`) → http://localhost:8000. 화면 `/`(PC 웹 앱), `/admin`(운영 대시보드), `/privacy`·`/terms`(안내 문서), `/static/proto.html`(시연 프로토타입). Swagger `/docs`는 개발 모드에서만 열린다(`APP_ENV=production`이면 404).
모든 API 경로는 `/v1/`로 시작하고 JSON을 주고받는다. 자동 전송은 없다 — 추천 결과는 사용자가 직접 복사해 보낸다 (project-design §3).
배포는 [deploy.md](deploy.md).

## 인증

| 방법 | 언제 |
|---|---|
| **세션 쿠키** `pi_session` (HttpOnly, 운영은 Secure) + 쓰기 요청의 `X-CSRF-Token` 헤더 | 브라우저 화면. 아이폰 앱(`apps/ios`)도 이 방식을 쓴다 |
| **`Authorization: Bearer <토큰>`** | 모바일 키보드·PC 프로그램 같은 비브라우저 클라이언트. `POST /v1/auth/token`으로 받는다. CSRF 헤더가 필요 없다. 쿠키와 함께 오면 Bearer가 우선하고, 틀리면 쿠키로 넘어가지 않고 401 |
| `X-User-Id: <아무 문자열>` 헤더 | 개발·테스트·평가 스크립트 전용. `AUTH_ALLOW_HEADER_STUB=false`면 거부(운영은 항상 거부). 관리자 API는 이 방식으로 열리지 않는다 |

| 메서드·경로 | 내용 |
|---|---|
| `GET /v1/auth/config` (인증 불필요) | 가입 화면이 쓸 설정: `{allow_register, invite_required, require_consent, consent_version, app_env}` |
| `POST /v1/auth/register` `{username, password, invite_code?, consent?}` | 가입 + 로그인. 아이디는 영문·숫자·`_`·`-` 3~32자, 대소문자만 다른 이름은 409. 비밀번호 8자 이상. 초대 코드가 틀리면 403 `invalid_invite`, 동의가 필요한데 없으면 422 `consent_required`. 운영 모드에서 관리자 이름은 화면 가입 불가(409) |
| `POST /v1/auth/login` `{username, password}` | 로그인. 같은 곳(IP)에서 같은 아이디를 계속 틀리면 그 조합만 429 `too_many_attempts` + `Retry-After`(IP 단독·아이디 단독은 훨씬 높은 횟수에서) |
| `POST /v1/auth/token` `{username, password, client?}` | Bearer 토큰 발급: `{access_token, token_type: "bearer", expires_at, user_id, username}`. `client`는 `mobile`·`desktop`·`api` |
| `POST /v1/auth/logout` | 쿠키 세션과 Bearer 토큰을 폐기 |
| `GET /v1/auth/me` | 내 정보 + CSRF 토큰 재조회 |
| `POST /v1/auth/consent` `{}` (CSRF) | 현재 `CONSENT_VERSION`으로 동의 기록 |
| `GET /v1/auth/sessions` | 로그인된 기기 목록 `[{id, created_at, expires_at, current}]` |
| `POST /v1/auth/logout-all` (CSRF) | 지금 세션만 남기고 전부 로그아웃 → `{revoked_sessions}` |
| `POST /v1/auth/password` `{current_password, new_password}` (CSRF) | 비밀번호 변경. 다른 세션은 모두 로그아웃 |
| `DELETE /v1/auth/me` `{password}` (CSRF) | **계정 삭제**. 상대·대화·묶음·첨삭 이력·그래프·사용량·세션을 모두 지운다. 되돌릴 수 없다 |

로그인·가입·`me` 응답: `{user_id, username, csrf_token, expires_at, is_admin, consented_at, consent_version, consent_current}`. `is_admin`은 아이디가 `ADMIN_USERNAMES`와 대소문자까지 같을 때만 true. `consent_current`는 기록된 동의 버전이 지금 버전과 같은지.
비밀번호를 잊은 사용자는 운영자가 서버에서 재설정한다(이메일이 없어 자가 재설정이 없다): `scripts/reset_password.py --username <이름>` (`--create`면 계정이 없을 때 만든다 — 운영에서 관리자 계정을 만드는 길).

```bash
curl -s -c cookie.txt -X POST localhost:8000/v1/auth/register -H "Content-Type: application/json" -d '{"username":"minsu","password":"long-enough-pw","consent":true}'
```
응답의 `csrf_token`을 쓰기 요청에 `-H "X-CSRF-Token: ..."`로 보낸다. (Windows Git Bash의 curl은 한글 JSON 본문을 CP949로 보내 400이 난다 — 한글이 든 본문은 `--data-binary @파일`로.)

## 상대·페르소나

| 메서드·경로 | 내용 |
|---|---|
| `POST /v1/contacts` `{alias, relationship_type, use_default_persona?}` | 상대 생성. relationship_type: superior·colleague·subordinate·friend·family·partner·acquaintance. `use_default_persona: true`면 관계 유형 기본값으로 페르소나 v1을 함께 만든다 |
| `GET /v1/contacts`, `GET /v1/contacts/{id}` | 목록·조회 (`persona_version` 포함) |
| `PATCH /v1/contacts/{id}` `{alias?, relationship_type?}` | 이름·관계 수정 |
| `PUT /v1/contacts/{id}/persona` `{settings, expected_version}` | 새 페르소나 버전. 현재 버전과 다르면 409 |
| `GET /v1/contacts/{id}/persona` | 최신 버전 |
| `GET /v1/contacts/{id}/persona/versions` | 버전 기록 (최신 먼저, 최대 50) |
| `GET /v1/contacts/{id}/persona/suggestion` | 이 상대에게 보낸 **내 과거 메시지**로 추정한 말투 제안: `{based_on_messages, settings|null, signals: [{field, value, reason}]}`. 규칙 기반(모델 호출 없음), 저장하지 않는다 — 화면에서 사용자가 확인하고 저장한다. 내 메시지가 5건 미만이면 `settings: null` |
| `GET /v1/persona-defaults` | 관계 유형별 기본 페르소나 7종 |
| `DELETE /v1/contacts/{id}` | **상대 삭제**. 그 상대의 대화방·메시지·묶음·그래프 에피소드·페르소나·첨삭 이력을 모두 지운다 (그래프 삭제가 실패해도 DB는 지우고 사유를 돌려준다) |

settings: `formality·friendliness·directness`(1~5), `length`(shorter/same/longer), `emoji`(none/rare/often), `address_term`, `preferred_phrases[]`, `forbidden_phrases[]`, `notes`.

## 과거 대화 가져오기 (문체 검색 C·그래프 D의 재료)

| 메서드·경로 | 내용 |
|---|---|
| `POST /v1/imports/parse` `{format?, text, tz_offset_minutes?, chat_id?}` | **미리보기**(저장 없음). format: `auto`(기본)·`kakao`·`telegram`·`plain`·`json`. 응답: `{format, variant, title_hint, messages_total, max_messages, date_from, date_to, speakers: [{name, count, suggested_role}], sample(앞 20건), skipped_lines, system_lines, masked_pii, problems[], chats}` |
| `POST /v1/conversations` `{contact_id, title?, source_key?, me_name?}` | 대화방. `me_name`은 그래프 수집에 쓰는 내 이름(`회원님`·`나`·`me` 같은 자리표시 이름은 저장하지 않는다) |
| `POST /v1/conversations/{id}/imports/raw` `{format?, text, speaker_map, tz_offset_minutes?, chat_id?, keep_recent?}` | **원문 가져오기**. `speaker_map`: `{"<이름>": "me"|"contact"|"skip"}` — 지도에 없는 화자가 있으면 422(이름 나열), `skip`은 그 화자 메시지를 버린다(단체방). `keep_recent`는 최근 N건만. 응답 202 + 작업 |
| `POST /v1/conversations/{id}/imports` `{messages[], speaker_map?}` | JSON 메시지 가져오기(기존). 메시지: `{"id":"m01","speaker":"김민수","sent_at":"2026-10-05T09:31:00+09:00","text":"..."}` |
| `GET /v1/imports/{job_id}` | 작업 상태. `status`: `processing`(메시지는 저장됐고 말투 묶음·임베딩을 뒤에서 만드는 중) → `completed` / `failed`(`error_code: reindex_failed`, 같은 내용을 다시 올리면 같은 작업으로 다시 시도). 두 가져오기 요청은 묶음 생성을 `IMPORT_INDEX_WAIT_SECONDS`(기본 2초)만 기다린다 — 그 안에 끝나면 응답이 이미 `completed`, 아니면 `processing`이라 이 주소(또는 대화방 `stats`의 `latest_job`)를 다시 읽는다. `processing` 동안 C/D 첨삭은 예전 묶음(첫 가져오기면 없음)으로 돌고, 그래프 수집(`graph_status: pending`)은 묶음이 끝난 뒤 시작한다. 서버가 도중에 재시작되면 다음 시작 때 이어서 끝낸다 |
| `GET /v1/conversations?contact_id=`, `GET /v1/conversations/{id}` | 목록·조회 |
| `GET /v1/conversations/{id}/messages?limit=` | 메시지 목록. `limit`(1~5000)을 주면 가장 최근 N건, 생략하면 전부 |
| `GET /v1/conversations/{id}/stats` | `{messages, my_messages, episodes, graph_enabled, graph_ingested, graph_pending, graph_held, graph_limited, first_message_at, last_message_at, latest_job}` — 가져오기 진행 표시용. `graph_held`는 거듭 실패해 보류된 메시지 수(`graph_pending`에는 들어가지 않는다) |
| `DELETE /v1/conversations/{id}` | 원문·묶음·그래프 에피소드 삭제. 이후 검색에 나오지 않는다 |

- **지원 형식**: 카카오톡 내보내기(.txt — 안드로이드·iOS·PC, PC의 .csv), 텔레그램 Desktop 내보내기(`result.json` — 계정 전체 내보내기면 `chats` 목록이 오고 `chat_id`로 고른다), 붙여넣기(`날짜 시각 | 화자 | 내용`), JSON 배열. **카카오톡 파서는 PC의 .csv만 실물 내보내기로 검증했다(2026-11-13). .txt 세 변형은 합성 픽스처 기준이다** — 못 읽은 줄은 `skipped_lines`·`problems`로 드러난다.
- 문체 묶음(임베딩)은 요청 안에서 끝난다(실제 e5로 메시지 수천 건이면 수십 초~몇 분. 본문이 같은 묶음은 다시 임베딩하지 않는다). 같은 대화방의 가져오기·삭제는 한 번에 하나씩 처리한다. **그래프 수집은 백그라운드**: `graph_status` skipped(그래프 꺼짐) → pending → running → completed / failed / **limited**(수집 상한 도달 — 종료 상태, `graph_error`는 `user_cap` 또는 `daily_cap`).
- 그래프 수집 상한(`GRAPH_INGEST_USER_MESSAGE_CAP`·`GRAPH_INGEST_DAILY_GLOBAL_CAP`)은 지워지지 않는 하루 카운터로 센다 — 대화를 지워도 그날 쓴 예산은 돌아오지 않는다. 메시지 1건이 1이고 1,000자마다 1을 더 센다. 예산이 모자라면 **가장 최근 메시지부터** 넣는다. `limited`·`failed`였던 파일을 다시 올리면 예산이 있을 때 이어서 수집한다.
- 작업의 `error_message`·`graph_error`와 삭제 응답의 `graph_error(s)`에는 예외의 **종류**만 남는다(예: `RuntimeError`, `DBAPIError(23505)`) — 문장에 대화 원문이 섞일 수 있어서.
- 같은 내용을 다시 올리면 같은 작업을 돌려주고, 겹친 메시지는 건너뛴다. 메시지 id는 내용 기반(UTC 분 + 같은 분·같은 본문 순번 + 본문)이라 범위가 겹치는 내보내기를 올려도 중복 저장되지 않는다. `tz_offset_minutes`(기본 +540 = 한국 시간)를 바꾸면 id가 달라지므로 같은 대화는 같은 값으로 올린다.
- 화자 지정을 잘못한 채 가져왔으면 대화방을 지우고 다시 가져온다(다시 올리면 전부 중복으로 건너뛴다).
- `IMPORT_MASK_PII=true`(기본)면 API로 들어온 가져오기 본문에서 전화번호·이메일·주민등록번호·카드/계좌 번호 **모양**을 `[전화번호]`처럼 가려 저장한다. 패턴 기반이라 이름·주소는 가리지 못하고 표기가 다르면 놓칠 수 있다.
- 한도: 본문 `MAX_BODY_BYTES`(8MB), 원문 `MAX_IMPORT_CHARS`(200만 자), 한 번에 `MAX_IMPORT_MESSAGES`(기본 5000, 운영 예시 2000)건, 사용자별 저장 한도(`MAX_CONTACTS_PER_USER` 200·`MAX_CONVERSATIONS_PER_USER` 500·`MAX_MESSAGES_PER_USER` 50000 — 넘으면 아무것도 저장하지 않고 422 `quota_exceeded`). 메시지 한 건은 4,000자까지, `sent_at`은 1990~2100년.
- 대화 본문의 깨진 글자(NUL, 짝 없는 대리 문자)는 `?`로 바꿔 읽는다. 이름·제목·메시지 id 같은 다른 칸에 있으면 422.

## 첨삭

| 메서드·경로 | 내용 |
|---|---|
| `POST /v1/rewrites` `{contact_id, draft_text, situation?, condition?, adjust?, channel?, persona_version?}` + `Idempotency-Key` 헤더 | 추천 생성 |
| `GET /v1/rewrites?contact_id=&limit=&before=` | 첨삭 이력 (최근 순, 최대 100). `before=<ISO 시각>`으로 다음 쪽 |
| `GET /v1/rewrites/{id}` | 결과 조회 |
| `GET /v1/rewrites/{id}/evidence` | 참고한 내 과거 표현의 **지금 남아 있는 원문**: `{items: [{id, source_message_ids, messages: [{speaker_role, sent_at, text}]}], missing, refs_recorded}`. 결과에는 원문 사본을 저장하지 않으므로 대화방을 지우면 여기서도 사라진다(`missing` = 원문이 전부 지워진 근거 수). 출처 참조를 남기기 전(2026-11-11 이전)의 결과는 `refs_recorded: false`, `items: []` |
| `POST /v1/rewrites/{id}/feedback` `{accepted, edited_text?, reason?, reason_code?, consent_to_reuse?}` | 적용·수정·거절 피드백. `reason_code`: too_stiff·too_casual·too_servile·not_my_style·fact_changed·too_long·too_short·other |
| `DELETE /v1/rewrites/{id}`, `DELETE /v1/rewrites?contact_id=` | 이력 한 건 / 상대별 전체 삭제 |
| `GET /v1/usage` | 오늘(KST) 요청 수·토큰과 한도. `graph_messages_ingested`(지금 그래프에 든 양과 오늘 넣은 양 중 큰 값)·`graph_message_cap` |

- `condition`: `A`(기준선) · `B`(+페르소나, 기본값) · `C`(+내 과거 표현) · `D`(+그래프 맥락) · **`auto`** — 이 상대에 쓸 수 있는 가장 풍부한 조건을 고른다(그래프 수집분 있음 → D / 문체 묶음 있음 → C / 페르소나 있음 → B / 아니면 A). 응답의 `condition`은 실제 적용된 A~D. 직접 고른 B~D는 저장된 페르소나가 없으면 422, `auto`는 페르소나 없이도 가져온 대화가 있으면 C/D로 간다. `NEO4J_URI`가 비어 있으면 D = C.
- `adjust`: `shorter`·`longer`·`more_formal`·`more_casual` — 이번 요청만 페르소나 위에 덮어쓴다(저장된 페르소나는 그대로).
- `channel`: `web`(기본)·`mobile`·`desktop`·`api` — 관리자 화면의 채널 집계에 쓴다. 모바일 클라이언트는 `mobile`을 보낸다.
- 같은 `Idempotency-Key`·같은 본문 재요청은 기존 결과를 돌려주고 사용량을 세지 않는다(동시에 와도 모델은 한 번만 부른다). 같은 키·다른 본문은 409. 키는 128자까지.
- 사용량: 요청 수는 모델을 부르기 전에 예약하고, 토큰 한도는 진행 중인 요청까지 합쳐 본다. 모델이 응답 없이 끝난 호출도 추정 입력 토큰을 사용량에 더한다(`token_usage.estimated_input_tokens`).

응답: `{id, contact_id, condition, channel, created_at, feedback|null, result}`. `result`: `status`(recommended / fallback_original), `result_text`, `change_reasons[]`, `warnings[]`(모델이 적은 주의점 — 화면에 보여 준다), `evidence_ids[]`, `evidence_refs`, `adjust`, `preserved_items[]`, `validation`, `attempts`, `attempt_log[]`, `retrieval_version`, `token_usage`, `latency_ms`.
서버는 초안 원문을 따로 저장하지 않는다. 다만 원문 유지(`fallback_original`) 결과의 `result_text`는 초안 그대로이고, `preserved_items`(초안에서 뽑은 날짜·수량 등)는 항상 결과에 남는다. `REWRITE_RETENTION_DAYS`가 0이 아니면 그 기간이 지난 이력은 주기 정리 때 지운다.

## 내 데이터

| 메서드·경로 | 내용 |
|---|---|
| `GET /v1/data/summary` | 상대별 요약 `{contacts: [{contact_id, alias, relationship_type, persona_version, conversations, messages, my_messages, graph_ingested, rewrites, last_rewrite_at}], totals, graph_enabled, retention_days}` |
| `GET /v1/export` | 내 데이터 전체 JSON(첨부 다운로드): 상대·페르소나 버전·대화방·메시지·첨삭 이력 |

## 관리자 (운영 대시보드)

화면 `/admin`. API는 모두 `GET /v1/admin/*`. 접근: 로그인(세션 쿠키 또는 Bearer) + 아이디가 `ADMIN_USERNAMES`에 대소문자까지 같게 있는 계정. 미로그인 401 · 관리자 아님 403 `forbidden` · `X-User-Id`만으로는 401 · 인메모리 저장소 503 `admin_unavailable`.
통과한 호출은 `admin_access_log`에 남는다. **대화 원문·첨삭 문장·자유 서술 사유·편집본은 어떤 응답에도 없고**, 사용자 id는 `u_8f2…a1`로 줄인다.

| 경로 | 내용 |
|---|---|
| `GET /v1/admin/overview?days=1\|7\|30` | 기간 요약: `users`, `rewrites`(성공률, `by_condition`·`by_channel`·`by_day`), `latency_ms`(p50·p95), `fallback_reasons`, `feedback`, `tokens`, `graph`, `limits`, `health` |
| `GET /v1/admin/users?limit=50&offset=0` | `{total, items[]}` 최근 가입 순 |
| `GET /v1/admin/jobs?limit=50` | 최근 가져오기 작업. `graph_error`는 오류 종류만 |
| `GET /v1/admin/feedback?limit=50` | 최근 거절 피드백 (선택형 사유만) |
| `GET /v1/admin/access-log?limit=50` | 관리자 접근 기록 |

- 기간은 사용량과 같은 하루 경계(KST)의 달력 날짜 N일(오늘 포함). 표본이 없으면 `success_rate`·`acceptance_rate`·`latency_ms`는 null.
- `tokens`는 `usage_counters` 기준. `estimated_cost_usd`는 `COST_*` 단가가 0이면 null(숫자를 지어내지 않는다).
- `fallback_reasons[].count`는 요청 수. `by_reason_code`는 거절만, 사유 미선택은 `unspecified`. `graph.jobs`·`pending_messages`·`held_messages`(거듭 실패해 보류된 메시지)는 기간과 무관한 현재 상태.

## 오류 모양

`{"code": "...", "message": "사람이 읽는 한국어"}`가 최상위에 온다. 예전에 `{"detail": …}`이던 응답은 `detail`도 그대로 있다(하위 호환).
요청 검증 실패(422)는 `details: [{"loc": "body.username", "msg": "…"}]`가 붙는다(도메인 규칙 위반 422에는 없다). 5xx는 `{"code":"internal_error","message","request_id"}`이고 같은 값이 `X-Request-ID` 응답 헤더에 있다(운영자가 로그에서 찾는 열쇠).

| 상태 | code |
|---|---|
| 400 | `bad_request`, `invalid_host`(허용되지 않은 Host) |
| 401 | `unauthorized` |
| 403 | `forbidden`(CSRF·권한), `invalid_invite` |
| 404 | `not_found` (남의 것도 404) |
| 409 | `conflict` (버전 충돌, 같은 키·다른 본문, 이미 있는 이름) |
| 413 | `payload_too_large` |
| 422 | `invalid_input`(NUL·깨진 유니코드가 든 본문도 저장 전에 여기서 끝난다), `consent_required`, `quota_exceeded`(사용자별 저장 한도) |
| 429 | `usage_exceeded`(사용자 하루 한도), `budget_exceeded`(서비스 전체 하루 한도), `rate_limited`, `too_many_attempts` — 모두 `Retry-After`가 붙는다(하루 한도는 한국 시간 자정까지 남은 초) |
| 503 | `model_unavailable`, `admin_unavailable` |

운영에서는 프록시(Caddy)가 직접 만드는 오류도 같은 모양이다: 413 `payload_too_large`, 502~504 `service_unavailable` + `Retry-After: 10`(배포·재시작 중).

## 모바일·PC 프로그램에서 쓸 때

- 쿠키+CSRF(지금 `apps/ios`가 쓰는 방식)는 그대로 동작한다. Bearer 토큰(`POST /v1/auth/token`)을 쓰면 쿠키·CSRF 없이 `Authorization` 헤더 하나로 된다.
- 첨삭 요청에 `channel: "mobile"`(또는 `desktop`)을 넣으면 관리자 화면에서 채널별로 보인다. `condition: "auto"`를 쓰면 서버가 조건을 고른다.
- 같은 요청의 재시도는 같은 `Idempotency-Key`로 보낸다(동시에 두 번 보내지는 않는다).
- 429는 `Retry-After`만큼 기다린다. `usage_exceeded`·`budget_exceeded`는 다음 날(KST) 풀린다.
- 살아 있는지 자주 볼 때는 쿼리 없는 `/healthz`를 쓴다. `/healthz?deep=true`(DB·그래프까지 확인)는 요청 제한 대상이고 결과가 최대 5초 묵는다.

## 그래프 담당용 Cypher (개발: Neo4j Browser http://localhost:7474)

```cypher
MATCH (n:Entity {group_id: "demo"}) RETURN n.name, labels(n) ORDER BY n.name;
MATCH (a:Entity {group_id: "demo"})-[r:RELATES_TO]->(b:Entity) RETURN a.name, r.name, r.fact, r.valid_at, r.invalid_at, b.name LIMIT 50;
MATCH (e:Episodic {group_id: "demo"}) RETURN e.name, e.valid_at, left(e.content, 80) ORDER BY e.valid_at;
```
`group_id`는 사용자 id다. 합성 대화를 넣은 사용자는 `scripts/import_synthetic.py --user demo --graph`의 `demo`. 운영 구성은 Neo4j 포트를 밖에 열지 않는다(SSH 터널로만).
