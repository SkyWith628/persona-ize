// 관리자 화면의 표시 규칙. DOM을 쓰지 않는 순수 함수만 둔다 (tests/admin-format.test.js가 node로 검사한다).
// 원칙: 표본이 0이면 0%가 아니라 "자료 없음", 숫자에는 단위, 모르는 값은 지어내지 않는다.

const NUMBER = new Intl.NumberFormat("ko-KR");
const WEEKDAYS = "일월화수목금토";

export const NO_DATA = "자료 없음";

/** 1234567 → "1,234,567" */
export function num(value) {
  const n = Number(value);
  return NUMBER.format(Number.isFinite(n) ? n : 0);
}

/** 비율(0~1) → "96.2%". 표본이 없어 null이면 null을 그대로 돌려준다(호출한 쪽이 "자료 없음"을 쓴다). */
export function percent(rate) {
  if (rate === null || rate === undefined || !Number.isFinite(rate)) return null;
  return `${(rate * 100).toFixed(1).replace(/\.0$/, "")}%`;
}

/** 전체에 대한 몫(0~1). 전체가 0이면 null. */
export function share(part, whole) {
  return whole > 0 ? part / whole : null;
}

/** 밀리초 → "850ms" / "7.4초" / "12초". 값이 없으면 null. */
export function duration(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  return `${seconds >= 10 ? Math.round(seconds) : seconds.toFixed(1)}초`;
}

/** 달러 금액. 단가가 없어 null이면 null. 아주 작은 금액은 0으로 뭉개지 않는다. */
export function usd(value) {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  if (value === 0) return "$0";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  if (value < 1000) return `$${value.toFixed(2)}`;
  return `$${NUMBER.format(Math.round(value))}`;
}

function pad(n) {
  return String(n).padStart(2, "0");
}

/** "2026-11-05" → "11.05" (막대 아래 눈금) */
export function dayShort(isoDay) {
  const [, month, day] = String(isoDay).split("-");
  return `${Number(month)}.${day}`;
}

/** "2026-11-05" → "11월 5일(목)" */
export function dayLong(isoDay) {
  const [year, month, day] = String(isoDay).split("-").map(Number);
  const weekday = WEEKDAYS[new Date(Date.UTC(year, month - 1, day)).getUTCDay()];
  return `${month}월 ${day}일(${weekday})`;
}

/** ISO 시각 → 보는 사람의 시간대로 "11.11 14:32". 올해가 아니면 연도를 붙인다. 값이 없으면 "–". */
export function dateTime(iso, now = new Date()) {
  if (!iso) return "–";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return "–";
  const body = `${pad(t.getMonth() + 1)}.${pad(t.getDate())} ${pad(t.getHours())}:${pad(t.getMinutes())}`;
  return t.getFullYear() === now.getFullYear() ? body : `${t.getFullYear()}.${body}`;
}

