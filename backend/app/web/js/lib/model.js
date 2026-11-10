// 서버 응답 → 화면에 그릴 모양. DOM을 만지지 않는 순수 함수만 둔다(테스트: tests/model.test.js).

import { hasFieldError } from "./errors.js";
import { formatSeconds } from "./format.js";

export const REL_LABEL = {
  superior: "상사",
  colleague: "동료·동기",
  subordinate: "후배",
  friend: "친구",
  family: "가족",
  partner: "연인",
  acquaintance: "지인·거래처",
};
export const REL_ORDER = ["superior", "colleague", "subordinate", "friend", "family", "partner", "acquaintance"];

export function relLabel(type) {
  return REL_LABEL[type] || String(type ?? "");
}

// 실험 조건. 화면 문구는 사용자의 말로 풀되, 시연과 평가에 필요한 A~D 표기는 남긴다.
export const CONDITIONS = [
  { value: "auto", label: "자동", desc: "이 상대에게 쓸 수 있는 가장 풍부한 방법을 알아서 골라요." },
  { value: "A", label: "A 기준선", desc: "말투 설정 없이 일반적인 다듬기만 해요." },
  { value: "B", label: "B 페르소나", desc: "이 상대에게 정해 둔 말투 설정을 써요." },
  { value: "C", label: "C +내 과거 표현", desc: "말투 설정에 더해, 이 상대에게 내가 실제로 썼던 표현을 참고해요." },
  { value: "D", label: "D +그래프 맥락", desc: "C에 더해, 지난 약속·사건 정리 결과를 주의 문구에 참고해요." },
];

export const SITUATION_CHIPS = ["부탁", "거절", "사과", "일정 조율", "보고"];
export const MAX_DRAFT_CHARS = 2000;
export const MAX_SITUATION_CHARS = 500;

export const ADJUSTS = [
  { value: "shorter", label: "더 짧게", key: "1" },
  { value: "more_formal", label: "더 공손하게", key: "2" },
  { value: "more_casual", label: "더 편하게", key: "3" },
];
const ADJUST_LABEL = { shorter: "더 짧게", longer: "더 길게", more_formal: "더 공손하게", more_casual: "더 편하게" };

export function adjustLabel(value) {
  return ADJUST_LABEL[value] || "";
}

export const REASON_CODES = [
  { value: "too_stiff", label: "너무 딱딱해요" },
  { value: "too_casual", label: "너무 가벼워요" },
  { value: "too_servile", label: "너무 저자세예요" },
  { value: "not_my_style", label: "평소 내 말투가 아니에요" },
  { value: "fact_changed", label: "사실이 바뀌었어요" },
  { value: "too_long", label: "너무 길어요" },
  { value: "too_short", label: "너무 짧아요" },
  { value: "other", label: "그 밖의 이유" },
];

export function reasonLabel(code) {
  return REASON_CODES.find((r) => r.value === code)?.label || "";
}

/** 상황 칩과 자유 입력을 서버의 situation 한 줄로 합친다(최대 500자). */
export function buildSituation(chip, free) {
  const parts = [chip, free].map((s) => String(s ?? "").trim()).filter(Boolean);
  return parts.join(" · ").slice(0, MAX_SITUATION_CHARS);
}

// ---------- 보존 항목 ----------

const FACT_KINDS = new Set(["datetime", "quantity", "person"]);

/**
 * 결과 문장에서 보존 항목이 글자 그대로 남은 구간을 찾는다 → [{text, mark}] 조각.
 * 긴 항목부터 잡고 겹치는 구간은 버린다('이번 주 금요일' 안의 '금요일'). 한 글자짜리 부정 표현('안')은
 * '안녕' 같은 낱말에 걸리므로 강조하지 않는다.
 */
export function markSegments(text, items) {
  const source = String(text ?? "");
  const ranges = [];
  const needles = [...new Set((items || []).filter((it) => it && it.kind !== "negation").map((it) => it.text))]
    .filter((t) => typeof t === "string" && t.trim().length >= 2)
    .sort((a, b) => b.length - a.length);
  for (const needle of needles) {
    let from = 0;
    for (;;) {
      const at = source.indexOf(needle, from);
      if (at < 0) break;
      const end = at + needle.length;
      if (!ranges.some((r) => at < r.end && end > r.start)) ranges.push({ start: at, end });
      from = end;
    }
  }
  ranges.sort((a, b) => a.start - b.start);
  const out = [];
  let pos = 0;
  for (const r of ranges) {
    if (r.start > pos) out.push({ text: source.slice(pos, r.start), mark: false });
    out.push({ text: source.slice(r.start, r.end), mark: true });
    pos = r.end;
  }
  if (pos < source.length || out.length === 0) out.push({ text: source.slice(pos), mark: false });
  return out;
}

