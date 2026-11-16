// 페르소나(상대별 말투 설정) 화면의 순수 로직. 테스트: tests/persona.test.js
// 값의 범위는 backend/app/domain/persona.py와 같다.

export const LIMITS = { address_term: 30, notes: 500, preferred_phrases: 20, forbidden_phrases: 50, phrase: 40 };

// 5단계 척도. 숫자만 보여 주지 않고 단계마다 말로 설명한다(0~100 슬라이더는 근거 없는 정밀도라 쓰지 않는다).
export const SCALES = [
  {
    field: "formality",
    label: "격식",
    question: "얼마나 격식을 차리나요?",
    steps: [
      { short: "반말", desc: "친한 사이처럼 편한 반말로 써요." },
      { short: "편한 존댓말", desc: "존댓말이지만 가볍고 편하게 써요." },
      { short: "보통 존댓말", desc: "무난한 존댓말로 써요." },
      { short: "정중하게", desc: "예의를 갖춘 정중한 말투로 써요." },
      { short: "아주 격식 있게", desc: "공식 문서에 가까운 격식체로 써요." },
    ],
  },
  {
    field: "friendliness",
    label: "친근함",
    question: "얼마나 친근하게 쓰나요?",
    steps: [
      { short: "거리를 둬요", desc: "용건만, 감정 표현 없이 거리를 두고 써요." },
      { short: "조금 거리 있게", desc: "예의는 지키되 살가운 표현은 줄여요." },
      { short: "보통", desc: "너무 딱딱하지도 살갑지도 않게 써요." },
      { short: "친근하게", desc: "부드럽고 살가운 표현을 섞어요." },
      { short: "아주 친근하게", desc: "가까운 사이처럼 다정하게 써요." },
    ],
  },
  {
    field: "directness",
    label: "직접성",
    question: "돌려 말하나요, 바로 말하나요?",
    steps: [
      { short: "많이 돌려서", desc: "부담을 주지 않게 최대한 완곡하게 말해요." },
      { short: "조금 돌려서", desc: "완충 표현을 두고 부드럽게 말해요." },
      { short: "보통", desc: "돌려 말하지도 단도직입도 아니게 써요." },
      { short: "분명하게", desc: "하려는 말을 분명하게 써요." },
      { short: "결론부터", desc: "돌리지 않고 결론부터 바로 말해요." },
    ],
  },
];

export const LENGTH_OPTIONS = [
  { value: "shorter", label: "더 짧게", desc: "초안보다 짧게 줄여요." },
  { value: "same", label: "초안만큼", desc: "초안과 비슷한 길이로 써요." },
  { value: "longer", label: "조금 길게", desc: "초안보다 조금 길게 풀어 써요." },
];

export const EMOJI_OPTIONS = [
  { value: "none", label: "안 써요", desc: "이모지를 넣지 않아요." },
  { value: "rare", label: "가끔", desc: "어울릴 때만 가끔 넣어요." },
  { value: "often", label: "자주", desc: "이모지를 자주 넣어요." },
];

export const DEFAULT_SETTINGS = Object.freeze({
  formality: 3,
  friendliness: 3,
  directness: 3,
  length: "same",
  emoji: "none",
  address_term: null,
  preferred_phrases: [],
  forbidden_phrases: [],
  notes: null,
});

const SCALE_FIELDS = SCALES.map((s) => s.field);
const LENGTHS = LENGTH_OPTIONS.map((o) => o.value);
const EMOJIS = EMOJI_OPTIONS.map((o) => o.value);

function clampStep(value, fallback) {
  const n = Math.round(Number(value));
  return Number.isFinite(n) && n >= 1 && n <= 5 ? n : fallback;
}

function cleanText(value, max) {
  const s = String(value ?? "").trim();
  return s ? s.slice(0, max) : null;
}

/** 표현 목록: 앞뒤 공백 제거, 빈 값·중복 제거, 최대 개수까지. */
export function cleanPhrases(values, max) {
  const out = [];
  for (const raw of Array.isArray(values) ? values : []) {
    const v = String(raw ?? "").trim();
    if (v && !out.includes(v)) out.push(v);
    if (out.length >= max) break;
  }
  return out;
}

/** 어떤 입력이든 서버가 받는 모양의 설정으로 맞춘다(빠진 값은 기본값). */
export function normalizeSettings(raw) {
  const s = raw && typeof raw === "object" ? raw : {};
  return {
    formality: clampStep(s.formality, DEFAULT_SETTINGS.formality),
    friendliness: clampStep(s.friendliness, DEFAULT_SETTINGS.friendliness),
    directness: clampStep(s.directness, DEFAULT_SETTINGS.directness),
    length: LENGTHS.includes(s.length) ? s.length : DEFAULT_SETTINGS.length,
    emoji: EMOJIS.includes(s.emoji) ? s.emoji : DEFAULT_SETTINGS.emoji,
    address_term: cleanText(s.address_term, LIMITS.address_term),
    preferred_phrases: cleanPhrases(s.preferred_phrases, LIMITS.preferred_phrases),
    forbidden_phrases: cleanPhrases(s.forbidden_phrases, LIMITS.forbidden_phrases),
    notes: cleanText(s.notes, LIMITS.notes),
  };
}

export function settingsEqual(a, b) {
  return JSON.stringify(normalizeSettings(a)) === JSON.stringify(normalizeSettings(b));
}

export function stepLabel(field, value) {
  const scale = SCALES.find((s) => s.field === field);
  return scale?.steps[Number(value) - 1]?.short || "";
}

function optionLabel(options, value) {
  return options.find((o) => o.value === value)?.label || "";
}

