// 대화 가져오기의 순수 로직: 파일 인코딩 판별, 미리보기 모델, 화자 지정, 진행 상태.
// 테스트: tests/importing.test.js

import { countCodePoints, errorCode, formatNumber, formatRange, percent } from "./format.js";

// 서버 기본값(계약 §3: MAX_IMPORT_CHARS, MAX_BODY_BYTES). 서버 설정이 다르면 서버가 413/422로 알려 준다 —
// 여기 값은 큰 파일을 올려 보내기 전에 미리 막기 위한 안내용이다.
export const MAX_IMPORT_CHARS = 2_000_000;
export const MAX_BODY_BYTES = 8_000_000;
// 한 번에 가져올 수 있는 메시지 수는 미리보기 응답의 max_messages를 쓴다. 응답에 없을 때(옛 서버)만 이 값.
export const DEFAULT_MAX_MESSAGES = 5000;

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
 * ① 서버 추천값 ② 이 상대의 다른 대화방에서 '나'였던 이름이면 나 ③ 상대 이름과 일치하면 상대
 * ④ 화자가 둘이고 한쪽만 정해졌으면 나머지는 반대쪽.
 * 그래도 모르면 null로 두고 사용자가 고르게 한다(단체방 등).
 *
 * @param {Array<{name: string, suggested_role?: string}>} speakers
 * @param {string} contactAlias 가져올 상대의 이름
 * @param {Array<string|null>} [knownMeNames] 이 상대의 기존 대화방에 저장된 내 이름(me_name)
 */
