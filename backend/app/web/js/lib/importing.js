// 대화 가져오기의 순수 로직: 파일 인코딩 판별, 미리보기 모델, 화자 지정, 진행 상태.
// 테스트: tests/importing.test.js

import { countCodePoints, formatNumber, formatRange, percent } from "./format.js";

// 서버 기본값(계약 §3). 서버 설정이 다르면 서버가 413/422로 알려 준다 — 여기 값은 미리 막기 위한 안내용이다.
export const MAX_IMPORT_CHARS = 2_000_000;
export const MAX_BODY_BYTES = 3_000_000;
export const MAX_IMPORT_MESSAGES = 5000;

export const METHODS = [
  {
    value: "kakao",
    format: "kakao",
    label: "카카오톡 대화 내보내기 파일",
    desc: "채팅방 메뉴 → 대화 내용 내보내기로 만든 파일 (.txt, PC는 .csv)",
    accept: ".txt,.csv,text/plain,text/csv",
    file: true,
  },
  {
    value: "telegram",
    format: "telegram",
    label: "텔레그램 내보내기 파일",
    desc: "PC용 텔레그램에서만 만들 수 있어요 (result.json)",
    accept: ".json,application/json",
    file: true,
  },
  {
    value: "paste",
    format: "auto",
    label: "붙여넣기",
    desc: "다른 메신저·사내 메신저는 대화를 복사해서 붙여 넣어요",
    accept: "",
    file: false,
  },
];

const FORMAT_LABEL = { kakao: "카카오톡", telegram: "텔레그램", plain: "붙여넣기(줄 형식)", json: "JSON" };
const VARIANT_LABEL = { android: "안드로이드", ios: "아이폰", pc: "PC", csv: "PC · CSV" };

export function formatLabel(format, variant) {
  const base = FORMAT_LABEL[format] || String(format ?? "알 수 없음");
  const v = VARIANT_LABEL[variant];
  return v ? `${base} (${v})` : base;
}

// ---------- 파일 인코딩 ----------

function defaultDecode(label, bytes, fatal) {
  return new TextDecoder(label, { fatal }).decode(bytes);
}

function zeroParity(bytes, limit = 4096) {
  let even = 0;
  let odd = 0;
  const n = Math.min(bytes.length, limit);
  for (let i = 0; i < n; i += 1) {
    if (bytes[i] !== 0) continue;
    if (i % 2 === 0) even += 1;
    else odd += 1;
  }
  return { even, odd, sample: n };
}

/**
 * 파일 바이트 → { text, encoding, replaced }.
 * 순서: BOM → UTF-8(엄격) → 0 바이트가 섞였으면 UTF-16 → EUC-KR(엄격) → 마지막 수단 UTF-8(깨진 글자는 �).
 * replaced는 깨져서 �로 바뀐 글자 수(0이 아니면 화면에서 경고한다).
 * decode는 테스트에서 바꿔 끼울 수 있다: (label, bytes, fatal) => string, 실패하면 throw.
 */
export function decodeBytes(input, decode = defaultDecode) {
  const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
  const done = (text, encoding) => ({ text, encoding, replaced: (text.match(/�/g) || []).length });
  const attempt = (label, view) => {
    try {
      return decode(label, view, true);
    } catch {
      return null;
    }
  };

  if (bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) {
    return done(decode("utf-8", bytes.subarray(3), false), "utf-8");
  }
  if (bytes.length >= 2 && bytes[0] === 0xff && bytes[1] === 0xfe) {
    return done(decode("utf-16le", bytes.subarray(2), false), "utf-16le");
  }
  if (bytes.length >= 2 && bytes[0] === 0xfe && bytes[1] === 0xff) {
    return done(decode("utf-16be", bytes.subarray(2), false), "utf-16be");
  }

  const zeros = zeroParity(bytes);
  const looksUtf16 = zeros.even + zeros.odd > zeros.sample * 0.05;
  if (!looksUtf16) {
    const utf8 = attempt("utf-8", bytes);
    if (utf8 !== null) return done(utf8, "utf-8");
  } else {
    const label = zeros.odd >= zeros.even ? "utf-16le" : "utf-16be";
    const utf16 = attempt(label, bytes);
    if (utf16 !== null) return done(utf16, label);
  }
  const euckr = attempt("euc-kr", bytes);
  if (euckr !== null) return done(euckr, "euc-kr");
  return done(decode("utf-8", bytes, false), "utf-8");
}

