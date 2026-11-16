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
  const [config, me] = await Promise.allSettled([api.authConfig(), api.me()]);
  if (config.status === "fulfilled" && config.value) store.setConfig(config.value);
  else {
    store.setConfig(null);
    if (!isMissingEndpoint(config.reason)) store.config.error = true; // 서버 연결 문제일 수 있다
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

export async function register(payload) {
  const out = await api.register(payload);
  entered(out);
  store.emit("session");
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
