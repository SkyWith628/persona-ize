// Persona_ize 모바일 웹. 빌드 없는 바닐라 JS(ES 모듈) — iPhone 앱(apps/ios/App)의 네 탭을 그대로 옮겼다.
// 인증: 같은 출처의 세션 쿠키(HttpOnly) + 쓰기 요청의 X-CSRF-Token.
// 화면에 넣는 글은 모두 textContent·텍스트 노드로만 넣는다 (innerHTML을 쓰지 않는다).

// ---------- 순수 함수 (DOM 없이 동작, 단위 확인용으로 내보낸다) ----------

export class ServiceError extends Error {
  /**
   * @param {string} message 사용자에게 보일 문장
   * @param {number} [status] HTTP 상태 (없으면 0)
   */
  constructor(message, status = 0) {
    super(message);
    this.name = "ServiceError";
    this.status = status;
  }
}

/** 도메인 오류는 `{code, message}`, 입력 검증 오류는 FastAPI 기본형 `{detail: [{msg}]}`로 온다. */
export function failureMessage(data, status) {
  if (data && typeof data.message === "string" && data.message) return data.message;
  const first = data && Array.isArray(data.detail) ? data.detail[0] : null;
  if (first && typeof first.msg === "string") return `입력을 확인하세요: ${first.msg}`;
  if (status === 401) return "로그인이 만료되었습니다. 다시 로그인하세요.";
  if (status === 429) return "요청이 너무 많습니다. 잠시 뒤 다시 시도하세요.";
  return "서버 응답을 확인하세요.";
}

export const Labels = {
  relationships: [["superior", "상사"], ["colleague", "동료·동기"], ["subordinate", "후배"], ["friend", "친구"],
    ["family", "가족"], ["partner", "연인"], ["acquaintance", "지인·거래처"]],
  conditions: [["D", "D 페르소나 + 과거 표현 + 맥락"], ["C", "C 페르소나 + 과거 표현"],
    ["B", "B 페르소나"], ["A", "A 기준선(페르소나 없음)"]],
  // 페르소나 5단계의 말 풀이. 값은 1~5.
  formality: ["반말·캐주얼", "편한 말", "보통", "정중", "매우 정중"],
  friendliness: ["거리감 있게", "낮음", "보통", "친근", "매우 친근"],
  directness: ["많이 돌려 말하기", "완곡", "보통", "분명하게", "결론부터"],

  step(names, value) {
    return names[Math.min(Math.max(Number(value) || 1, 1), names.length) - 1];
  },

  relationship(value) {
    const found = Labels.relationships.find(([key]) => key === value);
    return found ? found[1] : String(value ?? "");
  },

  /** 원문 유지 사유(서버 검증 항목)를 사용자 말로. */
  violation(check) {
    switch (check) {
      case "preserve_missing": return "지켜야 할 내용이 빠졌어요";
      case "fact_added": return "초안에 없던 내용이 들어갔어요";
      case "negation_changed": return "가능·불가가 뒤바뀌었어요";
      case "forbidden_phrase": return "금지 표현이 들어갔어요";
      case "refusal": return "모델이 다듬지 않았어요";
      default: return "검증을 통과하지 못했어요";
    }
  },

  /** 서버 시각(ISO 8601)을 기기 형식으로. 못 읽으면 앞부분을 그대로 보인다. */
  date(iso) {
    const text = String(iso ?? "");
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return text.slice(0, 16).replace("T", " ");
    try {
      return new Intl.DateTimeFormat("ko-KR", { dateStyle: "medium", timeStyle: "short" }).format(date);
    } catch {
      return text.slice(0, 16).replace("T", " ");
    }
  },
};

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

// Swift의 `.newlines`와 같은 문자 집합. "\r\n"은 두 줄로 세어 줄 번호(메시지 id)가 iPhone 앱과 같게 나온다.
const NEWLINES = /[\n\r\u000B\u000C\u0085\u2028\u2029]/;
// Swift의 `.whitespaces`(줄바꿈 제외)에 맞춘 다듬기.
const trimSpaces = (text) => text.replace(/^[ \t\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]+|[ \t\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]+$/g, "");

/** 붙여 넣은 대화를 가져오기 요청으로 바꾼다. iPhone 앱 `ImportParser`와 같은 규칙. */
export const ImportParser = {
  /**
   * @param {string} text 한 줄에 `2026-10-05 09:25 | 화자 | 내용`, 메시지 JSON 배열, 또는 텔레그램 result.json
   * @param {string} me 내 발화로 볼 화자 이름. 나머지 이름은 상대로 지정한다.
   * @param {string} contact 상대 별칭 (JSON 배열 입력의 화자 지정에 쓴다)
   * @returns {{speaker_map: Record<string, string>, messages: Array<{id: string, speaker: string, sent_at: string, text: string}>}}
   */
  parse(text, me, contact) {
    const trimmed = String(text ?? "").trim();
    if (!trimmed) throw new ServiceError("가져올 대화를 붙여 넣으세요.");
    if (trimmed.startsWith("{")) return ImportParser.telegram(trimmed, me);
    if (trimmed.startsWith("[")) {
      let items = null;
      try { items = JSON.parse(trimmed); } catch { items = null; }
      const valid = Array.isArray(items) && items.every((item) => item && typeof item === "object"
        && ["id", "speaker", "sent_at", "text"].every((field) => typeof item[field] === "string"));
      if (!valid) throw new ServiceError("JSON 배열을 읽지 못했습니다. id·speaker·sent_at·text가 필요합니다.");
      const speakerMap = new Map(me ? [[me, "me"], [contact, "contact"]] : []);
      return {
        speaker_map: Object.fromEntries(speakerMap),
        messages: items.map((item) => ({ id: item.id, speaker: item.speaker, sent_at: item.sent_at, text: item.text })),
      };
    }
    const messages = [];
    const speakerMap = new Map();
    trimmed.split(NEWLINES).forEach((line, index) => {
      if (!trimSpaces(line)) return;
      const parts = line.split("|").map(trimSpaces);
      if (parts.length < 3 || !parts[0] || !parts[1]) {
        throw new ServiceError(`${index + 1}번째 줄: '날짜 시각 | 화자 | 내용' 형식이 아닙니다.`);
      }
      const speaker = parts[1];
      if (speaker !== "me" && speaker !== "contact") speakerMap.set(speaker, speaker === me ? "me" : "contact");
      messages.push({
        id: `m${String(index + 1).padStart(2, "0")}`,
        speaker,
        sent_at: ImportParser.timestamp(parts[0]),
        text: parts.slice(2).join(" | "),
      });
    });
    return { speaker_map: Object.fromEntries(speakerMap), messages };
  },

  /** 시간대가 없으면 한국 시간(+09:00)으로 본다. */
  timestamp(raw) {
    const when = String(raw).replace(" ", "T");
    const time = Array.from(when).slice(10).join("");
    if (time.includes("+") || time.includes("Z")) return when;
    const colons = time.split(":").length - 1;
    return `${when}${colons < 2 ? ":00" : ""}+09:00`;
  },

  /**
   * PC용 텔레그램의 대화 내보내기(result.json). 일반 메시지만 쓰고 서비스 메시지·빈 글은 건너뛴다.
   * `text`는 문자열이거나 조각 배열(문자열 또는 `{type, text}`)이다.
   */
  telegram(json, me) {
    let root = null;
    try { root = JSON.parse(json); } catch { root = null; }
    const items = root && typeof root === "object" && !Array.isArray(root) ? root.messages : null;
    const isObject = (item) => item !== null && typeof item === "object" && !Array.isArray(item);
    if (!Array.isArray(items) || !items.every(isObject)) {
      throw new ServiceError("텔레그램 내보내기 파일(result.json)을 읽지 못했습니다.");
    }
    if (!me) throw new ServiceError("내 이름으로 쓸 화자를 텔레그램 표시 이름 그대로 입력하세요.");
    const messages = [];
    const speakerMap = new Map();
    for (const item of items) {
      if (item.type !== "message") continue;
      const speaker = item.from;
      if (typeof speaker !== "string" || !speaker || item.id === undefined || item.id === null) continue;
      const text = ImportParser.telegramText(item.text).trim();
      if (!text) continue;
      let sentAt = null;
      const unix = typeof item.date_unixtime === "string" && /^[+-]?(\d+\.?\d*|\.\d+)$/.test(item.date_unixtime)
        ? Number(item.date_unixtime) : NaN;
      const moment = new Date(unix * 1000);
      if (Number.isFinite(unix) && !Number.isNaN(moment.getTime())) {
        sentAt = moment.toISOString().replace(/\.\d{3}Z$/, "Z");
      } else if (typeof item.date === "string") {
        sentAt = ImportParser.timestamp(item.date);
      } else {
        continue;
      }
      speakerMap.set(speaker, speaker === me ? "me" : "contact");
      messages.push({ id: `tg${item.id}`, speaker, sent_at: sentAt, text: Array.from(text).slice(0, 4000).join("") });
    }
    if (!messages.length) throw new ServiceError("가져올 일반 메시지가 없습니다.");
    if (speakerMap.get(me) !== "me") {
      throw new ServiceError(`‘${me}’ 화자가 파일에 없습니다. 화자: ${[...speakerMap.keys()].sort().join(", ")}`);
    }
    return { speaker_map: Object.fromEntries(speakerMap), messages };
  },

  telegramText(value) {
    if (typeof value === "string") return value;
    if (!Array.isArray(value)) return "";
    return value.map((part) => {
      if (typeof part === "string") return part;
      return part && typeof part === "object" && typeof part.text === "string" ? part.text : "";
    }).join("");
  },
};