export function suggestRoles(speakers, contactAlias, knownMeNames = []) {
  const roles = {};
  const list = speakers || [];
  for (const sp of list) {
    const suggested = ["me", "contact", "skip"].includes(sp.suggested_role) ? sp.suggested_role : null;
    roles[sp.name] = suggested;
  }
  const unassigned = () => list.filter((sp) => roles[sp.name] === null);
  const mine = new Set((knownMeNames || []).filter(Boolean).map(squash));
  for (const sp of unassigned()) {
    if (mine.has(squash(sp.name))) roles[sp.name] = "me";
  }
  if (contactAlias) {
    const hit = unassigned().filter((sp) => nameMatches(sp.name, contactAlias));
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
  const contacts = values.filter((r) => r === "contact").length;
  let warning = null;
  if (contacts === 0) warning = "'상대'로 지정한 화자가 없어요. 내 메시지만 저장돼요.";
  else if (contacts > 1) {
    // 대화방은 상대 한 사람의 것이다 — 여럿을 상대로 두면 그 사람들의 말이 모두 이 상대의 말로 저장된다
    warning = `'상대'로 지정한 화자가 ${contacts}명이에요. 모두 이 상대가 한 말로 저장돼요. 다른 사람이라면 '제외'로 바꿔 주세요.`;
  }
  return { ok: true, error: null, warning };
}

/** 단체방으로 본다: 말한 사람이 셋 이상. (둘이면 나와 상대다.) */
export const GROUP_ROOM_SPEAKERS = 3;

export function isGroupRoom(speakers) {
  return (speakers || []).length >= GROUP_ROOM_SPEAKERS;
}

/** 아직 정하지 않은 화자를 모두 '제외'로 둔 새 지정표. 이미 정한 것은 건드리지 않는다. */
export function skipUnassigned(roles) {
  return Object.fromEntries(Object.entries(roles || {}).map(([name, r]) => [name, r || "skip"]));
}

/** 아직 정하지 않은 화자 수. */
export function unassignedCount(roles) {
  return Object.values(roles || {}).filter((r) => !r).length;
}

// 내보내기 파일이 나를 가리킬 때 쓰는 자리표시 이름. 진짜 이름이 아니므로 대화방의 "내 이름"으로 쓰지 않는다.
// (카카오톡 안드로이드는 나를 "회원님"으로 적는다. 붙여넣기 형식의 me/나도 같다.)
const PLACEHOLDER_ME_NAMES = new Set(["회원님", "me", "나"]);

export function isPlaceholderName(name) {
  return PLACEHOLDER_ME_NAMES.has(squash(name));
}

/**
 * 대화방에 저장할 "내 이름": '나'로 지정한 화자 중 진짜 이름이면서 메시지가 가장 많은 것.
 * 자리표시 이름(회원님 등)뿐이면 null — 보내지 않는다(서버가 채우거나 비워 둔다).
 */
export function meName(speakers, roles) {
  const mine = (speakers || []).filter((sp) => roles?.[sp.name] === "me" && !isPlaceholderName(sp.name));
  mine.sort((a, b) => (b.count || 0) - (a.count || 0));
  return mine[0]?.name || null;
}

/**
 * '제외'로 둔 화자만 뺀 메시지 수 — 아직 정하지 않은 화자는 가져올 것으로 센다.
 * "최근 N개만" 선택지는 이 수로 만든다: 서버는 제외한 사람의 메시지를 먼저 버리고 그 뒤에 최근 N개를 남기므로,
 * 단체방에서 여럿을 제외해 한도 안으로 들어오면 자를 필요가 없다.
 */
export function plannedMessages(speakers, roles) {
  return (speakers || [])
    .filter((sp) => roles?.[sp.name] !== "skip")
    .reduce((sum, sp) => sum + (sp.count || 0), 0);
}

/**
 * 화자 지정이 바뀐 뒤의 "최근 N개만" 상태 → { keep: 선택지, value: 고른 값(null=전체) }.
 * 전에 고른 값이 아직 선택지에 있으면 그대로 두고, 한도를 넘는데 고른 값이 없으면 가장 큰 선택지를 고른다.
 */
export function keepRecentFor(speakers, roles, maxMessages, current = null) {
  const keep = keepRecentOptions(plannedMessages(speakers, roles), maxMessages);
  let value = keep.options.includes(current) ? current : null;
  if (keep.forced && value === null) value = keep.options[keep.options.length - 1] ?? null;
  return { keep, value };
}

/** 제외한 화자를 뺀, 실제로 저장될 메시지 수(미리보기 기준). */
export function keptMessages(speakers, roles) {
  return (speakers || [])
    .filter((sp) => roles?.[sp.name] && roles[sp.name] !== "skip")
    .reduce((sum, sp) => sum + (sp.count || 0), 0);
}

/**
 * "최근 N개만" 선택지. 메시지가 많을 때만 내놓는다.
 * maxMessages는 서버가 한 번에 받는 수(미리보기 응답의 max_messages). 그보다 많으면 전체는 고를 수 없다(forced).
 */
export function keepRecentOptions(total, maxMessages = DEFAULT_MAX_MESSAGES) {
  const n = Number(total) || 0;
  const max = Number(maxMessages) > 0 ? Number(maxMessages) : DEFAULT_MAX_MESSAGES;
  if (n <= Math.min(1000, max)) return { needed: false, options: [], forced: false, max };
  const options = [...new Set([500, 1000, 3000, max])].filter((v) => v < n && v <= max).sort((a, b) => a - b);
  return { needed: true, options, forced: n > max, max };
}

const CHAT_TYPE_LABEL = {
  personal_chat: "1:1 대화",
  private_group: "그룹",
  private_supergroup: "그룹",
  public_supergroup: "공개 그룹",
  private_channel: "채널",
  public_channel: "공개 채널",
  saved_messages: "저장한 메시지",
  bot_chat: "봇",
};

/** 텔레그램 대화 종류를 사람이 읽는 말로. 모르는 값은 그대로. */
export function chatTypeLabel(type) {
  return CHAT_TYPE_LABEL[type] || (type ? String(type) : "");
}

/** 미리보기 응답(계약 §4.4)을 화면용으로 정리한다. */
export function previewView(preview, contactAlias = "", knownMeNames = []) {
  const p = preview || {};
  const speakers = Array.isArray(p.speakers) ? p.speakers : [];
  // 텔레그램 "계정 전체 내보내기": 메시지 없이 대화 목록(chats)만 온다 → 하나를 골라 chat_id로 다시 읽는다
  const chats = Array.isArray(p.chats)
    ? p.chats.map((c) => ({
        id: String(c.id),
        name: String(c.name ?? ""),
        typeLabel: chatTypeLabel(c.type),
        messages: c.messages ?? 0,
      }))
    : null;
  return {
    formatLabel: formatLabel(p.format, p.variant),
    format: p.format || "auto",
    titleHint: p.title_hint || "",
    total: p.messages_total ?? 0,
    chats,
    needsChat: chats !== null,
    maxMessages: Number(p.max_messages) > 0 ? Number(p.max_messages) : DEFAULT_MAX_MESSAGES,
    range: formatRange(p.date_from, p.date_to),
    speakers: speakers.map((sp) => ({ name: String(sp.name), count: sp.count ?? 0 })),
    roles: suggestRoles(speakers, contactAlias, knownMeNames),
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
    keepRecent: keepRecentOptions(p.messages_total, p.max_messages),
  };
}

// ---------- 가져온 뒤 진행 상태 ----------

/**
 * 1단계(말투): 가져오기 작업 응답과 통계로 만든 한 줄. 상태: processing·completed·failed.
 * polling이 true인 동안 화면이 통계를 다시 읽는다. processing이고 서버가 진행률을 알려 주면
 * count("묶음 1,500개 중 420개")와 progress({done, total}, 막대용)가 함께 온다.
 */
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
  const empty = imported === 0 && duplicates > 0;
  // 통계의 최근 작업이 이 작업이면 그쪽 상태가 더 새롭다(묶음 생성은 응답 뒤에 끝날 수 있다)
  const latest = stats?.latest_job;
  const status = (latest && (!job || latest.id === job.id) ? latest.status : job?.status) || "completed";
  if (status === "processing") {
    // 서버가 진행률을 알 때만(index_total) 수치와 막대를 보인다 — 옛 서버·재시작 직후에는 문구만 나온다
    const progress = indexProgress(latest && (!job || latest.id === job.id) ? latest : job);
    return {
      status,
      tone: "",
      polling: true,
      empty,
      progress,
      count: progress ? `묶음 ${formatNumber(progress.total)}개 중 ${formatNumber(progress.done)}개` : "",
      title: "말투 분석 중이에요",
      detail: [...parts, "대화가 길면 몇 분 걸려요. 이 창을 닫아도 계속되고, 끝나기 전에는 말투 예시 없이 첨삭돼요."].join(" · "),
    };
  }
  if (status === "failed") {
    return {
      status,
      tone: "bad",
      polling: false,
      empty,
      title: "말투 분석을 끝내지 못했어요",
      detail: [...parts, "메시지는 저장됐어요. 같은 대화를 다시 가져오면 이어서 분석해요."].join(" · "),
    };
  }
  if (episodes != null) parts.push(`말투 묶음 ${formatNumber(episodes)}개`);
  return { status, tone: "ok", polling: false, empty, title: "말투 분석 완료 · 지금 바로 쓸 수 있어요", detail: parts.join(" · ") };
}

