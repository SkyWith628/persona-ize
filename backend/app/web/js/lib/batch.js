// "여러 상대에게 한 번에" — POST /v1/rewrites/batch의 요청 대상과 응답을 화면에 그릴 모양으로 바꾼다.
// DOM을 만지지 않는 순수 함수만 둔다(테스트: tests/batch.test.js).
//
// 서버는 상대마다 단건 첨삭을 그대로 한 번씩 부른다. 그래서 항목의 rewrite는 단건 응답과 같은 모양이고,
// 항목의 error.code는 단건 오류의 code와 같은 낱말이다(HTTP 상태만 없다 — 응답 전체는 200이다).

import { isUnavailable } from "./errors.js";
import { fallbackView, rewriteErrorView } from "./model.js";

/** 한 번에 보낼 수 있는 상대 수(서버 계약: contact_ids 1~5개). */
export const MAX_BATCH_CONTACTS = 5;

/**
 * "함께 보낼 상대 찾기" 칸에서 폼 제출을 막아야 하는 Enter인가(조합 중이 아닌 Enter).
 * 이 찾기 칸은 "새 상대 추가" 항목이 없어서, 맞는 상대가 없으면 Enter를 목록이 받지 않는다. 칸이 첨삭 폼 안에
 * 있으므로 그대로 두면 브라우저가 폼을 제출해 지금 고른 상대들에게 요청이 나간다(사용량을 쓴다).
 * 한글 조합을 끝내는 Enter(isComposing, keyCode 229)는 조합에 맡긴다 — 그 Enter는 폼을 제출하지 않는다.
 */
export function blocksSubmitInAddPicker(event) {
  return Boolean(event) && event.key === "Enter" && !event.isComposing && event.keyCode !== 229;
}

/**
 * 이번 요청의 상대 id 목록: 먼저 고른 상대 → 더한 상대 순서.
 * 중복과 목록에 없는(다른 곳에서 지운) 상대는 빼고 max명에서 자른다 — 서버가 중복·5명 초과를 422로 돌려보내기 때문이다.
 * 결과가 1개면 화면은 예전 그대로 단건 API를 쓴다.
 */
export function batchTargets(primaryId, extraIds, contacts, max = MAX_BATCH_CONTACTS) {
  const known = new Set((contacts || []).map((c) => c.id));
  const out = [];
  for (const id of [primaryId, ...(extraIds || [])]) {
    if (id && known.has(id) && !out.includes(id)) out.push(id);
  }
  return out.slice(0, max);
}

/**
 * 더한 상대 목록에 id를 넣는다. 바꾸지 못했으면 added=false와 까닭을 돌려준다.
 * reason: "duplicate"(이미 고른 상대) | "full"(먼저 고른 상대까지 합쳐 max명이 찼다) | null.
 */
export function addExtraContact(extraIds, id, primaryId, max = MAX_BATCH_CONTACTS) {
  const ids = [...(extraIds || [])];
  if (!id || id === primaryId || ids.includes(id)) return { ids, added: false, reason: "duplicate" };
  if (ids.length + 1 >= max) return { ids, added: false, reason: "full" };
  return { ids: [...ids, id], added: true, reason: null };
}

/** 더 고를 수 있는 상대: 이미 고른 상대(먼저 고른 상대·더한 상대)를 뺀 나머지. */
export function extraCandidates(contacts, primaryId, extraIds) {
  const taken = new Set([primaryId, ...(extraIds || [])]);
  return (contacts || []).filter((c) => !taken.has(c.id));
}

// 묶음 항목의 오류에는 HTTP 상태가 없다. 단건 오류 안내(rewriteErrorView)를 그대로 쓰려고
// 단건 경로에서 그 code가 나갈 때의 상태로 되돌린다. 모르는 code는 서버 문제(500)로 본다.
const STATUS_BY_CODE = {
  not_found: 404,
  invalid_input: 422,
  quota_exceeded: 422,
  usage_exceeded: 429,
  budget_exceeded: 429,
  rate_limited: 429,
  too_many_attempts: 429,
  model_unavailable: 503,
  service_unavailable: 503,
};

/** 묶음 항목의 error({code, message}) → 그 상대 카드에 보여 줄 안내(rewriteErrorView와 같은 모양). */
export function batchItemErrorView(error, { contact = null, condition = "auto", usage = null } = {}) {
  const code = String(error?.code || "");
  const err = {
    status: STATUS_BY_CODE[code] ?? 500,
    code,
    message: String(error?.message || "잠시 뒤 다시 시도해 주세요."),
    details: [],
  };
  return rewriteErrorView(err, { contact, condition, usage });
}