/**
 * "지킨 항목" 칩. 추천(검증 통과) 결과에만 만든다 — 서버 규칙이 실제로 확인한 것만 ✓로 보여 준다.
 *  - 날짜·수량·사람: 초안의 표현 그대로(더 긴 항목에 든 것은 뺀다).
 *  - 가능/불가·약속: 서버는 방향·존재를 확인한다. 문장에 그대로 남은 표현이 있으면 그것을, 없으면 "불가 표현"처럼 하나로.
 *  - 조건: 서버가 따로 검증하지 않으므로 문장에 글자 그대로 남았을 때만.
 *  - 부정: "부정 표현" 하나.
 */
export function preservedChips(result) {
  if (!result || result.status !== "recommended") return [];
  const items = result.preserved_items || [];
  const text = String(result.result_text ?? "");
  const chips = [];
  const seen = new Set();
  const push = (key, label) => {
    if (seen.has(key)) return;
    seen.add(key);
    chips.push({ key, label });
  };

  const factTexts = [...new Set(items.filter((i) => FACT_KINDS.has(i.kind)).map((i) => i.text))];
  for (const t of factTexts) {
    if (factTexts.some((o) => o !== t && o.includes(t))) continue;
    push(`fact:${t}`, t);
  }
  const verbatim = (it) => Boolean(it.text) && text.includes(it.text);
  const group = (label, list, generic) => {
    if (!list.length) return;
    const hits = list.filter(verbatim);
    if (hits.length) for (const it of hits) push(`${label}:${it.text}`, `${it.text}(${label})`);
    else push(`${label}:any`, generic);
  };
  const possibility = items.filter((i) => i.kind === "possibility");
  group("불가", possibility.filter((i) => i.normalized === "불가"), "불가 표현");
  group("가능", possibility.filter((i) => i.normalized !== "불가"), "가능 표현");
  group("약속", items.filter((i) => i.kind === "promise"), "약속 표현");
  for (const it of items.filter((i) => i.kind === "condition")) {
    if (verbatim(it)) push(`조건:${it.text}`, `${it.text}(조건)`);
  }
  if (items.some((i) => i.kind === "negation")) push("negation", "부정 표현");
  return chips;
}

// ---------- 검증 위반(원문 유지) ----------

const KIND_KO = {
  datetime: "날짜·시간",
  quantity: "숫자·수량",
  person: "사람 이름",
  possibility: "가능·불가 표현",
  promise: "약속 표현",
  condition: "조건",
  negation: "부정 표현",
};

/** 서버 위반 문구의 영어 종류 이름(datetime 등)을 우리말로 바꾼다. 나머지 문장은 그대로 둔다. */
export function humanizeViolation(detail) {
  return String(detail ?? "")
    .replace(/\b(datetime|quantity|person|possibility|promise|condition|negation)\b/g, (m) => KIND_KO[m])
    .replace(/\s+이\(가\)/g, "이(가)")
    .trim();
}

const FACT_CHECKS = new Set(["preserve_missing", "fact_added", "negation_changed"]);

/**
 * 원문 유지 상태의 제목·설명·지키지 못한 항목.
 * history=true면 지난 기록을 보는 중이라 "입력란" 이야기를 하지 않는다.
 */
export function fallbackView(result, { history = false } = {}) {
  const violations = result?.validation?.violations || [];
  const checks = new Set(violations.map((v) => v.check));
  const items = [...new Set(violations.map((v) => humanizeViolation(v.detail)).filter(Boolean))];
  const kept = history ? "초안을 그대로 뒀어요." : "초안은 입력란에 그대로 있어요.";
  let headline = "추천 문장을 만들지 못했어요";
  let lead = kept;
  if ([...checks].some((c) => FACT_CHECKS.has(c))) {
    headline = "바꾼 문장이 초안의 사실을 지키지 못했어요";
    lead = `그래서 추천하지 않았어요. ${kept}`;
  } else if (checks.has("forbidden_phrase")) {
    headline = "바꾼 문장에 금지 표현이 들어갔어요";
    lead = `그래서 추천하지 않았어요. ${kept}`;
  } else if (checks.has("refusal")) {
    headline = "모델이 이 문장은 다듬지 않았어요";
  } else if (checks.has("empty")) {
    headline = history ? "그때는 추천 문장을 만들지 못했어요" : "지금은 추천 문장을 만들지 못했어요";
    lead = history ? kept : `${kept} 잠시 뒤 다시 만들어 보세요.`;
  }
  return { headline, lead, items };
}

