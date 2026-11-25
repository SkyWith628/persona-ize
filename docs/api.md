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
| `GET /v1/auth/providers` (인증 불필요) | 가입 정책과 켜진 소셜 로그인: `{signup_open, requires_approval, invite_required, require_consent, google, kakao}` |
| `POST /v1/auth/signup` `{email, password, display_name?, invite_code?, consent?}` | 이메일 가입. 문(가입 닫힘 403 → 초대 코드 → 동의)은 `register`와 같다. 이메일은 소문자로 저장, 중복 409. 아이디는 서버가 이메일 앞부분에서 만든다(응답 `username`). 응답은 `register`와 같다 |
| `POST /v1/auth/login` `{username, password}` | 로그인. `username`에 **아이디 또는 이메일**. 비밀번호가 맞은 뒤에만 403 `account_pending`(승인 대기)·`account_suspended`(정지). 같은 곳(IP)에서 같은 아이디를 계속 틀리면 그 조합만 429 `too_many_attempts` + `Retry-After`(IP 단독·아이디 단독은 훨씬 높은 횟수에서). 실패는 **계정 기준**으로 센다 — 아이디로 틀리든 이메일로 틀리든 같은 계정의 횟수이고 한쪽으로 성공하면 함께 지워진다(없는 계정은 입력값 기준, 2026-11-17) |
| `POST /v1/auth/token` `{username, password, client?}` | Bearer 토큰 발급: `{access_token, token_type: "bearer", expires_at, user_id, username}`. `client`는 `mobile`·`desktop`·`api` |
| `GET /v1/auth/oauth/{google\|kakao}/start?next=/경로` | 소셜 로그인 시작 → 제공자로 302. 링크로 바로 연다. 설정되지 않은 제공자는 404 |
| `POST /v1/auth/oauth/{google\|kakao}/start` `{next?, consent?, invite_code?}` | 같은 시작인데 새 계정에 필요한 동의·초대 코드를 함께 보낸다 → `{authorize_url}`(그 주소로 이동). 초대 코드가 틀리면 403 `invalid_invite`. 진행 중인 소셜 로그인이 서버 상한에 닿아 있으면 429 `too_many_attempts` + `Retry-After` |
| `GET /v1/auth/oauth/{google\|kakao}/callback` | 제공자가 돌려보내는 곳(화면이 부르지 않는다). 성공: 세션 쿠키 + 302 → `next`. 실패: 302 → `<next>?auth_error=<코드>` |
| `POST /v1/auth/logout` | 쿠키 세션과 Bearer 토큰을 폐기 |
| `GET /v1/auth/me` | 내 정보 + CSRF 토큰 재조회 |
| `POST /v1/auth/consent` `{}` (CSRF) | 현재 `CONSENT_VERSION`으로 동의 기록 |
| `GET /v1/auth/sessions` | 로그인된 기기 목록 `[{id, created_at, expires_at, current}]` |
| `POST /v1/auth/logout-all` (CSRF) | 지금 세션만 남기고 전부 로그아웃 → `{revoked_sessions}` |
| `POST /v1/auth/password` `{current_password, new_password}` (CSRF) | 비밀번호 변경. 다른 세션은 모두 로그아웃 |
| `DELETE /v1/auth/me` `{password}` (CSRF) | **계정 삭제**. 비밀번호가 없는 소셜 전용 계정은 `{"password": ""}`. 상대·대화·묶음·첨삭 이력·그래프·사용량·세션을 모두 지운다. 되돌릴 수 없다. 하나뿐인 활성 관리자는 409 `last_admin`(아무것도 지우지 않는다 — 다른 관리자를 먼저 지정한다) |

로그인·가입·`me` 응답: `{user_id, username, csrf_token, expires_at, is_admin, consented_at, consent_version, consent_current, email, display_name, role, status}`. `me`에는 `expires_at` 대신 `providers`(로그인 수단: `password`·`google`·`kakao`)가 있다. `is_admin`은 `role`이 `admin`이거나 아이디가 `ADMIN_USERNAMES`와 대소문자까지 같을 때 true. `consent_current`는 기록된 동의 버전이 지금 버전과 같은지.

