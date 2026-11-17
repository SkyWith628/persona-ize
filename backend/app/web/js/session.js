// 로그인 상태와 여러 화면이 함께 쓰는 데이터(상대 목록, 사용량)를 서버에서 읽어 store에 둔다.

import { api } from "./api.js";
import { isMissingEndpoint } from "./lib/errors.js";
import { store } from "./store.js";

let lastUserId = null; // 다른 사람이 로그인하면 화면에 남은 초안 등을 비우기 위해 기억한다

function entered(me) {
  if (lastUserId && lastUserId !== me.user_id) store.emit("user-changed");
  lastUserId = me.user_id;
  store.setSession(me);
}

/**
 * 앱 시작: 서버 설정과 로그인 여부를 확인한다.
 * 로그인하지 않았으면(401) 첫 화면으로 간다. 서버가 답하지 못해 로그인 여부를 알 수 없으면(연결 실패·429·5xx)
 * store.bootError에 까닭을 남긴다 — 로그인돼 있던 사람에게 로그인 화면을 보여 주지 않기 위해서다.
 */
export async function boot() {
  const [config, me, providers] = await Promise.allSettled([api.authConfig(), api.me(), api.authProviders()]);
  if (config.status === "fulfilled" && config.value) store.setConfig(config.value);
  else {
    store.setConfig(null);
    if (!isMissingEndpoint(config.reason)) store.config.error = true; // 서버 연결 문제일 수 있다
  }
  // 가입 정책·소셜 로그인(/v1/auth/providers). 읽지 못했으면 null — 첫 화면은 예전처럼 아이디 로그인·가입만 보인다
  store.config.providers =
    providers.status === "fulfilled" && providers.value && typeof providers.value === "object" ? providers.value : null;
  // 소셜 로그인에서 실패해 돌아왔으면(?auth_error=…) 주소에서 지우고, 까닭을 첫 화면에 한 번 보여 준다
  const authError = takeAuthError();
  if (authError && !(me.status === "fulfilled" && me.value)) {
    store.notice = { text: oauthErrorText(authError), tone: "warn", authError };
  }
  store.bootError = null;
  if (me.status === "fulfilled" && me.value) entered(me.value);
  else {
    store.setSession(null);
    const reason = me.status === "rejected" ? me.reason : null;
    if (reason && reason.status !== 401 && !isMissingEndpoint(reason)) store.bootError = reason;
  }
  return store.me;
}

export async function login(username, password) {
  const out = await api.login(username, password);
  entered(out);
  store.emit("session");
}

/**
 * 가입 응답이 "승인 대기"(202)인가. 승인제 서버는 계정만 만들고 세션을 열지 않는다 — 쿠키도 CSRF 토큰도 없다.
 * 로그인된 응답(201)에도 status 필드가 있으므로("active") 토큰이 없는지도 함께 본다.
 */
export function isPendingSignup(out) {
  return Boolean(out && out.status === "pending" && !out.csrf_token);
}

/** 가입 응답 처리: 세션이 열렸으면 앱으로 들어가고, 승인 대기면 들어가지 않고 그 사실만 돌려준다. */
function signedUp(out) {
  if (isPendingSignup(out)) return { pending: true, username: out.username ?? "", email: out.email ?? null };
  entered(out);
  store.emit("session");
  return { pending: false };
}

/** 아이디 가입. @returns {Promise<{pending: boolean, username?: string, email?: string|null}>} */
export async function register(payload) {
  return signedUp(await api.register(payload));
}

/** 이메일 가입(아이디는 서버가 이메일 앞부분에서 만든다). 돌려주는 값은 register와 같다. */
export async function signup(payload) {
  return signedUp(await api.signup(payload));
}

// ---------- 소셜 로그인 ----------

// 소셜 로그인 콜백이 실패했을 때 서버가 돌려보내는 코드(?auth_error=…) → 첫 화면 안내 (routes/auth.py OAUTH_ERROR_CODES)
const OAUTH_ERROR_TEXT = {
  oauth_failed: "소셜 로그인을 마치지 못했어요. 다시 시도해 주세요.",
  oauth_denied: "소셜 로그인을 취소했어요. 다시 하려면 버튼을 한 번 더 눌러 주세요.",
  signup_closed: "지금은 새로 가입할 수 없어요. 이미 가입한 계정으로만 로그인할 수 있어요.",
  invalid_invite: "처음 오셨다면 초대 코드가 필요해요. ‘가입’ 화면에서 초대 코드를 넣은 뒤 소셜 로그인 버튼을 눌러 주세요.",
  consent_required: "처음 오셨다면 안내에 동의해야 해요. ‘가입’ 화면에서 동의에 체크한 뒤 소셜 로그인 버튼을 눌러 주세요.",
  account_pending: "가입 신청을 받았어요. 관리자가 승인한 뒤에 로그인할 수 있어요.",
  account_suspended: "정지된 계정이에요. 운영자에게 문의해 주세요.",
  email_in_use: "같은 이메일로 가입한 계정이 이미 있어요. 그 계정의 이메일과 비밀번호로 로그인해 주세요.",
};