/**
 * 묶음 요청 자체가 실패했을 때(응답이 200이 아닐 때)의 안내.
 * 서버가 옛 버전이라 묶음 경로가 없으면 "이 상대를 찾을 수 없어요"(404의 단건 뜻)가 아니라 기능이 없다고 알린다.
 * 요청 전체의 422는 본문 문제(목록·초안)이지 한 상대의 말투 설정 문제가 아니므로 contact를 넘기지 않는다.
 */
export function batchRequestErrorView(err, { condition = "auto", usage = null } = {}) {
  if (isUnavailable(err)) {
    return {
      kind: "batch_unavailable",
      title: "여러 상대에게 한 번에 맞추기는 지금 쓸 수 없어요",
      body: "상대를 한 명만 남기면 예전처럼 쓸 수 있어요. 초안은 입력란에 그대로 있어요.",
      action: null,
    };
  }
  return rewriteErrorView(err, { contact: null, condition, usage });
}

/**
 * 묶음 응답의 항목 하나 → 카드 하나.
 * kind: "recommended"(추천 문장, 복사할 수 있다) | "fallback"(원문 유지) | "error"(이 상대만 실패).
 * 화면은 kind별로 한 가지만 그린다: text(추천 문장) / fallback(원문 유지 안내) / error(오류 안내).
 */
export function batchItemView(item, { contact = null, condition = "auto", usage = null } = {}) {
  const contactId = item?.contact_id ?? contact?.id ?? null;
  const base = {
    contactId,
    alias: contact?.alias || "상대",
    contact,
    rec: null,
    text: "",
    copyable: false,
    fallback: null,
    error: null,
  };
  const rec = item?.rewrite;
  if (rec && rec.result && !item.error) {
    if (rec.result.status === "recommended") {
      const text = String(rec.result.result_text ?? "");
      return { ...base, kind: "recommended", rec, text, copyable: Boolean(text.trim()) };
    }
    return { ...base, kind: "fallback", rec, fallback: fallbackView(rec.result) };
  }
  // rewrite도 error도 없는 항목(계약 밖)은 서버 문제로 보여 준다 — 빈 카드를 두지 않는다
  return { ...base, kind: "error", error: batchItemErrorView(item?.error || { code: "internal_error" }, { contact, condition, usage }) };
}

/** "3명 모두 추천 문장이 준비됐어요." / "3명 중 추천 1명, 원문 유지 1명, 실패 1명이에요." — 화면 요약과 낭독 알림이 함께 쓴다. */
export function batchSummary(counts) {
  const { total, recommended, fallback, error } = counts;
  if (total > 0 && recommended === total) return `${total}명 모두 추천 문장이 준비됐어요.`;
  const parts = [];
  if (recommended) parts.push(`추천 ${recommended}명`);
  if (fallback) parts.push(`원문 유지 ${fallback}명`);
  if (error) parts.push(`실패 ${error}명`);
  return `${total}명 중 ${parts.join(", ")}이에요.`;
}

/**
 * POST /v1/rewrites/batch 응답 → { items, counts, summary }.
 * 카드는 언제나 보낸 순서(requestedIds)대로, 보낸 상대마다 하나씩 만든다. 응답 항목은 contact_id로 찾는다 —
 * 순서가 어긋나거나 항목이 빠져도 다른 상대의 문장이 엉뚱한 이름 아래 놓이지 않게 하기 위해서다.
 * 응답에 빠진 상대는 실패 카드로, 보내지 않은 상대의 항목은 버린다.
 */
export function batchView(response, { requestedIds = [], contacts = [], condition = "auto", usage = null } = {}) {
  const byContact = new Map();
  for (const item of Array.isArray(response?.items) ? response.items : []) {
    if (item && item.contact_id && !byContact.has(item.contact_id)) byContact.set(item.contact_id, item);
  }
  const contactById = new Map((contacts || []).map((c) => [c.id, c]));
  const items = requestedIds.map((contactId) =>
    batchItemView(byContact.get(contactId) || { contact_id: contactId, rewrite: null, error: null }, {
      contact: contactById.get(contactId) || null,
      condition,
      usage,
    }),
  );
  const counts = { total: items.length, recommended: 0, fallback: 0, error: 0 };
  for (const it of items) counts[it.kind] += 1;
  return { items, counts, summary: batchSummary(counts) };
}