회원 상태·소셜 로그인 (2026-11-16):
- **승인제**(`AUTH_SIGNUP_REQUIRES_APPROVAL=true`, 기본 꺼짐): `register`·`signup`이 201 대신 **202 `{status: "pending", username, email}`** 이고 쿠키를 내리지 않는다. 관리자가 승인하기 전의 로그인은 403 `account_pending`. 첫 소셜 로그인은 `auth_error=account_pending`으로 돌아온다.
- **정지**: 로그인 403 `account_suspended`. 열려 있던 쿠키 세션·Bearer 토큰은 다음 요청부터 401.
- **소셜 로그인**은 `GOOGLE_*`·`KAKAO_*`의 ID와 시크릿이 모두 있을 때만 켜진다(기본 꺼짐). `next`는 `/`로 시작하는 같은 출처 경로만 받는다(아니면 `/`). `auth_error` 값: `oauth_failed`(state 불일치·만료·재사용, 교환 실패) · `oauth_denied`(사용자가 취소) · `signup_closed` · `invalid_invite` · `consent_required` · `account_pending` · `account_suspended` · `email_in_use`.
- 계정 연결: 제공자가 **확인해 준** 이메일이 기존 계정의 **확인된** 이메일(= 소셜로 만든 계정)과 같을 때만 그 계정에 묶는다. 이메일로 가입한 계정(주소 미확인)에는 묶지 않고 `email_in_use` — 비밀번호로 로그인하라고 안내한다. 확인되지 않은 이메일은 이메일 없는 새 계정이 된다.
- 이메일 가입·소셜 시작/콜백은 `RATE_LIMIT_AUTH_PER_MINUTE`에 함께 걸린다.
- 진행 중인 소셜 로그인(시작만 하고 아직 돌아오지 않은 것)은 클라이언트(IP, IPv6는 /64)마다 20개까지 둔다 — 넘으면 **그 클라이언트의** 가장 오래된 것이 무효가 된다. 서버 전체 상한(5,000)에 닿으면 새로 시작하지 못한다: `POST …/start`는 429 `too_many_attempts`, `GET …/start`는 제공자로 가지 않고 `<next>?auth_error=oauth_failed`. 이미 시작한 다른 사람의 로그인은 끊기지 않는다(2026-11-17).
- 이메일 형식(가입·이메일 로그인·제공자가 준 이메일 공통, 2026-11-17): 254자 이하, `@` 하나, 출력 가능한 ASCII만. 앞부분은 영문·숫자와 ``.!#$%&'*+/=?^_`{|}~-``(1~64자, 점은 맨 앞·뒤·연속 불가), 도메인은 점으로 나뉜 영문·숫자·하이픈 라벨 둘 이상이고 마지막 라벨은 영문 2자 이상. 한글·전각 글자·폭 없는 문자가 든 주소와 IP 주소 형식(`a@127.0.0.1`)은 422 `invalid_input`(소셜 로그인에서는 이메일 없는 계정으로 처리).
비밀번호를 잊은 사용자는 운영자가 서버에서 재설정한다(이메일이 없어 자가 재설정이 없다): `scripts/reset_password.py --username <이름>` (`--create`면 계정이 없을 때 만든다 — 운영에서 `ADMIN_USERNAMES` 관리자 계정을 만드는 길). 권한으로 관리자를 만들거나 올릴 때는 `cd backend && python -m app.tools.create_user <아이디> [--admin] [--reset-password]`.

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
| `GET /v1/contacts/{id}/graph` | 상대 주변 맥락 그래프 `{enabled, center_id, nodes[{id,name,kind,summary,event_type,importance,is_contact}], edges[{id,source,target,relation,fact,valid_at,invalid_at,current}]}`. 상대 노드와 그 상대가 관여한 사건에 닿은 사실만(D 검색 범위와 같음), 유효한 사실 먼저·최근 순. 그래프가 꺼져 있으면 `enabled=false`. **말투 간선** `register`(선택): 내가 이 상대에게 쓰는 말투의 구간 — 화면에는 (나)→(상대) 간선처럼 보이지만 그래프에 저장된 값이 아니라 내 메시지(최근 500건)에서 읽을 때 규칙으로 계산한다(모델 호출 없음). 그래서 `enabled=false`여도 온다. `{segments: [{level: hapsyo|haeyo|hae, start, end|null, count, status: confirmed|candidate|previous, message_ids(최대 5, 그 구간의 최근 것, `c3:m08` 꼴), trigger: {event, at}|null}], address: {term, count, status: confirmed|candidate}|null, based_on_messages}` — 구간은 시간 순, `start`·`end`·`at`은 한국 시간 날짜, 마지막 구간만 `end: null`이고 그 앞은 `status: previous`. `trigger`는 그래프가 켜져 있고 말투가 바뀐 **확정된 현재 구간**에만 붙는다(변화 시점 ±7일의 사건, 제안 카드와 같은 규칙). 내 메시지가 없거나 계산이 실패하면, 또는 말투 프로필을 끈 서버(`REGISTER_SUGGESTIONS=false`)에서는 `register` 항목 자체가 없다(나머지 응답은 예전 그대로) |
| `GET /v1/contacts/{id}/persona/versions` | 버전 기록 (최신 먼저, 최대 50) |
| `GET /v1/contacts/{id}/persona/suggestion` | 이 상대에게 보낸 **내 과거 메시지**로 추정한 말투 제안: `{based_on_messages, settings|null, signals: [{field, value, reason, source, since}]}`. 규칙 기반(모델 호출 없음), 저장하지 않는다 — 화면에서 사용자가 확인하고 저장한다. 내 메시지가 5건 미만이면 `settings: null` 그래프가 켜져 있으면 관계 사실의 시간 순 변화에서 읽은 제안(`source: "graph"`, `since`: 그 값이 처음 확인된 날 — 예: "10/17부터 존댓말", "10/21부터 '파트장님'")이 뒤에 붙고, 같은 항목이면 그래프 쪽이 `settings`에 반영된다. 말투 프로필(`source: "register"`, `REGISTER_SUGGESTIONS=false`로 끈다)은 내 메시지를 문장 단위로 분류해 말투·호칭이 바뀐 시점을 말한다("10/21부터 합쇼체 2건 (그 전에는 반말 3건)"). 메시지 수 문턱은 없다 — 구간 규칙(같은 쪽 말투 연속 2건, 호칭 2회)만 있고 건수를 근거에 적으므로, 내 메시지가 5건 미만이어도 register 근거와 `card`는 올 수 있다(그때 `settings`는 기본값 위에 그 근거만 얹은 것이다). 근거 순서는 messages → register → graph. 제안할 것이 있으면 `card`가 온다(없으면 항목 자체가 없다): `{contact_alias, speech_previous: {level, count, message_ids}|null, speech_current: {level, count, message_ids, since}|null, two_valued: {level: 건수}|null, address: {term, count, since, status: confirmed|candidate, message_ids}|null, trigger: {event, at, message_ids}|null, scope_hints: [{text, message_ids}], ask, ask_reason, proposed: {formality|null, address_term|null}}` — `level`은 `hapsyo`·`haeyo`·`hae`, `message_ids`는 `c3:m08` 꼴, `trigger`·`scope_hints`는 그래프가 켜져 있을 때 변화 시점 ±7일의 사건과 관계 사실. `ask`가 참이면(두 말투 혼재, 범위 단서 있음, 변화 뒤 관찰 2건 이하) 말투 프로필의 값은 `settings`에 넣지 않고 `card.proposed`로만 보여 준다 |
| `GET /v1/persona-defaults` | 관계 유형별 기본 페르소나 7종 |
| `DELETE /v1/contacts/{id}` | **상대 삭제**. 그 상대의 대화방·메시지·묶음·그래프 에피소드·페르소나·첨삭 이력을 모두 지운다 (그래프 삭제가 실패해도 DB는 지우고 사유를 돌려준다) |

