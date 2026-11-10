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

export const JOB_STATUS_LABEL = { completed: "완료", failed: "실패" };

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
};

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