/** 상대를 가운데에 두고 직접 이어진 노드를 안쪽 원, 나머지를 바깥 원에 놓는다. 좌표는 0~1. */
export const GraphLayout = {
  innerRadius: 0.21,
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
      result.set(id, point(angle, GraphLayout.innerRadius));
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

function setProps(el, props) {
  for (const [name, value] of Object.entries(props ?? {})) {
    if (value === null || value === undefined || value === false || name === "value") continue;
    if (name === "class") el.setAttribute("class", value);
    else if (name === "text") el.textContent = value;
    else if (name.startsWith("on")) el.addEventListener(name.slice(2), value);
    else el.setAttribute(name, value === true ? "" : String(value));
  }
}

/** 요소를 만든다. 자식 문자열은 텍스트 노드가 되므로 서버·사용자 글이 HTML로 해석되지 않는다. */
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  setProps(el, props);
  el.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
  if (props && props.value !== undefined && props.value !== null) el.value = props.value;
  return el;
}

function svg(tag, props, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  setProps(el, props);
  el.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
  return el;
}

/** 라벨과 입력을 묶는다. */
function field(label, control, { inline = false } = {}) {
  const id = control.id || uid("f");
  control.id = id;
  return h("div", { class: inline ? "field inline" : "field" }, h("label", { for: id, text: label }), control);
}

const options = (pairs) => pairs.map(([value, label]) => h("option", { value, text: label }));

function card(title, ...children) {
  return h("section", { class: "card" }, title ? h("h2", { class: "card-title", text: title }) : null, ...children);
}

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

function uuid() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

// ---------- 상태 ----------

const TABS = { compose: "작성", contacts: "상대", history: "이력", account: "계정" };
const REQUEST_TIMEOUT_MS = 120_000;
const MAX_FILE_BYTES = 2_000_000;

/** 로그인한 동안의 연결과 상대 목록. epoch는 로그인·로그아웃마다 올라 늦게 온 응답을 버리는 데 쓴다. */
const session = { user: null, csrf: "", contacts: [], epoch: 0 };
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
  const text = ui.busy ? "처리 중" : ui.notice;
  if ($("banner-text").textContent !== text) $("banner-text").textContent = text;
}

function notify(message, error = false) {
  ui.notice = message;
  ui.error = error;
  renderBanner();
}

function setBusy(busy) {
  ui.busy = busy;
  $("screens").disabled = busy;
  for (const id of ["login-user", "login-pass", "back", "refresh"]) $(id).disabled = busy;
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
    if (!(error instanceof ServiceError)) console.error(error);
    notify(error instanceof ServiceError ? error.message : "처리하지 못했습니다. 다시 시도하세요.", true);
  }).finally(() => {
    setBusy(false);
    const lost = !document.activeElement || document.activeElement === document.body;
    if (lost && focused instanceof HTMLElement && focused.isConnected && !focused.disabled && focused.offsetParent !== null) {
      focused.focus({ preventScroll: true });
    } else if (lost && !session.user && !$("login").hidden) {
      $("login-user").focus({ preventScroll: true });
    }
  });
}

// ---------- API ----------

/**
 * 같은 출처의 JSON API를 부른다.
 * @param {string} method
 * @param {string} path
 * @param {{body?: unknown, query?: Record<string, string>, key?: string, signIn?: boolean, verifiesPassword?: boolean}} [options]
 *   signIn: 로그인·가입 요청(401이 세션 만료가 아님). verifiesPassword: 비밀번호를 확인하는 요청(401이 오답일 수 있음).
 */
async function api(method, path, options = {}) {
  const epoch = session.epoch;
  const headers = { Accept: "application/json" };
  if (method !== "GET" && session.csrf) headers["X-CSRF-Token"] = session.csrf;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.key) headers["Idempotency-Key"] = options.key;
  const url = options.query ? `${path}?${new URLSearchParams(options.query)}` : path;
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), REQUEST_TIMEOUT_MS);
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
    raw = await response.text();
  } catch (error) {
    if (error && error.name === "AbortError") throw new ServiceError("응답이 늦어지고 있습니다. 잠시 뒤 다시 시도하세요.");
    throw new ServiceError("서버에 연결하지 못했습니다. 네트워크를 확인하세요.");
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  if (raw) {
    try { data = JSON.parse(raw); } catch { data = null; }
  }
  if (response.ok) return data;
  const failure = new ServiceError(failureMessage(data, response.status), response.status);
  if (response.status === 401 && session.user && epoch === session.epoch && !options.signIn) {
    // 비밀번호 확인 요청의 401은 '비밀번호가 틀림'일 수 있다. 세션이 살아 있으면 서버 문구를 그대로 보인다.
    if (options.verifiesPassword && await sessionAlive()) throw failure;
    if (epoch === session.epoch) endSession("로그인이 만료되었습니다.", true);
  }
  throw failure;
}