/** auth_error 코드 → 안내 문장. 모르는 코드는 일반 실패 문장(주소에 적힌 글자를 화면에 그대로 옮기지 않는다). */
export function oauthErrorText(code) {
  return Object.hasOwn(OAUTH_ERROR_TEXT, code) ? OAUTH_ERROR_TEXT[code] : OAUTH_ERROR_TEXT.oauth_failed;
}

/**
 * 주소의 쿼리("?a=1&auth_error=x")에서 auth_error를 꺼낸다.
 * @returns {{code: string|null, search: string}} code는 없으면 null, search는 auth_error를 뺀 나머지("" 또는 "?a=1")
 */
export function splitAuthError(search) {
  const params = new URLSearchParams(search || "");
  if (!params.has("auth_error")) return { code: null, search: search || "" };
  const code = params.get("auth_error") || "oauth_failed";
  params.delete("auth_error");
  const rest = params.toString();
  return { code, search: rest ? `?${rest}` : "" };
}

/** 지금 주소에 auth_error가 있으면 주소에서 지우고(뒤로 가기·새로 고침에 다시 뜨지 않게) 코드를 돌려준다. */
function takeAuthError() {
  const { code, search } = splitAuthError(window.location.search);
  if (code === null) return null;
  try {
    window.history.replaceState(null, "", window.location.pathname + search + window.location.hash);
  } catch {
    /* 주소를 못 바꿔도 안내는 보여 준다 */
  }
  return code;
}

/**
 * 소셜 로그인 시작: 서버에서 제공자 로그인 주소를 받아 그곳으로 간다. 돌아오면 서버가 세션 쿠키를 심고 `/`로 보낸다.
 * 새 계정에 필요한 동의·초대 코드는 여기서 함께 보낸다(이미 가입한 사람에게는 쓰이지 않는다).
 */
export async function startOAuth(provider, { consent = false, inviteCode = "" } = {}) {
  const payload = { next: "/", consent: consent === true };
  if (inviteCode) payload.invite_code = inviteCode;
  const out = await api.oauthStart(provider, payload);
  const target = safeAuthorizeUrl(out?.authorize_url);
  if (!target) throw new Error("소셜 로그인을 시작하지 못했어요. 잠시 뒤 다시 시도해 주세요.");
  window.location.assign(target);
}

/** 서버가 준 로그인 주소가 http(s) 주소일 때만 쓴다 (javascript: 같은 것으로는 가지 않는다). */
export function safeAuthorizeUrl(value) {
  if (typeof value !== "string" || !value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

export async function acceptConsent() {
  const me = await api.consent();
  entered({ ...me, csrf_token: me.csrf_token || store.csrf });
  store.emit("session");
}

/** 로그아웃. 서버 요청이 실패해도 화면에서는 나간다(쿠키가 남으면 다음 방문 때 다시 들어와진다). */
export async function logout() {
  try {
    await api.logout();
  } catch {
    /* 아래에서 화면 상태를 비운다 */
  }
  leave();
}

/**
 * 서버 요청 없이 화면만 로그아웃 상태로. notice는 첫 화면에 보여 줄 안내.
 * forget=false(세션 만료)면 쓰던 초안을 남겨, 같은 사람이 다시 로그인했을 때 이어 쓸 수 있게 한다.
 * 다른 사람이 로그인하면 entered()가 비운다.
 */
export function leave(notice = null, { forget = true } = {}) {
  store.clearSession();
  store.notice = notice;
  if (forget) {
    store.emit("user-changed");
    lastUserId = null;
    // 보던 화면 주소(#/contacts/… 등)도 지운다 — 다음에 로그인하는 사람이 남의 주소에서 시작하지 않게
    try {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    } catch {
      /* 주소를 못 바꿔도 로그아웃은 된다 */
    }
  }
  store.emit("session");
}

/** 상대 목록. 한 번 읽으면 기억해 두고, force면 다시 읽는다. */
export async function loadContacts({ force = false } = {}) {
  if (store.contacts && !force) return store.contacts;
  const list = await api.contacts.list();
  store.contacts = Array.isArray(list) ? list : [];
  store.emit("contacts", store.contacts);
  return store.contacts;
}

export function invalidateContacts() {
  store.contacts = null;
}

/** 오늘 사용량(위쪽 표시용). 실패해도 조용히 넘어간다. */
export async function refreshUsage() {
  try {
    store.usage = await api.usage();
  } catch {
    return store.usage;
  }
  store.emit("usage", store.usage);
  return store.usage;
}
