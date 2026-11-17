// 백엔드 JSON API 호출. 같은 출처, 세션 쿠키(HttpOnly) + 쓰기 요청의 X-CSRF-Token.
// 경로와 필드 이름은 계약서 §4를 그대로 따른다.

import { ApiError, normalizeError } from "./lib/errors.js";
import { store } from "./store.js";

function buildUrl(path, query) {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

/**
 * @param {string} method
 * @param {string} path
 * @param {object} [opts]
 * @param {any} [opts.body]        JSON으로 보낼 본문
 * @param {object} [opts.query]    쿼리 값(빈 값은 뺀다)
 * @param {object} [opts.headers]
 * @param {AbortSignal} [opts.signal]
 * @param {boolean} [opts.quiet401] true면 401을 "세션 만료"로 알리지 않는다(로그인·비밀번호 확인 요청)
 * @param {boolean} [opts.raw]      true면 Response를 그대로 돌려준다(파일 내려받기)
 */
export async function request(method, path, opts = {}) {
  const headers = { Accept: "application/json", ...(opts.headers || {}) };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (store.csrf && method !== "GET") headers["X-CSRF-Token"] = store.csrf;

  let res;
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: opts.signal,
    });
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new ApiError(normalizeError(0, null));
  }

  if (res.ok && opts.raw) return res;

  // 본문이 없는 204도 끝까지 읽는다 — 읽지 않고 버리면 브라우저가 그 요청을 "중단됨"으로 기록한다
  const text = await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null; // HTML 오류 쪽 등 — 기본 문구를 쓴다
    }
  }
  if (res.ok) return data;

  const error = new ApiError(normalizeError(res.status, data, res.headers.get("Retry-After")));
  if (res.status === 401 && !opts.quiet401 && store.me) store.emit("unauthorized", error);
  throw error;
}

const get = (path, opts) => request("GET", path, opts);
const post = (path, body, opts) => request("POST", path, { ...opts, body });
const id = (value) => encodeURIComponent(String(value));

export const api = {
  // ---- 인증·계정 (계약 §4.2) ----
  authConfig: () => get("/v1/auth/config"),
  me: () => get("/v1/auth/me", { quiet401: true }),
  login: (username, password) => post("/v1/auth/login", { username, password }, { quiet401: true }),
  register: (payload) => post("/v1/auth/register", payload, { quiet401: true }),
  logout: () => request("POST", "/v1/auth/logout", { quiet401: true }),
  consent: () => post("/v1/auth/consent", {}),
  changePassword: (current_password, new_password) =>
    post("/v1/auth/password", { current_password, new_password }, { quiet401: true }),
  deleteAccount: (password) => request("DELETE", "/v1/auth/me", { body: { password }, quiet401: true }),
  sessions: () => get("/v1/auth/sessions"),
  logoutAll: () => request("POST", "/v1/auth/logout-all"),

  usage: () => get("/v1/usage"),

  // ---- 상대·페르소나 (§4.3) ----
  contacts: {
    list: () => get("/v1/contacts"),
    get: (contactId) => get(`/v1/contacts/${id(contactId)}`),
    create: (alias, relationship_type) =>
      post("/v1/contacts", { alias, relationship_type, use_default_persona: true }),
    update: (contactId, patch) => request("PATCH", `/v1/contacts/${id(contactId)}`, { body: patch }),
    remove: (contactId) => request("DELETE", `/v1/contacts/${id(contactId)}`),
    persona: (contactId) => get(`/v1/contacts/${id(contactId)}/persona`),
    savePersona: (contactId, settings, expected_version) =>
      request("PUT", `/v1/contacts/${id(contactId)}/persona`, { body: { settings, expected_version } }),
    personaVersions: (contactId) => get(`/v1/contacts/${id(contactId)}/persona/versions`),
    personaSuggestion: (contactId) => get(`/v1/contacts/${id(contactId)}/persona/suggestion`),
  },
  personaDefaults: () => get("/v1/persona-defaults"),

  // ---- 대화 (§4.4) ----
  conversations: {
    list: (contactId) => get("/v1/conversations", { query: { contact_id: contactId } }),
    create: (payload) => post("/v1/conversations", payload),
    remove: (conversationId) => request("DELETE", `/v1/conversations/${id(conversationId)}`),
    messages: (conversationId) => get(`/v1/conversations/${id(conversationId)}/messages`),
    stats: (conversationId, opts) => get(`/v1/conversations/${id(conversationId)}/stats`, opts),
    importRaw: (conversationId, payload) => post(`/v1/conversations/${id(conversationId)}/imports/raw`, payload),
    speakers: (conversationId) => get(`/v1/conversations/${id(conversationId)}/speakers`),
    // 화자를 잘못 지정한 가져오기 고치기: payload는 lib/roles.js의 swapRequest(방 전체 맞바꾸기)나
    // speakerRolesPlan(화자별)이 만든다
    setSpeakerRoles: (conversationId, payload) =>
      request("PUT", `/v1/conversations/${id(conversationId)}/speaker-roles`, { body: payload }),
  },
  imports: {
    parse: (payload, opts) => post("/v1/imports/parse", payload, opts),
  },

  // ---- 첨삭 (§4.5) ----
  rewrites: {
    create: (payload, idempotencyKey, signal) =>
      post("/v1/rewrites", payload, { headers: { "Idempotency-Key": idempotencyKey }, signal }),
    createBatch: (payload, idempotencyKey, signal) => post("/v1/rewrites/batch", payload, { headers: { "Idempotency-Key": idempotencyKey }, signal }),
    list: ({ contactId, limit, before } = {}) =>
      get("/v1/rewrites", { query: { contact_id: contactId, limit, before } }),
    feedback: (requestId, payload) => post(`/v1/rewrites/${id(requestId)}/feedback`, payload),
    evidence: (requestId) => get(`/v1/rewrites/${id(requestId)}/evidence`),
    remove: (requestId) => request("DELETE", `/v1/rewrites/${id(requestId)}`),
    removeByContact: (contactId) => request("DELETE", "/v1/rewrites", { query: { contact_id: contactId } }),
  },

  // ---- 내 데이터 (§4.4) ----
  dataSummary: () => get("/v1/data/summary"),
  exportData: () => request("GET", "/v1/export", { raw: true }),
};
