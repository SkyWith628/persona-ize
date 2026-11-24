// Persona_ize 모바일 웹(/m). 빌드 없는 바닐라 JS(ES 모듈) — 탭 네 개(작성·상대·이력·계정).
//
// - 인증: 같은 출처의 세션 쿠키(HttpOnly) + 쓰기 요청의 X-CSRF-Token (토큰은 로그인 응답과 GET /v1/auth/me에서 받는다).
//   토큰·비밀번호·초안·대화 본문은 주소(URL)나 브라우저 저장소에 넣지 않는다. 저장소에는 탭 이름과 상대 id만 둔다.
// - 화면에 넣는 글은 모두 textContent·텍스트 노드로만 넣는다 (서버·사용자 글을 HTML로 해석하게 하는 길이 없다).
// - 서버 응답을 화면용으로 푸는 순수 로직은 PC 웹 앱의 js/lib/*.js를 그대로 쓴다 (문구·판정이 두 화면에서 같게).
// - 아무것도 자동으로 보내지 않는다. 추천 문장은 복사하거나 초안 칸에 옮길 뿐이다.

import { ApiError, describeError, normalizeError } from "./js/lib/errors.js";
import { formatBytes, formatDateTime, formatNumber, uuid4, withJosa } from "./js/lib/format.js";
import {
  MAX_BODY_BYTES,
  METHODS,
  ROLE_OPTIONS,
  applyKnownRoles,
  checkRoles,
  checkSize,
  decodeBytes,
  encodingLabel,
  graphPhaseView,
  keepRecentFor,
  keptMessages,
  knownRoles,
  previewView,
  stylePhaseView,
  trackProgress,
} from "./js/lib/importing.js";
import {
  CONDITIONS,
  MAX_DRAFT_CHARS,
  REL_ORDER,
  fallbackView,
  metaLine,
  preservedChips,
  relLabel,
  resultMeta,
  rewriteErrorView,
  statusView,
} from "./js/lib/model.js";
import {
  EMOJI_OPTIONS,
  LENGTH_OPTIONS,
  LIMITS,
  SCALES,
  applySignals,
  cardSignals,
  cardView,
  cleanPhrases,
  normalizeSettings,
  signalMatches,
  signalView,
  stepLabel,
  suggestionState,
  visibleSignals,
} from "./js/lib/persona.js";

// ---------- 순수 함수 (DOM 없이 동작) ----------

/** 서버가 아니라 화면에서 정한 안내(입력 확인 등). 문장을 그대로 상태 표시에 보인다. */
export class Notice extends Error {
  constructor(message) {
    super(message);
    this.name = "Notice";
  }
}

export const SIGNUP_CLOSED_TEXT = "지금은 새로 가입할 수 없어요. 이미 계정이 있으면 로그인해 주세요.";
export const SUSPENDED_TEXT = "정지된 계정이에요. 운영자에게 문의해 주세요.";
export const INVITE_TEXT = "초대 코드가 맞지 않아요. 받은 코드를 다시 확인해 주세요.";
export const CONSENT_TEXT = "안내를 확인하고 동의해야 가입할 수 있어요.";

/**
 * 소셜 로그인에서 돌아온 `/m?auth_error=<code>`를 사용자 말로 (코드는 routes/auth.py의 OAUTH_ERROR_CODES + oauth_denied).
 * 모르는 코드는 일반 문구 — 주소에 실려 온 글을 그대로 보이지 않는다.
 */
export function authErrorMessage(code) {
  switch (code) {
    case "oauth_denied": return "소셜 로그인을 취소했어요. 계속하려면 다시 시도해 주세요.";
    case "oauth_failed": return "소셜 로그인을 끝내지 못했어요. 잠시 뒤 다시 시도해 주세요.";
    case "signup_closed": return SIGNUP_CLOSED_TEXT;
    case "invalid_invite": return "처음 로그인하는 계정이라 초대 코드가 필요해요. 초대 코드를 넣고 다시 시도해 주세요.";
    case "consent_required": return "처음 로그인하는 계정이에요. 안내에 동의한다고 체크한 뒤 다시 시도해 주세요.";
    case "account_pending": return "가입 신청은 접수됐어요. 운영자가 승인하면 로그인할 수 있어요.";
    case "account_suspended": return SUSPENDED_TEXT;
    case "email_in_use": return "이 이메일로 가입한 계정이 이미 있어요. 이메일과 비밀번호로 로그인해 주세요.";
    default: return "로그인하지 못했어요. 다시 시도해 주세요.";
  }
}

/**
 * 로그인·가입·`GET /v1/auth/me` 응답(AuthOut·MeOut)을 화면이 쓰는 계정 정보로 바꾼다.
 * `providers`는 `me`에만 있다 — 없으면 방금 비밀번호로 들어온 것이므로 비밀번호로 본다.
 */
export function accountOf(data) {
  const text = (value) => (typeof value === "string" ? value : "");
  const source = data && typeof data === "object" ? data : {};
  return {
    userId: text(source.user_id),
    username: text(source.username),
    email: text(source.email),
    displayName: text(source.display_name),
    isAdmin: source.is_admin === true,
    consentCurrent: source.consent_current !== false,
    consentVersion: text(source.consent_version),
    providers: Array.isArray(source.providers)
      ? source.providers.filter((item) => typeof item === "string")
      : ["password"],
  };
}

/** 로그인 수단 이름. */
export function providerLabel(provider) {
  switch (provider) {
    case "password": return "비밀번호";
    case "google": return "Google";
    case "kakao": return "카카오";
    default: return String(provider ?? "");
  }
}

/**
 * 이메일 가입 입력 검사. 문제가 있는 칸만 사유를 담아 돌려준다(없으면 빈 객체).
 * @param {{email: string, password: string, confirm: string, name: string, invite: string, consent: boolean}} values
 * @param {{invite_required?: boolean, require_consent?: boolean}|null} policy `GET /v1/auth/providers` 응답
 */
export function signupProblems(values, policy = null) {
  const problems = {};
  const email = String(values.email ?? "").trim();
  const password = String(values.password ?? "");
  if (!email) problems.email = "이메일을 입력해 주세요.";
  else if (email.length > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) problems.email = "이메일 주소 형식을 확인해 주세요.";
  if (!password) problems.password = "비밀번호를 입력해 주세요.";
  else if (password.length < 8) problems.password = "비밀번호는 8자 이상이어야 해요.";
  else if (password.length > 128) problems.password = "비밀번호는 128자까지 쓸 수 있어요.";
  if (!values.confirm) problems.confirm = "비밀번호를 한 번 더 입력해 주세요.";
  else if (values.confirm !== password) problems.confirm = "비밀번호 확인이 서로 달라요.";
  if (Array.from(String(values.name ?? "").trim()).length > 40) problems.name = "이름은 40자까지 쓸 수 있어요.";
  if (policy?.invite_required && !String(values.invite ?? "").trim()) problems.invite = "초대 코드를 입력해 주세요.";
  if (policy?.require_consent && !values.consent) problems.consent = CONSENT_TEXT;
  return problems;
}

/**
 * 소셜 로그인 시작 응답의 `authorize_url`을 확인한다. http(s) 주소만 받는다 — 그 밖의 값(javascript: 등)이면 null.
 * @param {unknown} raw
 * @param {string} base 상대 주소를 풀 기준(지금 화면의 출처)
 */
