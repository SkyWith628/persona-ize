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

// 신호의 출처. 서버가 source를 주지 않으면 예전 응답이므로 "messages"로 본다.
//  messages: 내 메시지 전체를 한 덩어리로 본 문체 / register: 말투·호칭이 바뀐 시점(규칙 계산) / graph: 그래프의 관계 사실
export const SOURCE_LABEL = { messages: "내 메시지 전체", register: "말투 변화", graph: "그래프" };

// 말투 단계 이름 (backend/app/ai/register.py의 LEVEL_KO와 같다)
export const LEVEL_LABEL = { hapsyo: "합쇼체", haeyo: "해요체", hae: "반말", mixed: "혼용", unknown: "알 수 없음" };

function signalSource(signal) {
  const source = String(signal?.source ?? "messages");
  return Object.hasOwn(SOURCE_LABEL, source) ? source : "messages";
}

/** 서버가 준 날짜("2026-10-21" 또는 그걸로 시작하는 시각)에서 날짜만. 그 모양이 아니면 빈 글자. */
function dayText(value) {
  const m = /^(\d{4}-\d{2}-\d{2})(?:$|[T ])/.exec(String(value ?? ""));
  return m ? m[1] : "";
}

/** 내 메시지가 적어 "전체 문체"를 읽지 않는 경우인가. 서버가 표본이 적다고 settings: null을 주는 경우도 같다. */
export function isFewMessages(data) {
  const count = Number(data?.based_on_messages) || 0;
  return data?.settings === null || count < MIN_SUGGESTION_MESSAGES;
}

/**
 * 화면에 보여 줄 신호. 쓸 수 없는 것(모르는 항목·범위 밖 값)은 뺀다.
 * 표본이 적을 때는 "내 메시지 전체" 신호만 뺀다 — 우연한 말버릇을 말투로 읽기 쉽기 때문이다.
 * 말투 변화·그래프 신호는 근거 메시지가 따로 있으므로 표본이 적어도 보여 준다.
 */
export function visibleSignals(data) {
  const few = isFewMessages(data);
  return (Array.isArray(data?.signals) ? data.signals : []).filter(
    (s) => signalView(s).usable && !(few && signalSource(s) === "messages"),
  );
}

/**
 * 제안 응답을 어떻게 보여 줄지: "none"(내 메시지 없음) · "few"(표본이 적음) · "same"(바꿀 것 없음) · "ok".
 * 제안 카드나 보여 줄 신호가 하나라도 있으면 표본이 적어도 "ok"다(빈 상태로 가리지 않는다).
 */
export function suggestionState(data) {
  if (cardView(data?.card) || visibleSignals(data).length) return "ok";
  const count = Number(data?.based_on_messages) || 0;
  if (count <= 0) return "none";
  return isFewMessages(data) ? "few" : "same";
}

function cleanCount(value) {
  const n = Math.round(Number(value));
  return Number.isFinite(n) && n > 0 ? n : 0;
}

function cleanIds(value) {
  return (Array.isArray(value) ? value : []).map((v) => String(v ?? "").trim()).filter(Boolean);
}

/** 근거 메시지 id를 한 줄로. 많으면 앞의 몇 개만 적고 나머지는 건수로 줄인다. */
export function evidenceText(ids, max = 4) {
  const list = cleanIds(ids);
  if (list.length <= max) return list.join(", ");
  return `${list.slice(0, max).join(", ")} 외 ${list.length - max}건`;
}

function levelLabel(level) {
  const key = String(level ?? "");
  return Object.hasOwn(LEVEL_LABEL, key) ? LEVEL_LABEL[key] : "";
}

/** "(3건: c3:m02, c3:m04)"처럼 괄호 안에 넣을 조각들을 잇는다. 빈 조각은 뺀다. */
function paren(parts) {
  const kept = parts.filter(Boolean);
  return kept.length ? ` (${kept.join(", ")})` : "";
}

function speechPart(segment, countPrefix) {
  const label = levelLabel(segment?.level);
  if (!label) return null;
  const count = cleanCount(segment?.count);
  const head = count ? `${countPrefix}${count}건` : "";
  const ids = evidenceText(segment?.message_ids);
  const since = dayText(segment?.since);
  return { label, text: `${label}${paren([[head, ids].filter(Boolean).join(": "), since ? `${since}부터` : ""])}` };
}

/**
 * 카드가 정해 준 값(proposed)을 신호 모양으로 바꾼다 — 적용은 신호와 같은 길(applySignals)로 간다.
 * 격식과 호칭만 받는다. 범위 밖 값은 버린다.
 */