const ENCODING_LABEL = { "utf-8": "UTF-8", "utf-16le": "UTF-16", "utf-16be": "UTF-16", "euc-kr": "EUC-KR(완성형)" };

export function encodingLabel(encoding) {
  return ENCODING_LABEL[encoding] || String(encoding ?? "");
}

/** UTF-8로 보냈을 때의 바이트 수(본문 크기 확인용). */
export function utf8Bytes(text) {
  return new TextEncoder().encode(String(text ?? "")).length;
}

/**
 * 보낼 수 있는 크기인지 확인한다. 문제가 있으면 화면에 보여 줄 문장, 없으면 null.
 * 한글은 UTF-8에서 3바이트라 글자 수 한도보다 본문 크기 한도에 먼저 닿는다.
 */
export function checkSize(text) {
  const s = String(text ?? "");
  if (!s.trim()) return "내용이 비어 있어요.";
  const chars = countCodePoints(s);
  if (chars > MAX_IMPORT_CHARS) {
    return `글자가 너무 많아요(${formatNumber(chars)}자). 한 번에 ${formatNumber(MAX_IMPORT_CHARS)}자까지 올릴 수 있어요. 기간을 나눠 내보내 주세요.`;
  }
  const bytes = utf8Bytes(JSON.stringify(s)) + 1024; // 실제로 보낼 JSON 문자열 크기 + 다른 필드(화자 지정 등) 여유분
  if (bytes > MAX_BODY_BYTES) {
    return `파일이 너무 커요(약 ${(bytes / 1_000_000).toFixed(1)}MB). 한 번에 약 ${(MAX_BODY_BYTES / 1_000_000).toFixed(0)}MB까지 올릴 수 있어요. 기간을 나눠 내보내 주세요.`;
  }
  return null;
}

// ---------- 화자 지정 ----------

export const ROLE_OPTIONS = [
  { value: "me", label: "나" },
  { value: "contact", label: "상대" },
  { value: "skip", label: "제외" },
];

const squash = (s) => String(s ?? "").replace(/\s+/g, "").toLowerCase();

function nameMatches(a, b) {
  const x = squash(a);
  const y = squash(b);
  if (x.length < 2 || y.length < 2) return x !== "" && x === y;
  return x.includes(y) || y.includes(x);
}

/**
 * 화자별 역할을 미리 채운다 → { 이름: "me" | "contact" | "skip" | null }.
 * ① 서버 추천값 ② 상대 이름과 일치하면 상대 ③ 화자가 둘이고 한쪽만 정해졌으면 나머지는 반대쪽.
 * 그래도 모르면 null로 두고 사용자가 고르게 한다(단체방 등).
 */
export function suggestRoles(speakers, contactAlias) {
  const roles = {};
  const list = speakers || [];
  for (const sp of list) {
    const suggested = ["me", "contact", "skip"].includes(sp.suggested_role) ? sp.suggested_role : null;
    roles[sp.name] = suggested;
  }
  if (contactAlias && !Object.values(roles).includes("contact")) {
    const hit = list.filter((sp) => roles[sp.name] === null && nameMatches(sp.name, contactAlias));
    if (hit.length === 1) roles[hit[0].name] = "contact";
  }
  if (list.length === 2) {
    const [a, b] = list.map((sp) => sp.name);
    if (roles[a] === null && roles[b] === "contact") roles[a] = "me";
    else if (roles[b] === null && roles[a] === "contact") roles[b] = "me";
    else if (roles[a] === null && roles[b] === "me") roles[a] = "contact";
    else if (roles[b] === null && roles[a] === "me") roles[b] = "contact";
  }
  return roles;
}