// ---------- 결과 요약 ----------

/** 결과 카드 아래의 부가 정보. requested는 화면에서 고른 조건(자동이면 "auto"). */
export function resultMeta(rec, requested = null) {
  const r = rec?.result || {};
  const tokens = r.token_usage || {};
  return {
    condition: rec?.condition || "?",
    auto: requested === "auto",
    seconds: formatSeconds(r.latency_ms),
    attempts: Number(r.attempts) || 0,
    evidenceCount: (r.evidence_ids || []).length,
    adjust: r.adjust || null,
    adjustLabel: adjustLabel(r.adjust),
    channel: rec?.channel || null,
    model: r.model_version || "",
    prompt: r.prompt_version || "",
    retrieval: r.retrieval_version || "",
    tokensIn: tokens.input_tokens ?? null,
    tokensOut: tokens.output_tokens ?? null,
    attemptLog: (r.attempt_log || []).map((a) => ({
      attempt: a.attempt,
      passed: Boolean(a.passed),
      violations: (a.violations || []).map((v) => humanizeViolation(v.detail)),
    })),
  };
}

/** "조건 C(자동) · 4.4초 · 1회 시도" — 시연 대본이 가리키는 한 줄. */
export function metaLine(meta) {
  const cond = `조건 ${meta.condition}${meta.auto ? "(자동)" : ""}`;
  const parts = [cond, meta.seconds, `${meta.attempts}회 시도`];
  if (meta.adjustLabel) parts.push(meta.adjustLabel);
  return parts.join(" · ");
}

/**
 * 피드백 표시. feedback이 undefined면 서버가 아직 그 필드를 주지 않는 것(표시하지 않음),
 * null이면 피드백을 남기지 않은 것이다.
 */
export function feedbackView(feedback) {
  if (feedback === undefined) return { kind: "unknown", label: "", tone: "" };
  if (feedback === null) return { kind: "none", label: "피드백 없음", tone: "" };
  if (!feedback.accepted) {
    const reason = reasonLabel(feedback.reason_code);
    return { kind: "rejected", label: reason ? `거절 · ${reason}` : "거절", tone: "bad" };
  }
  if (feedback.edited_text) return { kind: "edited", label: "수정해서 적용", tone: "ok" };
  return { kind: "accepted", label: "적용", tone: "ok" };
}

export function statusView(result) {
  return result?.status === "recommended"
    ? { kind: "recommended", label: "추천", tone: "ok" }
    : { kind: "fallback", label: "원문 유지", tone: "warn" };
}

/** 더 보기로 받은 이력을 앞의 목록에 붙인다. 같은 id는 한 번만. 새로 붙은 수도 돌려준다. */
export function mergeHistory(existing, incoming) {
  const ids = new Set(existing.map((r) => r.id));
  const fresh = (incoming || []).filter((r) => r && !ids.has(r.id));
  return { rows: [...existing, ...fresh], added: fresh.length };
}

// ---------- 첨삭 오류 안내 ----------

/**
 * 첨삭 요청 실패 → 화면 안내. action은 "persona"(말투 설정으로) | "advanced"(조건 고르기) | "retry" | "login" | null.
 * 모든 경우에 초안은 입력란에 그대로 있다.
 */