export function cardSignals(card) {
  const proposed = card?.proposed && typeof card.proposed === "object" ? card.proposed : {};
  return [
    { field: "formality", value: proposed.formality, reason: "", source: "register" },
    { field: "address_term", value: proposed.address_term, reason: "", source: "register" },
  ].filter((s) => signalView(s).usable);
}

/** 카드의 적용 버튼 글자: "격식 4 + 호칭 추가" · "격식 4 적용" · "호칭 추가". 정해 준 값이 없으면 빈 글자. */
export function cardApplyLabel(card) {
  const views = cardSignals(card).map(signalView);
  const formality = views.find((v) => v.field === "formality");
  const address = views.some((v) => v.field === "address_term");
  if (formality && address) return `격식 ${formality.value} + 호칭 추가`;
  if (formality) return `격식 ${formality.value} 적용`;
  return address ? "호칭 추가" : "";
}

/**
 * 제안 카드(말투가 바뀐 시점과 그 근거)를 화면 표시용으로. 보여 줄 것이 없으면 null.
 * rows: [{ key, label, text }] — 말투 · 최근 말투(두 가지가 섞임) · 호칭 · 계기(그래프) · 범위 단서(그래프).
 * 근거 메시지 id는 글자로만 보여 준다. 계기와 범위 단서는 제안의 근거일 뿐, 설정에 들어가는 값이 아니다.
 */
export function cardView(card, fallbackAlias = "") {
  if (!card || typeof card !== "object") return null;
  const rows = [];

  const previous = speechPart(card.speech_previous, "");
  const current = speechPart(card.speech_current, "최근 ");
  if (previous && current) rows.push({ key: "speech", label: "말투", text: `${previous.text} → ${current.text}` });
  else if (current) rows.push({ key: "speech", label: "말투", text: current.text });

  const mixed =
    card.two_valued && typeof card.two_valued === "object"
      ? Object.entries(card.two_valued)
          .map(([level, n]) => ({ label: levelLabel(level), count: cleanCount(n) }))
          .filter((x) => x.label && x.count)
      : [];
  const twoValued = mixed.length >= 2;
  if (twoValued) {
    rows.push({ key: "two_valued", label: "최근 말투", text: `${mixed.map((x) => `${x.label} ${x.count}건`).join(" / ")}이 섞여 있어요` });
  }

  const term = cleanText(card.address?.term, LIMITS.address_term);
  if (term) {
    const count = cleanCount(card.address.count);
    // 한 번만 본 호칭은 "후보"다 — 얼마나 얇은 근거인지 그대로 적는다
    const candidate = card.address.status === "candidate" || count === 1;
    const head = count ? (candidate ? `${count}회 관찰` : `${count}회`) : "";
    const ids = evidenceText(card.address.message_ids);
    const since = dayText(card.address.since);
    rows.push({ key: "address", label: "호칭", text: `'${term}'${paren([[head, ids].filter(Boolean).join(": "), since ? `${since}부터` : ""])}` });
  }

  const event = String(card.trigger?.event ?? "").trim();
  if (event) {
    rows.push({
      key: "trigger",
      label: "계기(그래프)",
      text: `${event}${paren([dayText(card.trigger.at), evidenceText(card.trigger.message_ids)])}`,
    });
  }

  const hints = (Array.isArray(card.scope_hints) ? card.scope_hints : [])
    .map((hint) => ({ text: String(hint?.text ?? "").trim(), ids: evidenceText(hint?.message_ids) }))
    .filter((hint) => hint.text);
  if (hints.length) {
    rows.push({ key: "scope", label: "범위 단서(그래프)", text: hints.map((x) => `"${x.text}"${paren([x.ids])}`).join(" / ") });
  }

  if (!rows.length) return null;
  const alias = String(card.contact_alias ?? "").trim() || String(fallbackAlias ?? "").trim();
  const who = alias ? `${alias} 님에게 ` : "";
  let title = `${who}보낸 내 메시지에서 읽은 말투예요.`;
  if (previous && current) title = `${who}쓰는 말투가 바뀐 것으로 보여요.`;
  else if (twoValued) title = `${who}두 가지 말투를 섞어 쓰고 있어요.`;
  return {
    title,
    rows,
    ask: card.ask === true,
    askReason: String(card.ask_reason ?? "").trim(),
    applyLabel: cardApplyLabel(card),
  };
}