/** 화자 지정 검사 → { ok, error, warning }. 모든 화자에 역할이 있고 '나'가 한 명 이상이어야 한다. */
export function checkRoles(roles) {
  const entries = Object.entries(roles || {});
  if (!entries.length) return { ok: false, error: "화자를 찾지 못했어요.", warning: null };
  const missing = entries.filter(([, r]) => !r).map(([name]) => name);
  if (missing.length) {
    return { ok: false, error: `누구인지 정하지 않은 화자가 있어요: ${missing.join(", ")}`, warning: null };
  }
  const values = entries.map(([, r]) => r);
  if (!values.includes("me")) {
    return { ok: false, error: "'나'를 한 명 이상 골라 주세요. 내 메시지에서 말투를 읽어요.", warning: null };
  }
  const warning = values.includes("contact") ? null : "'상대'로 지정한 화자가 없어요. 내 메시지만 저장돼요.";
  return { ok: true, error: null, warning };
}

/** '나'로 지정한 화자 중 메시지가 가장 많은 이름(대화방의 내 이름으로 쓴다). */
export function meName(speakers, roles) {
  const mine = (speakers || []).filter((sp) => roles?.[sp.name] === "me");
  mine.sort((a, b) => (b.count || 0) - (a.count || 0));
  return mine[0]?.name || null;
}

/** 제외한 화자를 뺀, 실제로 저장될 메시지 수(미리보기 기준). */
export function keptMessages(speakers, roles) {
  return (speakers || [])
    .filter((sp) => roles?.[sp.name] && roles[sp.name] !== "skip")
    .reduce((sum, sp) => sum + (sp.count || 0), 0);
}

/** "최근 N개만" 선택지. 메시지가 많을 때만 내놓는다. 한도를 넘으면 전체는 고를 수 없다. */
export function keepRecentOptions(total) {
  const n = Number(total) || 0;
  if (n <= 1000) return { needed: false, options: [], forced: false };
  const options = [500, 1000, 3000, 5000].filter((v) => v < n && v <= MAX_IMPORT_MESSAGES);
  return { needed: true, options, forced: n > MAX_IMPORT_MESSAGES };
}

/** 미리보기 응답(계약 §4.4)을 화면용으로 정리한다. */
export function previewView(preview, contactAlias = "") {
  const p = preview || {};
  const speakers = Array.isArray(p.speakers) ? p.speakers : [];
  return {
    formatLabel: formatLabel(p.format, p.variant),
    format: p.format || "auto",
    titleHint: p.title_hint || "",
    total: p.messages_total ?? 0,
    range: formatRange(p.date_from, p.date_to),
    speakers: speakers.map((sp) => ({ name: String(sp.name), count: sp.count ?? 0 })),
    roles: suggestRoles(speakers, contactAlias),
    sample: (Array.isArray(p.sample) ? p.sample : []).map((m) => ({
      id: m.id,
      speaker: String(m.speaker ?? ""),
      sentAt: m.sent_at,
      text: String(m.text ?? ""),
    })),
    skippedLines: p.skipped_lines ?? 0,
    systemLines: p.system_lines ?? 0,
    maskedPii: p.masked_pii ?? 0,
    problems: Array.isArray(p.problems) ? p.problems.map(String) : [],
    keepRecent: keepRecentOptions(p.messages_total),
  };
}

// ---------- 가져온 뒤 진행 상태 ----------