/** 작업 응답의 말투 묶음 진행률({done, total}). 서버가 모르거나(null) 값이 이상하면 null. */
function indexProgress(source) {
  const total = source?.index_total;
  const done = source?.index_done;
  if (!Number.isFinite(total) || !Number.isFinite(done) || total <= 0) return null;
  return { done: Math.min(Math.max(done, 0), total), total };
}

const ACTIVE = new Set(["pending", "running"]);

/**
 * 통계(없으면 작업 응답)에서 사건 정리 상태를 정한다. 알 수 없으면 null.
 * 작업 기록의 상태(latest_job.graph_status)는 서버가 다시 시작되면 running·pending으로 남을 수 있으므로,
 * 한도 표시(graph_limited)와 진행 수치(graph_ingested / messages)를 먼저 본다.
 */
export function graphStatus(stats, job) {
  if (stats) {
    if (stats.graph_enabled === false) return "skipped";
    if (stats.graph_limited) return "limited";
    // 보류된 메시지(거듭 실패해 더 시도하지 않는 것)는 끝난 것으로 친다 — 기다려도 들어가지 않는다
    if (stats.messages > 0 && stats.graph_ingested + (stats.graph_held ?? 0) >= stats.messages) return "completed";
    const fromJob = stats.latest_job?.graph_status;
    if (fromJob) return fromJob;
    if (stats.graph_pending > 0) return "running";
    return null;
  }
  return job?.graph_status || null;
}

/** 진행 수치가 이만큼(밀리초) 그대로면 더 읽지 않는다 — 서버가 다시 시작돼 작업 상태만 "진행 중"으로 남은 경우. */
export const POLL_STALL_MS = 3 * 60 * 1000;

/**
 * 진행이 멈췄는지 따진다. prev는 앞서 돌려준 값(처음이면 null).
 * 수치(graph_ingested / messages)가 바뀌면 시계를 새로 잡고, stallMs 동안 그대로면 stalled=true.
 */