export function safeAuthorizeUrl(raw, base) {
  if (typeof raw !== "string" || !raw) return null;
  try {
    const url = new URL(raw, base);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

/** 최근 상대 기록 (기기에 id만 저장, 최신이 앞). */
export const Recents = {
  limit: 3,

  push(id, stored) {
    if (!id) return stored;
    const rest = String(stored || "").split(",").filter((item) => item && item !== id);
    return [id, ...rest].slice(0, Recents.limit).join(",");
  },

  /** 기록 순서대로 아직 있는 상대를 고르고, 모자라면 목록 앞에서 채운다. */
  pick(stored, contacts) {
    const remembered = [];
    for (const id of String(stored || "").split(",")) {
      const found = contacts.find((contact) => contact.id === id);
      if (found && !remembered.includes(found)) remembered.push(found);
    }
    const filler = contacts.filter((candidate) => !remembered.includes(candidate));
    return [...remembered, ...filler].slice(0, Recents.limit);
  },
};

/** 상대를 가운데에 두고 직접 이어진 노드를 안쪽 원, 나머지를 바깥 원에 놓는다. 좌표는 0~1. */
export const GraphLayout = {
  innerRadius: 0.21,
  staggerRadius: 0.31,
  crowdedFrom: 8,
  outerRadius: 0.39,

  /** @returns {Map<string, {x: number, y: number}>} */
  positions(graph) {
    const result = new Map();
    const center = graph.center_id ?? graph.nodes[0]?.id;
    if (center === undefined || center === null) return result;
    const neighbors = new Map();
    const link = (from, to) => {
      if (!neighbors.has(from)) neighbors.set(from, new Set());
      neighbors.get(from).add(to);
    };
    for (const edge of graph.edges) {
      if (edge.source === edge.target) continue;
      link(edge.source, edge.target);
      link(edge.target, edge.source);
    }
    const point = (angle, radius) => ({ x: 0.5 + radius * Math.cos(angle), y: 0.5 + radius * Math.sin(angle) });
    const others = graph.nodes.map((node) => node.id).filter((id) => id !== center);
    const inner = others.filter((id) => neighbors.get(center)?.has(id));
    const angles = new Map();
    result.set(center, { x: 0.5, y: 0.5 });
    inner.forEach((id, index) => {
      const angle = -Math.PI / 2 + (2 * Math.PI * index) / inner.length;
      angles.set(id, angle);
      // 안쪽 노드가 많으면 한 줄에 다 못 놓는다(이름표가 겹친다) — 하나 걸러 조금 바깥에 둔다.
      const crowded = inner.length > GraphLayout.crowdedFrom && index % 2 === 1;
      result.set(id, point(angle, crowded ? GraphLayout.staggerRadius : GraphLayout.innerRadius));
    });
    // 바깥 노드는 이어진 안쪽 노드 가까이 오도록 그 각도 순으로 늘어놓는다.
    const anchor = (id) => {
      const linked = [...(neighbors.get(id) ?? [])].filter((other) => angles.has(other)).map((other) => angles.get(other));
      return linked.length ? linked.reduce((sum, value) => sum + value, 0) / linked.length : 0;
    };
    const outer = others.filter((id) => !inner.includes(id)).sort((a, b) => anchor(a) - anchor(b));
    outer.forEach((id, index) => {
      const offset = inner.length ? Math.PI / Math.max(outer.length, 2) : 0;
      result.set(id, point(-Math.PI / 2 + offset + (2 * Math.PI * index) / outer.length, GraphLayout.outerRadius));
    });
    return result;
  },

  /** 노드 쌍마다 선 하나. 유효한 사실이 하나라도 있으면 실선(current). */
  links(graph) {
    const pairs = new Map();
    for (const edge of graph.edges) {
      if (edge.source === edge.target) continue;
      const [a, b] = [edge.source, edge.target].sort();
      const key = JSON.stringify([a, b]);
      const known = pairs.get(key);
      pairs.set(key, { a, b, current: Boolean(known?.current) || Boolean(edge.current) });
    }
    return [...pairs.values()];
  },
};

// ---------- DOM 도우미 ----------

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";
let serial = 0;
const uid = (prefix) => `${prefix}-${++serial}`;
/** 주소의 경로 조각 하나로 쓸 값 (id 등). */
const seg = (value) => encodeURIComponent(String(value));

function setProps(el, props) {
  for (const [name, value] of Object.entries(props ?? {})) {
    if (value === null || value === undefined || value === false || name === "value") continue;
    if (name === "class") el.setAttribute("class", value);
    else if (name === "text") el.textContent = value;
    else if (name.startsWith("on")) el.addEventListener(name.slice(2), value);
    else el.setAttribute(name, value === true ? "" : String(value));
  }
}

const kids = (children) => children.flat(3).filter((child) => child !== null && child !== undefined && child !== false);

/** 요소를 만든다. 자식 문자열은 텍스트 노드가 되므로 서버·사용자 글이 HTML로 해석되지 않는다. */
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  setProps(el, props);
  el.append(...kids(children));
  if (props && props.value !== undefined && props.value !== null) el.value = props.value;
  return el;
}

function svg(tag, props, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  setProps(el, props);
  el.append(...kids(children));
  return el;
}

/** 라벨과 입력을 묶는다. */
function field(label, control, { inline = false } = {}) {
  const id = control.id || uid("f");
  control.id = id;
  return h("div", { class: inline ? "field inline" : "field" }, h("label", { for: id, text: label }), control);
}

/** el의 자식을 통째로 바꾼다. null·false는 건너뛴다 (replaceChildren에 그대로 주면 "null"이라는 글자가 된다). */
function fill(el, ...children) {
  el.replaceChildren(...kids(children));
}

const options = (pairs) => pairs.map(([value, label]) => h("option", { value, text: label }));

function card(title, ...children) {
  return h("section", { class: "card" }, title ? h("h2", { class: "card-title", text: title }) : null, ...children);
}

/** 서버가 남긴 확인할 점(warnings). 본문에 섞지 않고 따로 보여 준다. */
function warningBox(warnings, title = "확인해 주세요") {
  const list = (warnings ?? []).filter(Boolean);
  if (!list.length) return null;
  return h("div", { class: "warning", role: "note" },
    h("span", { class: "strong", text: `⚠ ${title}` }),
    h("ul", null, list.map((warning) => h("li", { text: warning }))));
}

// 이 브라우저에 남기는 것: 마지막으로 본 탭(tab), 마지막으로 고른 상대 id(lastContact), 최근 상대 id 셋(recentContacts).
// 토큰·초안·대화 본문·이름은 넣지 않는다. 로그아웃하면 상대 id는 지운다.
const store = {
  get(key) {
    try { return window.localStorage.getItem(`pi.${key}`) ?? ""; } catch { return ""; }
  },
  set(key, value) {
    try { window.localStorage.setItem(`pi.${key}`, value); } catch { /* 저장 못 해도 동작에는 지장 없다 */ }
  },
  remove(key) {
    try { window.localStorage.removeItem(`pi.${key}`); } catch { /* 위와 같음 */ }
  },
};

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

// ---------- 상태 ----------

const TABS = { compose: "작성", contacts: "상대", history: "이력", account: "계정" };
const HOME_PATH = "/m"; // 소셜 로그인 뒤 돌아올 곳 (서버가 같은 출처 경로인지 다시 검사한다)
const REQUEST_TIMEOUT_MS = 120_000;
const PROVIDERS_TIMEOUT_MS = 5_000;
const POLL_MS = 4_000;
const MESSAGES_SHOWN = 100;
const HISTORY_PAGE = 20;
// 요청 처리 중에 잠그는 고정 요소 (로그인·가입·승인 대기·동의 화면과 상단 막대).
const BUSY_LOCKED = ["login-user", "login-pass", "to-signup", "auth-google", "auth-kakao", "social-consent",
  "social-invite", "signup-email", "signup-pass", "signup-pass2", "signup-name", "signup-invite", "signup-consent",
  "signup-go", "to-login", "pending-back", "consent-check", "consent-go", "consent-logout", "back", "refresh"];

/**
 * 로그인한 동안의 연결과 상대 목록. user는 accountOf()의 결과, ready는 탭 화면이 열려 있는지(동의 화면에서는 false).
 * epoch는 로그인·로그아웃마다 올라 늦게 온 응답을 버리는 데 쓴다.
 */
const session = { user: null, ready: false, csrf: "", contacts: [], epoch: 0 };
/** 로그인 화면. policy는 `GET /v1/auth/providers` 응답(읽지 못하면 null), mode는 보이는 양식. */
const gate = { policy: null, mode: "login" };
const ui = { busy: false, notice: "", error: false };
const nav = { tab: "compose", stacks: { compose: [], contacts: [], history: [], account: [] }, roots: {} };

const aliasOf = (id) => session.contacts.find((contact) => contact.id === id)?.alias ?? "삭제된 상대";

// ---------- 상태 표시 · 요청 실행 ----------

function renderBanner() {
  const visible = ui.busy || Boolean(ui.notice);
  $("banner").hidden = !visible;
  $("banner").classList.toggle("error", !ui.busy && ui.error);
  $("banner-spin").hidden = !ui.busy;
  $("banner-close").hidden = ui.busy || !ui.notice;
  const text = ui.busy ? "처리하는 중이에요" : ui.notice;
  if ($("banner-text").textContent !== text) $("banner-text").textContent = text;
}

function notify(message, error = false) {
  ui.notice = message;
  ui.error = error;
  renderBanner();
}

/** 오류를 화면에 보일 문장으로. 서버 오류는 PC 화면과 같은 규칙(기다릴 시간·요청 번호)으로 푼다. */
function messageOf(error) {
  if (error instanceof ApiError) return describeError(error);
  if (error instanceof Notice) return error.message;
  console.error(error);
  return "처리하지 못했어요. 다시 시도해 주세요.";
}

function setBusy(busy) {
  ui.busy = busy;
  $("screens").disabled = busy;
  for (const id of BUSY_LOCKED) $(id).disabled = busy;
  syncLoginButtons();
  renderBanner();
}

/**
 * 화면의 요청 하나를 실행한다. 처리 중에는 중복 탭을 막고 실패 사유를 상태 표시에 남긴다.
 * action은 클릭 처리 안에서 바로 시작되므로(첫 await 전까지 동기) 클립보드 쓰기가 허용된다.
 */
function run(action) {
  if (ui.busy) return;
  const epoch = session.epoch;
  const focused = document.activeElement;
  notify("");
  setBusy(true);
  let pending;
  try { pending = Promise.resolve(action()); } catch (error) { pending = Promise.reject(error); }
  pending.catch((error) => {
    // 세션이 바뀐 뒤(만료·로그아웃) 도착한 실패는 로그인 화면의 안내를 덮지 않는다.
    if (epoch !== session.epoch) return;
    notify(messageOf(error), true);
  }).finally(() => {
    setBusy(false);
    const lost = !document.activeElement || document.activeElement === document.body;
    if (!lost) return;
    if (focused instanceof HTMLElement && focused.isConnected && !focused.disabled && focused.offsetParent !== null) {
      focused.focus({ preventScroll: true });
    } else if (!$("pending").hidden) {
      $("pending-title").focus({ preventScroll: true });
    } else if (!$("consent").hidden) {
      $("consent-title").focus({ preventScroll: true });
    } else if (!$("login").hidden) {
      $(gate.mode === "signup" ? "signup-email" : "login-user").focus({ preventScroll: true });
    }
  });
}

/** 화면을 막지 않는 조회(목록·진행 상태). 실패는 상태 표시에 남기고, 세션이 바뀐 뒤의 실패는 버린다. */
function quiet(action) {
  const epoch = session.epoch;
  Promise.resolve().then(action).catch((error) => {
    if (epoch === session.epoch) notify(messageOf(error), true);
  });
}

// ---------- API ----------

/**
 * 같은 출처의 JSON API를 부른다. 경로·필드는 docs/api.md와 api/routes/*.py를 따른다.
 * @param {string} method
 * @param {string} path
 * @param {{body?: unknown, query?: Record<string, string>, key?: string, signIn?: boolean, verifiesPassword?: boolean,
 *   full?: boolean, timeout?: number}} [options]
 *   key: Idempotency-Key 헤더. signIn: 세션이 없을 수 있는 요청(401이 세션 만료가 아님).
 *   verifiesPassword: 비밀번호를 확인하는 요청(401이 오답일 수 있음). full: `{status, data}`로 돌려준다(201과 202 구분).
 */
async function api(method, path, options = {}) {
  const epoch = session.epoch;
  const headers = { Accept: "application/json" };
  if (method !== "GET" && session.csrf) headers["X-CSRF-Token"] = session.csrf;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.key) headers["Idempotency-Key"] = options.key;
  const url = options.query ? `${path}?${new URLSearchParams(options.query)}` : path;
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), options.timeout ?? REQUEST_TIMEOUT_MS);
  let response;
  let raw;
  try {
    response = await fetch(url, {
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: abort.signal,
    });
    raw = await response.text(); // 본문이 없는 204도 끝까지 읽는다
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new ApiError({ status: 0, code: "timeout", message: "응답이 늦어지고 있어요. 잠시 뒤 다시 시도해 주세요." });
    }
    throw new ApiError(normalizeError(0, null));
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  if (raw) {
    try { data = JSON.parse(raw); } catch { data = null; }
  }
  if (response.ok) return options.full ? { status: response.status, data } : data;
  const failure = new ApiError(normalizeError(response.status, data, response.headers.get("Retry-After")));
  if (response.status === 401 && session.user && epoch === session.epoch && !options.signIn) {
    // 비밀번호 확인 요청의 401은 '비밀번호가 틀림'일 수 있다. 세션이 살아 있으면 서버 문구를 그대로 보인다.
    if (options.verifiesPassword && await sessionAlive()) throw failure;
    if (epoch === session.epoch) endSession("로그인이 만료됐어요. 다시 로그인해 주세요.", true);
  }
  throw failure;
}

async function sessionAlive() {
  try {
    await api("GET", "/v1/auth/me", { signIn: true, timeout: PROVIDERS_TIMEOUT_MS });
    return true;
  } catch (error) {
    return !(error instanceof ApiError && error.status === 401); // 확인하지 못했으면 로그아웃시키지 않는다
  }
}

// ---------- 확인 대화상자 ----------

/**
 * 화면 안 확인 대화상자. 되돌릴 수 없는 동작 앞에 쓴다.
 * @param {{title: string, message?: string, confirm: string, danger?: boolean, input?: string}} config input은 선택 입력의 라벨
 * @returns {Promise<{ok: boolean, value: string}>}
 */
function ask(config) {
  const dialog = $("confirm");
  if (dialog.open) return Promise.resolve({ ok: false, value: "" });
  $("confirm-title").textContent = config.title;
  $("confirm-message").textContent = config.message ?? "";
  $("confirm-message").hidden = !config.message;
  $("confirm-field").hidden = !config.input;
  $("confirm-label").textContent = config.input ?? "";
  $("confirm-input").value = "";
  $("confirm-ok").textContent = config.confirm;
  $("confirm-ok").className = config.danger ? "btn danger solid" : "btn primary";
  return new Promise((resolve) => {
    settleDialog = resolve;
    if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
    (config.input ? $("confirm-input") : $("confirm-cancel")).focus();
  });
}

let settleDialog = null;

/**
 * 대화상자를 닫고 답을 돌려준다. 브라우저의 close 이벤트는 화면이 가려져 있으면 늦게 오므로
 * 그 이벤트를 기다리지 않고 여기서 바로 답한다 (close·cancel 이벤트는 Esc 같은 경로의 보조 수단).
 */
function closeDialog(ok) {
  const dialog = $("confirm");
  const settle = settleDialog;
  settleDialog = null;
  if (dialog.open) {
    if (typeof dialog.close === "function") dialog.close(); else dialog.removeAttribute("open");
  }
  if (settle) settle({ ok, value: $("confirm-input").value });
}

// ---------- 로그인 화면 ----------

/** 로그인 전 화면 넷(로그인·승인 대기·동의·앱) 중 하나만 보인다. */
function showScreen(name) {
  for (const id of ["login", "pending", "consent"]) $(id).hidden = id !== name;
  $("app").hidden = name !== "app";
  document.body.classList.toggle("signed-in", name === "app");
}

function syncLoginButtons() {
  $("login-go").disabled = ui.busy || !$("login-user").value.trim() || !$("login-pass").value;
}

/** 로그인 화면 위쪽의 안내(로그인 실패·소셜 로그인 실패·정지된 계정). 다음 로그인·가입 시도 때 지운다. */
function showAuthAlert(message, focus = false) {
  $("auth-alert").textContent = message;
  $("auth-alert").hidden = !message;
  if (message && focus) $("auth-alert").focus({ preventScroll: false });
}

/**
 * 가입 정책(`GET /v1/auth/providers`)에 맞춰 버튼과 칸을 보인다.
 * 소셜 버튼은 서버가 켜져 있다고 한 것만, 가입 버튼은 가입이 열려 있을 때만, 초대 코드·동의는 서버가 요구할 때만.
 * 정책을 읽지 못했으면(null) 로그인 양식만 남긴다.
 */
function renderGate() {
  const policy = gate.policy;
  const google = policy?.google === true;
  const kakao = policy?.kakao === true;
  const open = policy?.signup_open === true;
  const invite = open && policy?.invite_required === true;
  const consent = open && policy?.require_consent === true;
  if (gate.mode === "signup" && !open) gate.mode = "login";
  $("auth-google").hidden = !google;
  $("auth-kakao").hidden = !kakao;
  $("auth-social").hidden = !google && !kakao;
  // 소셜 로그인으로 새 계정이 만들어질 때만 쓰이는 값. 이미 가입한 사람은 비워 둬도 로그인된다.
  $("social-terms").hidden = !(invite || consent);
  $("social-consent-row").hidden = !consent;
  $("social-invite-row").hidden = !invite;
  $("login-form").hidden = gate.mode !== "login";
  $("signup-form").hidden = gate.mode !== "signup";
  $("to-signup").hidden = !open;
  $("signup-closed").hidden = !policy || open;
  $("policy-missing").hidden = Boolean(policy);
  $("signup-invite-row").hidden = !invite;
  $("signup-consent-row").hidden = !consent;
  $("signup-docs").hidden = consent;
  $("signup-approval").hidden = policy?.requires_approval !== true;
  if (policy?.requires_approval) $("signup-go").setAttribute("aria-describedby", "signup-approval");
  else $("signup-go").removeAttribute("aria-describedby");
}

function setGateMode(mode) {
  if (ui.busy) return;
  gate.mode = mode;
  notify("");
  showAuthAlert("");
  clearSignupErrors();
  for (const id of ["login-pass", "signup-pass", "signup-pass2"]) $(id).value = "";
  syncLoginButtons();
  renderGate();
  $(gate.mode === "signup" ? "signup-email" : "login-user").focus();
}

/** 가입 정책과 켜진 소셜 로그인을 묻는다. 로그인 전에 부르므로 실패는 조용히 넘긴다(null). */
async function loadPolicy() {
  try {
    const data = await api("GET", "/v1/auth/providers", { signIn: true, timeout: PROVIDERS_TIMEOUT_MS });
    return data && typeof data === "object" && !Array.isArray(data) ? data : null;
  } catch {
    return null;
  }
}

