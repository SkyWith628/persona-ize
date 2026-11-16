// 운영 콘솔 (와이어프레임 A1). 같은 출처의 세션 쿠키 + CSRF 토큰으로 /v1/auth, /v1/admin API를 부른다.
// 화면은 DOM API와 textContent로만 만든다. 대화 원문·첨삭 문장은 API가 주지 않으며 여기서도 다루지 않는다.

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

/** 요소 만들기. props: class, text, dataset, 그 밖은 속성(true면 빈 속성, null/false면 생략). */
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  node.append(...children.filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

// ── 표시 문구 ────────────────────────────────────────────────────────────────

const SECTIONS = {
  dashboard: { title: "운영 대시보드", sub: "품질과 비용을 먼저 봅니다. 대화 원문과 첨삭 문장은 이 화면에 나오지 않습니다.", overview: true },
  users: { title: "사용자", sub: "가입 승인, 정지, 권한, 세션, 삭제를 처리합니다." },
  quality: { title: "품질 · 피드백", sub: "추천 성공률과 원문을 유지한 사유, 사용자가 거절한 추천을 봅니다.", overview: true },
  cost: { title: "비용 · 사용량", sub: "토큰 사용량과 하루 한도에 닿은 사용자를 봅니다.", overview: true },
  jobs: { title: "수집 작업", sub: "사건 정리(그래프 수집) 대기열과 저장소 상태를 봅니다.", overview: true },
  privacy: { title: "개인정보 · 삭제", sub: "관리자가 볼 수 있는 범위와 회원 삭제가 지우는 것." },
  audit: { title: "접근 기록", sub: "관리자가 회원 정보를 열람하거나 바꾼 기록입니다." },
};

const REASONS = {
  preserve_missing: "날짜·수량·약속 등 지켜야 할 내용 누락",
  fact_added: "초안에 없던 내용 추가",
  negation_changed: "가능/불가 뒤바뀜",
  forbidden_phrase: "금지 표현 사용",
  evidence_unknown: "근거 표시 오류",
  empty: "빈 응답",
  refusal: "모델 거절",
  unknown: "사유 기록 없음 (모델 장애·시간 초과 등)",
};

const STATUS = {
  active: { label: "사용 중", tone: "" },
  pending: { label: "승인 대기", tone: "warn" },
  suspended: { label: "정지", tone: "bad" },
};
const PROVIDERS = { password: "비밀번호", google: "Google", kakao: "카카오" };

const AUDIT_ACTIONS = {
  "user.approve": "가입 승인",
  "user.suspend": "계정 정지",
  "user.activate": "정지 해제",
  "user.role": "권한 변경",
  "user.revoke_sessions": "세션 모두 끊기",
  "user.delete": "회원 삭제",
  "users.view": "회원 목록 열람",
};
const AUDIT_DETAIL_KEYS = { status: "상태", role: "권한", revoked: "끊은 세션", query: "검색어", from: "이전", to: "이후" };
const AUDIT_DETAIL_VALUES = { active: "사용 중", pending: "승인 대기", suspended: "정지", user: "일반", admin: "관리자" };

const AUTH_ERRORS = {
  oauth_denied: "소셜 로그인을 취소했습니다. 다시 시도해 주세요.",
  oauth_failed: "소셜 로그인에 실패했습니다. 잠시 뒤 다시 시도해 주세요.",
  signup_closed: "지금은 새 계정 가입을 받지 않습니다. 이미 가입한 계정으로 로그인해 주세요.",
  account_suspended: "정지된 계정입니다. 관리자에게 문의해 주세요.",
  account_pending: "아직 승인되지 않은 계정입니다. 승인된 뒤 다시 들어와 주세요.",
};

const HEALTH_NAMES = { db: "DB", database: "DB", postgres: "DB", graph: "그래프 DB", neo4j: "그래프 DB" };

const PAGE_SIZE = 20;
const AUDIT_LIMIT = 100;

// ── 형식 ────────────────────────────────────────────────────────────────────

const numberFormat = new Intl.NumberFormat("ko-KR");
const number = (value) => (typeof value === "number" && Number.isFinite(value) ? numberFormat.format(value) : "–");
const percent = (rate) => (rate === null || rate === undefined ? "–" : `${(rate * 100).toFixed(1)}%`);
const seconds = (ms) => (ms === null || ms === undefined ? "–" : `${(ms / 1000).toFixed(1)}초`);
const compactFormat = new Intl.NumberFormat("ko-KR", { notation: "compact", maximumFractionDigits: 1 });
/** 타일의 큰 숫자용: 10만 이상은 "1,290만"처럼 줄인다. 정확한 값은 옆 설명에 둔다. */
const compact = (value) => (typeof value === "number" && Math.abs(value) >= 100000 ? compactFormat.format(value) : number(value));
const share = (part, whole) => (whole > 0 ? part / whole : null);

function parseDate(iso) {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

const shortDate = (date) => date.toLocaleDateString("ko-KR", { year: "2-digit", month: "2-digit", day: "2-digit" });

function relative(date) {
  const diff = Math.floor((Date.now() - date.getTime()) / 1000);
  if (diff < 60) return "방금 전";
  if (diff < 3600) return `${Math.floor(diff / 60)}분 전`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}시간 전`;
  if (diff < 7 * 86400) return `${Math.floor(diff / 86400)}일 전`;
  return shortDate(date);
}

/** <time> 요소. mode: relative(N분 전) | date(날짜만) | full(날짜+시각). 없으면 대체 문구. */
function timeEl(iso, mode, fallback = "–") {
  const date = parseDate(iso);
  if (!date) return el("span", { class: "muted", text: fallback });
  const full = date.toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });
  let text = full;
  if (mode === "relative") text = relative(date);
  else if (mode === "date") text = shortDate(date);
  else if (mode === "full") text = date.toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "medium" });
  return el("time", { datetime: date.toISOString(), title: full, text });
}

// ── 상태 ────────────────────────────────────────────────────────────────────

const state = {
  me: null,
  csrf: null,
  section: "dashboard",
  days: 7,
  overview: null,
  overviewSeq: 0,
  health: null,
  pendingCount: null,
  users: { query: "", status: "", role: "", offset: 0, total: 0, items: [], seq: 0, loaded: false },
  auditSeq: 0,
};

// ── API ─────────────────────────────────────────────────────────────────────

class ApiError extends Error {
  constructor(status, code, message, handled = false) {
    super(message);
    this.status = status;
    this.code = code;
    this.handled = handled; // 이미 화면 전환(로그인·권한 없음)으로 처리한 오류
  }
}

/** 오류 본문에서 사람이 읽을 문장을 꺼낸다. {error:{code,message}} · {code,message} · FastAPI {detail} 모두 받는다. */
function readError(status, body) {
  const inner = body && typeof body === "object" ? (body.error && typeof body.error === "object" ? body.error : body) : {};
  let message = typeof inner.message === "string" ? inner.message : null;
  if (!message && body && typeof body.detail === "string") message = body.detail;
  if (!message && body && Array.isArray(body.detail) && body.detail[0] && typeof body.detail[0].msg === "string") {
    message = `입력값을 확인해 주세요. (${body.detail[0].msg})`;
  }
  if (!message) {
    if (status === 401) message = "로그인이 필요합니다.";
    else if (status === 403) message = "이 작업을 할 권한이 없습니다.";
    else if (status === 404) message = "대상을 찾지 못했습니다. 이미 삭제됐을 수 있습니다.";
    else if (status === 429) message = "요청이 너무 많습니다. 잠시 뒤 다시 시도해 주세요.";
    else if (status >= 500) message = "서버에서 오류가 났습니다. 서버 로그를 확인해 주세요.";
    else message = `요청을 처리하지 못했습니다. (${status})`;
  }
  return { code: typeof inner.code === "string" ? inner.code : null, message };
}

/**
 * JSON API 호출. 쓰기 요청에는 X-CSRF-Token을 붙인다.
 * gate=true(기본)이면 401은 로그인 화면으로, 읽기 요청의 403은 권한 없음 화면으로 넘긴다.
 */
async function api(path, { method = "GET", body, gate = true } = {}) {
  const headers = { Accept: "application/json" };
  const write = method !== "GET";
  if (write && state.csrf) headers["X-CSRF-Token"] = state.csrf;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "network", "서버에 연결하지 못했습니다. 잠시 뒤 다시 시도해 주세요.");
  }
  let data = null;
  if (response.status !== 204) {
    try {
      data = await response.json();
    } catch {
      data = null;
    }
  }
  if (response.ok) return data;
  const { code, message } = readError(response.status, data);
  if (gate && response.status === 401) {
    showLogin("세션이 끝났습니다. 다시 로그인해 주세요.", "info");
    throw new ApiError(401, code, message, true);
  }
  if (gate && response.status === 403 && !write) {
    showDenied();
    throw new ApiError(403, code, message, true);
  }
  throw new ApiError(response.status, code, message);
}

// ── 알림 ────────────────────────────────────────────────────────────────────

let toastTimer = 0;
function toast(message, kind = "ok") {
  const box = $("#toast");
  box.textContent = message;
  box.className = kind === "error" ? "toast error" : "toast";
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    box.hidden = true;
  }, kind === "error" ? 7000 : 4000);
}

function sectionError(message) {
  const box = $("#section-error");
  box.textContent = message ?? "";
  box.hidden = !message;
}

/** 화면 전환으로 이미 처리한 오류는 넘기고, 나머지만 보여 준다. */
function report(error, show = toast) {
  if (error instanceof ApiError && error.handled) return;
  const message = error instanceof ApiError ? error.message : "화면을 그리다 오류가 났습니다. 새로 고쳐 주세요.";
  if (!(error instanceof ApiError)) console.error(error);
  if (show === toast) toast(message, "error");
  else show(message);
}

// ── 로그인 관문 ─────────────────────────────────────────────────────────────

function showGate(which) {
  $("#app").hidden = true;
  $("#gate").hidden = false;
  $("#gate-loading").hidden = which !== "loading";
  $("#gate-login").hidden = which !== "login";
  $("#gate-denied").hidden = which !== "denied";
  const dialog = $("#confirm");
  if (dialog.open) dialog.close("cancel");
}

function clearData() {
  state.overview = null;
  state.health = null;
  state.pendingCount = null;
  state.users = { query: "", status: "", role: "", offset: 0, total: 0, items: [], seq: state.users.seq + 1, loaded: false };
  state.overviewSeq += 1;
  state.auditSeq += 1;
  $("#users-body").replaceChildren();
  $("#audit-body").replaceChildren();
  $("#users-query").value = "";
  $("#users-status").value = "";
  $("#users-role").value = "";
}

function showLogin(message, kind = "error") {
  state.me = null;
  state.csrf = null;
  clearData();
  const box = $("#gate-message");
  box.textContent = message ?? "";
  box.className = kind === "info" ? "banner info" : "banner";
  box.hidden = !message;
  $("#login-password").value = "";
  showGate("login");
  document.title = "로그인 · Persona_ize 운영";
  ($("#login-username").value ? $("#login-password") : $("#login-username")).focus();
  loadProviders();
}

function showDenied() {
  clearData();
  $("#denied-user").textContent = state.me?.display_name || state.me?.username || "현재";
  showGate("denied");
  document.title = "권한 없음 · Persona_ize 운영";
  $("#denied-title").focus();
}

async function loadProviders() {
  let providers;
  try {
    providers = await api("/v1/auth/providers", { gate: false });
  } catch {
    return; // 옛 서버(404)거나 연결 실패면 소셜 로그인 버튼을 감춘 채 둔다.
  }
  const google = providers?.google === true;
  const kakao = providers?.kakao === true;
  $("#login-google").hidden = !google;
  $("#login-kakao").hidden = !kakao;
  $("#login-social").hidden = !(google || kakao);
}

function adopt(account) {
  state.me = account;
  state.csrf = account.csrf_token ?? null;
}

function enterApp(moveFocus = false) {
  // is_admin이 false로 분명히 오면 바로 막는다. 필드가 없는 옛 서버는 관리자 API의 403으로 가린다.
  if (state.me.is_admin === false) return showDenied();
  $("#who-name").textContent = `${state.me.display_name || state.me.username} 님`;
  $("#gate").hidden = true;
  $("#app").hidden = false;
  route(moveFocus);
}

async function boot() {
  const params = new URLSearchParams(location.search);
  const authError = params.get("auth_error");
  if (authError !== null) {
    params.delete("auth_error");
    const rest = params.toString();
    history.replaceState(null, "", location.pathname + (rest ? `?${rest}` : "") + location.hash);
  }
  showGate("loading");
  try {
    adopt(await api("/v1/auth/me", { gate: false }));
  } catch (error) {
    if (error.status === 401) {
      const message = authError === null ? null : (AUTH_ERRORS[authError] ?? "로그인하지 못했습니다. 다시 시도해 주세요.");
      return showLogin(message);
    }
    if (error.status === 403 && authError === null) return showLogin(error.message);
    return showLogin(authError !== null ? (AUTH_ERRORS[authError] ?? error.message) : error.message);
  }
  enterApp();
}

async function onLogin(event) {
  event.preventDefault();
  const username = $("#login-username").value.trim();
  const password = $("#login-password").value;
  const box = $("#gate-message");
  if (!username || !password) {
    box.textContent = "아이디(또는 이메일)와 비밀번호를 입력해 주세요.";
    box.className = "banner";
    box.hidden = false;
    (username ? $("#login-password") : $("#login-username")).focus();
    return;
  }
  const submit = $("#login-submit");
  submit.disabled = true;
  submit.textContent = "로그인하는 중…";
  try {
    adopt(await api("/v1/auth/login", { method: "POST", body: { username, password }, gate: false }));
    $("#login-password").value = "";
    box.hidden = true;
    enterApp(true);
  } catch (error) {
    box.textContent = error instanceof ApiError ? error.message : "로그인하지 못했습니다.";
    box.className = "banner";
    box.hidden = false;
    $("#login-password").focus();
  } finally {
    submit.disabled = false;
    submit.textContent = "로그인";
  }
}

async function logout() {
  try {
    await api("/v1/auth/logout", { method: "POST", gate: false });
  } catch (error) {
    if (error.status !== 401) return toast(error.message, "error");
  }
  $("#login-username").value = "";
  showLogin("로그아웃했습니다.", "info");
}

// ── 메뉴·라우팅 ─────────────────────────────────────────────────────────────

function currentSection() {
  const name = location.hash.replace(/^#/, "");
  return Object.hasOwn(SECTIONS, name) ? name : "dashboard";
}

function route(moveFocus = true) {
  if (!state.me || $("#app").hidden) return;
  const name = currentSection();
  const info = SECTIONS[name];
  state.section = name;
  for (const view of $$(".view")) view.hidden = view.id !== `view-${name}`;
  for (const link of $$("#menu a")) {
    if (link.dataset.section === name) {
      link.setAttribute("aria-current", "page");
      link.scrollIntoView({ block: "nearest", inline: "nearest" });
    } else link.removeAttribute("aria-current");
  }
  $("#page-title").textContent = info.title;
  $("#page-sub").textContent = info.sub;
  $("#range").hidden = !info.overview;
  document.title = `${info.title} · Persona_ize 운영`;
  sectionError(null);
  if (moveFocus) {
    window.scrollTo(0, 0);
    $("#page-title").focus({ preventScroll: true });
  }
  if (info.overview || name === "privacy") {
    if (state.overview && state.overview.days === state.days) renderOverview();
    else loadOverview();
  }
  if (name === "users") loadUsers();
  if (name === "audit") loadAudit();
}

// ── 집계(대시보드·품질·비용·수집) ───────────────────────────────────────────

function tile(label, value, hint, link) {
  return el(
    "div",
    { class: "tile" },
    el("span", { class: "label", text: label }),
    el("strong", { text: value }),
    el("span", { class: "hint", text: hint ?? "" }),
    link ? el("a", { href: link.href, text: link.text }) : null,
  );
}

function card(title, aside, ...children) {
  const heading = el("h2", { text: title });
  if (aside) heading.append(" ", el("span", { class: "muted", text: aside }));
  return el("section", { class: "card" }, heading, ...children);
}

function statusList(rows) {
  const list = el("dl", { class: "status" });
  for (const [term, value, tone] of rows) {
    const dd = el("dd", { class: tone || null });
    dd.append(value);
    list.append(el("div", {}, el("dt", { text: term }), dd));
  }
  return list;
}

/** 막대 목록. items: [{label, value, text}] — 길이는 가장 큰 값 기준. */
function bars(items) {
  const most = Math.max(1, ...items.map((item) => item.value));
  const list = el("ul", { class: "bars" });
  for (const item of items) {
    const fill = el("span", { class: "fill" });
    fill.style.width = `${Math.max(0, Math.min(100, (item.value / most) * 100))}%`;
    list.append(
      el("li", {}, el("span", { text: item.label }), el("span", { class: "n", text: item.text }), el("span", { class: "track" }, fill)),
    );
  }
  return list;
}

const notRecorded = (text) => el("p", { class: "note", text: `아직 기록하지 않는 항목: ${text}` });

function reasonsCard(data, aside) {
  if (!data.fallback_reasons.length) {
    return card("원문 유지 사유", aside, el("p", { class: "muted", text: "이 기간에 원문을 유지한 첨삭이 없습니다." }));
  }
  const total = data.fallback_reasons.reduce((sum, reason) => sum + reason.count, 0);
  const items = data.fallback_reasons.map((reason) => ({
    label: REASONS[reason.check] ?? reason.check,
    value: reason.count,
    text: `${number(reason.count)}건 · ${percent(share(reason.count, total))}`,
  }));
  return card("원문 유지 사유", aside, bars(items));
}

function conditionsCard(data, withRate) {
  const head = el("tr", {}, el("th", { scope: "col", text: "조건" }), el("th", { scope: "col", class: "num", text: "요청" }), el("th", { scope: "col", class: "num", text: "원문 유지" }));
  if (withRate) head.append(el("th", { scope: "col", class: "num", text: "원문 유지 비율" }));
  const body = el("tbody");
  for (const row of data.by_condition) {
    const tr = el("tr", {}, el("th", { scope: "row", text: row.condition }), el("td", { class: "num", text: number(row.requests) }), el("td", { class: "num", text: number(row.kept) }));
    if (withRate) tr.append(el("td", { class: "num", text: percent(share(row.kept, row.requests)) }));
    body.append(tr);
  }
  if (!data.by_condition.length) return card("조건별", null, el("p", { class: "muted", text: "이 기간에 첨삭 요청이 없습니다." }));
  return card("조건별", withRate ? "(검색 조건 A~D)" : null, el("div", { class: "scroll" }, el("table", {}, el("thead", {}, head), body)));
}

function rejectionsCard(data) {
  const section = card("최근 거절한 추천", "(나답지 않아요)");
  section.classList.add("wide");
  if (!data.recent_rejections.length) {
    section.append(el("p", { class: "muted", text: "이 기간에 거절한 추천이 없습니다." }));
  } else {
    const body = el("tbody");
    for (const row of data.recent_rejections) {
      body.append(
        el("tr", {}, el("th", { scope: "row", text: row.user }), el("td", { text: row.condition }), el("td", { text: row.has_reason ? "있음" : "없음" }), el("td", {}, timeEl(row.at, "relative"))),
      );
    }
    const head = el("tr", {}, ...["사용자", "조건", "사유 입력", "시각"].map((text) => el("th", { scope: "col", text })));
    section.append(el("div", { class: "scroll" }, el("table", {}, el("thead", {}, head), body)));
  }
  section.append(el("p", { class: "muted small", text: "사용자는 가린 표시입니다. 사유 글은 보여 주지 않고 입력 여부만 봅니다." }));
  return section;
}

function tokenBars(data) {
  const total = data.input_tokens + data.output_tokens;
  return bars([
    { label: "입력 토큰", value: data.input_tokens, text: `${number(data.input_tokens)} · ${percent(share(data.input_tokens, total))}` },
    { label: "출력 토큰", value: data.output_tokens, text: `${number(data.output_tokens)} · ${percent(share(data.output_tokens, total))}` },
  ]);
}

const limitText = (data) =>
  data.daily_rewrite_limit ? `${number(data.users_at_limit_today)}명 (하루 ${number(data.daily_rewrite_limit)}회)` : "한도 없음";

function healthRows() {
  const health = state.health;
  if (!health) return [["DB · 그래프 DB", "확인 중", ""]];
  if (health.failed) return [["DB · 그래프 DB", "확인 실패", "broken"]];
  const word = (value) => (value === "ok" ? "정상" : value === "skipped" ? "사용 안 함" : "오류");
  const tone = (value) => (value === "ok" ? "ok" : value === "skipped" ? "" : "broken");
  const entries = Object.entries(health.checks);
  if (!entries.length) return [["DB · 그래프 DB", health.status ?? "–", health.ok ? "ok" : "broken"]];
  return entries.map(([name, value]) => [HEALTH_NAMES[name] ?? name, word(value), tone(value)]);
}

function jobRows(data) {
  const pendingTone = data.graph_enabled && data.graph_pending_messages > 0 ? "attention" : "";
  return [
    ["사건 정리 대기 메시지", data.graph_enabled ? `${number(data.graph_pending_messages)}건` : "그래프 사용 안 함", pendingTone],
    ["수집 실패 작업", `${number(data.graph_failed_jobs)}건`, data.graph_failed_jobs > 0 ? "broken" : ""],
  ];
}

function renderOverview() {
  const data = state.overview;
  if (!data) return;
  const users = data.users && typeof data.users === "object" ? data.users : null;
  const tokens = data.input_tokens + data.output_tokens;

  // 대시보드
  const dashTiles = [
    tile("첨삭 요청", number(data.requests), `활성 사용자 ${number(data.active_users)}명`),
    tile("추천 성공률", percent(data.success_rate), `원문 유지 ${number(data.kept)}건`),
    tile("응답 시간 p95", seconds(data.latency_p95_ms), `중앙값 ${seconds(data.latency_p50_ms)}`),
    tile("추천을 받아들인 비율", percent(data.accept_rate), `응답 ${number(data.feedback_total)}건`),
    tile("토큰 (입력 + 출력)", compact(tokens), `입력 ${number(data.input_tokens)} · 출력 ${number(data.output_tokens)}`),
  ];
  if (users) {
    dashTiles.push(
      tile("회원", `${number(users.total)}명`, `승인 대기 ${number(users.pending)}명 · 기간 내 신규 ${number(users.new_in_range)}명`, { href: "#users", text: "사용자 관리로" }),
    );
  }
  $("#dash-tiles").replaceChildren(...dashTiles);

  const privacyRows = users
    ? [
        ["승인 대기 계정", `${number(users.pending)}명`, users.pending > 0 ? "attention" : ""],
        ["정지된 계정", `${number(users.suspended)}명`, ""],
      ]
    : [];
  $("#dash-cards").replaceChildren(
    reasonsCard(data, "(검증 실패)"),
    card(
      "비용 · 사용량",
      null,
      tokenBars(data),
      statusList([["오늘 한도에 닿은 사용자", limitText(data), ""]]),
      notRecorded("금액 환산, 채널(모바일 / PC) 비율"),
    ),
    conditionsCard(data, false),
    card("수집 작업 · 상태", null, statusList([...jobRows(data), ...healthRows()]), notRecorded("키보드 '전체 접근' 꺼짐 비율")),
    card(
      "개인정보 처리",
      null,
      privacyRows.length ? statusList(privacyRows) : null,
      notRecorded("삭제 요청 대기열"),
      el("p", {}, el("a", { class: "button", href: "#privacy", text: "개인정보 · 삭제 보기" }), " ", el("a", { class: "button", href: "#audit", text: "접근 기록 보기" })),
    ),
    rejectionsCard(data),
  );

  // 품질 · 피드백
  $("#quality-tiles").replaceChildren(
    tile("추천 성공률", percent(data.success_rate), `추천 ${number(data.recommended)}건 · 원문 유지 ${number(data.kept)}건`),
    tile("추천을 받아들인 비율", percent(data.accept_rate), `받아들임 ${number(data.feedback_accepted)}건 / 응답 ${number(data.feedback_total)}건`),
    tile("응답 시간 p95", seconds(data.latency_p95_ms), "끝난 첨삭 기준"),
    tile("응답 시간 중앙값", seconds(data.latency_p50_ms), "끝난 첨삭 기준"),
  );
  const reasons = reasonsCard(data, "(검증 실패 · 한 건에 여러 사유가 있을 수 있음)");
  reasons.classList.add("big");
  $("#quality-cards").replaceChildren(
    reasons,
    conditionsCard(data, true),
    rejectionsCard(data),
    el("section", { class: "card wide" }, el("h2", { text: "피드백에서 보지 않는 것" }), notRecorded("상대와의 관계, 채널(모바일 / PC), 선택형 거절 사유. 사유 글은 저장하지만 관리자 화면에는 입력 여부만 나옵니다.")),
  );

  // 비용 · 사용량
  $("#cost-tiles").replaceChildren(
    tile("토큰 합계", compact(tokens), `${number(tokens)} · 최근 ${number(data.days)}일`),
    tile("입력 토큰", compact(data.input_tokens), `${number(data.input_tokens)} · ${percent(share(data.input_tokens, tokens))}`),
    tile("출력 토큰", compact(data.output_tokens), `${number(data.output_tokens)} · ${percent(share(data.output_tokens, tokens))}`),
    tile("요청당 평균 토큰", data.requests ? number(Math.round(tokens / data.requests)) : "–", `첨삭 요청 ${number(data.requests)}건`),
    tile("오늘 한도에 닿은 사용자", data.daily_rewrite_limit ? `${number(data.users_at_limit_today)}명` : "–", data.daily_rewrite_limit ? `하루 ${number(data.daily_rewrite_limit)}회` : "한도 없음"),
  );
  const tokenCard = card("토큰 사용량", "(첨삭 요청 기준)", tokenBars(data));
  tokenCard.classList.add("big");
  $("#cost-cards").replaceChildren(
    tokenCard,
    conditionsCard(data, false),
    el(
      "section",
      { class: "card wide" },
      el("h2", { text: "비용으로 보지 못하는 것" }),
      notRecorded("금액 환산(달러·예산 대비), 모델별 구분(첨삭 / 사건 정리), 채널(모바일 / PC) 비율."),
      el("p", { class: "muted small", text: "토큰 수는 첨삭 요청에서 모델이 보고한 값의 합입니다. 사건 정리(그래프 수집)에 쓴 토큰은 포함하지 않습니다. 실제 청구 금액은 모델 제공사의 콘솔에서 확인하세요." }),
    ),
  );

  // 수집 작업
  $("#jobs-tiles").replaceChildren(
    tile("사건 정리 대기 메시지", data.graph_enabled ? `${number(data.graph_pending_messages)}건` : "–", data.graph_enabled ? "아직 그래프에 넣지 않은 메시지" : "그래프 사용 안 함"),
    tile("수집 실패 작업", `${number(data.graph_failed_jobs)}건`, data.graph_failed_jobs > 0 ? "확인이 필요합니다" : "실패한 작업 없음"),
  );
  $("#jobs-cards").replaceChildren(
    card("저장소 상태", "(지금)", statusList(healthRows())),
    card(
      "대기열",
      null,
      statusList(jobRows(data)),
      el("p", { class: "muted small", text: "대기 중인 메시지는 다음 가져오기나 서버 시작 때 이어서 수집합니다. 실패한 작업은 서버 로그에서 원인을 확인한 뒤 scripts/graph_reset.py로 다시 수집합니다." }),
    ),
    el("section", { class: "card wide" }, el("h2", { text: "수집에서 보지 못하는 것" }), notRecorded("키보드 '전체 접근' 꺼짐 비율, 작업별 진행 목록.")),
  );

  // 개인정보 · 삭제
  $("#privacy-users").replaceChildren(
    ...(users
      ? statusList([
          ["전체 회원", `${number(users.total)}명`, ""],
          ["승인 대기", `${number(users.pending)}명`, users.pending > 0 ? "attention" : ""],
          ["정지", `${number(users.suspended)}명`, ""],
        ]).children
      : [el("div", {}, el("dt", { text: "회원 수 집계를 받지 못했습니다." }), el("dd", { text: "–" }))]),
  );

  const stamp = parseDate(data.generated_at);
  const stampText = `${stamp ? stamp.toLocaleString("ko-KR") : "–"} 기준 · 최근 ${number(data.days)}일${data.truncated ? " · 건수가 많아 최근 2만 건만 집계했습니다" : ""}`;
  for (const target of $$("[data-stamp]")) target.textContent = stampText;

  if (users) setPendingCount(users.pending);
}

async function loadHealth(seq) {
  let health;
  try {
    const response = await fetch("/healthz?deep=true", { credentials: "same-origin" });
    const body = await response.json();
    const checks = body && typeof body.checks === "object" && body.checks ? body.checks : {};
    health = { ok: response.ok, status: body?.status, checks };
  } catch {
    health = { failed: true };
  }
  if (seq !== state.overviewSeq) return;
  state.health = health;
  renderOverview();
}

async function loadOverview() {
  const seq = ++state.overviewSeq;
  for (const button of $$("#range button")) button.setAttribute("aria-pressed", String(Number(button.dataset.days) === state.days));
  try {
    const data = await api(`/v1/admin/overview?days=${state.days}`);
    if (seq !== state.overviewSeq) return;
    state.overview = data;
    sectionError(null);
    renderOverview();
    loadHealth(seq);
  } catch (error) {
    if (seq !== state.overviewSeq) return;
    report(error, sectionError);
  }
}

// ── 사용자(회원관리) ────────────────────────────────────────────────────────

function setPendingCount(count) {
  state.pendingCount = count;
  const button = $("#users-pending");
  button.textContent = count > 0 ? `승인 대기 ${number(count)}명` : "승인 대기 없음";
  const badge = $("#menu-pending");
  badge.hidden = !(count > 0);
  badge.textContent = count > 0 ? number(count) : "";
  if (count > 0) badge.setAttribute("aria-label", `승인 대기 ${number(count)}명`);
}

async function refreshPendingCount() {
  try {
    const data = await api("/v1/admin/users?status=pending&limit=1&offset=0");
    setPendingCount(data.total);
  } catch (error) {
    report(error);
  }
}

function usersUrl() {
  const { query, status, role, offset } = state.users;
  const params = new URLSearchParams();
  if (query) params.set("query", query);
  if (status) params.set("status", status);
  if (role) params.set("role", role);
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(offset));
  return `/v1/admin/users?${params}`;
}

const isSelf = (user) => state.me && user.user_id === state.me.user_id;

function userActions(user) {
  const list = [];
  if (user.status === "pending") list.push({ action: "approve", label: "승인", cls: "primary" });
  if (user.status === "suspended") list.push({ action: "activate", label: "정지 해제" });
  else list.push({ action: "suspend", label: "정지" });
  list.push(user.role === "admin" ? { action: "demote", label: "관리자 해제" } : { action: "promote", label: "관리자 지정" });
  list.push({ action: "revoke", label: "세션 끊기", full: "세션 모두 끊기" });
  list.push({ action: "delete", label: "삭제", cls: "danger" });
  return list;
}

function userRow(user) {
  const self = isSelf(user);
  const status = STATUS[user.status] ?? { label: user.status ?? "–", tone: "plain" };
  const providers = Array.isArray(user.providers) ? user.providers : [];
  const td = (label, props, ...children) => el("td", { ...props, dataset: { label } }, ...children);

  const name = el("th", { scope: "row" }, user.username ?? "–");
  if (self) name.append(" ", el("span", { class: "tag", text: "나" }));

  const roleCell = td("권한", {});
  if (user.role === "admin") roleCell.append(el("span", { class: "tag", text: "관리자" }));
  else if (user.is_admin) roleCell.append(el("span", { class: "tag", text: "관리자" }), " ", el("span", { class: "muted small", text: "서버 설정" }));
  else roleCell.append("일반");

  const actions = el("div", { class: "actions" });
  for (const item of userActions(user)) {
    actions.append(
      el("button", {
        type: "button",
        class: `sm ${item.cls ?? ""}`.trim(),
        dataset: { action: item.action, id: user.user_id },
        "aria-label": `${user.username} ${item.full ?? item.label}`,
        text: item.label,
        disabled: self,
        title: self ? "본인 계정에는 쓸 수 없습니다" : null,
      }),
    );
  }

  return el(
    "tr",
    { class: self ? "self" : null, dataset: { id: user.user_id } },
    name,
    td("이메일", { class: "email", title: user.email || null }, user.email ? user.email : el("span", { class: "muted", text: "없음" })),
    td("이름", {}, user.display_name ? user.display_name : el("span", { class: "muted", text: "–" })),
    td(
      "가입 방법",
      {},
      el("span", { class: "chips" }, ...(providers.length ? providers.map((p) => el("span", { class: "tag plain", text: PROVIDERS[p] ?? p })) : [el("span", { class: "muted", text: "–" })])),
    ),
    td("상태", {}, el("span", { class: `tag ${status.tone}`.trim(), text: status.label })),
    roleCell,
    td("가입일", {}, timeEl(user.created_at, "date")),
    td("마지막 로그인", {}, timeEl(user.last_login_at, "relative", "기록 없음")),
    td("오늘 첨삭", { class: "num" }, number(user.rewrites_today ?? 0)),
    td("상대 수", { class: "num" }, number(user.contacts ?? 0)),
    td("관리", { class: "actions-cell" }, actions),
  );
}

function renderUsers() {
  const { items, total, offset, query, status, role } = state.users;
  $("#users-body").replaceChildren(...items.map(userRow));
  const filtered = Boolean(query || status || role);
  const empty = $("#users-empty");
  empty.hidden = items.length > 0;
  $("#users-table").hidden = items.length === 0;
  if (!items.length) empty.textContent = filtered ? "조건에 맞는 회원이 없습니다. 검색어나 필터를 바꿔 보세요." : "아직 가입한 회원이 없습니다.";
  const first = total ? offset + 1 : 0;
  const last = offset + items.length;
  $("#users-summary").textContent = total ? `${filtered ? "조건에 맞는 회원" : "전체 회원"} ${number(total)}명 중 ${number(first)}–${number(last)}` : "";
  $("#users-page").textContent = total ? `${number(Math.floor(offset / PAGE_SIZE) + 1)} / ${number(Math.max(1, Math.ceil(total / PAGE_SIZE)))} 쪽` : "";
  $("#users-prev").disabled = offset <= 0;
  $("#users-next").disabled = last >= total;
  $("#users-pending").setAttribute("aria-pressed", String(status === "pending"));
}

async function loadUsers() {
  const seq = ++state.users.seq;
  try {
    const data = await api(usersUrl());
    if (seq !== state.users.seq) return;
    const items = Array.isArray(data?.items) ? data.items : [];
    const total = typeof data?.total === "number" ? data.total : items.length;
    // 삭제 등으로 마지막 쪽이 비면 한 쪽 앞으로.
    if (!items.length && total > 0 && state.users.offset > 0) {
      state.users.offset = Math.max(0, (Math.ceil(total / PAGE_SIZE) - 1) * PAGE_SIZE);
      return loadUsers();
    }
    state.users.items = items;
    state.users.total = total;
    state.users.loaded = true;
    sectionError(null);
    renderUsers();
    const { query, status, role } = state.users;
    if (status === "pending" && !query && !role) setPendingCount(total);
    else if (state.pendingCount === null) refreshPendingCount();
  } catch (error) {
    if (seq !== state.users.seq) return;
    // 읽지 못한 목록을 "회원 없음"으로 보이게 두지 않는다.
    state.users.items = [];
    state.users.total = 0;
    renderUsers();
    $("#users-empty").hidden = true;
    report(error, sectionError);
  }
}

function setUserFilter(patch) {
  Object.assign(state.users, patch, { offset: 0 });
  $("#users-status").value = state.users.status;
  $("#users-role").value = state.users.role;
  loadUsers();
}

/** 확인 창. requireText가 있으면 그 글자를 그대로 입력해야 확인 버튼이 켜진다. */
function confirmDialog({ title, body, confirmLabel, danger = false, requireText = null }) {
  const dialog = $("#confirm");
  const ok = $("#confirm-ok");
  const input = $("#confirm-input");
  $("#confirm-title").textContent = title;
  $("#confirm-body").textContent = body;
  ok.textContent = confirmLabel;
  ok.className = danger ? "danger solid" : "primary";
  $("#confirm-type").hidden = !requireText;
  input.value = "";
  input.dataset.expect = requireText ?? "";
  $("#confirm-input-label").textContent = requireText ? `확인을 위해 아이디 ${requireText} 를 입력하세요` : "";
  ok.disabled = Boolean(requireText);
  dialog.returnValue = "";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.showModal();
    (requireText ? input : $("#confirm-cancel")).focus();
  });
}

const ACTIONS = {
  approve: { request: (u) => ["PATCH", `/v1/admin/users/${encodeURIComponent(u.user_id)}`, { status: "active" }], done: (u) => `${u.username} 님의 가입을 승인했습니다.` },
  activate: { request: (u) => ["PATCH", `/v1/admin/users/${encodeURIComponent(u.user_id)}`, { status: "active" }], done: (u) => `${u.username} 님의 정지를 풀었습니다.` },
  suspend: {
    confirm: (u) => ({ title: "계정을 정지할까요?", body: `${u.username} 님은 로그인할 수 없게 되고, 지금 열려 있는 세션도 모두 끊깁니다. 데이터는 지워지지 않으며 언제든 정지를 풀 수 있습니다.`, confirmLabel: "정지", danger: true }),
    request: (u) => ["PATCH", `/v1/admin/users/${encodeURIComponent(u.user_id)}`, { status: "suspended" }],
    done: (u) => `${u.username} 님을 정지했습니다.`,
  },
  promote: {
    confirm: (u) => ({ title: "관리자로 지정할까요?", body: `${u.username} 님이 운영 콘솔에서 회원 목록을 보고 승인·정지·삭제를 할 수 있게 됩니다.`, confirmLabel: "관리자 지정" }),
    request: (u) => ["PATCH", `/v1/admin/users/${encodeURIComponent(u.user_id)}`, { role: "admin" }],
    done: (u) => `${u.username} 님을 관리자로 지정했습니다.`,
  },
  demote: {
    confirm: (u) => ({ title: "관리자 권한을 해제할까요?", body: `${u.username} 님은 더 이상 운영 콘솔에 들어올 수 없습니다. 일반 회원으로는 계속 쓸 수 있습니다.`, confirmLabel: "관리자 해제", danger: true }),
    request: (u) => ["PATCH", `/v1/admin/users/${encodeURIComponent(u.user_id)}`, { role: "user" }],
    done: (u) => `${u.username} 님의 관리자 권한을 해제했습니다.`,
  },
  revoke: {
    confirm: (u) => ({ title: "세션을 모두 끊을까요?", body: `${u.username} 님이 로그인한 모든 기기에서 로그아웃됩니다. 다시 로그인하면 계속 쓸 수 있습니다.`, confirmLabel: "세션 모두 끊기" }),
    request: (u) => ["POST", `/v1/admin/users/${encodeURIComponent(u.user_id)}/revoke-sessions`],
    done: (u, result) => `${u.username} 님의 세션 ${number(result?.revoked ?? 0)}개를 끊었습니다.`,
  },
  delete: {
    confirm: (u) => ({
      title: "회원을 삭제할까요?",
      body: `${u.username} 님의 계정과 상대·페르소나, 가져온 대화, 첨삭 이력, 관계 그래프, 사용량 기록, 세션이 모두 지워집니다.\n되돌릴 수 없습니다.`,
      confirmLabel: "영구 삭제",
      danger: true,
      requireText: u.username,
    }),
    request: (u) => ["DELETE", `/v1/admin/users/${encodeURIComponent(u.user_id)}`],
    done: (u, result) => {
      const counts = result && typeof result.contacts === "number" ? ` (상대 ${number(result.contacts)} · 대화방 ${number(result.conversations)} · 메시지 ${number(result.messages)})` : "";
      return `${u.username} 님을 삭제했습니다.${counts}`;
    },
  },
};

async function runUserAction(action, userId, invoker = null) {
  const spec = ACTIONS[action];
  const user = state.users.items.find((item) => item.user_id === userId);
  if (!spec || !user || isSelf(user)) return;
  if (spec.confirm && !(await confirmDialog(spec.confirm(user)))) {
    // 취소하면 눌렀던 버튼으로 돌아간다 (Safari는 버튼을 눌러도 초점을 주지 않아 직접 옮긴다).
    if (invoker?.isConnected) invoker.focus({ preventScroll: true });
    return;
  }

  const row = $$("#users-body tr").find((tr) => tr.dataset.id === userId);
  const buttons = row ? $$("button", row) : [];
  row?.setAttribute("aria-busy", "true");
  for (const button of buttons) button.disabled = true;

  const [method, path, body] = spec.request(user);
  try {
    const result = await api(path, { method, body });
    const graphError = action === "delete" && result && (result.graph_error || (Array.isArray(result.graph_errors) && result.graph_errors.length));
    toast(spec.done(user, result) + (graphError ? " 다만 그래프 삭제에 실패했습니다. 서버 로그를 확인해 주세요." : ""), graphError ? "error" : "ok");

    if (action === "delete") {
      await loadUsers();
      refreshPendingCount();
      $("#page-title").focus({ preventScroll: true });
      return;
    }
    if (action === "revoke") {
      restoreRow(user, action);
      return;
    }
    const updated = result && typeof result === "object" && result.user_id ? result : null;
    const { status, role } = state.users;
    const stillListed = updated && (!status || updated.status === status) && (!role || updated.role === role);
    if (stillListed) {
      state.users.items = state.users.items.map((item) => (item.user_id === userId ? updated : item));
      restoreRow(updated, action);
    } else {
      // 응답이 회원 정보가 아니거나, 바뀐 뒤 지금 필터에 맞지 않으면 목록을 다시 읽는다.
      await loadUsers();
      $("#page-title").focus({ preventScroll: true });
    }
    if (action === "approve" || action === "suspend" || action === "activate") refreshPendingCount();
  } catch (error) {
    report(error);
    if (error instanceof ApiError && error.handled) return;
    if (error instanceof ApiError && error.status === 404) {
      await loadUsers();
      return;
    }
    restoreRow(user, action);
  }
}

/** 한 줄만 다시 그리고, 눌렀던 자리(없으면 그 줄의 첫 버튼)로 초점을 되돌린다. */
function restoreRow(user, action) {
  const old = $$("#users-body tr").find((tr) => tr.dataset.id === user.user_id);
  if (!old) return;
  const fresh = userRow(user);
  old.replaceWith(fresh);
  const target = $(`button[data-action="${action}"]`, fresh) ?? $("button:not(:disabled)", fresh);
  target?.focus({ preventScroll: true });
}

// ── 접근 기록 ───────────────────────────────────────────────────────────────

function auditDetail(detail) {
  if (detail === null || detail === undefined || detail === "") return "–";
  if (typeof detail !== "object") return String(detail);
  const parts = Object.entries(detail)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => {
      const shown = typeof value === "object" ? JSON.stringify(value) : (AUDIT_DETAIL_VALUES[value] ?? String(value));
      return `${AUDIT_DETAIL_KEYS[key] ?? key}: ${shown}`;
    });
  return parts.length ? parts.join(" · ") : "–";
}

async function loadAudit() {
  const seq = ++state.auditSeq;
  const summary = $("#audit-summary");
  summary.textContent = "불러오는 중…";
  try {
    const data = await api(`/v1/admin/audit?limit=${AUDIT_LIMIT}`);
    if (seq !== state.auditSeq) return;
    const items = Array.isArray(data?.items) ? data.items : [];
    $("#audit-body").replaceChildren(
      ...items.map((item) =>
        el(
          "tr",
          {},
          el("td", {}, timeEl(item.at, "full")),
          el("td", { text: item.admin_username ?? "–" }),
          el("td", { text: AUDIT_ACTIONS[item.action] ?? item.action ?? "–" }),
          el("td", {}, item.target_username ? item.target_username : el("span", { class: "muted", text: "–" })),
          el("td", { text: auditDetail(item.detail) }),
        ),
      ),
    );
    $("#audit-table").hidden = items.length === 0;
    $("#audit-empty").hidden = items.length > 0;
    summary.textContent = items.length ? `최근 ${number(items.length)}건 (최대 ${number(AUDIT_LIMIT)}건)` : "";
    sectionError(null);
  } catch (error) {
    if (seq !== state.auditSeq) return;
    summary.textContent = "";
    report(error, sectionError);
  }
}

// ── 이벤트 연결 ─────────────────────────────────────────────────────────────

$("#login-form").addEventListener("submit", onLogin);
$("#logout").addEventListener("click", logout);
$("#denied-logout").addEventListener("click", logout);
window.addEventListener("hashchange", () => route(true));

for (const button of $$("#range button")) {
  button.addEventListener("click", () => {
    state.days = Number(button.dataset.days);
    loadOverview();
  });
}

let searchTimer = 0;
$("#users-query").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  const value = event.target.value.trim();
  searchTimer = setTimeout(() => {
    if (value !== state.users.query) setUserFilter({ query: value });
  }, 300);
});
$("#users-status").addEventListener("change", (event) => setUserFilter({ status: event.target.value }));
$("#users-role").addEventListener("change", (event) => setUserFilter({ role: event.target.value }));
$("#users-pending").addEventListener("click", () => setUserFilter({ status: state.users.status === "pending" ? "" : "pending" }));
$("#users-prev").addEventListener("click", () => {
  state.users.offset = Math.max(0, state.users.offset - PAGE_SIZE);
  loadUsers();
});
$("#users-next").addEventListener("click", () => {
  state.users.offset += PAGE_SIZE;
  loadUsers();
});
$("#users-body").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (button && !button.disabled) runUserAction(button.dataset.action, button.dataset.id, button);
});
$("#audit-refresh").addEventListener("click", loadAudit);

const confirmInput = $("#confirm-input");
confirmInput.addEventListener("input", () => {
  $("#confirm-ok").disabled = confirmInput.value.trim() !== confirmInput.dataset.expect;
});
confirmInput.addEventListener("keydown", (event) => {
  // Enter는 폼의 첫 제출 버튼(취소)을 누르므로, 입력이 맞을 때만 확인으로 넘긴다.
  if (event.key !== "Enter" || event.isComposing) return;
  event.preventDefault();
  if (!$("#confirm-ok").disabled) $("#confirm").close("ok");
});

boot();