export function trackProgress(prev, stats, now, stallMs = POLL_STALL_MS) {
  const key = stats ? `${stats.graph_ingested ?? ""}/${stats.messages ?? ""}` : "";
  if (!prev || prev.key !== key) return { key, since: now, stalled: false };
  return { key, since: prev.since, stalled: now - prev.since >= stallMs };
}

/**
 * 2단계(지난 약속·사건 정리) 표시. 상태: skipped·pending·running·completed·failed·limited.
 * polling이 true인 동안 화면이 통계를 다시 읽는다.
 */
export function graphPhaseView(stats, job, { stalled = false } = {}) {
  const status = graphStatus(stats, job);
  const done = stats?.graph_ingested ?? null;
  const total = stats?.messages ?? null;
  const count = done != null && total != null ? `${formatNumber(done)} / ${formatNumber(total)}` : "";
  const pct = done != null && total != null ? percent(done, total) : null;
  const base = { status, count, pct, polling: ACTIVE.has(status), tone: "" };
  if (stalled && ACTIVE.has(status)) {
    // 한동안 수치가 그대로다 — 더 읽지 않고, 나중에 다시 보라고 알린다
    return {
      ...base,
      polling: false,
      tone: "warn",
      title: "지난 약속·사건 정리가 한동안 멈춰 있어요",
      detail: "서버가 다시 시작됐을 수 있어요. 나중에 상대 화면에서 다시 확인해 주세요. 말투 맞추기는 그대로 쓸 수 있어요.",
    };
  }
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
    case "completed": {
      const held = stats?.graph_held ?? 0;
      if (held > 0) {
        // 다 넣은 것처럼 보이면 안 된다 — 빠진 건수를 그대로 알린다
        return {
          ...base,
          tone: "warn",
          title: `지난 약속·사건 정리 완료 · 메시지 ${formatNumber(held)}개는 넣지 못했어요`,
          detail: "여러 번 시도해도 정리되지 않아 건너뛰었어요. 그 메시지에 담긴 약속은 챙기지 못할 수 있어요. 말투 맞추기는 그대로 쓸 수 있어요.",
        };
      }
      return { ...base, tone: "ok", title: "지난 약속·사건 정리 완료", detail: "이제 지난번에 약속한 내용과 겹치면 알려 드려요." };
    }
    case "failed": {
      // 서버의 예외 문장은 보여 주지 않는다 — 짧은 코드일 때만 운영자에게 전할 수 있게 덧붙인다.
      const why = errorCode(stats?.latest_job?.graph_error || job?.graph_error);
      // 연달아 보류되면 실행이 멈춘다(failed) — 그때도 건너뛴 건수를 숨기지 않는다
      const held = stats?.graph_held ?? 0;
      const skipped = held > 0 ? ` 메시지 ${formatNumber(held)}개는 여러 번 시도해도 정리되지 않아 건너뛰었어요.` : "";
      return {
        ...base,
        tone: "bad",
        title: "지난 약속·사건 정리 중 문제가 생겼어요",
        detail: `말투 맞추기는 그대로 쓸 수 있어요. 나중에 대화를 다시 가져오면 남은 부분을 이어서 정리해요.${skipped}${why ? ` (오류 종류: ${why})` : ""}`,
      };
    }
    case "limited": {
      // 끝난 상태다(더 읽어도 바뀌지 않는다). graph_error가 어느 한도인지 알려 준다: user_cap | daily_cap
      const why = stats?.latest_job?.graph_error || job?.graph_error;
      const detail =
        why === "daily_cap"
          ? "오늘 서비스 전체의 정리 한도에 닿았어요. 남은 메시지는 한도가 풀린 뒤에 이어서 정리해요. 내 과거 표현 찾기는 전부 동작해요."
          : why === "user_cap"
            ? "계정마다 정해진 정리 한도에 닿았어요. 남은 메시지는 사건 정리에 쓰지 않아요. 내 과거 표현 찾기는 전부 동작해요."
            : "남은 메시지는 사건 정리에 쓰지 않아요. 내 과거 표현 찾기는 전부 동작해요.";
      return { ...base, tone: "warn", title: "정리 한도에 닿아 여기까지만 정리했어요", detail };
    }
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