settings: `formality·friendliness·directness`(1~5), `length`(shorter/same/longer), `emoji`(none/rare/often), `address_term`(30자), `preferred_phrases[]`(20개)·`forbidden_phrases[]`(50개, 표현 하나는 앞뒤 공백을 빼고 40자까지 — 넘으면 422), `notes`(500자).

## 과거 대화 가져오기 (문체 검색 C·그래프 D의 재료)

| 메서드·경로 | 내용 |
|---|---|
| `POST /v1/imports/parse` `{format?, text, tz_offset_minutes?, chat_id?}` | **미리보기**(저장 없음). format: `auto`(기본)·`kakao`·`telegram`·`plain`·`json`. 응답: `{format, variant, title_hint, messages_total, max_messages, date_from, date_to, speakers: [{name, count, suggested_role}], sample(앞 20건), skipped_lines, system_lines, masked_pii, problems[], chats}` |
| `POST /v1/conversations` `{contact_id, title?, source_key?, me_name?}` | 대화방. `me_name`은 그래프 수집에 쓰는 내 이름(`회원님`·`나`·`me` 같은 자리표시 이름은 저장하지 않는다) |
| `POST /v1/conversations/{id}/imports/raw` `{format?, text, speaker_map, tz_offset_minutes?, chat_id?, keep_recent?}` | **원문 가져오기**. `speaker_map`: `{"<이름>": "me"|"contact"|"skip"}` — 지도에 없는 화자가 있으면 422(이름 나열), `skip`은 그 화자 메시지를 버린다(단체방). `keep_recent`는 최근 N건만. 응답 202 + 작업 |
| `POST /v1/conversations/{id}/imports` `{messages[], speaker_map?}` | JSON 메시지 가져오기(기존). 메시지: `{"id":"m01","speaker":"김민수","sent_at":"2026-10-05T09:31:00+09:00","text":"..."}` |
| `GET /v1/imports/{job_id}` | 작업 상태. `status`: `processing`(메시지는 저장됐고 말투 묶음·임베딩을 뒤에서 만드는 중) → `completed` / `failed`(`error_code: reindex_failed`, 같은 내용을 다시 올리면 같은 작업으로 다시 시도). 두 가져오기 요청은 묶음 생성을 `IMPORT_INDEX_WAIT_SECONDS`(기본 2초)만 기다린다 — 그 안에 끝나면 응답이 이미 `completed`, 아니면 `processing`이라 이 주소(또는 대화방 `stats`의 `latest_job`)를 다시 읽는다. `processing` 동안 C/D 첨삭은 예전 묶음(첫 가져오기면 없음)으로 돌고, 그래프 수집(`graph_status: pending`)은 묶음이 끝난 뒤 시작한다. 서버가 도중에 재시작되면 다음 시작 때 이어서 끝낸다. **진행률**: `processing` 동안 `index_total`(이번에 만들 말투 묶음 전체 수)과 `index_done`(그중 벡터가 준비된 수 — 본문이 그대로라 다시 쓰는 묶음은 처음부터 센다)이 함께 나간다. 임베딩 64개가 끝날 때마다 오른다. 서버 메모리에만 있는 값이라 모르면 둘 다 `null`이다(끝난 작업, 묶음을 세기 전, 재시작 직후 이어받기 전) — 그때는 수치 없이 진행 중으로만 보여 준다 |
| `GET /v1/conversations?contact_id=`, `GET /v1/conversations/{id}` | 목록·조회 |
| `GET /v1/conversations/{id}/messages?limit=` | 메시지 목록 `{id, source_key, speaker_role, speaker_name, sent_at, text}`. `speaker_name`은 내보내기의 화자 이름(모르면 null). `limit`(1~5000)을 주면 가장 최근 N건, 생략하면 전부 |
| `GET /v1/conversations/{id}/speakers` | 이 대화방의 화자 `[{name, speaker_role, messages}]` — 많이 말한 순. `name`이 null인 줄은 이름 없이 저장된 메시지(2026-11-15 전에 가져왔거나 `speaker: "me"`처럼 역할로만 올린 것)로, 화자별로 고칠 수 없다. 같은 내보내기를 다시 올리면 이름이 채워진다 |
| `GET /v1/conversations/{id}/stats` | `{messages, my_messages, episodes, graph_enabled, graph_ingested, graph_pending, graph_held, graph_limited, first_message_at, last_message_at, latest_job}` — 가져오기 진행 표시용. `graph_held`는 거듭 실패해 보류된 메시지 수(`graph_pending`에는 들어가지 않는다). `latest_job`은 `GET /v1/imports/{job_id}`와 같은 모양이라 말투 분석 진행률(`index_done`·`index_total`)도 여기서 읽는다 |
| `PUT /v1/conversations/{id}/speaker-roles` `{message_id, speaker_role, me_name?}` 또는 `{speakers, me_name?}` | **화자 역할 고치기** — 화자를 잘못 지정한 가져오기를 대화방을 지우지 않고 고친다. 두 방식 중 하나로 보낸다(섞으면 422). ① `message_id` + `speaker_role`: "이 메시지는 X가 한 말" — 지금과 다르면 **방 전체의 나↔상대를 맞바꾼다**(화자 이름이 없는 예전 대화도 된다). `me_name`을 안 주면 비운다. ② `speakers: {"이름": "me"|"contact"|"skip"}`: **화자 이름별 지정** — 적지 않은 화자는 그대로 두고, `skip`은 그 화자의 메시지를 **지운다**. 이름은 `GET …/speakers`의 것(이름이 저장된 메시지만 대상). 방에 없는 이름에 me/contact를 주면 422, 전부 지워지게 되면 422, 이미 없는 이름의 `skip`은 아무 일도 하지 않는다. 내 이름은 '나'였던 화자가 '나'에서 빠질 때만 바뀐다(`me_name`, 없으면 '나'가 한 사람일 때 그 이름). 둘 다 맞는 상태를 적으므로 다시 보내도 결과가 같다(`changed: false`). 응답 `{conversation_id, changed, messages, messages_removed, my_messages, me_name, graph_episodes_removed, graph_requeued, job}` — 바뀌면 묶음을 뒤에서 다시 만들고(`job`, `GET /v1/imports/{id}`로 확인), 그래프가 켜져 있으면 에피소드를 지우고 상한 안에서 다시 수집한다(①은 방 전체, ②는 바뀐 화자의 메시지만 — 내 이름이 바뀌면 방 전체). 수집이 도는 중이면 넣던 메시지까지만 끝내고 비켜 준다 — 제때 못 비키면 409, 그래프에 닿지 못하면 아무것도 바꾸지 않고 503 `graph_unavailable` |
| `DELETE /v1/conversations/{id}` | 원문·묶음·그래프 에피소드 삭제. 이후 검색에 나오지 않는다 |

