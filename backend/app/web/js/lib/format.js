// 날짜·수 표기. 순수 함수만 둔다(테스트: tests/format.test.js).
// Intl에 기대지 않고 직접 만든다 — 어느 환경에서나 같은 글자가 나오게.

const pad = (n) => String(n).padStart(2, "0");
const WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];

/** 지금 브라우저 시간대의 분 단위 차이(동쪽이 +, 한국은 540). */
export function localOffsetMinutes(date = new Date()) {
  return -date.getTimezoneOffset();
}

function toDate(value) {
  if (value === null || value === undefined || value === "") return null; // new Date(null)은 1970년이 된다
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 시각을 주어진 시간대(없으면 브라우저 시간대)의 연·월·일·시·분으로 푼다. 잘못된 값이면 null. */
export function dateParts(value, offsetMinutes) {
  const d = toDate(value);
  if (!d) return null;
  const off = offsetMinutes ?? localOffsetMinutes(d);
  const s = new Date(d.getTime() + off * 60000);
  return {
    year: s.getUTCFullYear(),
    month: s.getUTCMonth() + 1,
    day: s.getUTCDate(),
    hour: s.getUTCHours(),
    minute: s.getUTCMinutes(),
    weekday: WEEKDAYS[s.getUTCDay()],
  };
}

/** "11월 10일 14:02" — 올해가 아니면 "2025년 11월 10일 14:02". */
export function formatDateTime(value, { offsetMinutes, now = new Date() } = {}) {
  const p = dateParts(value, offsetMinutes);
  if (!p) return "";
  const thisYear = dateParts(now, offsetMinutes)?.year;
  const head = p.year === thisYear ? "" : `${p.year}년 `;
  return `${head}${p.month}월 ${p.day}일 ${pad(p.hour)}:${pad(p.minute)}`;
}

/** "2026년 10월 5일 (월)" — 대화 보기의 날짜 구분선. */
export function formatDay(value, { offsetMinutes } = {}) {
  const p = dateParts(value, offsetMinutes);
  return p ? `${p.year}년 ${p.month}월 ${p.day}일 (${p.weekday})` : "";
}

/** "09:31" */
export function formatTime(value, { offsetMinutes } = {}) {
  const p = dateParts(value, offsetMinutes);
  return p ? `${pad(p.hour)}:${pad(p.minute)}` : "";
}

/** "2026-10-05" — 같은 날 묶기와 기간 표시에 쓴다. */
export function dayKey(value, { offsetMinutes } = {}) {
  const p = dateParts(value, offsetMinutes);
  return p ? `${p.year}-${pad(p.month)}-${pad(p.day)}` : "";
}

/** "2026-10-01 ~ 2026-10-31" (같은 날이면 하루만). 둘 다 없으면 "". */
export function formatRange(from, to, opts = {}) {
  const a = dayKey(from, opts);
  const b = dayKey(to, opts);
  if (!a && !b) return "";
  if (!a || !b || a === b) return a || b;
  return `${a} ~ ${b}`;
}

/** 1234567 → "1,234,567". 숫자가 아니면 "—". */
export function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const sign = n < 0 ? "-" : "";
  const [int, frac] = String(Math.abs(n)).split(".");
  return sign + int.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (frac ? `.${frac}` : "");
}

/** 4400 → "4.4초", 820 → "0.8초". */
export function formatSeconds(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return "—";
  return `${(n / 1000).toFixed(1)}초`;
}

/** 1536 → "1.5KB" */
export function formatBytes(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / 1024 / 1024).toFixed(1)}MB`;
}

/** 서버(Python len)와 같은 기준으로 글자 수를 센다 — 이모지 같은 보조 평면 글자를 1로. */
export function countCodePoints(text) {
  const s = String(text ?? "");
  let count = 0;
  for (let i = 0; i < s.length; i += 1) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length) {
      const next = s.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) i += 1;
    }
    count += 1;
  }
  return count;
}

/** 비율(0~1)을 0~100 정수로. 분모가 0이면 0. */
export function percent(done, total) {
  const d = Number(done);
  const t = Number(total);
  if (!Number.isFinite(d) || !Number.isFinite(t) || t <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((d / t) * 100)));
}

/**
 * 이름 뒤에 붙는 조사를 받침에 맞게 고른다: josa("박지현", "을/를") → "을", josa("최유진", "과/와") → "과".
 * 한글로 끝나지 않으면(영문·숫자) "을(를)"처럼 둘 다 적는다.
 */
export function josa(word, pair) {
  const [closed, open] = pair.split("/");
  const last = [...String(word ?? "").trim()].pop() || "";
  const code = last.charCodeAt(0);
  if (!(code >= 0xac00 && code <= 0xd7a3)) return `${closed}(${open})`;
  const final = (code - 0xac00) % 28; // 0이면 받침 없음
  if (pair === "으로/로" && final === 8) return open; // ㄹ 받침 뒤에는 '로'
  return final ? closed : open;
}

/** 이름과 조사를 붙인다: withJosa("박지현", "을/를") → "박지현을" */
export function withJosa(word, pair) {
  return `${word}${josa(word, pair)}`;
}

/** 이름의 첫 글자(아바타용). 비면 "?". */
export function initial(name) {
  const first = [...String(name ?? "").trim()][0];
  return first || "?";
}

/** Mac 계열인지(단축키 표기 ⌘ / Ctrl). */
export function isMacLike(platform = "") {
  return /mac|iphone|ipad|ipod/i.test(String(platform));
}

/** RFC 4122 v4 UUID. 보안 맥락이 아니어서 randomUUID가 없을 때(예: http 내부망)도 동작한다. */
export function uuid4(cryptoObj = globalThis.crypto) {
  if (cryptoObj && typeof cryptoObj.randomUUID === "function") return cryptoObj.randomUUID();
  const b = new Uint8Array(16);
  if (cryptoObj && typeof cryptoObj.getRandomValues === "function") cryptoObj.getRandomValues(b);
  else for (let i = 0; i < 16; i += 1) b[i] = Math.floor(Math.random() * 256);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const hex = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