async function sessionAlive() {
  try {
    const response = await fetch("/v1/auth/me", { credentials: "same-origin", cache: "no-store", redirect: "error" });
    return response.ok;
  } catch {
    return true; // 확인하지 못했으면 로그아웃시키지 않는다.
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

// ---------- 세션 ----------

function syncLoginButtons() {
  const incomplete = ui.busy || !$("login-user").value.trim() || !$("login-pass").value;
  $("login-go").disabled = incomplete;
  $("login-register").disabled = incomplete;
}

function sign(register) {
  const username = $("login-user").value.trim();
  const password = $("login-pass").value;
  if (!username || !password) return;
  run(async () => {
    const auth = await api("POST", register ? "/v1/auth/register" : "/v1/auth/login",
      { body: { username, password }, signIn: true });
    $("login-pass").value = "";
    await startSession({ username: auth.username ?? username, csrf: auth.csrf_token });
  });
}

/** 상대 목록을 받은 뒤에 화면을 연다. 실패하면 로그인 화면에 사유가 남는다. */
async function startSession({ username, csrf }) {
  session.csrf = csrf ?? "";
  const contacts = await api("GET", "/v1/contacts");
  session.epoch += 1;
  session.user = { username };
  session.contacts = Array.isArray(contacts) ? contacts : [];
  $("login-pass").value = "";
  $("login-user").value = "";
  syncLoginButtons();
  mountApp();
}

/** 화면을 로그인으로 돌리고 이 브라우저에 남긴 상대 기록·입력을 지운다. 서버 세션 정리는 호출한 쪽이 먼저 한다. */
function endSession(message = "", error = false) {
  session.epoch += 1;
  session.user = null;
  session.csrf = "";
  session.contacts = [];
  store.remove("lastContact");
  store.remove("recentContacts");
  closeDialog(false);
  for (const tab of Object.keys(nav.stacks)) {
    for (const view of nav.stacks[tab]) view.dispose?.();
    nav.stacks[tab] = [];
  }
  nav.roots = {};
  for (const tab of ["contacts", "history", "account"]) $(`tab-${tab}`).replaceChildren();
  compose.reset();
  document.body.classList.remove("signed-in");
  $("app").hidden = true;
  $("login").hidden = false;
  $("login-pass").value = "";
  syncLoginButtons();
  notify(message, error);
  $("login-user").focus();
}

function mountApp() {
  nav.roots = { contacts: contactsRoot(), history: historyRoot(), account: accountRoot() };
  for (const tab of ["contacts", "history", "account"]) $(`tab-${tab}`).replaceChildren(nav.roots[tab].el);
  $("login").hidden = true;
  $("app").hidden = false;
  document.body.classList.add("signed-in");
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

async function boot() {
  let me = null;
  let problem = "";
  try {
    me = await api("GET", "/v1/auth/me");
  } catch (error) {
    if (!(error instanceof ServiceError) || error.status !== 401) problem = error.message;
  }
  $("boot").hidden = true;
  if (me) {
    try {
      await startSession({ username: me.username, csrf: me.csrf_token });
      return;
    } catch (error) {
      session.csrf = "";
      problem = error.message;
    }
  }
  $("login").hidden = false;
  if (problem) notify(problem, true);
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
    if (ui.notice) notify(""); // 안내는 그 화면의 것이다. 다른 탭까지 따라가지 않는다.
  }
  (topView(tab) ? null : nav.roots[tab])?.appear?.();
  if (tab === "compose") compose.appear();
}

/** 탭 안에서 다음 화면으로 들어간다. 이전 화면은 입력을 그대로 둔 채 숨긴다. 주소(URL)는 바꾸지 않는다. */
function pushView(view) {
  const tab = nav.tab;
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

// ---------- 작성 ----------

const compose = {
  contact: "",
  recents: "",
  result: null,
  edited: "",
  fixing: false,
  usage: null,
  key: "",
  answered: false,

  get selected() { return session.contacts.find((item) => item.id === this.contact) ?? null; },
  get alias() { return this.selected?.alias ?? "상대"; },
  get condition() { return $("c-condition").value || "D"; },
  get hasPersona() { return this.selected?.persona_version !== null && this.selected?.persona_version !== undefined; },
  get blocked() {
    return !this.contact || (!this.hasPersona && this.condition !== "A") || !$("c-draft").value.trim();
  },
  get usageLine() {
    return this.usage ? ` 오늘 ${this.usage.rewrites} / ${this.usage.rewrite_limit}회 사용.` : "";
  },

  /** 한 번만: 고정 요소에 동작을 건다. */
  wire() {
    $("c-condition").append(...options(Labels.conditions));
    $("c-condition").value = "D";
    $("c-draft").addEventListener("input", () => this.invalidate());
    $("c-situation").addEventListener("input", () => this.invalidate());
    $("c-condition").addEventListener("change", () => { this.invalidate(); this.renderWho(); });
    $("c-contact").addEventListener("change", (event) => this.choose(event.target.value));
    $("c-go").addEventListener("click", () => this.rewrite());
  },

  /** 로그인 직후: 이 브라우저가 기억한 상대를 불러온다. */
  start() {
    this.contact = store.get("lastContact");
    this.recents = store.get("recentContacts");
    this.key = uuid();
    this.sync();
    this.loadUsage();
  },

  reset() {
    Object.assign(this, { contact: "", recents: "", result: null, edited: "", fixing: false, usage: null, answered: false });
    $("c-draft").value = "";
    $("c-situation").value = "";
    $("c-condition").value = "D";
    $("c-result").replaceChildren();
  },

  appear() { this.loadUsage(); },

  /** 상대 목록이 바뀌었을 때. 고른 상대가 사라졌으면 페르소나가 있는 첫 상대로 옮긴다. */
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

  choose(id, quiet = false) {
    if (id === this.contact) return;
    this.contact = id;
    if (id) store.set("lastContact", id); else store.remove("lastContact");
    this.invalidate(quiet);
    this.renderWho();
  },

  /** 입력이 바뀌면 이전 추천을 적용할 수 없게 하고 새 요청 키를 쓴다. */
  invalidate(quiet = false) {
    this.key = uuid();
    this.answered = false;
    this.fixing = false;
    if (this.result) {
      this.result = null;
      this.renderResult();
    }
    if (!quiet && !ui.busy && ui.notice) notify("");
    this.renderGo();
  },

  renderGo() {
    $("c-go").textContent = `${this.alias}에게 맞게`;
    $("c-go").disabled = this.blocked;
  },

  renderWho() {
    const none = session.contacts.length === 0;
    $("c-empty").hidden = !none;
    $("c-pick").hidden = none;
    $("c-recents").replaceChildren(...Recents.pick(this.recents, session.contacts).map((item) => h("button", {
      type: "button", class: "chip", "aria-pressed": String(item.id === this.contact), text: item.alias,
      onclick: () => this.choose(item.id),
    })));
    $("c-contact").replaceChildren(...options(session.contacts.map((item) => [item.id, item.alias])));
    $("c-contact").value = this.contact;
    $("c-nopersona").hidden = none || this.hasPersona || this.condition === "A";
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

  /** 실패 뒤 다시 누르면 같은 Idempotency-Key로 재시도한다 (키는 성공하거나 입력이 바뀔 때만 바뀐다). */
  rewrite() {
    if (this.blocked) return;
    run(async () => {
      const current = this.key;
      const contact = this.contact;
      try {
        const response = await api("POST", "/v1/rewrites", {
          key: current,
          body: { contact_id: contact, draft_text: $("c-draft").value, situation: $("c-situation").value, condition: this.condition },
        });
        if (current !== this.key) return;
        this.result = response;
        this.edited = response.result.result_text;
        this.fixing = false;
        this.answered = false;
        this.key = uuid();
        this.recents = Recents.push(contact, this.recents);
        store.set("recentContacts", this.recents);
        this.renderWho();
        this.renderResult(true);
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
    if (!this.result) { box.replaceChildren(); return; }
    const rewrite = this.result;
    const view = rewrite.result.status === "recommended" ? this.recommendation(rewrite) : this.kept(rewrite);
    box.replaceChildren(...view);
    if (reveal) {
      const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      box.scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "start" });
    }
  },

  // 추천: 문장 → 보존 항목 → 주의 → 복사
  recommendation(rewrite) {
    const result = rewrite.result;
    const empty = () => !this.edited.trim();
    const copy = h("button", { type: "button", class: "btn primary", text: "복사하기", disabled: empty(), onclick: () => this.copy(rewrite) });
    const body = this.fixing
      ? h("textarea", { id: "c-edited", rows: "4", "aria-label": "추천 문장 고치기", value: this.edited,
        oninput: (event) => { this.edited = event.target.value; copy.disabled = empty(); } })
      : h("p", { id: "c-edited", class: "result-text", text: this.edited });
    const preserved = result.preserved_items ?? [];
    const warnings = result.warnings ?? [];
    return [
      h("section", { class: "card", "aria-labelledby": "c-result-title" },
        h("h2", { class: "card-title", id: "c-result-title", text: `${this.alias}에게 맞게` }),
        body,
        preserved.length ? h("ul", { class: "preserved", "aria-label": "지킨 내용" },
          preserved.map((item) => h("li", { text: `✓ ${item.text}` }))) : null,
        warnings.length ? h("div", { class: "warning" },
          h("span", { class: "strong", text: "⚠ 확인해 주세요" }),
          h("ul", null, warnings.map((warning) => h("li", { text: warning })))) : null,
        h("div", { class: "link-row" },
          h("button", { type: "button", class: "text-btn", text: "다시 만들기", onclick: () => this.rewrite() }),
          h("button", { type: "button", class: "text-btn", text: this.fixing ? "고치기 완료" : "직접 고치기",
            onclick: () => { this.fixing = !this.fixing; this.renderResult(); $("c-edited").focus?.(); } }),
          h("button", { type: "button", class: "text-btn", text: "초안 그대로", onclick: () => this.reject() })),
        copy,
        h("button", { type: "button", class: "btn", text: "초안에 적용", onclick: () => this.apply(rewrite) }),
        (result.change_reasons ?? []).map((reason) => h("p", { class: "small muted", text: `바꾼 이유 · ${reason}` }))),
      this.footer("복사만 해요. 전송은 직접 합니다."),
    ];
  },

  // 추천하지 않는 경우
  kept(rewrite) {
    const violations = rewrite.result.validation?.violations ?? [];
    const warnings = rewrite.result.warnings ?? [];
    return [
      h("section", { class: "card", "aria-labelledby": "c-result-title" },
        h("h2", { class: "card-title", id: "c-result-title", text: `${this.alias}에게 맞게` }),
        h("p", { class: "headline", text: "✋ 초안을 그대로 뒀어요" }),
        !violations.length && !warnings.length
          ? h("p", { class: "row-text", text: "사실 보존을 확인하지 못해 추천하지 않았습니다. 초안은 그대로 있어요." }) : null,
        violations.map((violation) => h("div", { class: "stack" },
          h("span", { class: "strong", text: Labels.violation(violation.check) }),
          h("span", { class: "small muted", text: violation.detail }))),
        warnings.map((warning) => h("p", { class: "small", text: warning })),
        h("div", { class: "link-row" },
          h("button", { type: "button", class: "text-btn", text: "닫기", onclick: () => { this.result = null; this.renderResult(); } }),
          h("button", { type: "button", class: "text-btn", text: "다시 만들기", onclick: () => this.rewrite() }))),
      this.footer("입력한 초안은 바뀌지 않았습니다."),
    ];
  },

  /** 피드백은 추천 하나에 한 번만 보낸다. 직접 고친 문장은 함께 보낸다. */
  async sendFeedback(rewrite, accepted, text = null, reason = null) {
    if (this.answered) return;
    this.answered = true;
    const changed = text !== null && text !== rewrite.result.result_text ? text : null;
    await api("POST", `/v1/rewrites/${encodeURIComponent(rewrite.id)}/feedback`,
      { body: { accepted, edited_text: changed, reason } });
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
      notify(copied ? "복사했습니다. 메신저에 붙여 넣고 직접 보내세요." : COPY_BLOCKED);
    });
  },

  apply(rewrite) {
    const text = this.edited;
    run(async () => {
      // 초안을 바꾸면 invalidate가 추천을 닫는다. 피드백은 그 전에 상태를 잡아 둔다.
      const first = !this.answered;
      $("c-draft").value = text;
      this.invalidate();
      if (first) {
        const changed = text === rewrite.result.result_text ? null : text;
        await api("POST", `/v1/rewrites/${encodeURIComponent(rewrite.id)}/feedback`,
          { body: { accepted: true, edited_text: changed, reason: null } });
      }
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
    });
  },
};

const COPY_BLOCKED = "자동 복사가 막혀 있어 문장을 선택해 두었습니다. 직접 복사하세요.";

/** 클립보드에 쓴다. 클릭 처리 안에서 불러야 한다. 막히면 false. */
async function copyText(text) {
  try {
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function selectText(el) {
  if (!el) return;
  try {
    const range = document.createRange();
    range.selectNodeContents(el);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  } catch { /* 선택을 못 해도 문장은 화면에 있다 */ }
}

// ---------- 상대 ----------

/** 상대 목록과 등록. 상대를 누르면 페르소나·과거 대화로 들어간다. */
function contactsRoot() {
  const list = h("div");
  const alias = h("input", { type: "text", maxlength: "50", placeholder: "예: 박지현", autocomplete: "off" });
  const relationship = h("select", { value: "colleague" }, options(Labels.relationships));
  const add = h("button", { type: "submit", class: "btn primary", text: "추가", disabled: true });
  alias.addEventListener("input", () => { add.disabled = !alias.value.trim(); });

  const sync = () => {
    if (!session.contacts.length) {
      list.replaceChildren(h("p", { class: "row-text", text: "아직 상대가 없습니다. 아래에서 별칭과 관계를 넣고 추가하세요." }));
      return;
    }
    list.replaceChildren(h("ul", { class: "list" }, session.contacts.map((contact) => h("li", null,
      h("button", { type: "button", class: "list-btn", onclick: () => pushView(contactDetail(contact)) },
        h("span", { class: "stack" },
          h("span", { text: contact.alias }),
          h("span", { class: "small muted", text: Labels.relationship(contact.relationship_type)
            + (contact.persona_version !== null && contact.persona_version !== undefined
              ? ` · 페르소나 v${contact.persona_version}` : " · 페르소나 없음") })))))));
  };

  const form = h("form", { class: "card", novalidate: true, onsubmit: (event) => {
    event.preventDefault();
    const name = Array.from(alias.value.trim()).slice(0, 50).join("");
    if (!name) return;
    run(async () => {
      await api("POST", "/v1/contacts", { body: { alias: name, relationship_type: relationship.value } });
      alias.value = "";
      add.disabled = true;
      await reloadContacts();
    });
  } },
  h("h2", { class: "card-title", text: "상대 추가" }),
  field("별칭", alias),
  field("관계", relationship, { inline: true }),
  add,
  h("p", { class: "note", text: "실명 대신 별칭을 써도 됩니다." }));

  sync();
  return {
    el: h("div", { class: "view" }, card("상대", list), form),
    sync,
    refresh: () => run(reloadContacts),
  };
}

const PERSONA_AXES = [
  ["formality", "격식", Labels.formality],
  ["friendliness", "친근함", Labels.friendliness],
  ["directness", "돌려 말하기 ↔ 직설", Labels.directness],
];

/** 상대 한 명의 페르소나 편집, 과거 대화방 목록, 삭제. */
function contactDetail(contact) {
  const path = `/v1/contacts/${encodeURIComponent(contact.id)}`;
  const state = { version: null, conversations: [] };
  const personaTitle = h("h2", { class: "card-title", text: "페르소나 (저장 전)" });
  const sliders = {};
  const axisRows = PERSONA_AXES.map(([name, label, names]) => {
    const id = uid("axis");
    const caption = h("label", { for: id });
    const slider = h("input", { id, type: "range", min: "1", max: "5", step: "1", value: "3" });
    const paint = () => {
      caption.textContent = `${label} · ${Labels.step(names, slider.value)}`;
      slider.setAttribute("aria-valuetext", Labels.step(names, slider.value));
    };
    slider.addEventListener("input", paint);
    sliders[name] = { slider, paint };
    paint();
    return h("div", { class: "field" }, caption, slider,
      h("div", { class: "scale-ends", "aria-hidden": "true" }, h("span", { text: names[0] }), h("span", { text: names[4] })));
  });
  const length = h("select", { value: "same" }, options([["shorter", "짧게"], ["same", "원문만큼"], ["longer", "길게"]]));
  const emoji = h("select", { value: "none" }, options([["none", "없음"], ["rare", "가끔"], ["often", "자주"]]));
  const address = h("input", { type: "text", maxlength: "30", placeholder: "예: 팀장님, 형", autocomplete: "off" });
  const preferred = h("input", { type: "text", placeholder: "쉼표로 구분", autocomplete: "off" });
  const forbidden = h("input", { type: "text", placeholder: "예: ㅋㅋ, ㅠ", autocomplete: "off" });
  const notes = h("textarea", { rows: "2", maxlength: "500", placeholder: "예: 업무 얘기는 결론부터" });
  const saveButton = h("button", { type: "button", class: "btn primary", text: "저장", onclick: () => save() });

  const show = (persona) => {
    const settings = persona.settings ?? {};
    state.version = persona.version;
    personaTitle.textContent = `페르소나 v${persona.version}`;
    saveButton.textContent = "저장 (새 버전)";
    for (const [name] of PERSONA_AXES) {
      sliders[name].slider.value = String(settings[name] ?? 3);
      sliders[name].paint();
    }
    length.value = settings.length ?? "same";
    emoji.value = settings.emoji ?? "none";
    address.value = settings.address_term ?? "";
    preferred.value = (settings.preferred_phrases ?? []).join(", ");
    forbidden.value = (settings.forbidden_phrases ?? []).join(", ");
    notes.value = settings.notes ?? "";
  };

  const phrases = (text) => text.split(",").map((item) => item.trim()).filter(Boolean);

  /** 서버가 버전을 비교한다. 다른 기기에서 먼저 저장했으면 409 메시지가 표시된다. */
  const save = () => run(async () => {
    const settings = {
      formality: Number(sliders.formality.slider.value),
      friendliness: Number(sliders.friendliness.slider.value),
      directness: Number(sliders.directness.slider.value),
      length: length.value,
      emoji: emoji.value,
      address_term: address.value.trim() || null,
      preferred_phrases: phrases(preferred.value),
      forbidden_phrases: phrases(forbidden.value),
      notes: notes.value.trim() || null,
    };
    show(await api("PUT", `${path}/persona`, { body: { settings, expected_version: state.version ?? 0 } }));
    await reloadContacts();
    notify("페르소나를 저장했습니다.");
  });

  const conversationList = h("div");
  const renderConversations = () => {
    if (!state.conversations.length) {
      conversationList.replaceChildren(h("p", { class: "row-text", text: "대화방이 없습니다. 없어도 A·B 조건 첨삭은 됩니다." }));
      return;
    }
    conversationList.replaceChildren(h("ul", { class: "list" }, state.conversations.map((conversation) => h("li", null,
      h("button", { type: "button", class: "list-btn",
        onclick: () => pushView(conversationView(conversation, contact, loadConversations)) },
      h("span", { class: "stack" }, h("span", { text: conversation.title })))))));
  };
  const loadConversations = async () => {
    const epoch = session.epoch;
    try {
      const list = await api("GET", "/v1/conversations", { query: { contact_id: contact.id } });
      if (epoch !== session.epoch) return;
      state.conversations = Array.isArray(list) ? list : [];
      renderConversations();
    } catch { /* 목록 갱신 실패는 조용히 둔다 (이전 목록 유지) */ }
  };

  const title = h("input", { type: "text", maxlength: "200", autocomplete: "off" });
  const meName = h("input", { type: "text", maxlength: "50", placeholder: "예: 김민수", autocomplete: "off" });
  const conversationForm = h("form", { class: "card", novalidate: true, onsubmit: (event) => {
    event.preventDefault();
    run(async () => {
      const body = { contact_id: contact.id };
      if (title.value.trim()) body.title = title.value.trim();
      if (meName.value.trim()) body.me_name = meName.value.trim();
      await api("POST", "/v1/conversations", { body });
      title.value = "";
      await loadConversations();
    });
  } },
  h("h2", { class: "card-title", text: "과거 대화" }),
  conversationList,
  field("대화방 이름", title),
  field("내 이름 (그래프용)", meName),
  h("button", { type: "submit", class: "btn", text: "대화방 추가" }),
  h("p", { class: "note", text: "C·D 조건이 참고하는 재료입니다. 합성 대화만 넣으세요." }));

  const remove = async () => {
    const answer = await ask({
      title: "이 상대 삭제",
      message: `${contact.alias}의 대화방·페르소나·첨삭 이력을 모두 지웁니다. 되돌릴 수 없습니다.`,
      confirm: "삭제",
      danger: true,
    });
    if (!answer.ok) return;
    run(async () => {
      await api("DELETE", path);
      await reloadContacts();
      popView("contacts");
    });
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
      h("p", { class: "note", text: "각 항목은 5단계입니다. 직접 고른 값이며 서버가 추정하지 않습니다. 호칭은 여기 넣은 것만 씁니다." })),
    conversationForm,
    h("section", { class: "card" },
      h("ul", { class: "list" }, h("li", null,
        h("button", { type: "button", class: "list-btn", onclick: () => pushView(graphView(contact)) },
          h("span", { class: "stack" }, h("span", { text: "맥락 그래프 보기" })))))),
    h("p", { class: "outside-note", text: "가져온 대화에서 서버가 뽑은 사람·사건·사실입니다. D 조건 첨삭이 참고합니다." }),
    h("button", { type: "button", class: "btn danger", text: "이 상대 삭제", onclick: remove }));

  renderConversations();
  run(async () => {
    let persona = null;
    try {
      persona = await api("GET", `${path}/persona`);
    } catch (error) {
      if (!(error instanceof ServiceError) || error.status !== 404) throw error; // 저장된 페르소나가 없으면 404
    }
    if (persona) show(persona);
    const list = await api("GET", "/v1/conversations", { query: { contact_id: contact.id } });
    state.conversations = Array.isArray(list) ? list : [];
    renderConversations();
  });

  return { title: contact.alias, el };
}

const IMPORT_METHODS = [["paste", "붙여넣기"], ["telegram", "텔레그램"], ["kakao", "카카오톡"]];
const IMPORT_HELP = {
  paste: "한 줄에 한 메시지: 2026-10-05 09:25 | 박지현 | 내용. 이름 대신 me / contact도 됩니다.",
  telegram: "PC용 텔레그램에서만 만들 수 있어요. 대화방 메뉴 → 채팅 기록 내보내기 → JSON으로 만든 result.json을 첨부하세요.",
  kakao: "카카오톡 내보내기(.txt)는 기기와 버전에 따라 형식이 달라 아직 자동으로 읽지 못합니다. 대화를 복사해 ‘붙여넣기’ 형식으로 넣어 주세요.",
};

/** 고른 파일을 UTF-8 글로 읽는다. 큰 파일은 요청 한 번에 담기 어려워 막는다. */
function readTextFile(file) {
  return new Promise((resolve, reject) => {
    if (file.size > MAX_FILE_BYTES) {
      reject(new ServiceError("파일이 너무 큽니다(2MB 초과). 기간을 나눠 내보내 주세요."));
      return;
    }
    const reader = new FileReader();
    reader.onerror = () => reject(new ServiceError("파일을 읽지 못했습니다."));
    reader.onload = () => {
      try {
        resolve(new TextDecoder("utf-8", { fatal: true }).decode(reader.result));
      } catch {
        reject(new ServiceError("UTF-8 글 파일만 읽을 수 있습니다."));
      }
    };
    reader.readAsArrayBuffer(file);
  });
}

/** 대화방 하나: 대화 가져오기, 분석 두 단계 표시, 저장된 메시지, 삭제. */
function conversationView(conversation, contact, changed) {
  const path = `/v1/conversations/${encodeURIComponent(conversation.id)}`;
  const state = { method: "paste", messages: [], job: null, alive: true, following: 0 };

  const help = h("p", { class: "small" });
  const text = h("textarea", { class: "mono", rows: "6", "aria-label": "가져올 합성 대화", spellcheck: "false" });
  const meName = h("input", { type: "text", maxlength: "64", placeholder: "예: 김민수", autocomplete: "off",
    value: conversation.me_name ?? "" });
  const importButton = h("button", { type: "button", class: "btn primary", text: "가져오기", disabled: true,
    onclick: () => importText() });
  text.addEventListener("input", () => { importButton.disabled = !text.value.trim(); });
  const picker = h("input", { type: "file", accept: ".txt,.json,text/plain,application/json", hidden: true, tabindex: "-1",
    "aria-hidden": "true" });
  picker.addEventListener("change", () => {
    const file = picker.files && picker.files[0];
    picker.value = "";
    if (!file) return;
    run(async () => {
      text.value = await readTextFile(file);
      importButton.disabled = !text.value.trim();
    });
  });
  const inputs = h("div", { class: "group" },
    text,
    picker,
    h("button", { type: "button", class: "btn", text: "대화 파일 첨부하기 (.txt · .json)", onclick: () => picker.click() }),
    field("내 이름으로 쓸 화자", meName),
    importButton);
  const methods = h("div", { class: "segmented", role: "group", "aria-label": "가져오는 방법" },
    IMPORT_METHODS.map(([value, label]) => h("button", { type: "button", "data-method": value, text: label,
      onclick: () => { state.method = value; renderMethod(); } })));
  const renderMethod = () => {
    for (const button of methods.children) button.setAttribute("aria-pressed", String(button.dataset.method === state.method));
    help.textContent = IMPORT_HELP[state.method];
    inputs.hidden = state.method === "kakao";
  };

  const analysis = h("div", { "aria-live": "polite" });
  const messagesTitle = h("h2", { class: "card-title" });
  const messageList = h("div");

  /** 말투(문체 묶음)는 가져오는 즉시 쓸 수 있고, 사건 정리(그래프)는 서버에서 뒤이어 돈다. */
  const renderAnalysis = () => {
    const job = state.job;
    if (!job) { analysis.replaceChildren(); return; }
    const mine = state.messages.filter((message) => message.speaker_role === "me").length;
    const parts = [
      h("h2", { class: "card-title", text: "분석" }),
      h("p", { class: "ok strong", text: "✓ 말투 분석 완료 · 지금 바로 쓸 수 있어요" }),
      h("p", { class: "small", text: `메시지 ${state.messages.length}개 중 내 발화 ${mine}개 · 이번에 ${job.imported}건 추가, ${job.duplicates}건 중복, 문체 묶음 ${job.episodes}개` }),
    ];
    if (job.graph_status === "pending" || job.graph_status === "running") {
      const id = uid("progress");
      parts.push(
        h("label", { class: "small", for: id, text: `지난 약속·사건 정리 중 ${job.graph_episodes} / ${job.imported}` }),
        h("progress", { id, max: String(Math.max(job.imported, 1)), value: String(Math.min(job.graph_episodes, job.imported)) }),
        h("p", { class: "small muted", text: "화면을 닫아도 서버에서 계속됩니다. 끝나면 지난번에 약속한 내용까지 참고합니다." }));
    } else if (job.graph_status === "completed") {
      parts.push(h("p", { class: "ok", text: `✓ 지난 약속·사건 정리 완료 (${job.graph_episodes}개)` }));
    } else if (job.graph_status === "failed") {
      parts.push(h("p", { text: `지난 약속·사건 정리 실패: ${job.graph_error ?? "사유 없음"}` }));
    }
    analysis.replaceChildren(h("section", { class: "card" }, parts),
      h("p", { class: "outside-note below", text: "이 상대에게 쓰는 말투는 이전 화면의 페르소나에서 직접 정합니다." }));
  };

  const renderMessages = () => {
    messagesTitle.textContent = `메시지 ${state.messages.length}건`;
    messageList.replaceChildren(state.messages.length ? h("ul", { class: "list" }, state.messages.map((message) => h("li", { class: "list-item" },
      h("div", { class: "small muted", text: `${message.speaker_role === "me" ? "나" : contact.alias} · ${Labels.date(message.sent_at)}` }),
      h("div", { class: "pre", text: message.text })))) : "");
  };

  const loadMessages = async () => {
    const list = await api("GET", `${path}/messages`);
    state.messages = Array.isArray(list) ? list : [];
    renderMessages();
  };

  /** 그래프 수집 진행을 4초마다 확인한다. 화면을 떠나면 확인만 멈춘다(수집은 계속된다). */
  const followGraph = async () => {
    const token = ++state.following;
    const epoch = session.epoch;
    const active = () => state.alive && epoch === session.epoch && token === state.following
      && state.job && (state.job.graph_status === "pending" || state.job.graph_status === "running");
    while (active()) {
      await sleep(4000);
      if (!active()) return;
      try {
        const next = await api("GET", `/v1/imports/${encodeURIComponent(state.job.id)}`);
        if (!active()) return;
        state.job = next;
        renderAnalysis();
      } catch { /* 다음 차례에 다시 확인한다 */ }
    }
  };

  const importText = () => run(async () => {
    const payload = ImportParser.parse(text.value, meName.value.trim(), contact.alias);
    state.job = await api("POST", `${path}/imports`, { body: payload });
    text.value = "";
    importButton.disabled = true;
    await loadMessages();
    renderAnalysis();
    followGraph();
  });

  const remove = async () => {
    const answer = await ask({
      title: "이 대화방 삭제",
      message: "이 대화방의 원문과 문체 묶음을 지웁니다. 되돌릴 수 없습니다.",
      confirm: "삭제",
      danger: true,
    });
    if (!answer.ok) return;
    run(async () => {
      await api("DELETE", path);
      await changed();
      popView("contacts");
    });
  };

  renderMethod();
  renderMessages();
  run(loadMessages);

  return {
    title: conversation.title,
    dispose: () => { state.alive = false; },
    el: h("div", { class: "view" },
      card("상대와 나눈 대화를 올려주세요", methods, help, inputs),
      h("p", { class: "outside-note", text: "🔒 올린 대화는 내 계정에만 저장되고 말투 분석에 씁니다. 상대별로 언제든 지울 수 있어요. 시연에는 합성 대화만 넣으세요." }),
      analysis,
      h("section", { class: "card" }, messagesTitle, messageList),
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

/** 상대 한 명 주변의 맥락 그래프: 그림(노드를 누르면 그 노드의 사실만) + 사실 목록. */
function graphView(contact) {
  const state = { graph: null, selected: null };
  const body = h("div", { class: "view" });
  let paintSelection = () => {};

  const nameOf = (id) => state.graph.nodes.find((node) => node.id === id)?.name ?? "?";
  const caption = (edge) => {
    const parts = [`${nameOf(edge.source)} → ${nameOf(edge.target)}`];
    if (edge.valid_at) parts.push(Labels.date(edge.valid_at));
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
        "aria-label": `${event ? "사건" : "사람"} ${node.name}`,
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
        const touched = state.selected === null || state.selected === link.a || state.selected === link.b;
        line.classList.toggle("dim", !touched);
      }
      for (const { node, badge } of badges) badge.setAttribute("aria-pressed", String(state.selected === node.id));
    };
    return svg("svg", { class: "graph", viewBox: `0 0 ${GRAPH_W} ${GRAPH_H}`, role: "group", "aria-label": `${contact.alias} 맥락 그래프` },
      lines.map((item) => item.line), badges.map((item) => item.badge));
  };

  const render = () => {
    const graph = state.graph;
    if (!graph) { body.replaceChildren(); return; }
    if (!graph.enabled) {
      body.replaceChildren(card(null, h("p", { class: "row-text", text: "이 서버는 맥락 그래프를 쓰지 않습니다. D 조건은 C와 같게 동작합니다." })));
      return;
    }
    if (!graph.nodes.length) {
      body.replaceChildren(card(null, h("p", { class: "row-text",
        text: `아직 ${contact.alias}의 그래프가 없습니다. 과거 대화를 가져오면 서버가 수집합니다. 대화 속 이름이 상대 별칭과 같아야 이 상대의 그래프로 묶입니다.` })));
      return;
    }
    const detail = h("div", { class: "view", "aria-live": "polite" });
    const details = () => {
      const node = graph.nodes.find((item) => item.id === state.selected);
      const facts = state.selected === null ? graph.edges
        : graph.edges.filter((edge) => edge.source === state.selected || edge.target === state.selected);
      detail.replaceChildren(
        node && node.summary ? card(node.name, h("p", { class: "small", text: node.summary })) : "",
        card(`사실 ${facts.length}건`, facts.length ? h("ul", { class: "list" }, facts.map((edge) => h("li", { class: "list-item" },
          h("div", { class: edge.current ? "" : "muted", text: edge.fact }),
          h("div", { class: "small muted", text: caption(edge) })))) : null));
    };
    body.replaceChildren(
      card(null, diagram(graph, details)),
      h("p", { class: "outside-note", text: "● 사람 ■ 사건 · 점선은 지난 사실만 남은 연결 · 노드를 누르면 그 노드의 사실만 봅니다." }),
      detail);
    details();
  };

  const load = async () => {
    const graph = await api("GET", `/v1/contacts/${encodeURIComponent(contact.id)}/graph`);
    state.graph = { ...graph, nodes: Array.isArray(graph?.nodes) ? graph.nodes : [], edges: Array.isArray(graph?.edges) ? graph.edges : [] };
    state.selected = null;
    render();
  };

  run(load);
  return { title: `${contact.alias} 맥락 그래프`, el: body, refresh: () => run(load) };
}

// ---------- 이력 ----------

const statusLabel = (rewrite) => (rewrite.result.status === "recommended" ? "추천" : "원문 유지");

/** 최근 첨삭 20건. 상대별로 좁혀 볼 수 있다. */
function historyRoot() {
  const state = { rewrites: [], loaded: false, request: 0 };
  const filter = h("select");
  const title = h("h2", { class: "card-title", text: "최근 첨삭" });
  const list = h("div");

  const sync = () => {
    const keep = filter.value;
    filter.replaceChildren(h("option", { value: "", text: "전체" }), ...options(session.contacts.map((item) => [item.id, item.alias])));
    filter.value = session.contacts.some((item) => item.id === keep) ? keep : "";
    if (filter.value !== keep) load();
    else render();
  };

  const render = () => {
    title.textContent = `최근 첨삭 ${state.rewrites.length}건`;
    if (!state.rewrites.length) {
      list.replaceChildren(state.loaded ? h("p", { class: "row-text", text: "아직 받은 추천이 없습니다." }) : "");
      return;
    }
    list.replaceChildren(h("ul", { class: "list" }, state.rewrites.map((rewrite) => h("li", null,
      h("button", { type: "button", class: "list-btn", onclick: () => pushView(rewriteDetail(rewrite, aliasOf(rewrite.contact_id))) },
        h("span", { class: "stack" },
          h("span", { class: "clamp2", text: rewrite.result.result_text }),
          h("span", { class: "small muted", text: [aliasOf(rewrite.contact_id), statusLabel(rewrite), `조건 ${rewrite.condition}`,
            Labels.date(rewrite.created_at)].join(" · ") })))))));
  };

  /** 목록 조회는 화면을 막지 않는다. 늦게 온 응답은 버린다. */
  const load = async () => {
    const request = ++state.request;
    const epoch = session.epoch;
    const query = { limit: "20" };
    if (filter.value) query.contact_id = filter.value;
    try {
      const rewrites = await api("GET", "/v1/rewrites", { query });
      if (request !== state.request || epoch !== session.epoch) return;
      state.rewrites = Array.isArray(rewrites) ? rewrites : [];
      state.loaded = true;
      render();
    } catch (error) {
      if (request === state.request && epoch === session.epoch && !ui.busy) notify(error.message, true);
    }
  };

  filter.addEventListener("change", load);
  filter.replaceChildren(h("option", { value: "", text: "전체" }), ...options(session.contacts.map((item) => [item.id, item.alias])));

  return {
    el: h("div", { class: "view" },
      h("section", { class: "card" }, field("상대", filter, { inline: true })),
      h("section", { class: "card" }, title, list)),
    sync,
    appear: load,
    resume: load,
    refresh: load,
  };
}

function rewriteDetail(rewrite, alias) {
  const result = rewrite.result;
  const text = h("p", { class: "result-text", text: result.result_text });
  const warnings = result.warnings ?? [];
  const preserved = result.preserved_items ?? [];
  const reasons = result.change_reasons ?? [];
  return {
    title: "첨삭 결과",
    el: h("div", { class: "view" },
      card(statusLabel(rewrite),
        text,
        h("p", { class: "small muted", text: `${alias} · 조건 ${rewrite.condition} · ${Labels.date(rewrite.created_at)}` }),
        result.status === "recommended" ? h("button", { type: "button", class: "btn primary", text: "복사하기", onclick: () => run(async () => {
          const copied = await copyText(result.result_text);
          if (!copied) selectText(text);
          notify(copied ? "복사했습니다. 메신저에 붙여 넣고 직접 보내세요." : COPY_BLOCKED);
        }) }) : null),
      warnings.length ? card("주의", h("ul", { class: "plain-list" }, warnings.map((item) => h("li", { text: item })))) : null,
      preserved.length ? card("보존 항목", h("p", { text: preserved.map((item) => item.text).join(", ") })) : null,
      reasons.length ? card("바꾼 이유", h("ul", { class: "plain-list" }, reasons.map((item) => h("li", { text: item })))) : null),
  };
}

// ---------- 계정 ----------

/** 오늘 사용량, 비밀번호 변경, 로그아웃, 계정 삭제. */
function accountRoot() {
  const username = session.user.username;
  const usage = h("p", { class: "muted", hidden: true });
  const current = h("input", { type: "password", autocomplete: "current-password", maxlength: "128" });
  const next = h("input", { type: "password", autocomplete: "new-password", maxlength: "128" });
  const change = h("button", { type: "submit", class: "btn", text: "비밀번호 변경", disabled: true });
  const deleting = h("input", { type: "password", autocomplete: "current-password", maxlength: "128" });
  const remove = h("button", { type: "submit", class: "btn danger", text: "계정 삭제", disabled: true });
  const syncButtons = () => {
    change.disabled = !current.value || !next.value;
    remove.disabled = !deleting.value;
  };
  for (const input of [current, next, deleting]) input.addEventListener("input", syncButtons);

  const loadUsage = async () => {
    const epoch = session.epoch;
    try {
      const data = await api("GET", "/v1/usage");
      if (epoch !== session.epoch) return;
      usage.textContent = `오늘 첨삭 ${data.rewrites}/${data.rewrite_limit}회 · ${data.input_tokens + data.output_tokens} 토큰`;
      usage.hidden = false;
    } catch { /* 표시용 */ }
  };

  const logout = () => run(async () => {
    await api("POST", "/v1/auth/logout");
    endSession("");
  });

  const passwordForm = h("form", { class: "card", novalidate: true, onsubmit: (event) => {
    event.preventDefault();
    if (!current.value || !next.value) return;
    run(async () => {
      await api("POST", "/v1/auth/password",
        { body: { current_password: current.value, new_password: next.value }, verifiesPassword: true });
      current.value = "";
      next.value = "";
      syncButtons();
      notify("비밀번호를 바꿨습니다.");
    });
  } },
  h("h2", { class: "card-title", text: "비밀번호" }),
  // 비밀번호 관리자가 어느 계정인지 알 수 있게 아이디를 함께 둔다 (화면에는 보이지 않는다).
  h("input", { type: "text", autocomplete: "username", value: username, hidden: true, readonly: true, tabindex: "-1", "aria-hidden": "true" }),
  field("현재 비밀번호", current),
  field("새 비밀번호", next),
  change,
  h("p", { class: "note", text: "변경하면 다른 기기의 로그인은 모두 풀립니다." }));

  const deleteForm = h("form", { class: "card", novalidate: true, onsubmit: async (event) => {
    event.preventDefault();
    if (!deleting.value) return;
    const answer = await ask({
      title: "계정 삭제",
      message: `${username} 계정과 모든 데이터를 지웁니다. 되돌릴 수 없습니다.`,
      confirm: "계정 삭제",
      danger: true,
    });
    if (!answer.ok || !deleting.value) return;
    run(async () => {
      await api("DELETE", "/v1/auth/me", { body: { password: deleting.value }, verifiesPassword: true });
      deleting.value = "";
      endSession("계정을 삭제했습니다.");
    });
  } },
  h("h2", { class: "card-title", text: "계정 삭제" }),
  field("비밀번호 확인", deleting),
  remove,
  h("p", { class: "note", text: "상대·대화·첨삭 이력·맥락 그래프를 모두 지웁니다. 되돌릴 수 없습니다." }));

  return {
    el: h("div", { class: "view" },
      card("계정",
        h("p", { class: "strong", text: username }),
        usage,
        h("button", { type: "button", class: "btn", text: "로그아웃", onclick: logout })),
      passwordForm,
      deleteForm),
    appear: loadUsage,
  };
}

// ---------- 시작 ----------

function init() {
  $("login-form").addEventListener("submit", (event) => { event.preventDefault(); sign(false); });
  $("login-register").addEventListener("click", () => sign(true));
  $("login-user").addEventListener("input", syncLoginButtons);
  $("login-pass").addEventListener("input", syncLoginButtons);

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
    if (!ui.busy && session.user) popView();
  });
  // 뒤로-앞으로 캐시에서 되살아난 화면은 세션을 다시 확인한다.
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