- **지원 형식**: 카카오톡 내보내기(.txt — 안드로이드·iOS·PC, PC의 .csv), 텔레그램 Desktop 내보내기(`result.json` — 계정 전체 내보내기면 `chats` 목록이 오고 `chat_id`로 고른다), 붙여넣기(`날짜 시각 | 화자 | 내용`), JSON 배열. **카카오톡 파서는 PC의 .csv만 실물 내보내기로 검증했다(2026-11-13). .txt 세 변형은 합성 픽스처 기준이다** — 못 읽은 줄은 `skipped_lines`·`problems`로 드러난다.
- 문체 묶음(임베딩)은 요청 안에서 끝난다(실제 e5로 메시지 수천 건이면 수십 초~몇 분. 본문이 같은 묶음은 다시 임베딩하지 않는다). 같은 대화방의 가져오기·삭제는 한 번에 하나씩 처리한다. **그래프 수집은 백그라운드**: `graph_status` skipped(그래프 꺼짐) → pending → running → completed / failed / **limited**(수집 상한 도달 — 종료 상태, `graph_error`는 `user_cap` 또는 `daily_cap`).
- 그래프 수집 상한(`GRAPH_INGEST_USER_MESSAGE_CAP`·`GRAPH_INGEST_DAILY_GLOBAL_CAP`)은 지워지지 않는 하루 카운터로 센다 — 대화를 지워도 그날 쓴 예산은 돌아오지 않는다. 메시지 1건이 1이고 1,000자마다 1을 더 센다. 예산이 모자라면 **가장 최근 메시지부터** 넣는다. `limited`·`failed`였던 파일을 다시 올리면 예산이 있을 때 이어서 수집한다.
- 작업의 `error_message`·`graph_error`와 삭제 응답의 `graph_error(s)`에는 예외의 **종류**만 남는다(예: `RuntimeError`, `DBAPIError(23505)`) — 문장에 대화 원문이 섞일 수 있어서.
- 같은 내용을 다시 올리면 같은 작업을 돌려주고, 겹친 메시지는 건너뛴다. 메시지 id는 내용 기반(UTC 분 + 같은 분·같은 본문 순번 + 본문)이라 범위가 겹치는 내보내기를 올려도 중복 저장되지 않는다. `tz_offset_minutes`(기본 +540 = 한국 시간)를 바꾸면 id가 달라지므로 같은 대화는 같은 값으로 올린다.
- 화자를 잘못 지정한 채 가져왔으면 `PUT /v1/conversations/{id}/speaker-roles`로 고친다(다시 올려도 이미 있는 메시지의 역할은 바뀌지 않는다 — 전부 중복으로 건너뛴다). 방 전체 맞바꾸기, 화자 한 명만 나/상대로, 잘못 넣은 화자 빼기(`skip`)가 된다. `skip`으로 뺐던 화자를 되살리려면 그 화자를 넣어 같은 파일을 다시 올린다 — 빠진 메시지만 더 들어온다(끝난 가져오기와 같은 내용이어도 빠진 메시지가 있으면 다시 넣는다).
- 메시지에는 내보내기의 화자 이름이 함께 저장된다(`speaker_name`, 64자까지, 2026-11-15부터). 그전에 가져온 대화는 같은 내보내기를 다시 올리면 빈 이름만 채워진다. 내 데이터 내보내기(`GET /v1/export`)의 메시지에도 들어간다.
- `IMPORT_MASK_PII=true`(기본)면 API로 들어온 가져오기 본문에서 전화번호·이메일·주민등록번호·카드/계좌 번호 **모양**을 `[전화번호]`처럼 가려 저장한다. 패턴 기반이라 이름·주소는 가리지 못하고 표기가 다르면 놓칠 수 있다.
- 한도: 본문 `MAX_BODY_BYTES`(8MB), 원문 `MAX_IMPORT_CHARS`(200만 자), 한 번에 `MAX_IMPORT_MESSAGES`(기본 5000, 운영 예시 2000)건, 사용자별 저장 한도(`MAX_CONTACTS_PER_USER` 200·`MAX_CONVERSATIONS_PER_USER` 500·`MAX_MESSAGES_PER_USER` 50000 — 넘으면 아무것도 저장하지 않고 422 `quota_exceeded`). 메시지 한 건은 4,000자까지, `sent_at`은 1990~2100년.
- 대화 본문의 깨진 글자(NUL, 짝 없는 대리 문자)는 `?`로 바꿔 읽는다. 이름·제목·메시지 id 같은 다른 칸에 있으면 422.