/** 제안 신호 한 건을 화면 표시용으로. 모르는 항목·범위 밖 값이면 usable=false. */
export function signalView(signal) {
  const field = String(signal?.field ?? "");
  const value = signalValue(field, signal?.value);
  const source = signalSource(signal);
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
    source,
    sourceLabel: SOURCE_LABEL[source],
    since: dayText(signal?.since), // 이 값이 처음 확인된 날. 없으면 빈 글자
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

// 카드가 물을 때 말투 변화 신호로는 정하지 않는 항목 (backend/app/services/suggestion_service.py의 ASKED_FIELDS)
const ASKED_FIELDS = ["formality", "address_term"];

/** 카드에서 근거 메시지 id만 뺀 사본. 카드가 없으면 그대로 돌려준다. */
function withoutEvidenceIds(card) {
  if (!card || typeof card !== "object") return card;
  const strip = (part) => (part && typeof part === "object" ? { ...part, message_ids: [] } : part);
  return {
    ...card,
    speech_previous: strip(card.speech_previous),
    speech_current: strip(card.speech_current),
    address: strip(card.address),
    trigger: strip(card.trigger),
    scope_hints: Array.isArray(card.scope_hints) ? card.scope_hints.map(strip) : card.scope_hints,
  };
}

function changeValueLabel(field, value) {
  if (value === null || (Array.isArray(value) && !value.length)) return "없음";
  return signalView({ field, value }).valueLabel || String(value);
}

/**
 * 가져오기 직후의 "이 상대에게 쓰는 말투" 제안: 제안 응답과 지금 설정으로, 저장할 설정과 바뀌는 항목을 만든다.
 * 저장은 하지 않는다 — 사용자가 한 번 확인하고 누를 때만 저장한다.
 *
 * state: "none"(내 메시지 없음) · "few"(표본이 적음) · "same"(지금 설정과 같음) · "ask"(값을 정해 주지 않고 묻는다) · "ok"
 * - settings: 저장할 설정 전체("ok"일 때만, 아니면 null)
 * - changes: [{ field, label, from, to }] — 지금 설정에서 바뀌는 항목
 * - rows: 근거 줄(말투 · 호칭 · 계기 · 범위 단서). cardView의 rows에서 근거 메시지 id만 뺀 것 —
 *   방금 가져온 화면에서는 건수와 날짜면 충분하고, id는 상대 화면의 제안에서 볼 수 있다
 * - asking: 카드가 묻는 중인가(상황에 따라 다른 말투일 수 있다). 이때 화면은 왜 묻는지(askReason)를 함께 보여 준다
 *
 * 제안 값은 서버가 합쳐 준 settings를 쓴다. 표본이 적어 settings가 없으면 보여 줄 신호(말투 변화·그래프)만
 * 지금 설정에 덮어쓴다. 카드가 물을 때 서버는 격식·호칭을 settings에서 빼지만, 여기서는 카드가 정해 둔 값
 * (proposed)을 다시 얹는다 — 이 화면의 저장 버튼이 곧 그 물음에 대한 사용자의 답이기 때문이다.
 * 카드가 값을 정해 두지 않았으면(두 말투가 섞인 경우 등) 격식·호칭은 건드리지 않는다.
 */
export function importProposal(data, current) {
  const base = normalizeSettings(current);
  const view = cardView(withoutEvidenceIds(data?.card));
  const asking = Boolean(view?.ask);
  const out = { state: suggestionState(data), settings: null, changes: [], rows: view?.rows ?? [], asking, askReason: view?.askReason ?? "" };
  if (out.state !== "ok") return out;

  let proposed;
  if (!isFewMessages(data) && data.settings && typeof data.settings === "object") {
    proposed = normalizeSettings(data.settings);
  } else {
    const signals = visibleSignals(data).filter(
      (s) => !(asking && signalSource(s) === "register" && ASKED_FIELDS.includes(String(s.field))),
    );
    proposed = applySignals(base, signals).settings;
  }
  if (asking) proposed = applySignals(proposed, cardSignals(data.card)).settings;
  for (const field of Object.keys(FIELD_LABEL)) {
    if (JSON.stringify(base[field]) === JSON.stringify(proposed[field])) continue;
    out.changes.push({
      field,
      label: FIELD_LABEL[field],
      from: changeValueLabel(field, base[field]),
      to: changeValueLabel(field, proposed[field]),
    });
  }
  if (out.changes.length) out.settings = proposed;
  else out.state = asking ? "ask" : "same";
  return out;
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
