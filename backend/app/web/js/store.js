// 앱 전체가 함께 보는 상태(로그인 정보, 서버 설정, 상대 목록, 사용량)와 브라우저에 남기는 작은 설정.
// 브라우저 저장소에는 테마와 상대 id만 둔다 — 초안·대화·이름 같은 내용은 넣지 않는다.

const DEFAULT_CONFIG = Object.freeze({
  allow_register: true,
  invite_required: false,
  require_consent: false,
  consent_version: null,
  app_env: null,
  loaded: false, // 서버가 /v1/auth/config를 아직 주지 않으면 false (기본값으로 동작)
});

const listeners = new Map();

export const store = {
  me: null, // { user_id, username, is_admin?, consented_at?, consent_version?, consent_current? }
  csrf: null,
  config: { ...DEFAULT_CONFIG },
  contacts: null, // 아직 안 불렀으면 null
  usage: null,
  notice: null, // 첫 화면에 한 번 보여 줄 안내({ text, tone })
  bootError: null, // 시작할 때 로그인 여부를 확인하지 못한 까닭(서버 연결 실패·일시 차단·서버 오류). 로그아웃과 구분한다

  on(event, fn) {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(fn);
    return () => listeners.get(event)?.delete(fn);
  },

  emit(event, payload) {
    for (const fn of [...(listeners.get(event) || [])]) {
      try {
        fn(payload);
      } catch (err) {
        console.error(err);
      }
    }
  },

  setConfig(config) {
    this.config = config ? { ...DEFAULT_CONFIG, ...config, loaded: true } : { ...DEFAULT_CONFIG };
  },

  setSession(me) {
    this.me = me
      ? {
          user_id: me.user_id,
          username: me.username,
          is_admin: me.is_admin === true,
          consented_at: me.consented_at ?? null,
          consent_version: me.consent_version ?? null,
          consent_current: me.consent_current, // 옛 서버는 이 값을 주지 않는다(undefined)
        }
      : null;
    if (me && me.csrf_token) this.csrf = me.csrf_token;
    if (!me) this.csrf = null;
  },

  clearSession() {
    this.me = null;
    this.csrf = null;
    this.contacts = null;
    this.usage = null;
  },

  /** 동의를 먼저 받아야 하는가: 서버가 동의를 요구하고, 지금 버전에 동의한 기록이 없을 때만. */
  needsConsent() {
    return Boolean(this.me && this.config.require_consent && this.me.consent_current === false);
  },
};

// ---------- 브라우저에 남기는 설정 ----------

export const prefs = {
  get(key, fallback = null) {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* 저장하지 못해도 동작한다 */
    }
  },
  remove(key) {
    try {
      localStorage.removeItem(key);
    } catch {
      /* 무시 */
    }
  },
};

const userKey = (name) => `pz_${name}:${store.me?.user_id || "anon"}`;

export function recentContactIds() {
  const ids = prefs.get(userKey("recent"), []);
  return Array.isArray(ids) ? ids.filter((x) => typeof x === "string") : [];
}

export function saveRecentContactIds(ids) {
  prefs.set(userKey("recent"), ids);
}

/** 계정을 지울 때 이 브라우저에 남긴 그 계정의 흔적(최근 고른 상대 번호)도 지운다. 테마는 기기 설정이라 둔다. */
export function forgetUserPrefs() {
  prefs.remove(userKey("recent"));
}

// ---------- 테마 ----------

export const THEME_KEY = "pz_theme"; // js/theme-boot.js가 같은 키를 읽는다(따옴표 없는 문자열)

export function getTheme() {
  try {
    const t = localStorage.getItem(THEME_KEY);
    return t === "light" || t === "dark" ? t : "system";
  } catch {
    return "system";
  }
}

export function setTheme(theme) {
  const root = document.documentElement;
  try {
    if (theme === "light" || theme === "dark") localStorage.setItem(THEME_KEY, theme);
    else localStorage.removeItem(THEME_KEY);
  } catch {
    /* 저장하지 못해도 이번 화면에는 적용한다 */
  }
  if (theme === "light" || theme === "dark") root.setAttribute("data-theme", theme);
  else root.removeAttribute("data-theme");
}