## 첨삭

| 메서드·경로 | 내용 |
|---|---|
| `POST /v1/rewrites` `{contact_id, draft_text, situation?, condition?, adjust?, channel?, persona_version?}` + `Idempotency-Key` 헤더 | 추천 생성 |
| `POST /v1/rewrites/batch` `{contact_ids: [1~5개, 중복 불가], draft_text, …단건과 같은 나머지 필드}` + `Idempotency-Key` 헤더(91자 이내) | 같은 초안을 여러 상대에게, 상대마다 그 상대의 말투로. 200 `{items: [{contact_id, rewrite, error}]}` — 순서는 `contact_ids`와 같고, `rewrite`는 단건 응답과 같은 모양(이력에도 단건처럼 저장), 실패한 상대만 `error: {code, message}`(단건의 오류 코드)이고 나머지는 계속한다. 상대 1명 = 요청 1건으로 한도를 센다. 멱등 키는 상대마다 `<키>:<contact_id>`로 파생. 빈 목록·5개 초과·중복은 422. 모든 상대에 똑같이 걸리는 입력 오류(빈·한도를 넘는 초안, 긴 상황 설명, 저장 못 할 문자, 91자를 넘거나 보이지 않는 문자가 든 키)도 단건처럼 요청 전체가 422이고 아무것도 처리하지 않는다 — 항목별 `error`는 상대마다 다를 수 있는 것(없는 상대, 저장된 설정 없음, 한도 초과, 모델 실패)만이다 |
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