/** 1단계(말투): 가져오기 작업 응답과 통계로 만든 한 줄. */
export function stylePhaseView(job, stats) {
  const imported = job?.imported ?? null;
  const duplicates = job?.duplicates ?? 0;
  const episodes = stats?.episodes ?? job?.episodes ?? null;
  const parts = [];
  if (stats && stats.messages != null) {
    parts.push(`메시지 ${formatNumber(stats.messages)}개 중 내 메시지 ${formatNumber(stats.my_messages ?? 0)}개`);
  } else if (imported != null) {
    parts.push(`메시지 ${formatNumber(imported)}개 저장`);
  }
  if (duplicates > 0) parts.push(`이미 있던 ${formatNumber(duplicates)}개는 건너뜀`);
  if (episodes != null) parts.push(`말투 묶음 ${formatNumber(episodes)}개`);
  return { title: "말투 분석 완료 · 지금 바로 쓸 수 있어요", detail: parts.join(" · "), empty: imported === 0 && duplicates > 0 };
}

const ACTIVE = new Set(["pending", "running"]);

/** 통계(없으면 작업 응답)에서 사건 정리 상태를 정한다. 알 수 없으면 null. */
export function graphStatus(stats, job) {
  if (stats) {
    if (stats.graph_enabled === false) return "skipped";
    if (stats.graph_limited) return "limited";
    const fromJob = stats.latest_job?.graph_status;
    if (fromJob) return fromJob;
    if (stats.graph_pending > 0) return "running";
    return stats.messages > 0 && stats.graph_ingested >= stats.messages ? "completed" : null;
  }
  return job?.graph_status || null;
}

/**
 * 2단계(지난 약속·사건 정리) 표시. 상태: skipped·pending·running·completed·failed·limited.
 * polling이 true인 동안 화면이 통계를 다시 읽는다.
 */
export function graphPhaseView(stats, job) {
  const status = graphStatus(stats, job);
  const done = stats?.graph_ingested ?? null;
  const total = stats?.messages ?? null;
  const count = done != null && total != null ? `${formatNumber(done)} / ${formatNumber(total)}` : "";
  const pct = done != null && total != null ? percent(done, total) : null;
  const base = { status, count, pct, polling: ACTIVE.has(status), tone: "" };
  switch (status) {
    case "skipped":
      return {
        ...base,
        title: "지난 약속·사건 정리는 이 서버에서 쓰지 않아요",
        detail: "말투 맞추기는 그대로 쓸 수 있어요.",
        count: "",
        pct: null,
      };
    case "pending":
      return {
        ...base,
        title: "지난 약속·사건 정리 대기 중",
        detail: "곧 시작해요. 이 창을 닫아도 계속돼요.",
      };
    case "running":
      return {
        ...base,
        title: "지난 약속·사건 정리 중",
        detail: "메시지 하나에 10~15초쯤 걸려요. 이 창을 닫아도 계속되고, 끝나면 지난번에 약속한 내용까지 챙겨 드려요.",
      };
    case "completed":
      return { ...base, tone: "ok", title: "지난 약속·사건 정리 완료", detail: "이제 지난번에 약속한 내용과 겹치면 알려 드려요." };
    case "failed": {
      const why = stats?.latest_job?.graph_error || job?.graph_error;
      return {
        ...base,
        tone: "bad",
        title: "지난 약속·사건 정리 중 문제가 생겼어요",
        detail: `말투 맞추기는 그대로 쓸 수 있어요. 나중에 대화를 다시 가져오면 남은 부분을 이어서 정리해요.${why ? ` (오류: ${why})` : ""}`,
      };
    }
    case "limited":
      return {
        ...base,
        tone: "warn",
        title: "정리 한도에 닿아 여기까지만 정리했어요",
        detail: "남은 메시지는 사건 정리에 쓰지 않아요. 내 과거 표현 찾기는 전부 동작해요.",
      };
    default:
      return {
        ...base,
        title: "지난 약속·사건 정리 상태를 지금은 볼 수 없어요",
        detail: "말투 맞추기는 그대로 쓸 수 있어요.",
        count: "",
        pct: null,
      };
  }
}