export function rewriteErrorView(err, { contact = null, condition = "auto" } = {}) {
  const status = err?.status ?? 0;
  const code = err?.code || "";
  const kept = "초안은 입력란에 그대로 있어요.";
  if (status === 0) {
    return { kind: "network", title: "서버에 연결하지 못했어요", body: `${kept} 연결을 확인하고 다시 시도해 주세요.`, action: "retry" };
  }
  if (status === 401) {
    return { kind: "login", title: "로그인이 필요해요", body: `${kept} 다시 로그인하면 이어서 쓸 수 있어요.`, action: "login" };
  }
  if (status === 422) {
    if (hasFieldError(err, "condition") && condition === "auto") {
      return {
        kind: "auto_unavailable",
        title: "자동 선택은 지금 쓸 수 없어요",
        body: "'고급 · 실험 조건'에서 조건을 직접 골라 주세요. 초안은 그대로 있어요.",
        action: "advanced",
      };
    }
    if (contact && contact.persona_version == null && condition !== "A") {
      return {
        kind: "no_persona",
        title: `${contact.alias}에게 쓸 말투 설정이 아직 없어요`,
        body: "말투를 한 번 정해 두면 그 설정으로 다듬어요. 초안은 그대로 있어요.",
        action: "persona",
      };
    }
    return { kind: "invalid", title: "입력을 확인해 주세요", body: err.message, action: null };
  }
  if (status === 429) {
    if (code === "usage_exceeded") {
      return {
        kind: "usage",
        title: "오늘 쓸 수 있는 만큼 다 썼어요",
        body: `${err.message} 한도는 한국 시간 자정에 새로 시작돼요. ${kept}`,
        action: null,
      };
    }
    if (code === "budget_exceeded") {
      return {
        kind: "budget",
        title: "오늘은 서비스 전체 한도에 닿았어요",
        body: `내 사용량과 상관없이 서비스 전체가 오늘은 더 만들 수 없어요. 내일 다시 시도해 주세요. ${kept}`,
        action: null,
      };
    }
    const wait = err.retryAfter ? `${err.retryAfter}초 뒤에` : "잠시 뒤에";
    return { kind: "rate", title: "요청이 너무 잦아요", body: `${wait} 다시 시도해 주세요. ${kept}`, action: "retry" };
  }
  if (status === 503) {
    return { kind: "model", title: "지금은 문장을 만들 수 없어요", body: `${kept} 잠시 뒤 다시 시도해 주세요.`, action: "retry" };
  }
  if (status === 404) {
    return { kind: "missing", title: "이 상대를 찾을 수 없어요", body: "다른 곳에서 지웠을 수 있어요. 상대를 다시 골라 주세요.", action: null };
  }
  if (status === 413) {
    return { kind: "too_large", title: "보내는 내용이 너무 커요", body: "초안을 줄여서 다시 시도해 주세요.", action: null };
  }
  const tail = err?.requestId ? ` 문의할 때 이 번호를 알려 주세요: ${err.requestId}` : "";
  return { kind: "server", title: "문제가 생겨 만들지 못했어요", body: `${kept} 잠시 뒤 다시 시도해 주세요.${tail}`, action: "retry" };
}

// ---------- 상대 고르기 ----------

export const RECENT_CHIPS = 3;

/** 최근 고른 상대 id 목록의 맨 앞에 id를 넣는다(중복 제거, 최대 max개). */
export function pushRecent(ids, id, max = 12) {
  return [id, ...(ids || []).filter((x) => x !== id)].slice(0, max);
}

/**
 * 칩으로 보여 줄 상대 n명: 최근 고른 순서 → 모자라면 나머지 상대로 채운다.
 * 지금 고른 상대(selectedId)가 빠져 있으면 맨 앞에 넣는다.
 */
export function recentContacts(contacts, recentIds, selectedId = null, n = RECENT_CHIPS) {
  const byId = new Map((contacts || []).map((c) => [c.id, c]));
  const order = [];
  const add = (id) => {
    if (byId.has(id) && !order.includes(id)) order.push(id);
  };
  for (const id of recentIds || []) add(id);
  for (const c of [...(contacts || [])].reverse()) add(c.id); // 목록은 만든 순서 — 최근에 만든 상대부터 채운다
  let picked = order.slice(0, n);
  if (selectedId && byId.has(selectedId) && !picked.includes(selectedId)) {
    picked = [selectedId, ...picked].slice(0, n);
  }
  return picked.map((id) => byId.get(id));
}

/** 이름·관계로 상대를 거른다(공백·대소문자 무시). */
export function filterContacts(contacts, query) {
  const q = String(query ?? "").replace(/\s+/g, "").toLowerCase();
  if (!q) return [...(contacts || [])];
  return (contacts || []).filter((c) => {
    const hay = `${c.alias}${relLabel(c.relationship_type)}`.replace(/\s+/g, "").toLowerCase();
    return hay.includes(q);
  });
}

// ---------- 사용량 ----------

export function usageView(usage) {
  if (!usage) return null;
  const tokens = (usage.input_tokens || 0) + (usage.output_tokens || 0);
  return {
    rewrites: usage.rewrites ?? 0,
    rewriteLimit: usage.rewrite_limit ?? 0,
    remaining: usage.remaining_rewrites ?? Math.max((usage.rewrite_limit ?? 0) - (usage.rewrites ?? 0), 0),
    tokens,
    tokenLimit: usage.token_limit ?? 0,
    graphIngested: usage.graph_messages_ingested ?? null,
    graphCap: usage.graph_message_cap ?? null,
    exhausted: (usage.rewrite_limit ?? 0) > 0 && (usage.rewrites ?? 0) >= usage.rewrite_limit,
  };
}