화면 `/admin`. API는 `GET /v1/admin/*`와 쓰기(`POST /v1/admin/graph/retry-held`, 회원관리의 PATCH·POST·DELETE — 쿠키 인증이면 `X-CSRF-Token` 필수). 접근: 로그인(세션 쿠키 또는 Bearer) + 관리자(아이디가 `ADMIN_USERNAMES`에 대소문자까지 같게 있거나 `role`이 `admin`). 미로그인 401 · 관리자 아님 403 `forbidden` · `X-User-Id`만으로는 401 · 인메모리 저장소 503 `admin_unavailable`.
통과한 호출은 `admin_access_log`에 남는다. **대화 원문·첨삭 문장·자유 서술 사유·편집본은 어떤 응답에도 없고**, 사용자 id는 `u_8f2…a1`로 줄인다.

| 경로 | 내용 |
|---|---|
| `GET /v1/admin/overview?days=1\|7\|30` | 기간 요약: `users`, `rewrites`(성공률, `by_condition`·`by_channel`·`by_day`), `latency_ms`(p50·p95), `fallback_reasons`, `feedback`, `tokens`, `graph`, `limits`, `health` |
| `GET /v1/admin/users?limit=50&offset=0` | `{total, items[]}` 최근 가입 순 |
| `GET /v1/admin/jobs?limit=50` | 최근 가져오기 작업. `graph_error`는 오류 종류만 |
| `GET /v1/admin/feedback?limit=50` | 최근 거절 피드백 (선택형 사유만) |
| `GET /v1/admin/access-log?limit=50` | 관리자 접근 기록 |
| `POST /v1/admin/graph/retry-held` | 본문 없음 → `{released, users, started}`. 거듭 실패해 보류된 메시지**만** 풀고(실패 횟수·보류 표시를 지운다 — 그래프와 이미 들어간 메시지는 그대로) 그 사용자들의 수집을 백그라운드로 다시 시작한다. 바로 응답하며 진행은 `overview`의 `graph.pending_messages`·`held_messages`로 본다. 메시지마다 모델 호출 비용이 들고 수집 상한을 따른다(예산이 없으면 미수집으로 남아 나중에 이어진다). 풀 것이 없으면 0건·`started=false`, 그래프가 꺼진 서버는 409 `graph_disabled`. 접근 기록에 `graph_retry_held`와 결과(`graph_retry_held_done`, `released=N users=M`)가 남는다 |