/** 주소에 붙어 온 소셜 로그인 실패 코드를 꺼내고 주소에서 지운다 (새로 고침·공유에 남지 않게). 없으면 빈 문자열. */
function takeAuthError() {
  let url;
  try { url = new URL(window.location.href); } catch { return ""; }
  if (!url.searchParams.has("auth_error")) return "";
  const code = url.searchParams.get("auth_error") ?? "";
  url.searchParams.delete("auth_error");
  try { window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`); } catch { /* 주소만 남는다 */ }
  return code || "unknown";
}

const SIGNUP_FIELDS = { email: "signup-email", password: "signup-pass", confirm: "signup-pass2", name: "signup-name",
  invite: "signup-invite", consent: "signup-consent" };

function setSignupError(name, message) {
  const input = $(SIGNUP_FIELDS[name]);
  const note = $(`${SIGNUP_FIELDS[name]}-error`);
  note.textContent = message;
  note.hidden = !message;
  if (message) input.setAttribute("aria-invalid", "true"); else input.removeAttribute("aria-invalid");
}

function clearSignupErrors() {
  for (const name of Object.keys(SIGNUP_FIELDS)) setSignupError(name, "");
}

/** 칸별 사유를 보이고 첫 번째 문제 칸으로 간다. 문제가 없으면 false. */
function showSignupProblems(problems) {
  let first = null;
  for (const name of Object.keys(SIGNUP_FIELDS)) {
    setSignupError(name, problems[name] ?? "");
    if (problems[name] && !first) first = $(SIGNUP_FIELDS[name]);
  }
  if (first) {
    // 처리 중에는 칸이 잠겨 있다. 잠금이 풀린 뒤 그 칸으로 간다.
    setTimeout(() => { if (!first.disabled && first.offsetParent !== null) first.focus(); }, 0);
  }
  return Boolean(first);
}

// ---------- 세션 ----------

/** 아이디 또는 이메일 + 비밀번호 로그인 (`POST /v1/auth/login`, `username`에 둘 중 하나). */
function signIn() {
  const username = $("login-user").value.trim();
  const password = $("login-pass").value;
  if (!username || !password) return;
  showAuthAlert("");
  if (username.length < 3) {
    showAuthAlert("아이디 또는 이메일을 3자 이상 입력해 주세요.", true);
    return;
  }
  run(async () => {
    let auth;
    try {
      auth = await api("POST", "/v1/auth/login", { body: { username, password }, signIn: true });
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      if (error.status === 401 || error.status === 403) {
        $("login-pass").value = "";
        syncLoginButtons();
      }
      // 승인 대기(403 account_pending): 비밀번호는 맞았지만 로그인되지 않았다 → 대기 화면
      if (error.code === "account_pending") { showPending(username); return; }
      let message = describeError(error);
      if (error.code === "account_suspended") message = SUSPENDED_TEXT;
      else if (error.status === 401) message = "아이디(이메일)나 비밀번호가 맞지 않아요.";
      showAuthAlert(message, true);
      return;
    }
    $("login-pass").value = "";
    await enter(auth);
  });
}

/** 이메일 가입 (`POST /v1/auth/signup`). 201이면 로그인된 것이고, 202면 승인 대기(세션 없음)다. */
function signUp() {
  const policy = gate.policy;
  const values = {
    email: $("signup-email").value.trim().toLowerCase(),
    password: $("signup-pass").value,
    confirm: $("signup-pass2").value,
    name: $("signup-name").value.trim(),
    invite: $("signup-invite").value.trim(),
    consent: $("signup-consent").checked,
  };
  showAuthAlert("");
  if (showSignupProblems(signupProblems(values, policy))) return;
  run(async () => {
    const body = { email: values.email, password: values.password };
    if (values.name) body.display_name = values.name;
    if (policy?.invite_required && values.invite) body.invite_code = values.invite;
    if (policy?.require_consent) body.consent = values.consent;
    let out;
    try {
      out = await api("POST", "/v1/auth/signup", { body, signIn: true, full: true });
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      if (error.status === 409) {
        showSignupProblems({ email: "이미 가입된 이메일이에요. 로그인해 주세요." });
      } else if (error.code === "invalid_invite") {
        // 초대 코드 칸이 안 보이던 화면이면(화면을 연 뒤 정책이 바뀜) 칸을 연다.
        gate.policy = { ...(gate.policy ?? {}), signup_open: true, invite_required: true };
        renderGate();
        showSignupProblems({ invite: INVITE_TEXT });
      } else if (error.code === "consent_required") {
        gate.policy = { ...(gate.policy ?? {}), signup_open: true, require_consent: true };
        renderGate();
        showSignupProblems({ consent: CONSENT_TEXT });
      } else if (error.status === 403) {
        // 가입 닫힘(403 forbidden): 화면을 연 뒤에 닫혔다. 양식을 접고 사유를 남긴다.
        gate.policy = { ...(gate.policy ?? {}), signup_open: false };
        for (const id of ["signup-pass", "signup-pass2"]) $(id).value = "";
        renderGate();
        showAuthAlert(SIGNUP_CLOSED_TEXT, true);
      } else {
        showAuthAlert(describeError(error), true);
      }
      return;
    }
    for (const id of ["signup-email", "signup-pass", "signup-pass2", "signup-name", "signup-invite"]) $(id).value = "";
    $("signup-consent").checked = false;
    gate.mode = "login";
    renderGate();
    // 승인제(202 {status: "pending"}): 계정은 만들어졌지만 쿠키도 CSRF 토큰도 없다. 세션이 있다고 가정하지 않는다.
    if (out.status === 202 || out.data?.status === "pending" || !out.data?.csrf_token) {
      showPending(out.data?.email || out.data?.username || values.email);
      return;
    }
    await enter(out.data);
  });
}

/**
 * 소셜 로그인 시작: `POST /v1/auth/oauth/{provider}/start` `{next, consent, invite_code}` → `{authorize_url}`로 이동.
 * 동의·초대 코드는 새 계정이 만들어질 때만 쓰인다. 돌아올 때는 서버가 `/m`(또는 `/m?auth_error=…`)로 보낸다.
 */
function startSocial(provider) {
  showAuthAlert("");
  run(async () => {
    const body = { next: HOME_PATH, consent: $("social-consent").checked };
    const invite = $("social-invite").value.trim();
    if (invite) body.invite_code = invite;
    let out;
    try {
      out = await api("POST", `/v1/auth/oauth/${seg(provider)}/start`, { body, signIn: true });
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      let message = describeError(error);
      if (error.status === 404) {
        // 설정되지 않은 제공자: 버튼을 치운다
        gate.policy = { ...(gate.policy ?? {}), [provider]: false };
        renderGate();
        message = "이 로그인 방식은 지금 쓸 수 없어요.";
      } else if (error.code === "invalid_invite") {
        message = INVITE_TEXT;
      }
      showAuthAlert(message, true);
      return;
    }
    const target = safeAuthorizeUrl(out?.authorize_url, window.location.origin);
    if (!target) throw new Notice("소셜 로그인 주소를 받지 못했어요. 잠시 뒤 다시 시도해 주세요.");
    window.location.assign(target);
    await sleep(10_000); // 이동하는 동안 화면을 잠가 둔다 (이동하지 못하면 풀린다)
  });
}

/** 승인 대기 화면. 로그인된 상태가 아니다 — 세션·CSRF 토큰을 비워 두고 탭은 열지 않는다. */
function showPending(who = "") {
  session.epoch += 1;
  session.user = null;
  session.ready = false;
  session.csrf = "";
  session.contacts = [];
  $("pending-who").textContent = who;
  $("pending-facts").hidden = !who;
  showAuthAlert("");
  notify("");
  showScreen("pending");
  window.scrollTo(0, 0);
  $("pending-title").focus({ preventScroll: true });
}

function leavePending() {
  gate.mode = "login";
  renderGate();
  showScreen("login");
  syncLoginButtons();
  $("login-user").focus();
}

/** 동의를 먼저 받아야 하는가: 서버가 동의를 요구하고, 지금 버전에 동의한 기록이 없을 때만. */
const needsConsent = (user) => gate.policy?.require_consent === true && user.consentCurrent === false;

/** 로그인 응답으로 들어간다. 그 응답에는 로그인 수단(providers)이 없으므로 `GET /v1/auth/me`로 채운다. */
async function enter(auth) {
  session.csrf = auth?.csrf_token ?? "";
  let merged = auth;
  try {
    const me = await api("GET", "/v1/auth/me", { signIn: true });
    merged = { ...auth, ...me };
    if (me?.csrf_token) session.csrf = me.csrf_token;
  } catch { /* 못 받으면 로그인 응답만으로 연다 (로그인 수단은 비밀번호로 본다) */ }
  await admit(accountOf(merged));
}

/** 동의가 필요하면 동의 화면, 아니면 탭 화면. */
async function admit(user) {
  if (needsConsent(user)) showConsent(user);
  else await startSession(user);
}

function showConsent(user) {
  session.epoch += 1;
  session.user = user;
  session.ready = false;
  $("consent-check").checked = false;
  $("consent-error").hidden = true;
  $("consent-lead").textContent = user.consentVersion
    ? "안내 문서가 바뀌었어요. 다시 확인하고 동의해 주세요."
    : `${user.displayName || user.username || "회원"} 님, 쓰기 전에 아래 내용을 확인해 주세요.`;
  showAuthAlert("");
  showScreen("consent");
  window.scrollTo(0, 0);
  $("consent-title").focus({ preventScroll: true });
}

/** 지금 버전의 안내에 동의한다 (`POST /v1/auth/consent` `{}`, CSRF 필요). */
function acceptConsent() {
  $("consent-error").hidden = true;
  if (!$("consent-check").checked) {
    $("consent-error").textContent = "안내를 확인했다고 체크해 주세요.";
    $("consent-error").hidden = false;
    $("consent-check").focus();
    return;
  }
  run(async () => {
    const me = await api("POST", "/v1/auth/consent", { body: {} });
    if (me?.csrf_token) session.csrf = me.csrf_token;
    await startSession(me ? accountOf(me) : session.user);
  });
}

/** 상대 목록을 받은 뒤에 탭 화면을 연다. 실패하면 사유가 상태 표시에 남는다. session.csrf는 먼저 채워 둔다. */
async function startSession(user) {
  const contacts = await api("GET", "/v1/contacts", { signIn: !session.user });
  session.epoch += 1;
  session.user = user;
  session.ready = true;
  session.contacts = Array.isArray(contacts) ? contacts : [];
  for (const id of ["login-user", "login-pass"]) $(id).value = "";
  syncLoginButtons();
  showAuthAlert("");
  mountApp();
}

/** 탭 화면을 비운다 (로그아웃·세션 만료 공통). */
function clearApp() {
  closeDialog(false);
  for (const tab of Object.keys(nav.stacks)) {
    for (const view of nav.stacks[tab]) view.dispose?.();
    nav.stacks[tab] = [];
  }
  for (const root of Object.values(nav.roots)) root.dispose?.();
  nav.roots = {};
  for (const tab of ["contacts", "history", "account"]) fill($(`tab-${tab}`));
  compose.reset();
}

/** 로그아웃. 서버 요청이 실패해도 화면에서는 나간다 (쿠키가 남으면 다음 방문 때 다시 들어와진다). */
function logout() {
  run(async () => {
    try {
      await api("POST", "/v1/auth/logout", { signIn: true });
    } catch { /* 아래에서 화면 상태를 비운다 */ }
    endSession("");
  });
}

/** 화면을 로그인으로 돌리고 이 브라우저에 남긴 상대 기록·입력을 지운다. 서버 세션 정리는 호출한 쪽이 먼저 한다. */
function endSession(message = "", error = false) {
  session.epoch += 1;
  session.user = null;
  session.ready = false;
  session.csrf = "";
  session.contacts = [];
  store.remove("lastContact");
  store.remove("recentContacts");
  clearApp();
  gate.mode = "login";
  clearSignupErrors();
  for (const id of ["login-pass", "signup-pass", "signup-pass2"]) $(id).value = "";
  showAuthAlert("");
  renderGate();
  showScreen("login");
  syncLoginButtons();
  notify(message, error);
  $("login-user").focus();
}

function mountApp() {
  clearApp();
  nav.roots = { contacts: contactsRoot(), history: historyRoot(), account: accountRoot() };
  for (const tab of ["contacts", "history", "account"]) fill($(`tab-${tab}`), nav.roots[tab].el);
  showScreen("app");
  compose.start();
  const remembered = store.get("tab");
  showTab(Object.hasOwn(TABS, remembered) ? remembered : "compose");
}

async function reloadContacts() {
  const epoch = session.epoch;
  const contacts = await api("GET", "/v1/contacts");
  if (epoch !== session.epoch) return;
  session.contacts = Array.isArray(contacts) ? contacts : [];
  compose.sync();
  nav.roots.contacts?.sync();
  nav.roots.history?.sync();
}

/**
 * 시작: 소셜 로그인 실패 코드(`?auth_error=`)를 주소에서 꺼내고, 가입 정책과 로그인 여부를 함께 확인한다.
 * 로그인돼 있으면 (동의 화면을 거쳐) 탭 화면, 아니면 로그인 화면. `account_pending`으로 돌아왔으면 승인 대기 화면.
 */
async function boot() {
  const authError = takeAuthError();
  let me = null;
  let problem = null;
  const policy = loadPolicy();
  try {
    me = await api("GET", "/v1/auth/me", { signIn: true });
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 401) problem = error;
  }
  gate.policy = await policy;
  renderGate();
  $("boot").hidden = true;
  if (me) {
    try {
      session.csrf = me.csrf_token ?? "";
      await admit(accountOf(me));
      return;
    } catch (error) {
      session.csrf = "";
      problem = error;
    }
  }
  if (authError === "account_pending") {
    showPending("");
    return;
  }
  showScreen("login");
  if (authError) showAuthAlert(authErrorMessage(authError), true);
  if (problem) notify(messageOf(problem), true);
}

// ---------- 탭 · 화면 쌓기 ----------

const topView = (tab) => nav.stacks[tab][nav.stacks[tab].length - 1] ?? null;

function renderHeader() {
  const top = topView(nav.tab);
  const current = top ?? nav.roots[nav.tab];
  $("title").textContent = top ? top.title : TABS[nav.tab];
  $("back").hidden = !top;
  $("refresh").hidden = !current?.refresh;
  for (const button of document.querySelectorAll(".tab")) {
    if (button.dataset.tab === nav.tab) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
}

function showTab(tab) {
  const again = nav.tab === tab;
  nav.tab = tab;
  store.set("tab", tab);
  for (const name of Object.keys(TABS)) $(`tab-${name}`).hidden = name !== tab;
  renderHeader();
  if (!again) {
    window.scrollTo(0, 0);
    if (ui.notice && !ui.busy) notify(""); // 안내는 그 화면의 것이다. 다른 탭까지 따라가지 않는다.
  }
  (topView(tab) ? null : nav.roots[tab])?.appear?.();
  if (tab === "compose") compose.appear();
}

/** 탭 안에서 다음 화면으로 들어간다. 이전 화면은 입력을 그대로 둔 채 숨긴다. 주소(URL)는 바꾸지 않는다. */
function pushView(view, tab = nav.tab) {
  const below = topView(tab) ?? nav.roots[tab];
  below.scroll = window.scrollY;
  below.el.hidden = true;
  nav.stacks[tab].push(view);
  $(`tab-${tab}`).append(view.el);
  try { window.history.pushState({ depth: nav.stacks[tab].length }, ""); } catch { /* 뒤로 가기 연동만 빠진다 */ }
  renderHeader();
  window.scrollTo(0, 0);
  $("title").focus({ preventScroll: true });
}

function popView(tab = nav.tab) {
  const view = nav.stacks[tab].pop();
  if (!view) return;
  view.dispose?.();
  view.el.remove();
  const below = topView(tab) ?? nav.roots[tab];
  below.el.hidden = false;
  renderHeader();
  if (tab === nav.tab) window.scrollTo(0, below.scroll ?? 0);
  below.resume?.();
}

/** 맨 위 화면을 다른 화면으로 바꾼다 (가져오기 → 그 대화방). 뒤로 가기 기록은 그대로 쓴다. */
function replaceTop(view, tab = nav.tab) {
  const old = nav.stacks[tab].pop();
  if (!old) { pushView(view, tab); return; }
  old.dispose?.();
  old.el.remove();
  nav.stacks[tab].push(view);
  $(`tab-${tab}`).append(view.el);
  renderHeader();
  if (tab !== nav.tab) return; // 처리 중에 다른 탭으로 옮겨 갔으면 그 탭의 화면은 건드리지 않는다
  window.scrollTo(0, 0);
  $("title").focus({ preventScroll: true });
}

// ---------- 클립보드 ----------

const COPY_DONE = "복사했어요. 메신저에 붙여 넣고 직접 보내 주세요.";
const COPY_BLOCKED = "자동 복사가 막혀 있어 문장을 선택해 뒀어요. 직접 복사해 주세요.";

/** 보안 맥락이 아닌 곳(http 내부망 주소로 연 휴대폰 등)에서 쓰는 예전 방식. */
function legacyCopy(text) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.setAttribute("aria-hidden", "true");
  area.className = "sr-only";
  document.body.append(area);
  const previous = document.activeElement;
  let ok = false;
  try {
    area.select();
    area.setSelectionRange(0, text.length);
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  area.remove();
  if (previous instanceof HTMLElement) previous.focus({ preventScroll: true });
  return ok;
}

/** 클립보드에 쓴다. 클릭 처리 안에서 불러야 한다. 막히면 false — 다른 일(피드백 저장)과 묶지 않는다. */
async function copyText(text) {
  const value = String(text ?? "");
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch { /* 아래 예전 방식으로 한 번 더 */ }
  return legacyCopy(value);
}

function selectText(el) {
  if (!el) return;
  try {
    if (el instanceof HTMLTextAreaElement) { el.focus(); el.select(); return; }
    const range = document.createRange();
    range.selectNodeContents(el);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  } catch { /* 선택을 못 해도 문장은 화면에 있다 */ }
}

// ---------- 작성 ----------

const DRAFT_KEPT = "입력한 초안은 바뀌지 않았어요.";

const compose = {
  contact: "",
  recents: "",
  result: null,
  requested: "auto", // 지금 보이는 결과를 요청할 때 고른 조건 ("auto"면 적용 조건 옆에 '자동'을 붙인다)
  edited: "",
  fixing: false,
  usage: null,
  key: "",
  answered: false,

  get selected() { return session.contacts.find((item) => item.id === this.contact) ?? null; },
  get alias() { return this.selected?.alias ?? "상대"; },
  get condition() { return $("c-condition").value || "auto"; },
  get hasPersona() { return this.selected?.persona_version !== null && this.selected?.persona_version !== undefined; },
  /** 직접 고른 B~D는 저장된 말투 설정이 있어야 한다(없으면 서버가 422). auto와 A는 없어도 된다. */
  get needsPersona() { return !this.hasPersona && ["B", "C", "D"].includes(this.condition); },
  get tooLong() { return Array.from($("c-draft").value.trim()).length > MAX_DRAFT_CHARS; },
  get blocked() { return !this.selected || this.needsPersona || !$("c-draft").value.trim() || this.tooLong; },
  get usageLine() {
    if (!this.usage) return "";
    const used = formatNumber(this.usage.rewrites ?? 0);
    return this.usage.rewrite_limit > 0
      ? ` 오늘 ${used} / ${formatNumber(this.usage.rewrite_limit)}회 썼어요.`
      : ` 오늘 ${used}회 썼어요.`;
  },

  /** 한 번만: 고정 요소에 동작을 건다. */
  wire() {
    $("c-condition").append(...options(CONDITIONS.map((item) => [item.value, item.label])));
    $("c-condition").value = "auto";
    $("c-draft").addEventListener("input", () => this.invalidate());
    $("c-situation").addEventListener("input", () => this.invalidate());
    $("c-condition").addEventListener("change", () => { this.invalidate(); this.renderWho(); });
    $("c-contact").addEventListener("change", (event) => this.choose(event.target.value));
    $("c-go").addEventListener("click", () => this.rewrite());
    this.renderCondition();
  },

  /** 로그인 직후: 이 브라우저가 기억한 상대(id)를 불러온다. */
  start() {
    this.contact = store.get("lastContact");
    this.recents = store.get("recentContacts");
    this.key = uuid4();
    this.sync();
    this.loadUsage();
  },

  reset() {
    Object.assign(this, { contact: "", recents: "", result: null, requested: "auto", edited: "", fixing: false,
      usage: null, answered: false });
    $("c-draft").value = "";
    $("c-situation").value = "";
    $("c-condition").value = "auto";
    fill($("c-result"));
    this.renderCondition();
    this.renderCount();
  },

  appear() { this.loadUsage(); },

  /** 상대 목록이 바뀌었을 때. 고른 상대가 사라졌으면 말투 설정이 있는 첫 상대로 옮긴다. */
  sync() {
    if (!this.selected) {
      const next = session.contacts.find((item) => item.persona_version !== null && item.persona_version !== undefined)
        ?? session.contacts[0];
      this.choose(next?.id ?? "", true);
    }
    // 지워진 상대는 최근 기록에서도 뺀다.
    const kept = this.recents.split(",").filter((id) => session.contacts.some((item) => item.id === id)).join(",");
    if (kept !== this.recents) {
      this.recents = kept;
      if (kept) store.set("recentContacts", kept); else store.remove("recentContacts");
    }
    this.renderWho();
    if (this.result) this.renderResult();
  },

  choose(id, silent = false) {
    if (id === this.contact) return;
    this.contact = id;
    if (id) store.set("lastContact", id); else store.remove("lastContact");
    this.invalidate(silent);
    this.renderWho();
  },

  /** 입력이 바뀌면 이전 추천을 적용할 수 없게 하고 새 요청 키를 쓴다. */
  invalidate(silent = false) {
    this.key = uuid4();
    this.answered = false;
    this.fixing = false;
    if (this.result) {
      this.result = null;
      this.renderResult();
    }
    if (!silent && !ui.busy && ui.notice) notify("");
    this.renderCondition();
    this.renderCount();
    this.renderGo();
  },

  renderCondition() {
    const current = CONDITIONS.find((item) => item.value === this.condition) ?? CONDITIONS[0];
    $("c-condition-desc").textContent = current.desc;
  },

  renderCount() {
    const count = Array.from($("c-draft").value).length;
    $("c-count").textContent = `${formatNumber(count)} / ${formatNumber(MAX_DRAFT_CHARS)}자`;
  },

  renderGo() {
    $("c-go").textContent = `${this.alias}에게 맞게`;
    $("c-go").disabled = this.blocked;
  },

  renderWho() {
    const none = session.contacts.length === 0;
    $("c-empty").hidden = !none;
    $("c-pick").hidden = none;
    fill($("c-recents"), ...Recents.pick(this.recents, session.contacts).map((item) => h("button", {
      type: "button", class: "chip", "aria-pressed": String(item.id === this.contact), text: item.alias,
      onclick: () => this.choose(item.id),
    })));
    fill($("c-contact"), ...options(session.contacts.map((item) => [item.id, item.alias])));
    $("c-contact").value = this.contact;
    $("c-nopersona").hidden = none || !this.needsPersona;
    this.renderGo();
  },

  async loadUsage() {
    const epoch = session.epoch;
    try {
      const usage = await api("GET", "/v1/usage");
      if (epoch !== session.epoch) return;
      this.usage = usage;
      const line = $("c-usage");
      if (line) line.textContent = line.dataset.lead + this.usageLine;
    } catch { /* 표시용이라 실패해도 조용히 둔다 */ }
  },

  /**
   * 첨삭 요청 (`POST /v1/rewrites` + Idempotency-Key). 실패 뒤 다시 누르면 같은 키로 재시도한다 — 키는 성공하거나
   * 입력이 바뀔 때만 바뀐다(같은 키·다른 본문은 서버가 409로 막는다). 실패해도 초안은 그대로 둔다.
   */
  rewrite() {
    if (this.blocked) return;
    run(async () => {
      const current = this.key;
      const contact = this.selected;
      const condition = this.condition;
      try {
        const response = await api("POST", "/v1/rewrites", {
          key: current,
          body: {
            contact_id: contact.id,
            draft_text: $("c-draft").value.trim(),
            situation: $("c-situation").value.trim(),
            condition,
            channel: "mobile",
          },
        });
        if (current !== this.key) return;
        this.result = response;
        this.requested = condition;
        this.edited = response.result.result_text;
        this.fixing = false;
        this.answered = false;
        this.key = uuid4();
        this.recents = Recents.push(contact.id, this.recents);
        store.set("recentContacts", this.recents);
        this.renderWho();
        this.renderResult(true);
      } catch (error) {
        // 한도(429 usage_exceeded 등)·말투 설정 없음(422)·모델 장애(503)를 PC 화면과 같은 말로 알린다.
        if (!(error instanceof ApiError) || error.status === 401) throw error;
        const view = rewriteErrorView(error, { contact, condition, usage: this.usage });
        throw new Notice(`${view.title}. ${view.body}`);
      } finally {
        this.loadUsage();
      }
    });
  },

  footer(lead) {
    return h("p", { class: "outside-note", id: "c-usage", "data-lead": lead, text: lead + this.usageLine });
  },

  renderResult(reveal = false) {
    const box = $("c-result");
    if (!this.result) { fill(box); return; }
    const rewrite = this.result;
    const view = rewrite.result.status === "recommended" ? this.recommendation(rewrite) : this.kept(rewrite);
    fill(box, ...view);
    if (reveal) {
      const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      box.scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "start" });
    }
  },

  // 추천: 문장 → 지킨 항목 → 주의 → 복사
  recommendation(rewrite) {
    const result = rewrite.result;
    const empty = () => !this.edited.trim();
    const copy = h("button", { type: "button", class: "btn primary", text: "복사하기", disabled: empty(), onclick: () => this.copy(rewrite) });
    const body = this.fixing
      ? h("textarea", { id: "c-edited", rows: "4", "aria-label": "추천 문장 고치기", value: this.edited,
        oninput: (event) => { this.edited = event.target.value; copy.disabled = empty(); } })
      : h("p", { id: "c-edited", class: "result-text", lang: "ko", text: this.edited });
    const chips = preservedChips(result);
    const plain = this.requested === "auto" && rewrite.condition === "A" && !this.hasPersona;
    return [
      h("section", { class: "card", "aria-labelledby": "c-result-title" },
        h("h2", { class: "card-title", id: "c-result-title", text: `${this.alias}에게 맞게` }),
        body,
        chips.length ? h("ul", { class: "preserved", "aria-label": "지킨 항목" },
          chips.map((chip) => h("li", { text: `✓ ${chip.label}` }))) : null,
        warningBox(result.warnings),
        plain ? h("p", { class: "small muted", text: "이 상대의 말투 설정과 가져온 대화가 없어 일반적인 다듬기만 했어요." }) : null,
        h("div", { class: "link-row" },
          h("button", { type: "button", class: "text-btn", text: "다시 만들기", onclick: () => this.rewrite() }),
          h("button", { type: "button", class: "text-btn", text: this.fixing ? "고치기 완료" : "직접 고치기",
            onclick: () => { this.fixing = !this.fixing; this.renderResult(); $("c-edited").focus?.(); } }),
          h("button", { type: "button", class: "text-btn", text: "초안 그대로", onclick: () => this.reject() })),
        copy,
        h("button", { type: "button", class: "btn", text: "초안 칸에 옮기기", onclick: () => this.apply(rewrite) }),
        (result.change_reasons ?? []).filter(Boolean).map((reason) => h("p", { class: "small muted", text: `바꾼 이유 · ${reason}` })),
        h("p", { class: "small muted", text: metaLine(resultMeta(rewrite, this.requested)) })),
      this.footer("복사만 해요. 전송은 직접 합니다."),
    ];
  },

  // 추천하지 않는 경우(fallback_original): 검증을 통과하지 못해 초안을 그대로 뒀다는 것과 그 까닭을 알린다
  kept(rewrite) {
    const view = fallbackView(rewrite.result);
    return [
      h("section", { class: "card", "aria-labelledby": "c-result-title" },
        h("h2", { class: "card-title", id: "c-result-title", text: `${this.alias}에게 맞게` }),
        h("p", { class: "headline", text: "✋ 초안을 그대로 뒀어요" }),
        h("p", { class: "strong", text: view.headline }),
        h("p", { class: "row-text", text: view.lead }),
        view.items.length ? h("ul", { class: "plain-list small", "aria-label": "지키지 못한 항목" },
          view.items.map((item) => h("li", { text: item }))) : null,
        warningBox(rewrite.result.warnings),
        h("div", { class: "link-row" },
          h("button", { type: "button", class: "text-btn", text: "닫기", onclick: () => { this.result = null; this.renderResult(); } }),
          h("button", { type: "button", class: "text-btn", text: "다시 만들기", onclick: () => this.rewrite() })),
        h("p", { class: "small muted", text: metaLine(resultMeta(rewrite, this.requested)) })),
      this.footer(DRAFT_KEPT),
    ];
  },

  /**
   * 피드백(`POST /v1/rewrites/{id}/feedback`)은 추천 하나에 한 번만 보낸다. 직접 고친 문장은 함께 보낸다.
   * 저장에 실패해도 복사·옮기기는 그대로 된 것이다 — 성공 여부만 돌려준다.
   */
  async sendFeedback(rewrite, accepted, text = null, reason = null) {
    if (this.answered) return true;
    this.answered = true;
    const changed = text !== null && text !== rewrite.result.result_text ? text : null;
    try {
      await api("POST", `/v1/rewrites/${seg(rewrite.id)}/feedback`, { body: { accepted, edited_text: changed, reason } });
      return true;
    } catch {
      this.answered = false;
      return false;
    }
  },

  copy(rewrite) {
    const text = this.edited;
    if (!text.trim()) return;
    run(async () => {
      const copied = await copyText(text);
      this.fixing = false;
      this.renderResult();
      if (!copied) selectText($("c-edited"));
      await this.sendFeedback(rewrite, true, text);
      notify(copied ? COPY_DONE : COPY_BLOCKED);
    });
  },

  /** 추천 문장을 초안 칸으로 옮긴다 (이어서 더 고쳐 쓰려고). 어디로도 보내지 않는다. */
  apply(rewrite) {
    const text = this.edited;
    if (!text.trim()) return;
    run(async () => {
      // 초안을 바꾸면 invalidate가 추천을 닫는다. 피드백은 그 전에 보낼지 정해 둔다.
      const first = !this.answered;
      $("c-draft").value = text;
      this.invalidate();
      if (first) await this.sendFeedback(rewrite, true, text);
      notify("추천 문장을 초안 칸에 옮겼어요.");
    });
  },

  async reject() {
    const rewrite = this.result;
    if (!rewrite) return;
    const answer = await ask({ title: "초안을 그대로 둘까요?", confirm: "초안 그대로", input: "추천이 맞지 않은 이유 (선택)" });
    if (!answer.ok || this.result !== rewrite) return;
    const why = answer.value.trim();
    run(async () => {
      await this.sendFeedback(rewrite, false, null, why || null);
      this.result = null;
      this.renderResult();
      notify(DRAFT_KEPT);
    });
  },
};

// ---------- 상대 ----------

const RELATIONSHIPS = REL_ORDER.map((value) => [value, relLabel(value)]);

/** 상대 목록과 등록. 상대를 누르면 말투 설정·과거 대화로 들어간다. */
function contactsRoot() {
  const list = h("div");
  const alias = h("input", { type: "text", maxlength: "50", placeholder: "예: 박지현", autocomplete: "off" });
  const relationship = h("select", { value: "colleague" }, options(RELATIONSHIPS));
  const add = h("button", { type: "submit", class: "btn primary", text: "추가", disabled: true });
  alias.addEventListener("input", () => { add.disabled = !alias.value.trim(); });

  const sync = () => {
    if (!session.contacts.length) {
      fill(list, h("p", { class: "row-text", text: "아직 상대가 없어요. 아래에서 이름과 관계를 넣고 추가해 주세요." }));
      return;
    }
    fill(list, h("ul", { class: "list" }, session.contacts.map((contact) => h("li", null,
      h("button", { type: "button", class: "list-btn", onclick: () => pushView(contactDetail(contact)) },
        h("span", { class: "stack" },
          h("span", { text: contact.alias }),
          h("span", { class: "small muted", text: relLabel(contact.relationship_type)
            + (contact.persona_version !== null && contact.persona_version !== undefined
              ? ` · 말투 설정 v${contact.persona_version}` : " · 말투 설정 없음") })))))));
  };

  const form = h("form", { class: "card", novalidate: true, onsubmit: (event) => {
    event.preventDefault();
    const name = Array.from(alias.value.trim()).slice(0, 50).join("");
    if (!name) return;
    run(async () => {
      // use_default_persona: 관계 유형의 기본 말투로 v1을 함께 만든다 → 바로 첨삭할 수 있다 (PC 화면과 같다).
      await api("POST", "/v1/contacts", { body: { alias: name, relationship_type: relationship.value, use_default_persona: true } });
      alias.value = "";
      add.disabled = true;
      await reloadContacts();
      notify(`${name} 님을 추가했어요. 관계에 맞는 기본 말투로 시작해요.`);
    });
  } },
  h("h2", { class: "card-title", text: "상대 추가" }),
  field("이름(별칭)", alias),
  field("관계", relationship, { inline: true }),
  add,
  h("p", { class: "note", text: "실명 대신 별칭을 써도 돼요. 관계에 맞는 기본 말투로 시작하고, 상대를 눌러 고칠 수 있어요." }));

  sync();
  return {
    el: h("div", { class: "view" }, card("상대", list), form),
    sync,
    refresh: () => run(reloadContacts),
  };
}

/** 상대 한 명의 말투 설정 편집(+ 과거 대화 기준 제안), 과거 대화방 목록, 맥락 그래프, 삭제. */
function contactDetail(contact) {
  const state = { version: null, dirty: false, conversations: [], suggestion: null, cardDismissed: false };
  const personaTitle = h("h2", { class: "card-title" });
  const paintTitle = () => {
    const base = state.version === null ? "말투 설정 (아직 저장 전)" : `말투 설정 v${state.version}`;
    personaTitle.textContent = state.dirty && state.version !== null ? `${base} · 저장하지 않은 변경 있음` : base;
  };
  const touched = () => { state.dirty = true; paintTitle(); };

  const sliders = {};
  const axisRows = SCALES.map((scale) => {
    const id = uid("axis");
    const caption = h("label", { for: id });
    const slider = h("input", { id, type: "range", min: "1", max: "5", step: "1", value: "3" });
    const paint = () => {
      const word = stepLabel(scale.field, slider.value);
      caption.textContent = `${scale.label} · ${word}`;
      slider.setAttribute("aria-valuetext", word);
    };
    slider.addEventListener("input", () => { paint(); touched(); });
    sliders[scale.field] = { slider, paint };
    paint();
    return h("div", { class: "field" }, caption, slider,
      h("div", { class: "scale-ends", "aria-hidden": "true" },
        h("span", { text: scale.steps[0].short }), h("span", { text: scale.steps[4].short })));
  });
  const length = h("select", { value: "same", onchange: touched }, options(LENGTH_OPTIONS.map((item) => [item.value, item.label])));
  const emoji = h("select", { value: "none", onchange: touched }, options(EMOJI_OPTIONS.map((item) => [item.value, item.label])));
  const address = h("input", { type: "text", maxlength: String(LIMITS.address_term), placeholder: "예: 팀장님, 형", autocomplete: "off", oninput: touched });
  const preferred = h("input", { type: "text", placeholder: "쉼표로 구분", autocomplete: "off", oninput: touched });
  const forbidden = h("input", { type: "text", placeholder: "예: ㅋㅋ, ㅠ", autocomplete: "off", oninput: touched });
  const notes = h("textarea", { rows: "2", maxlength: String(LIMITS.notes), placeholder: "예: 업무 얘기는 결론부터", oninput: touched });
  const saveButton = h("button", { type: "button", class: "btn primary", text: "저장", onclick: () => save() });

  const phrases = (text) => text.split(",").map((item) => item.trim()).filter(Boolean);

  /** 편집 칸의 값 → 서버가 받는 모양의 설정. */
  const readSettings = () => normalizeSettings({
    formality: sliders.formality.slider.value,
    friendliness: sliders.friendliness.slider.value,
    directness: sliders.directness.slider.value,
    length: length.value,
    emoji: emoji.value,
    address_term: address.value,
    preferred_phrases: cleanPhrases(phrases(preferred.value), LIMITS.preferred_phrases),
    forbidden_phrases: cleanPhrases(phrases(forbidden.value), LIMITS.forbidden_phrases),
    notes: notes.value,
  });

  /** 설정을 편집 칸에 채운다 (저장하지 않는다). */
  const writeSettings = (raw) => {
    const settings = normalizeSettings(raw);
    for (const scale of SCALES) {
      sliders[scale.field].slider.value = String(settings[scale.field]);
      sliders[scale.field].paint();
    }
    length.value = settings.length;
    emoji.value = settings.emoji;
    address.value = settings.address_term ?? "";
    preferred.value = settings.preferred_phrases.join(", ");
    forbidden.value = settings.forbidden_phrases.join(", ");
    notes.value = settings.notes ?? "";
  };

  const show = (persona) => {
    state.version = persona.version;
    state.dirty = false;
    writeSettings(persona.settings);
    saveButton.textContent = "저장 (새 버전)";
    paintTitle();
  };

  /** 새 버전으로 저장한다 (`PUT …/persona` `{settings, expected_version}`). 다른 곳에서 먼저 저장했으면 409. */
  const save = () => run(async () => {
    let saved;
    try {
      saved = await api("PUT", `/v1/contacts/${seg(contact.id)}/persona`,
        { body: { settings: readSettings(), expected_version: state.version ?? 0 } });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        throw new Notice("다른 곳에서 먼저 저장했어요. ‘새로고침’으로 최신 설정을 불러온 뒤 다시 고쳐 주세요. (지금 고친 값은 저장되지 않았어요)");
      }
      throw error;
    }
    show(saved);
    if (state.suggestion) renderSuggestion();
    await reloadContacts();
    notify("말투 설정을 저장했어요.");
  });

  // ---- 과거 대화 기준 제안: 규칙으로 읽은 값을 보여 주기만 한다. 적용하면 편집 칸이 채워지고, 저장은 사용자가 누른다 ----
  const suggestionBox = h("div", { class: "group suggestion", hidden: true, "aria-live": "polite" });
  const suggestButton = h("button", { type: "button", class: "btn", text: "과거 대화 기준 제안 보기", onclick: () => openSuggestion() });

  const applySuggestion = (signals) => {
    const { settings, applied } = applySignals(readSettings(), signals);
    if (!applied.length) return;
    writeSettings(settings);
    touched();
    renderSuggestion();
    notify("제안을 편집 칸에 넣었어요. 아직 저장되지 않았어요 — 값을 확인한 뒤 ‘저장’을 눌러 주세요.");
    saveButton.focus({ preventScroll: true });
  };

  const renderCard = (rawCard, current) => {
    const view = cardView(rawCard, contact.alias);
    if (!view) return null;
    if (state.cardDismissed) return h("p", { class: "note", text: "말투 변화 제안은 그대로 두기로 했어요. 설정은 바뀌지 않았어요." });
    const proposed = cardSignals(rawCard);
    const same = proposed.length > 0 && proposed.every((signal) => signalMatches(current, signal));
    let question = "";
    if (view.ask) {
      const tail = same ? "" : proposed.length ? "어떻게 할까요?" : "값은 정해 드리지 않았어요. 위 칸에서 직접 골라 주세요.";
      question = [view.askReason, tail].filter(Boolean).join(" ");
    }
    return h("div", { class: "suggestion-card" },
      h("p", { class: "strong", text: view.title }),
      h("dl", { class: "suggestion-rows" }, view.rows.map((row) => h("div", null,
        h("dt", { text: row.label }), h("dd", { class: "selectable", text: row.text })))),
      question ? h("p", { class: "small", text: question }) : null,
      same ? h("p", { class: "small muted", text: "지금 편집 칸의 값과 같아요." }) : null,
      !same && proposed.length ? h("button", { type: "button", class: "btn", text: view.applyLabel || "적용",
        "aria-label": `${view.applyLabel || "적용"} (말투 변화 제안 적용)`, onclick: () => applySuggestion(proposed) }) : null,
      !same ? h("button", { type: "button", class: "text-btn", text: "그대로 두기",
        onclick: () => { state.cardDismissed = true; renderSuggestion(); } }) : null);
  };

  const renderSuggestion = () => {
    const data = state.suggestion;
    const head = h("h3", { class: "card-title", text: "과거 대화 기준 제안" });
    if (!data) { fill(suggestionBox); return; }
    const count = Number(data.based_on_messages) || 0;
    const status = suggestionState(data);
    if (status !== "ok") {
      const text = status === "none"
        ? "이 상대에게 보낸 내 메시지가 아직 없어요. 아래 ‘대화 가져오기’로 대화를 올리면 제안을 볼 수 있어요."
        : status === "few"
          ? `내 메시지가 ${formatNumber(count)}개뿐이라 말투를 읽기에 모자라요. 대화를 더 가져오면 제안을 볼 수 있어요.`
          : `내 메시지 ${formatNumber(count)}개를 봤지만 따로 제안할 것을 찾지 못했어요.`;
      fill(suggestionBox, head, h("p", { class: "row-text", text }));
      return;
    }
    const current = readSettings();
    const signals = visibleSignals(data);
    const open = signals.filter((signal) => !signalMatches(current, signal));
    fill(suggestionBox,
      head,
      h("p", { class: "small muted", text: `내 메시지 ${formatNumber(count)}개에서 규칙으로 읽은 값이에요 (AI를 부르지 않아요).` }),
      renderCard(data.card, current),
      signals.length ? h("ul", { class: "signal-list" }, signals.map((signal) => {
        const view = signalView(signal);
        const matches = signalMatches(current, signal);
        return h("li", { class: "signal" },
          h("div", { class: "stack" },
            h("span", { class: "strong", text: `${view.fieldLabel} · ${view.valueLabel}` }),
            view.reason ? h("span", { class: "small", text: view.reason }) : null,
            h("span", { class: "small muted", text: view.since ? `${view.sourceLabel} · ${view.since}부터` : view.sourceLabel })),
          matches
            ? h("span", { class: "small muted", text: "지금 값과 같아요" })
            : h("button", { type: "button", class: "btn compact", text: "적용",
              "aria-label": `${view.fieldLabel} ${view.valueLabel} 적용`, onclick: () => applySuggestion([signal]) }));
      })) : null,
      open.length > 1 ? h("button", { type: "button", class: "btn", text: "모두 적용", onclick: () => applySuggestion(open) }) : null,
      h("p", { class: "note", text: "적용해도 바로 저장되지 않아요. 값을 확인한 뒤 ‘저장’을 눌러 주세요." }),
      h("button", { type: "button", class: "text-btn", text: "제안 닫기",
        onclick: () => { suggestionBox.hidden = true; suggestButton.focus(); } }));
  };

  const openSuggestion = () => run(async () => {
    state.suggestion = await api("GET", `/v1/contacts/${seg(contact.id)}/persona/suggestion`);
    state.cardDismissed = false;
    renderSuggestion();
    suggestionBox.hidden = false;
  });

  // ---- 과거 대화 ----
  const conversationList = h("div");
  const renderConversations = () => {
    if (!state.conversations.length) {
      fill(conversationList, h("p", { class: "row-text", text: "아직 가져온 대화가 없어요. 없어도 말투 설정만으로 첨삭할 수 있어요." }));
      return;
    }
    fill(conversationList, h("ul", { class: "list" }, state.conversations.map((conversation) => h("li", null,
      h("button", { type: "button", class: "list-btn",
        onclick: () => pushView(conversationView(conversation, contact, loadConversations)) },
      h("span", { class: "stack" },
        h("span", { text: conversation.title }),
        h("span", { class: "small muted", text: formatDateTime(conversation.created_at) })))))));
  };
  const fetchConversations = async () => {
    const list = await api("GET", "/v1/conversations", { query: { contact_id: contact.id } });
    state.conversations = Array.isArray(list) ? list : [];
    renderConversations();
  };
  const loadConversations = async () => {
    const epoch = session.epoch;
    try {
      await fetchConversations();
      if (epoch === session.epoch) await reloadContacts();
    } catch { /* 목록 갱신 실패는 조용히 둔다 (이전 목록 유지) */ }
  };

  const openImport = () => pushView(importView(contact, null, {
    knownMeNames: state.conversations.map((item) => item.me_name),
    done: (conversation, job) => {
      replaceTop(conversationView(conversation, contact, loadConversations, job), "contacts");
      quiet(loadConversations);
    },
  }));

  const remove = async () => {
    const answer = await ask({
      title: "이 상대 삭제",
      message: `${contact.alias} 님의 대화방·말투 설정·첨삭 이력을 모두 지워요. 되돌릴 수 없어요.`,
      confirm: "삭제",
      danger: true,
    });
    if (!answer.ok) return;
    run(async () => {
      const out = await api("DELETE", `/v1/contacts/${seg(contact.id)}`);
      await reloadContacts();
      popView("contacts");
      const failed = Array.isArray(out?.graph_errors) && out.graph_errors.length > 0;
      notify(failed ? "상대를 지웠어요. 다만 맥락 그래프 일부는 지우지 못했어요 — 운영자에게 알려 주세요." : "상대를 지웠어요.", failed);
    });
  };

  const load = async () => {
    let persona = null;
    try {
      persona = await api("GET", `/v1/contacts/${seg(contact.id)}/persona`);
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) throw error; // 저장된 말투 설정이 없으면 404
    }
    if (persona) show(persona);
    await fetchConversations();
  };

  const el = h("div", { class: "view" },
    h("section", { class: "card" },
      personaTitle,
      axisRows,
      field("길이", length, { inline: true }),
      field("이모지", emoji, { inline: true }),
      field("호칭", address),
      field("선호 표현", preferred),
      field("금지 표현", forbidden),
      field("메모", notes),
      saveButton,
      suggestButton,
      suggestionBox,
      h("p", { class: "note", text: "여기에 저장한 값으로 다듬어요. 제안은 편집 칸만 채우고, ‘저장’을 눌러야 반영돼요. 호칭은 여기 넣은 것만 써요." })),
    h("section", { class: "card" },
      h("h2", { class: "card-title", text: "과거 대화" }),
      conversationList,
      h("button", { type: "button", class: "btn", text: "대화 가져오기", onclick: openImport }),
      h("p", { class: "note", text: "가져온 대화에서 내 말투와 지난 약속을 읽어 첨삭에 참고해요. 카카오톡·텔레그램 내보내기 파일이나 붙여넣기를 쓸 수 있어요." })),
    h("section", { class: "card" },
      h("ul", { class: "list" }, h("li", null,
        h("button", { type: "button", class: "list-btn", onclick: () => pushView(graphView(contact)) },
          h("span", { class: "stack" }, h("span", { text: "맥락 그래프 보기" })))))),
    h("p", { class: "outside-note", text: "가져온 대화에서 서버가 뽑은 사람·사건·사실이에요. 지난 약속과 겹치는지 볼 때 참고해요." }),
    h("button", { type: "button", class: "btn danger", text: "이 상대 삭제", onclick: remove }));

  paintTitle();
  renderConversations();
  run(load);

  return {
    title: contact.alias,
    el,
    refresh: () => run(async () => {
      await load();
      if (state.suggestion) renderSuggestion();
    }),
    resume: () => quiet(loadConversations),
  };
}

// ---------- 대화 가져오기 ----------

const IMPORT_TABS = [["kakao", "카카오톡"], ["telegram", "텔레그램"], ["paste", "붙여넣기"]];
const PASTE_HELP = "한 줄에 한 메시지: 2026-10-05 09:25 | 박지현 | 내용. 다른 메신저의 대화는 이 모양으로 붙여 넣어 주세요.";

/** 고른 파일을 글로 읽는다 (UTF-8·UTF-16·EUC-KR을 가려 읽는다). 너무 큰 파일은 요청 한 번에 담기 어려워 막는다. */
async function readTextFile(file) {
  if (file.size > MAX_BODY_BYTES) {
    throw new Notice(`파일이 너무 커요(${formatBytes(file.size)}). 기간을 나눠 내보내 주세요.`);
  }
  let buffer;
  try {
    buffer = await file.arrayBuffer();
  } catch {
    throw new Notice("파일을 읽지 못했어요. 다시 골라 주세요.");
  }
  return decodeBytes(buffer);
}

/**
 * 대화 가져오기: 원문 → 미리보기(`POST /v1/imports/parse`, 저장 없음) → 화자 지정 → 가져오기
 * (`POST /v1/conversations/{id}/imports/raw`, 202 + 작업). 파싱은 서버가 한다 — 화면은 원문을 그대로 보낸다.
 * @param {object} contact
 * @param {object|null} conversation 이어서 넣을 대화방. null이면 가져올 때 새로 만든다.
 * @param {{knownMeNames?: Array<string|null>, done: (conversation: object, job: object) => void}} hooks
 */
function importView(contact, conversation, hooks) {
  const state = { method: "kakao", text: "", sent: null, view: null, roles: {}, keepRecent: null };
  const method = () => METHODS.find((item) => item.value === state.method) ?? METHODS[0];

  const help = h("p", { class: "small" });
  const fileNote = h("p", { class: "small muted", hidden: true });
  const paste = h("textarea", { class: "mono", rows: "6", "aria-label": "가져올 대화", spellcheck: "false" });
  const previewButton = h("button", { type: "button", class: "btn primary", text: "미리보기", disabled: true, onclick: () => preview() });
  const picker = h("input", { type: "file", hidden: true, tabindex: "-1", "aria-hidden": "true" });
  const pickButton = h("button", { type: "button", class: "btn", text: "파일 고르기", onclick: () => picker.click() });
  const previewBox = h("div", { class: "group", "aria-live": "polite" });

  const source = () => (method().file ? state.text : paste.value);
  const syncPreviewButton = () => { previewButton.disabled = !source().trim(); };
  paste.addEventListener("input", syncPreviewButton);

  picker.addEventListener("change", () => {
    const file = picker.files && picker.files[0];
    picker.value = "";
    if (!file) return;
    run(async () => {
      const decoded = await readTextFile(file);
      state.text = decoded.text;
      const parts = [file.name, formatBytes(file.size), encodingLabel(decoded.encoding)];
      if (decoded.replaced > 0) parts.push(`깨진 글자 ${formatNumber(decoded.replaced)}개 — 인코딩을 확인해 주세요`);
      fileNote.textContent = parts.join(" · ");
      fileNote.hidden = false;
      resetPreview();
      syncPreviewButton();
    });
  });

  const resetPreview = () => {
    state.sent = null;
    state.view = null;
    fill(previewBox);
  };

  const tabs = h("div", { class: "segmented", role: "group", "aria-label": "가져오는 방법" },
    IMPORT_TABS.map(([value, label]) => h("button", { type: "button", "data-method": value, text: label,
      onclick: () => {
        if (state.method === value) return;
        state.method = value;
        state.text = "";
        fileNote.hidden = true;
        resetPreview();
        renderMethod();
      } })));
  const renderMethod = () => {
    const current = method();
    for (const button of tabs.children) button.setAttribute("aria-pressed", String(button.dataset.method === state.method));
    help.textContent = current.file ? current.desc : PASTE_HELP;
    if (current.accept) picker.setAttribute("accept", current.accept); else picker.removeAttribute("accept");
    pickButton.hidden = !current.file;
    paste.hidden = current.file;
    syncPreviewButton();
  };

  /** 미리보기를 받아 화자 지정표를 채운다. 이어서 넣는 대화방이면 이미 저장된 화자의 역할을 먼저 고른다. */
  const loadPreview = async (sent) => {
    const [raw, stored] = await Promise.all([
      api("POST", "/v1/imports/parse", { body: sent }),
      conversation
        ? api("GET", `/v1/conversations/${seg(conversation.id)}/speakers`).catch(() => [])
        : Promise.resolve(null),
    ]);
    const view = previewView(raw, contact.alias, hooks.knownMeNames ?? []);
    state.sent = sent;
    state.view = view;
    state.roles = stored ? applyKnownRoles(view.roles, knownRoles(stored)) : view.roles;
    state.keepRecent = keepRecentFor(view.speakers, state.roles, view.maxMessages, null).value;
    renderPreview();
  };

  const preview = () => run(async () => {
    const text = source();
    const problem = checkSize(text);
    if (problem) throw new Notice(problem);
    // tz_offset_minutes는 보내지 않는다(서버 기본값 = 한국 시간). 메시지 id가 시각으로 정해지므로 늘 같은 값이어야 한다.
    await loadPreview({ format: method().format, text });
    previewBox.scrollIntoView({ block: "start" });
  });

  const renderPreview = () => {
    const view = state.view;
    if (!view) { fill(previewBox); return; }
    if (view.needsChat) {
      // 텔레그램 "계정 전체 내보내기": 대화 목록만 온다 → 하나를 골라 chat_id로 다시 읽는다
      const pick = h("select", null, options(view.chats.map((chat) => [chat.id,
        [chat.name || "(이름 없음)", chat.typeLabel, `${formatNumber(chat.messages)}개`].filter(Boolean).join(" · ")])));
      fill(previewBox, card("어느 대화를 가져올까요?",
        h("p", { class: "small", text: "계정 전체 내보내기 파일이에요. 이 상대와 나눈 대화를 골라 주세요." }),
        field("대화", pick),
        h("button", { type: "button", class: "btn primary", text: "이 대화 읽기", disabled: !view.chats.length,
          onclick: () => run(() => loadPreview({ ...state.sent, chat_id: pick.value })) })));
      return;
    }
    const check = h("p", { class: "field-error", role: "alert", hidden: true });
    const caution = h("p", { class: "small", hidden: true });
    const keepRow = h("div");
    const go = h("button", { type: "button", class: "btn primary", onclick: () => submit() });
    const paintCheck = () => {
      const result = checkRoles(state.roles);
      check.textContent = result.error ?? "";
      check.hidden = !result.error;
      caution.textContent = result.warning ?? "";
      caution.hidden = !result.warning;
      const { keep, value } = keepRecentFor(view.speakers, state.roles, view.maxMessages, state.keepRecent);
      state.keepRecent = value;
      if (keep.needed && keep.options.length) {
        const select = h("select", { value: value === null ? "" : String(value),
          onchange: (event) => { state.keepRecent = event.target.value ? Number(event.target.value) : null; paintCheck(); } },
        keep.forced ? null : h("option", { value: "", text: "전부" }),
        keep.options.map((count) => h("option", { value: String(count), text: `최근 ${formatNumber(count)}개만` })));
        fill(keepRow, field("가져올 범위", select, { inline: true }),
          keep.forced ? h("p", { class: "small muted", text: `한 번에 ${formatNumber(keep.max)}개까지 가져올 수 있어요.` }) : null);
      } else {
        fill(keepRow);
      }
      const total = keptMessages(view.speakers, state.roles);
      const count = state.keepRecent ? Math.min(total, state.keepRecent) : total;
      go.textContent = result.ok ? `메시지 ${formatNumber(count)}개 가져오기` : "가져오기";
      go.disabled = !result.ok || count === 0;
    };
    const speakerRows = view.speakers.map((speaker) => {
      const select = h("select", { value: state.roles[speaker.name] ?? "",
        onchange: (event) => { state.roles = { ...state.roles, [speaker.name]: event.target.value || null }; paintCheck(); } },
      h("option", { value: "", text: "고르기" }),
      ROLE_OPTIONS.map((role) => h("option", { value: role.value, text: role.label })));
      return field(`${speaker.name} (${formatNumber(speaker.count)}개)`, select, { inline: true });
    });
    const facts = [view.formatLabel, `메시지 ${formatNumber(view.total)}개`, view.range].filter(Boolean).join(" · ");
    const notes = [];
    if (view.skippedLines > 0) notes.push(`읽지 못한 줄 ${formatNumber(view.skippedLines)}개는 가져오지 않아요.`);
    if (view.maskedPii > 0) notes.push(`전화번호·이메일 같은 개인정보 모양 ${formatNumber(view.maskedPii)}곳은 가려서 저장해요.`);
    fill(previewBox,
      card("미리보기",
        h("p", { class: "strong", text: facts }),
        warningBox(view.problems, "읽으면서 걸린 점"),
        notes.map((text) => h("p", { class: "small muted", text })),
        view.sample.length ? h("ul", { class: "list" }, view.sample.slice(0, 5).map((message) => h("li", { class: "list-item" },
          h("div", { class: "small muted", text: `${message.speaker} · ${formatDateTime(message.sentAt)}` }),
          h("div", { class: "pre", text: message.text })))) : null),
      card("누가 ‘나’인가요?",
        h("p", { class: "small", text: "내 메시지에서 말투를 읽어요. 단체방이면 이 상대가 아닌 사람은 ‘제외’로 둬 주세요." }),
        speakerRows,
        keepRow,
        check,
        caution,
        go,
        conversation ? h("p", { class: "note", text: "이미 가져온 메시지는 건너뛰어요. 이미 저장된 화자의 역할은 여기서 바꿔도 바뀌지 않아요." }) : null));
    paintCheck();
  };

  /** 가져오기. 새 대화방이면 먼저 만들고(내 이름은 서버가 '나'로 지정한 화자에서 정한다), 실패하면 빈 방을 지운다. */
  const submit = () => run(async () => {
    const view = state.view;
    const result = checkRoles(state.roles);
    if (!view || !state.sent || !result.ok) throw new Notice(result.error ?? "화자를 먼저 지정해 주세요.");
    let target = conversation;
    let created = false;
    if (!target) {
      // 제목: 내보내기 파일의 방 이름, 없으면 상대 이름으로 짓는다. source는 가져온 방법(기록용).
      const hint = Array.from(view.titleHint.trim()).slice(0, 200).join("");
      const body = { contact_id: contact.id, title: hint || `${withJosa(contact.alias, "과/와")} 나눈 대화`, source: state.method };
      target = await api("POST", "/v1/conversations", { body });
      created = true;
    }
    let job;
    try {
      const payload = { format: view.format, text: state.sent.text, speaker_map: { ...state.roles } };
      if (state.sent.chat_id !== undefined && state.sent.chat_id !== null) payload.chat_id = state.sent.chat_id;
      if (state.keepRecent) payload.keep_recent = state.keepRecent;
      job = await api("POST", `/v1/conversations/${seg(target.id)}/imports/raw`, { body: payload });
    } catch (error) {
      // 빈 대화방을 남기지 않는다 (세션이 끝났으면 그 요청도 실패하고 조용히 넘어간다)
      if (created) api("DELETE", `/v1/conversations/${seg(target.id)}`).catch(() => {});
      throw error;
    }
    state.text = "";
    state.sent = null;
    paste.value = "";
    hooks.done(target, job);
  });

  renderMethod();

  return {
    title: conversation ? "이어서 가져오기" : "대화 가져오기",
    el: h("div", { class: "view" },
      card(`${contact.alias} 님과 나눈 대화를 올려 주세요`, tabs, help, paste, picker, pickButton, fileNote, previewButton),
      h("p", { class: "outside-note", text: "🔒 올린 대화는 내 계정에만 저장되고 말투 분석에 써요. 대화방·상대별로 언제든 지울 수 있어요." }),
      previewBox),
  };
}

/**
 * 대화방 하나: 가져오기 두 단계의 진행(말투 분석 → 지난 약속·사건 정리), 최근 메시지, 이어서 가져오기, 삭제.
 * 가져오기는 비동기다 — 응답의 작업이 `processing`이면 `GET /v1/imports/{id}`와 대화방 통계를 다시 읽어 끝날 때까지 따라간다.
 */
function conversationView(conversation, contact, changed, firstJob = null) {
  const state = { job: firstJob, stats: null, messages: [], loaded: false, alive: true, following: 0, progress: null };

  const phases = h("div", { class: "group", "aria-live": "polite" });
  const messagesTitle = h("h2", { class: "card-title", text: "메시지" });
  const messageList = h("div");

  const phaseBlock = (view, progress) => h("div", { class: `phase ${view.tone || ""}`.trim() },
    h("p", { class: "strong", text: view.title }),
    view.detail ? h("p", { class: "small", text: view.detail }) : null,
    view.count ? h("p", { class: "small muted", text: view.count }) : null,
    progress ? h("progress", { max: String(progress.max), value: String(progress.value), "aria-label": view.title }) : null);

  const renderPhases = () => {
    if (!state.stats && !state.job) { fill(phases); return; }
    if (!state.job && state.stats && state.stats.messages === 0 && !state.stats.latest_job) {
      fill(phases, card("분석", h("p", { class: "row-text", text: "아직 가져온 메시지가 없어요. 아래 ‘이어서 가져오기’로 대화를 올려 주세요." })));
      return;
    }
    const style = stylePhaseView(state.job, state.stats);
    const graph = graphPhaseView(state.stats, state.job, { stalled: Boolean(state.progress?.stalled) });
    fill(phases, card("분석",
      phaseBlock(style, style.progress ? { max: style.progress.total, value: style.progress.done } : null),
      phaseBlock(graph, graph.polling && graph.pct !== null ? { max: 100, value: graph.pct } : null)));
  };

  const renderMessages = () => {
    const total = state.stats?.messages ?? state.messages.length;
    messagesTitle.textContent = `메시지 ${formatNumber(total)}개`;
    if (!state.messages.length) {
      fill(messageList, state.loaded ? h("p", { class: "row-text", text: "저장된 메시지가 없어요." }) : "");
      return;
    }
    fill(messageList,
      total > state.messages.length ? h("p", { class: "small muted", text: `가장 최근 ${formatNumber(state.messages.length)}개만 보여요.` }) : null,
      h("ul", { class: "list" }, state.messages.map((message) => h("li", { class: "list-item" },
        h("div", { class: "small muted", text: `${message.speaker_role === "me" ? "나" : message.speaker_name || contact.alias} · ${formatDateTime(message.sent_at)}` }),
        h("div", { class: "pre", text: message.text })))));
  };

  /** 작업과 통계를 다시 읽는다. 작업 id는 방금 받은 응답의 것, 없으면 통계의 최근 작업. */
  const refreshStatus = async () => {
    const stats = await api("GET", `/v1/conversations/${seg(conversation.id)}/stats`);
    state.stats = stats;
    const jobId = state.job?.id ?? stats?.latest_job?.id;
    if (jobId) {
      try {
        state.job = await api("GET", `/v1/imports/${seg(jobId)}`);
        // 방금 읽은 작업이 통계에 실린 것보다 새롭다 — 진행률은 이쪽을 쓴다
        if (stats?.latest_job?.id === state.job?.id) stats.latest_job = state.job;
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 404) throw error; // 지워진 작업이면 통계만 쓴다
        state.job = null;
      }
    }
  };

  const loadMessages = async () => {
    const list = await api("GET", `/v1/conversations/${seg(conversation.id)}/messages`, { query: { limit: String(MESSAGES_SHOWN) } });
    state.messages = Array.isArray(list) ? list : [];
    state.loaded = true;
  };

  /** 진행 중인 동안 몇 초마다 다시 읽는다. 화면을 떠나면 읽기만 멈춘다(서버의 작업은 계속된다). */
  const follow = async () => {
    const token = ++state.following;
    const epoch = session.epoch;
    const live = () => state.alive && epoch === session.epoch && token === state.following;
    while (live()) {
      state.progress = trackProgress(state.progress, state.stats, Date.now());
      const style = stylePhaseView(state.job, state.stats);
      const graph = graphPhaseView(state.stats, state.job, { stalled: state.progress.stalled });
      renderPhases();
      if (!style.polling && !graph.polling) return;
      await sleep(POLL_MS);
      if (!live()) return;
      try {
        await refreshStatus();
      } catch { /* 다음 차례에 다시 읽는다 */ }
      if (live()) renderMessages();
    }
  };

  const loadAll = async () => {
    await Promise.all([refreshStatus(), loadMessages()]);
    if (!state.alive) return;
    renderPhases();
    renderMessages();
    follow();
  };

  const remove = async () => {
    const answer = await ask({
      title: "이 대화방 삭제",
      message: "이 대화방의 원문과 말투 묶음, 정리된 약속·사건을 지워요. 되돌릴 수 없어요.",
      confirm: "삭제",
      danger: true,
    });
    if (!answer.ok) return;
    run(async () => {
      const out = await api("DELETE", `/v1/conversations/${seg(conversation.id)}`);
      state.alive = false;
      await changed();
      popView("contacts");
      notify(out?.graph_error
        ? "대화방을 지웠어요. 다만 맥락 그래프 일부는 지우지 못했어요 — 운영자에게 알려 주세요."
        : "대화방을 지웠어요.", Boolean(out?.graph_error));
    });
  };

  const openImport = () => pushView(importView(contact, conversation, {
    knownMeNames: [conversation.me_name],
    done: (_, job) => {
      state.job = job;
      state.progress = null;
      popView("contacts"); // resume()이 통계·메시지를 다시 읽고 진행을 따라간다
      notify("가져왔어요. 분석이 끝날 때까지 아래에서 진행을 볼 수 있어요.");
    },
  }));

  renderPhases();
  renderMessages();
  quiet(loadAll);

  return {
    title: conversation.title,
    dispose: () => { state.alive = false; },
    refresh: () => quiet(loadAll),
    resume: () => quiet(loadAll),
    el: h("div", { class: "view" },
      phases,
      h("p", { class: "outside-note", text: "이 상대에게 쓰는 말투는 이전 화면의 말투 설정에서 직접 정해요. 가져온 대화는 제안과 참고 표현에 쓰여요." }),
      h("section", { class: "card" }, messagesTitle, messageList),
      h("button", { type: "button", class: "btn", text: "이어서 가져오기", onclick: openImport }),
      h("button", { type: "button", class: "btn danger", text: "이 대화방 삭제", onclick: remove })),
  };
}

// ---------- 맥락 그래프 ----------

const GRAPH_W = 360;
const GRAPH_H = 340;

/** 노드 이름을 두 줄까지로 자른다 (SVG 글은 자동 줄바꿈이 없다). */
function labelLines(name) {
  const chars = Array.from(String(name ?? ""));
  if (chars.length <= 7) return [chars.join("")];
  const second = chars.slice(7, 14);
  if (chars.length > 14) second[second.length - 1] = "…";
  return [chars.slice(0, 7).join(""), second.join("")];
}

/** 상대 한 명 주변의 맥락 그래프(`GET /v1/contacts/{id}/graph`): 그림(노드를 누르면 그 노드의 사실만) + 사실 목록. */
function graphView(contact) {
  const state = { graph: null, selected: null };
  const body = h("div", { class: "view" });
  let paintSelection = () => {};

  const nameOf = (id) => state.graph.nodes.find((node) => node.id === id)?.name ?? "?";
  const caption = (edge) => {
    const parts = [`${nameOf(edge.source)} → ${nameOf(edge.target)}`];
    if (edge.valid_at) parts.push(formatDateTime(edge.valid_at));
    if (!edge.current) parts.push("지난 사실");
    return parts.join(" · ");
  };

  const diagram = (graph, details) => {
    const unit = GraphLayout.positions(graph);
    const place = (id) => {
      const point = unit.get(id);
      return point ? { x: point.x * GRAPH_W, y: point.y * GRAPH_H } : null;
    };
    const lines = [];
    for (const link of GraphLayout.links(graph)) {
      const from = place(link.a);
      const to = place(link.b);
      if (!from || !to) continue;
      const line = svg("line", { class: link.current ? "link" : "link past", x1: from.x, y1: from.y, x2: to.x, y2: to.y });
      lines.push({ link, line });
    }
    const badges = [];
    for (const node of graph.nodes) {
      const point = place(node.id);
      if (!point) continue;
      const size = node.is_contact ? 30 : 22;
      const event = node.kind === "event";
      const toggle = () => {
        state.selected = state.selected === node.id ? null : node.id;
        paintSelection();
        details();
      };
      const badge = svg("g", {
        class: `node${event ? " event" : ""}${node.is_contact ? " contact" : ""}`,
        role: "button", tabindex: "0", "aria-pressed": "false",
        "aria-label": `${event ? "사건" : node.kind === "person" ? "사람" : "항목"} ${node.name}`,
        transform: `translate(${point.x.toFixed(1)} ${point.y.toFixed(1)})`,
        onclick: toggle,
        onkeydown: (keys) => {
          if (keys.key !== "Enter" && keys.key !== " ") return;
          keys.preventDefault();
          toggle();
        },
      },
      svg("rect", { class: "hit", x: -39, y: -24, width: 78, height: 62, rx: 8 }),
      event
        ? svg("rect", { class: "shape", x: -size / 2, y: -size / 2, width: size, height: size, rx: 5 })
        : svg("circle", { class: "shape", r: size / 2 }),
      svg("circle", { class: "ring", r: size / 2 + 5 }),
      svg("text", { y: size / 2 + 13 }, labelLines(node.name).map((line, index) => svg("tspan", { x: 0, dy: index ? 12 : 0 }, line))));
      badges.push({ node, badge });
    }
    paintSelection = () => {
      for (const { link, line } of lines) {
        const touchedLink = state.selected === null || state.selected === link.a || state.selected === link.b;
        line.classList.toggle("dim", !touchedLink);
      }
      for (const { node, badge } of badges) badge.setAttribute("aria-pressed", String(state.selected === node.id));
    };
    return svg("svg", { class: "graph", viewBox: `0 0 ${GRAPH_W} ${GRAPH_H}`, role: "group", "aria-label": `${contact.alias} 맥락 그래프` },
      lines.map((item) => item.line), badges.map((item) => item.badge));
  };

  const render = () => {
    const graph = state.graph;
    if (!graph) { fill(body); return; }
    if (!graph.enabled) {
      fill(body, card(null, h("p", { class: "row-text", text: "이 서버는 맥락 그래프를 쓰지 않아요. 말투 맞추기는 그대로 쓸 수 있어요." })));
      return;
    }
    if (!graph.nodes.length) {
      fill(body, card(null, h("p", { class: "row-text",
        text: `아직 ${contact.alias} 님의 그래프가 없어요. 과거 대화를 가져오면 서버가 정리해요. 대화 속 이름이 상대 이름과 같아야 이 상대의 그래프로 묶여요.` })));
      return;
    }
    const detail = h("div", { class: "view", "aria-live": "polite" });
    const details = () => {
      const node = graph.nodes.find((item) => item.id === state.selected);
      const facts = state.selected === null ? graph.edges
        : graph.edges.filter((edge) => edge.source === state.selected || edge.target === state.selected);
      fill(detail,
        node && node.summary ? card(node.name, h("p", { class: "small", text: node.summary })) : "",
        card(`사실 ${formatNumber(facts.length)}건`, facts.length ? h("ul", { class: "list" }, facts.map((edge) => h("li", { class: "list-item" },
          h("div", { class: edge.current ? "" : "muted", text: edge.fact }),
          h("div", { class: "small muted", text: caption(edge) })))) : null));
    };
    fill(body,
      card(null, diagram(graph, details)),
      h("p", { class: "outside-note", text: "● 사람 ■ 사건 · 점선은 지난 사실만 남은 연결 · 노드를 누르면 그 노드의 사실만 봐요." }),
      detail);
    details();
  };

  const load = async () => {
    const graph = await api("GET", `/v1/contacts/${seg(contact.id)}/graph`);
    state.graph = { ...graph, nodes: Array.isArray(graph?.nodes) ? graph.nodes : [], edges: Array.isArray(graph?.edges) ? graph.edges : [] };
    state.selected = null;
    render();
  };

  run(load);
  return { title: `${contact.alias} 맥락 그래프`, el: body, refresh: () => run(load) };
}

// ---------- 이력 ----------

/** 최근 첨삭(`GET /v1/rewrites?contact_id=&limit=&before=`). 상대별로 좁혀 보고, 더 보기로 앞의 기록을 잇는다. */
function historyRoot() {
  const state = { rewrites: [], loaded: false, more: false, request: 0 };
  const filter = h("select");
  const title = h("h2", { class: "card-title", text: "최근 첨삭" });
  const list = h("div");
  const moreButton = h("button", { type: "button", class: "btn", text: "더 보기", hidden: true, onclick: () => load(true) });

  const fillFilter = () => {
    fill(filter, h("option", { value: "", text: "전체" }), ...options(session.contacts.map((item) => [item.id, item.alias])));
  };

  const sync = () => {
    const keep = filter.value;
    fillFilter();
    filter.value = session.contacts.some((item) => item.id === keep) ? keep : "";
    if (filter.value !== keep) load();
    else render();
  };

  const render = () => {
    title.textContent = `최근 첨삭 ${formatNumber(state.rewrites.length)}건`;
    moreButton.hidden = !state.more;
    if (!state.rewrites.length) {
      fill(list, state.loaded ? h("p", { class: "row-text", text: "아직 받은 추천이 없어요." }) : "");
      return;
    }
    fill(list, h("ul", { class: "list" }, state.rewrites.map((rewrite) => h("li", null,
      h("button", { type: "button", class: "list-btn",
        onclick: () => pushView(rewriteDetail(rewrite, aliasOf(rewrite.contact_id), () => load())) },
      h("span", { class: "stack" },
        h("span", { class: "clamp2", text: rewrite.result.result_text }),
        h("span", { class: "small muted", text: [aliasOf(rewrite.contact_id), statusView(rewrite.result).label,
          `조건 ${rewrite.condition}`, formatDateTime(rewrite.created_at)].join(" · ") })))))));
  };

  /** 목록 조회는 화면을 막지 않는다. 늦게 온 응답은 버린다. append면 마지막 항목의 created_at 앞을 더 읽는다. */
  const load = async (append = false) => {
    const request = ++state.request;
    const epoch = session.epoch;
    const query = { limit: String(HISTORY_PAGE) };
    if (filter.value) query.contact_id = filter.value;
    const last = state.rewrites[state.rewrites.length - 1];
    if (append && last) query.before = last.created_at;
    try {
      const rows = await api("GET", "/v1/rewrites", { query });
      if (request !== state.request || epoch !== session.epoch) return;
      const page = Array.isArray(rows) ? rows : [];
      const known = new Set(append ? state.rewrites.map((item) => item.id) : []);
      state.rewrites = append ? [...state.rewrites, ...page.filter((item) => !known.has(item.id))] : page;
      state.more = page.length >= HISTORY_PAGE;
      state.loaded = true;
      render();
    } catch (error) {
      if (request === state.request && epoch === session.epoch && !ui.busy) notify(messageOf(error), true);
    }
  };

  filter.addEventListener("change", () => load());
  fillFilter();

  return {
    el: h("div", { class: "view" },
      h("section", { class: "card" }, field("상대", filter, { inline: true })),
      h("section", { class: "card" }, title, list, moreButton)),
    sync,
    appear: () => load(),
    resume: () => load(),
    refresh: () => load(),
  };
}

/** 이력 한 건: 문장(복사만), 주의, 지킨 항목, 바꾼 이유, 삭제(`DELETE /v1/rewrites/{id}`). */
function rewriteDetail(rewrite, alias, changed) {
  const result = rewrite.result;
  const recommended = result.status === "recommended";
  const text = h("p", { class: "result-text", lang: "ko", text: result.result_text });
  const chips = preservedChips(result);
  const reasons = (result.change_reasons ?? []).filter(Boolean);
  const fallback = recommended ? null : fallbackView(result, { history: true });

  const remove = async () => {
    const answer = await ask({ title: "이 기록 삭제", message: "이 첨삭 결과와 피드백을 지워요. 되돌릴 수 없어요.", confirm: "삭제", danger: true });
    if (!answer.ok) return;
    run(async () => {
      await api("DELETE", `/v1/rewrites/${seg(rewrite.id)}`);
      popView("history");
      await changed();
      notify("기록을 지웠어요.");
    });
  };

  return {
    title: "첨삭 결과",
    el: h("div", { class: "view" },
      card(statusView(result).label,
        fallback ? h("p", { class: "strong", text: fallback.headline }) : null,
        fallback ? h("p", { class: "small", text: fallback.lead }) : null,
        fallback && fallback.items.length ? h("ul", { class: "plain-list small" }, fallback.items.map((item) => h("li", { text: item }))) : null,
        text,
        h("p", { class: "small muted", text: `${alias} · ${metaLine(resultMeta(rewrite))} · ${formatDateTime(rewrite.created_at)}` }),
        recommended ? h("button", { type: "button", class: "btn primary", text: "복사하기", onclick: () => run(async () => {
          const copied = await copyText(result.result_text);
          if (!copied) selectText(text);
          notify(copied ? COPY_DONE : COPY_BLOCKED);
        }) }) : null),
      warningBox(result.warnings) ? card("주의", warningBox(result.warnings)) : null,
      chips.length ? card("지킨 항목", h("ul", { class: "preserved" }, chips.map((chip) => h("li", { text: `✓ ${chip.label}` })))) : null,
      reasons.length ? card("바꾼 이유", h("ul", { class: "plain-list" }, reasons.map((item) => h("li", { text: item })))) : null,
      h("button", { type: "button", class: "btn danger", text: "이 기록 삭제", onclick: remove })),
  };
}

// ---------- 계정 ----------

/** 오늘 사용량, 비밀번호 변경, 로그아웃, 계정 삭제. */
function accountRoot() {
  const account = session.user;
  const username = account.username;
  const hasPassword = account.providers.includes("password");
  const label = account.email || username;
  const usage = h("p", { class: "muted", hidden: true });
  const current = h("input", { type: "password", autocomplete: "current-password", maxlength: "128" });
  const next = h("input", { type: "password", autocomplete: "new-password", maxlength: "128" });
  const change = h("button", { type: "submit", class: "btn", text: "비밀번호 변경", disabled: true });
  const deleting = h("input", { type: "password", autocomplete: "current-password", maxlength: "128" });
  // 소셜 전용 계정은 확인할 비밀번호가 없다 → 확인 대화상자만 거치고 빈 비밀번호를 보낸다.
  const remove = h("button", { type: "submit", class: "btn danger", text: "계정 삭제", disabled: hasPassword });
  const syncButtons = () => {
    change.disabled = !current.value || !next.value;
    remove.disabled = hasPassword && !deleting.value;
  };
  for (const input of [current, next, deleting]) input.addEventListener("input", syncButtons);

  const loadUsage = async () => {
    const epoch = session.epoch;
    try {
      const data = await api("GET", "/v1/usage");
      if (epoch !== session.epoch) return;
      const count = data.rewrite_limit > 0
        ? `${formatNumber(data.rewrites)} / ${formatNumber(data.rewrite_limit)}회`
        : `${formatNumber(data.rewrites)}회`;
      usage.textContent = `오늘 첨삭 ${count} · ${formatNumber((data.input_tokens ?? 0) + (data.output_tokens ?? 0))} 토큰`;
      usage.hidden = false;
    } catch { /* 표시용 */ }
  };

  const fact = (name, value) => h("div", null, h("dt", { text: name }), h("dd", { class: "selectable", text: value }));

  const passwordForm = h("form", { class: "card", novalidate: true, onsubmit: (event) => {
    event.preventDefault();
    if (!current.value || !next.value) return;
    if (next.value.length < 8) { notify("새 비밀번호는 8자 이상이어야 해요.", true); return; }
    run(async () => {
      await api("POST", "/v1/auth/password",
        { body: { current_password: current.value, new_password: next.value }, verifiesPassword: true });
      current.value = "";
      next.value = "";
      syncButtons();
      notify("비밀번호를 바꿨어요. 다른 기기는 로그아웃됐어요.");
    });
  } },
  h("h2", { class: "card-title", text: "비밀번호" }),
  // 비밀번호 관리자가 어느 계정인지 알 수 있게 아이디를 함께 둔다 (화면에는 보이지 않는다).
  h("input", { type: "text", autocomplete: "username", value: username, hidden: true, readonly: true, tabindex: "-1", "aria-hidden": "true" }),
  field("현재 비밀번호", current),
  field("새 비밀번호 (8자 이상)", next),
  change,
  h("p", { class: "note", text: "바꾸면 다른 기기의 로그인은 모두 풀려요." }));

  const deleteForm = h("form", { class: "card", novalidate: true, onsubmit: async (event) => {
    event.preventDefault();
    if (hasPassword && !deleting.value) return;
    const answer = await ask({
      title: "계정 삭제",
      message: `${label} 계정과 모든 데이터를 지워요. 되돌릴 수 없어요.`,
      confirm: "계정 삭제",
      danger: true,
    });
    if (!answer.ok || (hasPassword && !deleting.value)) return;
    run(async () => {
      await api("DELETE", "/v1/auth/me",
        { body: { password: hasPassword ? deleting.value : "" }, verifiesPassword: hasPassword });
      deleting.value = "";
      store.remove("tab");
      endSession("계정을 삭제했어요.");
    });
  } },
  h("h2", { class: "card-title", text: "계정 삭제" }),
  hasPassword ? field("비밀번호 확인", deleting) : null,
  remove,
  h("p", { class: "note", text: hasPassword
    ? "상대·대화·첨삭 이력·맥락 그래프를 모두 지워요. 되돌릴 수 없어요."
    : "소셜 로그인 계정이라 비밀번호 없이 삭제해요. 상대·대화·첨삭 이력·맥락 그래프를 모두 지워요. 되돌릴 수 없어요." }));

  return {
    el: h("div", { class: "view" },
      card("계정",
        h("p", { class: "strong", text: account.displayName || username }),
        h("dl", { class: "facts" },
          fact("아이디", username || "알 수 없음"),
          fact("이메일", account.email || "등록 안 됨"),
          fact("이름", account.displayName || "등록 안 됨"),
          fact("로그인 수단", account.providers.map(providerLabel).join(", ") || "알 수 없음")),
        usage,
        account.isAdmin ? h("a", { class: "btn", href: "/admin", text: "관리자 화면 열기" }) : null,
        h("button", { type: "button", class: "btn", text: "로그아웃", onclick: logout })),
      hasPassword ? passwordForm : null,
      deleteForm,
      h("p", { class: "outside-note links" },
        h("a", { href: "/privacy", target: "_blank", rel: "noopener", text: "개인정보 처리방침" }), " · ",
        h("a", { href: "/terms", target: "_blank", rel: "noopener", text: "이용약관" }), " · ",
        h("a", { href: "/", text: "PC 화면으로" }))),
    appear: loadUsage,
  };
}

// ---------- 시작 ----------

function init() {
  $("login-form").addEventListener("submit", (event) => { event.preventDefault(); signIn(); });
  $("login-user").addEventListener("input", syncLoginButtons);
  $("login-pass").addEventListener("input", syncLoginButtons);
  $("to-signup").addEventListener("click", () => setGateMode("signup"));
  $("to-login").addEventListener("click", () => setGateMode("login"));
  $("signup-form").addEventListener("submit", (event) => { event.preventDefault(); signUp(); });
  for (const [name, id] of Object.entries(SIGNUP_FIELDS)) {
    // 고치기 시작하면 그 칸의 사유를 지운다. 비밀번호를 고치면 확인 칸의 '서로 다름'도 함께.
    $(id).addEventListener(name === "consent" ? "change" : "input", () => {
      setSignupError(name, "");
      if (name === "password") setSignupError("confirm", "");
    });
  }
  $("auth-google").addEventListener("click", () => startSocial("google"));
  $("auth-kakao").addEventListener("click", () => startSocial("kakao"));
  $("pending-back").addEventListener("click", leavePending);
  $("consent-form").addEventListener("submit", (event) => { event.preventDefault(); acceptConsent(); });
  $("consent-logout").addEventListener("click", logout);

  for (const button of document.querySelectorAll(".tab")) {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  }
  $("back").addEventListener("click", () => { if (!ui.busy) popView(); });
  $("refresh").addEventListener("click", () => (topView(nav.tab) ?? nav.roots[nav.tab])?.refresh?.());
  $("banner-close").addEventListener("click", () => notify(""));
  $("confirm-cancel").addEventListener("click", () => closeDialog(false));
  $("confirm-form").addEventListener("submit", (event) => { event.preventDefault(); closeDialog(true); });
  $("confirm").addEventListener("cancel", () => closeDialog(false));
  $("confirm").addEventListener("close", () => closeDialog(false));
  // 기기의 뒤로 가기: 탭 안에서 들어간 화면을 하나 닫는다.
  window.addEventListener("popstate", () => {
    if ($("confirm").open) { closeDialog(false); return; }
    if (!ui.busy && session.ready) popView();
  });
  // 뒤로-앞으로 캐시에서 되살아난 화면은 세션을 다시 확인한다 (만료됐으면 로그인 화면으로 돌아간다).
  window.addEventListener("pageshow", (event) => {
    if (event.persisted && session.user) api("GET", "/v1/auth/me").catch(() => {});
  });

  compose.wire();
  boot();
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
}