/** 마지막 갱신 시각 "14:32:05" */
export function clock(date) {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** 기간 안내 문장. 날짜는 서버가 정한 하루 경계(기본 한국 시간)를 따른다. */
export function rangeText(overview) {
  const days = overview.rewrites.by_day;
  if (!days.length) return "";
  const offset = String(overview.range.from).slice(-6); // "+09:00"
  const zone = offset === "+09:00" ? "한국 시간 기준" : `하루 경계 UTC${offset}`;
  const first = dayLong(days[0].day);
  if (days.length === 1) return `오늘 ${first} · ${zone}`;
  return `${first} ~ ${dayLong(days[days.length - 1].day)} · ${days.length}일 · ${zone}`;
}

/** 막대 아래 날짜 눈금을 몇 칸마다 적을지. 30일이면 5칸마다. */
export function axisStep(count) {
  if (count <= 10) return 1;
  if (count <= 20) return 2;
  return 5;
}

export const CHECK_LABEL = {
  preserve_missing: "지켜야 할 내용이 빠짐",
  fact_added: "원문에 없는 사실이 추가됨",
  negation_changed: "가능·불가가 뒤바뀜",
  forbidden_phrase: "금지 표현 사용",
  evidence_unknown: "참고 표현 표시가 맞지 않음",
  empty: "모델 응답 없음 (장애·시간 초과)",
  refusal: "모델이 거절함",
  unknown: "사유 기록 없음",
  other: "기타",
};

export const REASON_LABEL = {
  too_stiff: "너무 딱딱함",
  too_casual: "너무 가벼움",
  too_servile: "저자세로 느껴짐",
  not_my_style: "평소 말투와 다름",
  fact_changed: "사실이 바뀜",
  too_long: "너무 김",
  too_short: "너무 짧음",
  other: "기타",
  unspecified: "사유 미선택",
};

export const RELATION_LABEL = {
  superior: "상사",
  colleague: "동료",
  subordinate: "후배",
  friend: "친구",
  family: "가족",
  partner: "연인",
  acquaintance: "지인·거래처",
};

export const CHANNEL_LABEL = { web: "웹", mobile: "모바일", desktop: "PC 프로그램", api: "API" };

export const CONDITION_LABEL = {
  A: "A 기준선",
  B: "B 페르소나",
  C: "C 내 과거 표현",
  D: "D 사건 맥락",
};

export const JOB_STATUS_LABEL = { processing: "처리 중", completed: "완료", failed: "실패" };

// 작업 종류. 화자 역할 고치기는 메시지를 넣지 않고 묶음만 다시 만든다 — 가져오기로 세지 않는다.
export const JOB_KIND_LABEL = { import: "가져오기", speaker_roles: "역할 고치기" };

export const GRAPH_STATUS_LABEL = {
  skipped: "안 함",
  pending: "대기",
  running: "진행 중",
  completed: "완료",
  failed: "실패",
  limited: "상한 도달",
};

export const ACTION_LABEL = {
  overview: "요약",
  users: "사용자",
  jobs: "가져오기 작업",
  feedback: "거절 피드백",
  access_log: "접근 기록",
  graph_held: "보류 목록",
  graph_retry_held: "보류 다시 시도",
  graph_retry_held_done: "보류 다시 시도 결과",
  // 회원 관리 (member_admin_service.py가 남기는 이름)
  members: "회원 목록",
  // 하려던 일 → 결과(_done) 또는 실패(_failed, detail에 reason=코드). 서버 규칙에 걸린 시도는 member_refused.
  member_approve: "가입 승인",
  member_approve_done: "가입 승인 완료",
  member_approve_failed: "가입 승인 실패",
  member_suspend: "계정 정지",
  member_suspend_done: "계정 정지 완료",
  member_suspend_failed: "계정 정지 실패",
  member_activate: "정지 해제",
  member_activate_done: "정지 해제 완료",
  member_activate_failed: "정지 해제 실패",
  member_role: "권한 변경",
  member_role_done: "권한 변경 완료",
  member_role_failed: "권한 변경 실패",
  member_revoke_sessions: "세션 끊기",
  member_revoke_sessions_done: "세션 끊기 완료",
  member_revoke_sessions_failed: "세션 끊기 실패",
  member_delete: "회원 삭제",
  member_delete_done: "회원 삭제 결과",
  member_delete_failed: "회원 삭제 실패",
  member_refused: "회원 관리 거절",
};

// ---------- 보류된 그래프 메시지 다시 시도 ----------

/**
 * 수집 표의 보류 줄: 건수, 꼬리표, 다시 시도 버튼을 낼지.
 * 보류가 없으면 버튼이 없다. 그래프가 꺼진 서버는 다시 넣을 곳이 없어(서버가 409) 버튼을 내지 않는다.
 */
export function heldView(graph) {
  const raw = Number(graph?.held_messages ?? 0);
  const count = Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : 0;
  if (count === 0) return { count, canRetry: false, tag: { kind: "ok", text: "없음" } };
  if (!graph.enabled) return { count, canRetry: false, tag: { kind: "off", text: "수집이 꺼져 다시 시도할 수 없음" } };
  return { count, canRetry: true, tag: { kind: "warn", text: "다시 시도할 수 있음" } };
}

/**
 * 누르기 전에 받는 확인. 메시지 1건 = 모델 호출 1건(에피소드)이라 건수만큼 비용이 든다.
 * `group`(보류 목록의 한 줄)을 주면 그 대화방만 푼다고 말한다.
 */
export function retryConfirmText(count, group = null) {
  const n = num(count);
  const what = group ? `대화방 ${shortConversationId(group.conversation_id)}의 보류 ${n}건` : `보류 ${n}건`;
  return `${what}을 다시 시도합니다. 모델 호출 비용이 ${n}건만큼 듭니다.\n계속할까요?`;
}

/** 대화방 id(UUID)의 앞 8자. 표에서 줄을 구분하는 데만 쓴다(전체 값은 다시 시도 요청에만 싣는다). */
export function shortConversationId(id) {
  const raw = String(id ?? "");
  return raw.length > 8 ? `${raw.slice(0, 8)}…` : raw || "–";
}

/** 다시 시도 요청의 본문. 줄을 주면 그 대화방만, 없으면 본문 없이(전체) 보낸다. */
export function retryBody(group = null) {
  return group ? { conversation_id: group.conversation_id } : undefined;
}

/** 보류 목록 한 줄의 표시값. 건수가 이상하면 0으로 본다(버튼도 내지 않는다). */
export function heldGroupView(group, canRetry = true) {
  const raw = Number(group?.messages ?? 0);
  const count = Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : 0;
  const tries = Number(group?.attempts ?? 0);
  return {
    count,
    conversation: shortConversationId(group?.conversation_id),
    attempts: Number.isFinite(tries) && tries > 0 ? `${num(Math.floor(tries))}번` : "–",
    canRetry: Boolean(canRetry) && count > 0 && Boolean(group?.conversation_id),
  };
}

/** 서버 응답({released, users, started}) → 결과 안내. */
export function retryResultText(result) {
  const released = Number(result?.released ?? 0);
  if (!(released > 0)) return "다시 시도할 보류 메시지가 없습니다.";
  const who = `보류 ${num(released)}건(사용자 ${num(result.users ?? 0)}명)을 풀었습니다.`;
  return `${who} 사건 정리를 뒤에서 다시 시작했습니다 — 메시지마다 10~15초쯤 걸리고, 수집 상한에 닿은 사용자는 여유가 생기면 이어집니다.`;
}

/** 다시 시도가 실패했을 때의 안내. 403은 권한 문제일 수도, 오래된 화면(CSRF 토큰)일 수도 있다. */
export function retryErrorText(error) {
  if (error?.status === 409 && error.code === "graph_disabled") {
    return "사건 정리(그래프)가 꺼진 서버라 다시 시도할 수 없습니다.";
  }
  if (error?.status === 403) {
    return "다시 시도하지 못했습니다. 권한이 없거나 로그인 정보가 바뀌었습니다 — 화면을 새로 고친 뒤 다시 누르세요.";
  }
  return `다시 시도하지 못했습니다. ${error?.message ?? ""}`.trim();
}

/** 코드 → 화면 문구. 표에 없는 코드는 코드를 그대로, 값이 없으면 "–". */
export function label(map, code) {
  if (code === null || code === undefined || code === "") return "–";
  return Object.hasOwn(map, code) ? map[code] : String(code);
}

/**
 * 비용 카드에 적을 값과 설명.
 * 단가가 없는 쪽은 금액을 만들지 않는다. 둘 다 없으면 "단가 미설정"과 토큰 수만 보여 준다.
 */
export function costSummary(overview) {
  const rewrite = overview.tokens.estimated_cost_usd;
  const graph = overview.graph.estimated_cost_usd;
  const tokens = overview.tokens.input + overview.tokens.output;
  const ingested = overview.graph.ingested_messages;
  if (rewrite === null && graph === null) {
    const extra = ingested > 0 ? ` · 사건 정리 ${num(ingested)}건` : "";
    return { value: "단가 미설정", known: false, sub: `토큰 ${num(tokens)}${extra}` };
  }
  if (rewrite !== null && graph !== null) {
    return {
      value: usd(rewrite + graph),
      known: true,
      sub: `첨삭 ${usd(rewrite)} + 사건 정리 ${usd(graph)} (추정)`,
    };
  }
  if (rewrite !== null) {
    const missing = ingested > 0 ? ` · 사건 정리 ${num(ingested)}건은 단가 미설정` : "";
    return { value: usd(rewrite), known: true, sub: `첨삭 토큰 ${num(tokens)} (추정)${missing}` };
  }
  return {
    value: usd(graph),
    known: true,
    sub: `사건 정리만 (추정) · 첨삭은 단가 미설정, 토큰 ${num(tokens)}`,
  };
}

/** 연결 점검 문자열("ok" | "skipped" | "error: …") → 표시 종류와 문구. */
export function healthView(value) {
  if (value === "ok") return { kind: "ok", text: "정상" };
  if (value === "skipped") return { kind: "off", text: "사용 안 함" };
  const detail = String(value ?? "").replace(/^error:\s*/, "") || "알 수 없음";
  return { kind: "bad", text: `오류 (${detail})` };
}

/**
 * 접근 기록을 보기 좋게 묶는다. 화면을 한 번 열면 호출이 여러 개 나가므로,
 * 같은 관리자가 1분 안에 이어서 한 호출은 한 줄로 합친다. rows는 최근 것이 먼저다.
 */
export function groupAccess(rows, windowMs = 60_000) {
  const groups = [];
  let current = null;
  let previousTime = 0;
  for (const row of rows) {
    const time = new Date(row.created_at).getTime();
    const joins =
      current !== null && current.admin_username === row.admin_username && previousTime - time <= windowMs;
    if (!joins) {
      current = { admin_username: row.admin_username, newest: row.created_at, oldest: row.created_at, count: 0, actions: [] };
      groups.push(current);
    }
    current.count += 1;
    current.oldest = row.created_at;
    if (!current.actions.includes(row.action)) current.actions.push(row.action);
    previousTime = time;
  }
  return groups;
}

/** 접근 검사에서 막힌 경우(401·403·503)의 안내. 그 밖의 오류는 null — 화면을 유지하고 오류만 알린다. */
export function gateFor(error) {
  if (error.status === 401) {
    return {
      title: "로그인이 필요합니다",
      text: "관리자 계정으로 로그인한 뒤 이 주소를 다시 여세요.",
      link: "로그인하러 가기",
    };
  }
  if (error.status === 403) {
    return {
      title: "관리자만 볼 수 있는 화면입니다",
      text: "지금 로그인한 계정에는 관리자 권한이 없습니다. 관리자 계정으로 다시 로그인하세요.",
      link: "서비스 화면으로",
    };
  }
  if (error.status === 503 && error.code === "admin_unavailable") {
    return {
      title: "PostgreSQL 저장소에서만 볼 수 있습니다",
      text: "이 서버는 임시(인메모리) 저장소로 떠 있어 집계할 자료가 없습니다. 데이터베이스를 연결한 서버에서 여세요.",
      link: "서비스 화면으로",
    };
  }
  return null;
}

// ---------- 회원 관리 ----------
// 서버 규칙(member_admin_service.py)을 화면에서도 미리 지킨다: 자기 자신은 정지·권한 내리기·삭제를 못 하고,
// ADMIN_USERNAMES로 지정된 관리자(config_admin)는 정지·삭제를 못 한다. 그래도 서버가 마지막에 다시 검사한다.

export const MEMBER_STATUS_VIEW = {
  active: { kind: "ok", text: "사용 중" },
  pending: { kind: "warn", text: "승인 대기" },
  suspended: { kind: "bad", text: "정지" },
};

export const PROVIDER_LABEL = { password: "비밀번호", google: "Google", kakao: "카카오" };

/** 상태 코드 → 꼬리표. 모르는 값은 코드를 그대로 보여 준다(지어내지 않는다). */
export function memberStatusView(status) {
  return Object.hasOwn(MEMBER_STATUS_VIEW, status)
    ? MEMBER_STATUS_VIEW[status]
    : { kind: "off", text: status ? String(status) : "–" };
}

/** 권한 칸의 문구. ADMIN_USERNAMES로 지정된 계정은 role과 상관없이 관리자다. */
export function memberRoleText(member) {
  if (member?.config_admin) return "관리자 (서버 설정)";
  return member?.role === "admin" || member?.is_admin ? "관리자" : "일반";
}

/** 로그인 수단 목록 → "비밀번호 · Google". 없으면 "–". */
export function providersText(providers) {
  const list = Array.isArray(providers) ? providers : [];
  return list.length ? list.map((p) => label(PROVIDER_LABEL, p)).join(" · ") : "–";
}

/** 목록 요청의 쿼리 문자열. 빈 검색어·필터는 싣지 않는다. 검색어는 서버 한도(100자)로 자른다. */
export function membersQuery({ query = "", status = "", role = "", limit = 50, offset = 0 } = {}) {
  const params = new URLSearchParams();
  const q = String(query ?? "").trim().slice(0, 100);
  if (q) params.set("query", q);
  if (status) params.set("status", status);
  if (role) params.set("role", role);
  params.set("limit", String(limit));
  params.set("offset", String(Math.max(0, offset)));
  return params.toString();
}

/** 상태 필터 버튼의 글자: "승인 대기 3". 수를 모르면(아직 못 읽음) 이름만. */
export function memberFilterText(status, counts) {
  if (!status) return "전체";
  const name = memberStatusView(status).text;
  const n = counts?.[status];
  return Number.isFinite(n) ? `${name} ${num(n)}` : name;
}

/** 위쪽 바로 가기의 글자. 승인을 기다리는 사람이 있으면 그 수를 함께 적는다. */
export function membersJumpText(counts) {
  const pending = Number(counts?.pending ?? 0);
  return pending > 0 ? `회원 관리 · 승인 대기 ${num(pending)}명` : "회원 관리";
}

/** 쪽 안내: "1–50 / 조건에 맞는 회원 132명". */
export function membersPageText(offset, shown, total, filtered = false) {
  const who = filtered ? "조건에 맞는 회원" : "전체 회원";
  if (!shown) return `${who} ${num(total)}명`;
  return `${num(offset + 1)}–${num(offset + shown)} / ${who} ${num(total)}명`;
}

/** 마지막 쪽의 시작 위치. 삭제로 지금 쪽이 비었을 때 돌아갈 곳. */
export function lastPageOffset(total, limit) {
  if (!(total > 0) || !(limit > 0)) return 0;
  return Math.floor((total - 1) / limit) * limit;
}

/** 이 회원에게 막힌 일이 있으면 그 까닭(버튼 아래에 적는다). 없으면 null. */
export function memberLockNote(member, selfId = null) {
  if (selfId && member.user_id === selfId) {
    return "내 계정입니다 — 여기서는 정지·관리자 해제·삭제를 할 수 없습니다.";
  }
  if (member.config_admin) {
    return "서버 설정(ADMIN_USERNAMES)으로 지정된 관리자입니다 — 정지·삭제할 수 없습니다.";
  }
  return null;
}

// [버튼 글자, 낭독·안내에 쓰는 온전한 이름]
const MEMBER_ACTION_NAME = {
  approve: ["승인", "가입 승인"],
  suspend: ["정지", "계정 정지"],
  activate: ["정지 해제", "정지 해제"],
  promote: ["관리자 지정", "관리자 지정"],
  demote: ["관리자 해제", "관리자 권한 해제"],
  revoke: ["세션 끊기", "세션 모두 끊기"],
  delete: ["삭제", "회원 삭제"],
};

/**
 * 한 회원의 줄에 낼 버튼들. 순서: 승인(대기일 때) → 정지/정지 해제 → 관리자 지정/해제 → 세션 끊기 → 삭제.
 * blocked가 있으면 누를 수 없는 버튼이고 그 값이 까닭이다. ariaLabel은 "누구에게 무엇을"을 말한다.
 */
export function memberActions(member, selfId = null) {
  const isSelf = Boolean(selfId) && member.user_id === selfId;
  const selfBlock = isSelf ? "내 계정" : null;
  const configBlock = member.config_admin ? "서버 설정 관리자" : null;
  const list = [];
  if (member.status === "pending") list.push({ key: "approve", primary: true });
  if (member.status === "suspended") list.push({ key: "activate" });
  else list.push({ key: "suspend", blocked: selfBlock ?? configBlock });
  if (member.role === "admin") list.push({ key: "demote", blocked: selfBlock });
  else list.push({ key: "promote" });
  list.push({ key: "revoke" });
  list.push({ key: "delete", danger: true, blocked: selfBlock ?? configBlock });
  return list.map((item) => {
    const [short, full] = MEMBER_ACTION_NAME[item.key];
    const blocked = item.blocked ?? null;
    return {
      key: item.key,
      label: short,
      ariaLabel: `${member.username} 님 ${full}${blocked ? ` (할 수 없음: ${blocked})` : ""}`,
      primary: item.primary === true,
      danger: item.danger === true,
      blocked,
    };
  });
}

/**
 * 버튼 하나가 하는 일: 보낼 요청(method·path·body)과 그 전에 받을 확인(confirm).
 * 삭제는 아이디를 그대로 입력해야 확인 버튼이 눌린다(requireText).
 * 상태를 바꾸는 요청(승인·정지·정지 해제)은 이 줄을 그릴 때의 상태를 expected_status로 함께 보낸다 —
 * 그 사이 다른 관리자가 상태를 바꿨으면 서버가 409 member_changed로 거절한다(오래된 화면의 "승인"이
 * 방금 걸린 정지를 풀지 않게).
 */
export function memberActionPlan(key, member, selfId = null) {
  const name = member.username;
  const path = `/v1/admin/members/${encodeURIComponent(member.user_id)}`;
  const isSelf = Boolean(selfId) && member.user_id === selfId;
  const configNote = member.config_admin
    ? " 서버 설정(ADMIN_USERNAMES)으로 지정된 관리자라, 이 값과 상관없이 관리자로 남습니다."
    : "";
  switch (key) {
    case "approve":
      return {
        method: "PATCH",
        path,
        body: { status: "active", expected_status: member.status },
        confirm: {
          title: "가입을 승인할까요?",
          message: `${name} 님이 로그인해 서비스를 쓸 수 있게 됩니다.`,
          confirmLabel: "승인",
        },
      };
    case "activate":
      return {
        method: "PATCH",
        path,
        body: { status: "active", expected_status: member.status },
        confirm: {
          title: "정지를 풀까요?",
          message: `${name} 님이 다시 로그인할 수 있게 됩니다. 자료는 정지 전 그대로입니다.`,
          confirmLabel: "정지 해제",
        },
      };
    case "suspend":
      return {
        method: "PATCH",
        path,
        body: { status: "suspended", expected_status: member.status },
        confirm: {
          title: "계정을 정지할까요?",
          message: `${name} 님은 로그인할 수 없게 되고, 지금 로그인된 기기에서도 모두 로그아웃됩니다. 자료는 지워지지 않고, 언제든 정지를 풀 수 있습니다.`,
          confirmLabel: "정지",
          danger: true,
        },
      };
    case "promote":
      return {
        method: "PATCH",
        path,
        body: { role: "admin" },
        confirm: {
          title: "관리자로 지정할까요?",
          message: `${name} 님이 이 대시보드를 보고 회원을 승인·정지·삭제할 수 있게 됩니다.${configNote}`,
          confirmLabel: "관리자 지정",
        },
      };
    case "demote":
      return {
        method: "PATCH",
        path,
        body: { role: "user" },
        confirm: {
          title: "관리자 권한을 해제할까요?",
          message: `${name} 님은 일반 회원으로 계속 쓸 수 있지만 이 대시보드에는 들어올 수 없게 됩니다.${configNote}`,
          confirmLabel: "관리자 해제",
          danger: true,
        },
      };
    case "revoke":
      return {
        method: "POST",
        path: `${path}/revoke-sessions`,
        body: undefined,
        confirm: {
          title: "세션을 모두 끊을까요?",
          message: isSelf
            ? "내 계정입니다. 지금 이 화면을 포함해 모든 기기에서 로그아웃되고, 다시 로그인해야 합니다."
            : `${name} 님이 로그인한 모든 기기에서 로그아웃됩니다. 다시 로그인하면 계속 쓸 수 있습니다.`,
          confirmLabel: "세션 끊기",
          danger: true,
        },
      };
    case "delete":
      return {
        method: "DELETE",
        path,
        body: undefined,
        confirm: {
          title: "회원을 삭제할까요?",
          message: `${name} 님의 계정과 아래 자료가 모두 지워집니다. 되돌릴 수 없고, 지운 뒤에는 복구할 방법이 없습니다.`,
          bullets: [
            "등록한 상대와 말투 설정",
            "가져온 대화 원문과 검색 자료",
            "지난 약속·사건 기록(관계 그래프)",
            "첨삭 이력·피드백·사용량 기록, 로그인 세션",
          ],
          confirmLabel: "영구 삭제",
          danger: true,
          requireText: name,
          requireLabel: `확인하려면 이 회원의 아이디 ${name} 을(를) 그대로 입력하세요`,
        },
      };
    default:
      throw new Error(`unknown member action: ${key}`);
  }
}

/** 성공 안내. result는 서버 응답(PATCH는 회원, 세션 끊기는 {revoked_sessions}, 삭제는 지운 수). */
export function memberActionDoneText(key, member, result) {
  const name = member.username;
  switch (key) {
    case "approve":
      return `${name} 님의 가입을 승인했습니다.`;
    case "activate":
      return `${name} 님의 정지를 풀었습니다.`;
    case "suspend":
      return `${name} 님을 정지했습니다. 로그인된 기기에서도 로그아웃됐습니다.`;
    case "promote":
      return `${name} 님을 관리자로 지정했습니다.`;
    case "demote":
      return `${name} 님의 관리자 권한을 해제했습니다.`;
    case "revoke":
      return `${name} 님의 세션 ${num(result?.revoked_sessions ?? 0)}개를 끊었습니다.`;
    case "delete": {
      const counts = `상대 ${num(result?.contacts ?? 0)}명 · 대화방 ${num(result?.conversations ?? 0)}개 · 메시지 ${num(result?.messages ?? 0)}건`;
      const graph = result?.graph_error
        ? ` 다만 관계 그래프는 지우지 못했습니다(${result.graph_error}) — 서버 로그를 확인하세요.`
        : "";
      return `${name} 님을 삭제했습니다 (${counts}).${graph}`;
    }
    default:
      return "처리했습니다.";
  }
}

/** 실패 안내. 서버 규칙에 걸린 경우(409)와 오래된 화면(403·404·409 member_changed)을 구분해 알려 준다. */
export function memberActionErrorText(key, error) {
  const what = (MEMBER_ACTION_NAME[key] ?? ["요청", "요청"])[1];
  if (error?.status === 409 && error.code === "cannot_modify_self") {
    return "자기 자신은 정지하거나 관리자 권한을 내리거나 삭제할 수 없습니다. 다른 관리자에게 요청하세요 (내 계정 삭제는 서비스 화면의 ‘내 데이터’에서 합니다).";
  }
  if (error?.status === 409 && error.code === "protected_admin") {
    return "서버 설정(ADMIN_USERNAMES)으로 지정된 관리자는 정지하거나 삭제할 수 없습니다. 먼저 서버 설정에서 이름을 빼야 합니다.";
  }
  if (error?.status === 409 && error.code === "member_changed") {
    return "그 사이 다른 관리자가 이 회원의 상태를 바꿨습니다. 아무것도 바꾸지 않았습니다 — 목록을 새로 읽었으니 지금 상태를 확인한 뒤 다시 누르세요.";
  }
  if (error?.status === 409 && error.code === "last_admin") {
    return "마지막 관리자는 정지하거나 권한을 내리거나 삭제할 수 없습니다. 먼저 다른 관리자를 지정하세요.";
  }
  if (error?.status === 404) {
    return "회원을 찾지 못했습니다. 이미 삭제됐을 수 있습니다 — 목록을 새로 읽었습니다.";
  }
  if (error?.status === 403) {
    return `${what}에 실패했습니다. 권한이 없거나 로그인 정보가 바뀌었습니다 — 화면을 새로 고친 뒤 다시 누르세요.`;
  }
  return `${what}에 실패했습니다. ${error?.message ?? ""}`.trim();
}