회원관리 (2026-11-16) — 여기서만 사용자 id 전체와 이메일이 나간다:

| 경로 | 내용 |
|---|---|
| `GET /v1/admin/members?query=&status=&role=&limit=50&offset=0` | `{total, counts: {active, pending, suspended}, items[]}` 최근 가입 순. `query`는 아이디·이메일·표시 이름 부분 일치. `counts`는 필터와 무관한 전체 수(승인 대기 배지). 항목: `{user_id, username, email, email_verified, display_name, role, status, is_admin, config_admin, providers[], created_at, last_login_at, consent_version}` |
| `PATCH /v1/admin/members/{user_id}` `{status?: "active"\|"suspended", role?: "user"\|"admin", expected_status?: "active"\|"pending"\|"suspended"}` | 승인(pending→active)·정지·정지 해제·권한 변경 → 바뀐 항목. 정지하면 그 회원의 세션·토큰을 모두 폐기한다. 빈 본문·`pending`·모르는 필드는 422. `expected_status`(선택)를 보내면 그 회원의 **지금 상태가 그 값일 때만** 바꾼다 — 다르면 409 `member_changed`(아무것도 바꾸지 않는다). 화면은 줄을 그릴 때 본 상태를 늘 보내고, 409면 목록을 다시 읽는다 |
| `POST /v1/admin/members/{user_id}/revoke-sessions` | 그 회원의 세션 전부 폐기 → `{revoked_sessions}` |
| `DELETE /v1/admin/members/{user_id}` | 계정과 자료 삭제(되돌릴 수 없다) → `{contacts, conversations, messages, graph_nodes_removed, graph_error}`. 대상이 하나뿐인 활성 관리자면 409 `last_admin` |