/** 버전 기록 한 줄 요약: "격식 정중하게 · 친근함 보통 · …" */
export function describeSettings(raw) {
  const s = normalizeSettings(raw);
  const parts = SCALES.map((scale) => `${scale.label} ${stepLabel(scale.field, s[scale.field])}`);
  parts.push(`길이 ${optionLabel(LENGTH_OPTIONS, s.length)}`, `이모지 ${optionLabel(EMOJI_OPTIONS, s.emoji)}`);
  if (s.address_term) parts.push(`호칭 ${s.address_term}`);
  if (s.forbidden_phrases.length) parts.push(`금지 ${s.forbidden_phrases.length}개`);
  if (s.preferred_phrases.length) parts.push(`선호 ${s.preferred_phrases.length}개`);
  if (s.notes) parts.push("메모 있음"); // 메모만 다른 버전도 구분되게
  return parts.join(" · ");
}

const FIELD_LABEL = {
  formality: "격식",
  friendliness: "친근함",
  directness: "직접성",
  length: "길이",
  emoji: "이모지",
  address_term: "호칭",
  preferred_phrases: "선호 표현",
  forbidden_phrases: "금지 표현",
  notes: "메모",
};

function signalValue(field, value) {
  if (SCALE_FIELDS.includes(field)) {
    // 서버는 단계를 숫자로도 글자("4")로도 준다. 그 밖의 값(true, 빈 글자 등)은 받지 않는다
    const numeric = typeof value === "number" || (typeof value === "string" && value.trim() !== "");
    const n = numeric ? clampStep(value, null) : null;
    return n === null ? null : n;
  }
  if (field === "length") return LENGTHS.includes(value) ? value : null;
  if (field === "emoji") return EMOJIS.includes(value) ? value : null;
  if (field === "address_term") return cleanText(value, LIMITS.address_term);
  if (field === "preferred_phrases" || field === "forbidden_phrases") {
    const list = cleanPhrases(Array.isArray(value) ? value : [value], LIMITS[field]);
    return list.length ? list : null;
  }
  return null; // 메모 등 그 밖의 항목은 제안으로 덮어쓰지 않는다
}

/** 내 메시지가 이보다 적으면 말투를 읽기에 표본이 모자란다고 보고 제안을 보여 주지 않는다. */
export const MIN_SUGGESTION_MESSAGES = 5;

/**
 * 제안 응답을 어떻게 보여 줄지: "none"(내 메시지 없음) · "few"(표본이 적음) · "same"(바꿀 것 없음) · "ok".
 * 서버가 표본이 적다고 settings: null을 주는 경우도 "few"다.
 */
export function suggestionState(data) {
  const count = Number(data?.based_on_messages) || 0;
  if (count <= 0) return "none";
  if (data?.settings === null || count < MIN_SUGGESTION_MESSAGES) return "few";
  const usable = (Array.isArray(data?.signals) ? data.signals : []).some((s) => signalView(s).usable);
  return usable ? "ok" : "same";
}

/** 제안 신호 한 건을 화면 표시용으로. 모르는 항목·범위 밖 값이면 usable=false. */
export function signalView(signal) {
  const field = String(signal?.field ?? "");
  const value = signalValue(field, signal?.value);
  let valueLabel = "";
  if (value !== null) {
    if (SCALE_FIELDS.includes(field)) valueLabel = `${value}단계 · ${stepLabel(field, value)}`;
    else if (field === "length") valueLabel = optionLabel(LENGTH_OPTIONS, value);
    else if (field === "emoji") valueLabel = optionLabel(EMOJI_OPTIONS, value);
    else if (Array.isArray(value)) valueLabel = value.join(", ");
    else valueLabel = String(value);
  }
  return {
    field,
    fieldLabel: FIELD_LABEL[field] || field,
    value,
    valueLabel,
    reason: String(signal?.reason ?? ""),
    usable: value !== null,
  };
}

/**
 * 제안을 편집 중인 설정에 덮어쓴다(저장은 하지 않는다 — 사용자가 확인하고 저장한다).
 * 표현 목록은 기존 것에 더한다. 적용한 항목 이름을 함께 돌려준다.
 */
export function applySignals(settings, signals) {
  const next = normalizeSettings(settings);
  const applied = [];
  for (const sig of signals || []) {
    const view = signalView(sig);
    if (!view.usable) continue;
    if (Array.isArray(view.value)) {
      next[view.field] = cleanPhrases([...next[view.field], ...view.value], LIMITS[view.field]);
    } else {
      next[view.field] = view.value;
    }
    if (!applied.includes(view.field)) applied.push(view.field);
  }
  return { settings: next, applied };
}

/** 지금 설정과 제안 값이 이미 같은가(같으면 "적용" 버튼을 숨긴다). */
export function signalMatches(settings, signal) {
  const view = signalView(signal);
  if (!view.usable) return false;
  const current = normalizeSettings(settings)[view.field];
  if (Array.isArray(view.value)) return view.value.every((v) => current.includes(v));
  return current === view.value;
}

/**
 * 표현 칩 입력: 새 표현을 더한다. 쉼표로 여러 개를 한 번에 넣을 수 있다.
 * 돌려주는 error는 화면에 그대로 보여 줄 문장(없으면 null).
 */
export function addPhrases(list, raw, max) {
  const current = [...(list || [])];
  const incoming = String(raw ?? "")
    .split(/[,\n]/)
    .map((v) => v.trim())
    .filter(Boolean);
  let error = null;
  for (const v of incoming) {
    if (v.length > LIMITS.phrase) {
      error = `표현 하나는 ${LIMITS.phrase}자까지 넣을 수 있어요.`;
      continue;
    }
    if (current.includes(v)) continue;
    if (current.length >= max) {
      error = `${max}개까지 넣을 수 있어요.`;
      break;
    }
    current.push(v);
  }
  return { list: current, error };
}