- 자기 보호: 관리자는 자기 자신을 정지·권한 내리기·삭제할 수 없다(409 `cannot_modify_self`). `ADMIN_USERNAMES`의 계정(`config_admin: true`)은 누구도 이 API로 정지·삭제할 수 없다(409 `protected_admin`).
- 마지막 관리자(2026-11-17): 권한 내리기·정지·삭제로 **활성 관리자**가 0명이 되면 409 `last_admin`이고 아무것도 바뀌지 않는다. 활성 관리자 = 상태가 `active`이고, `role`이 `admin`이거나 아이디가 `ADMIN_USERNAMES`에 있는 **실제로 있는** 계정(이름만 적혀 있고 계정이 없으면 세지 않는다. 정지·승인 대기 계정도 세지 않는다). 검사는 변경과 같은 DB 트랜잭션에서 한다 — 두 관리자가 동시에 서로를 내리거나 지워도 한 명은 남는다. 본인 탈퇴(`DELETE /v1/auth/me`)도 같은 규칙이다.
- 기록(`admin_access_log`): 하려는 일을 **바꾸기 전에** 적고(`members` · `member_approve` · `member_suspend` · `member_activate` · `member_role` · `member_revoke_sessions` · `member_delete`), 끝나면 결과를 한 줄 더 적는다 — `<이름>_done`(정지·세션 폐기는 `sessions=<수>`, 삭제는 지운 수) 또는 `<이름>_failed reason=<오류 코드\|예외 종류>`. 서버 규칙에 걸린 시도(409)는 `member_refused` `op=update\|delete status=… role=… reason=cannot_modify_self\|protected_admin\|member_changed`. 대상은 줄인 id(`target=u_8f2…a1`)로만 적는다 — 아이디·이메일은 적지 않는다(계정을 지운 뒤에도 기록에 개인정보가 남지 않게).

- 기간은 사용량과 같은 하루 경계(KST)의 달력 날짜 N일(오늘 포함). 표본이 없으면 `success_rate`·`acceptance_rate`·`latency_ms`는 null.
- `tokens`는 `usage_counters` 기준. `estimated_cost_usd`는 `COST_*` 단가가 0이면 null(숫자를 지어내지 않는다).
- `fallback_reasons[].count`는 요청 수. `by_reason_code`는 거절만, 사유 미선택은 `unspecified`. `graph.jobs`·`pending_messages`·`held_messages`(거듭 실패해 보류된 메시지 — `POST /v1/admin/graph/retry-held`로 다시 시도)는 기간과 무관한 현재 상태.

## 오류 모양

`{"code": "...", "message": "사람이 읽는 한국어"}`가 최상위에 온다. 예전에 `{"detail": …}`이던 응답은 `detail`도 그대로 있다(하위 호환).
요청 검증 실패(422)는 `details: [{"loc": "body.username", "msg": "…"}]`가 붙는다(도메인 규칙 위반 422에는 없다). 5xx는 `{"code":"internal_error","message","request_id"}`이고 같은 값이 `X-Request-ID` 응답 헤더에 있다(운영자가 로그에서 찾는 열쇠).

| 상태 | code |
|---|---|
| 400 | `bad_request`, `invalid_host`(허용되지 않은 Host) |
| 401 | `unauthorized` |
| 403 | `forbidden`(CSRF·권한·가입 닫힘), `invalid_invite`, `account_pending`, `account_suspended` |
| 404 | `not_found` (남의 것도 404) |
| 409 | `conflict` (버전 충돌, 같은 키·다른 본문, 이미 있는 이름), `graph_disabled` (그래프가 꺼진 서버에서 보류 다시 시도), `cannot_modify_self`·`protected_admin`(회원관리) |
| 413 | `payload_too_large` |
| 422 | `invalid_input`(NUL·깨진 유니코드가 든 본문도 저장 전에 여기서 끝난다), `consent_required`, `quota_exceeded`(사용자별 저장 한도) |
| 429 | `usage_exceeded`(사용자 하루 한도), `budget_exceeded`(서비스 전체 하루 한도), `rate_limited`, `too_many_attempts` — 모두 `Retry-After`가 붙는다(하루 한도는 한국 시간 자정까지 남은 초) |
| 503 | `model_unavailable`, `admin_unavailable`, `graph_unavailable`(화자 역할 고치기: 그래프에 닿지 못해 바꾸지 않았다) |

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
