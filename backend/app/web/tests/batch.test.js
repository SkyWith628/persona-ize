import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_BATCH_CONTACTS,
  addExtraContact,
  batchItemErrorView,
  batchItemView,
  batchRequestErrorView,
  batchSummary,
  batchTargets,
  batchView,
  blocksSubmitInAddPicker,
  extraCandidates,
} from "../js/lib/batch.js";
import { readFileSync } from "node:fs";
import { ApiError, normalizeError } from "../js/lib/errors.js";

// 묶음 첨삭 계약(POST /v1/rewrites/batch)의 응답 모양으로 만든 예시:
//   {"items": [{"contact_id", "rewrite": RewriteOut|null, "error": {"code","message"}|null}]}
const BOSS = { id: "a", alias: "지현 팀장", relationship_type: "superior", persona_version: 3 };
const FRIEND = { id: "b", alias: "최유진", relationship_type: "friend", persona_version: null };
const MOM = { id: "c", alias: "엄마", relationship_type: "family", persona_version: 1 };
const CONTACTS = [BOSS, FRIEND, MOM];

const rewrite = (contactId, text, status = "recommended", violations = []) => ({
  id: `rw-${contactId}`,
  contact_id: contactId,
  condition: "B",
  channel: "web",
  result: {
    status,
    result_text: text,
    change_reasons: [],
    warnings: [],
    evidence_ids: [],
    preserved_items: [],
    validation: { passed: status === "recommended", violations },
    attempts: 1,
  },
});
const ok = (contactId, text) => ({ contact_id: contactId, rewrite: rewrite(contactId, text), error: null });
const kept = (contactId) => ({
  contact_id: contactId,
  rewrite: rewrite(contactId, "금요일은 어려움", "fallback_original", [
    { check: "preserve_missing", detail: "datetime '금요일' 이(가) 결과에 없습니다." },
  ]),
  error: null,
});
const failed = (contactId, code, message = "실패했습니다.") => ({ contact_id: contactId, rewrite: null, error: { code, message } });

// ---------- 함께 보낼 상대 찾기 칸의 Enter ----------

test("찾기 칸의 Enter는 폼 제출을 막는다 — 조합 중인 Enter와 다른 키는 그대로 둔다", () => {
  assert.equal(blocksSubmitInAddPicker({ key: "Enter", isComposing: false, keyCode: 13 }), true);
  assert.equal(blocksSubmitInAddPicker({ key: "Enter" }), true);
  assert.equal(blocksSubmitInAddPicker({ key: "Enter", isComposing: true, keyCode: 229 }), false);
  assert.equal(blocksSubmitInAddPicker({ key: "Enter", isComposing: false, keyCode: 229 }), false);
  assert.equal(blocksSubmitInAddPicker({ key: "a", keyCode: 65 }), false);
  assert.equal(blocksSubmitInAddPicker({ key: "Escape" }), false);
  assert.equal(blocksSubmitInAddPicker(null), false);
});

test("첨삭 화면은 함께 보낼 상대 찾기 칸에 그 Enter 막기를 달아 둔다", () => {
  // 맞는 상대가 없는 이름을 치고 Enter를 누르면 폼이 제출돼 첨삭 요청이 나가던 문제 — 리스너가 빠지면 되살아난다.
  const src = readFileSync(new URL("../js/views/compose.js", import.meta.url), "utf8");
  assert.match(src, /"data-add-picker": true, onKeydown: keepEnterInPicker/);
  assert.match(src, /if \(blocksSubmitInAddPicker\(e\)\) e\.preventDefault\(\);/);
});

// ---------- 보낼 상대 ----------

test("보낼 상대: 먼저 고른 상대가 앞, 중복과 없는 상대는 뺀다", () => {
  assert.deepEqual(batchTargets("a", ["b", "a", "x", "c", "b"], CONTACTS), ["a", "b", "c"]);
  assert.deepEqual(batchTargets("a", [], CONTACTS), ["a"], "한 명이면 단건 흐름");
  assert.deepEqual(batchTargets(null, ["b"], CONTACTS), ["b"]);
  assert.deepEqual(batchTargets("x", null, CONTACTS), []);
});

test("보낼 상대는 최대 5명에서 자른다(서버는 5명 초과를 422로 돌려보낸다)", () => {
  const many = ["a", "b", "c", "d", "e", "f", "g"].map((id) => ({ id, alias: id }));
  assert.equal(MAX_BATCH_CONTACTS, 5);
  assert.deepEqual(batchTargets("a", ["b", "c", "d", "e", "f", "g"], many), ["a", "b", "c", "d", "e"]);
});

test("상대 더하기: 이미 고른 상대는 넣지 않고, 먼저 고른 상대까지 5명이 차면 막는다", () => {
  assert.deepEqual(addExtraContact([], "b", "a"), { ids: ["b"], added: true, reason: null });
  assert.deepEqual(addExtraContact(["b"], "c", "a"), { ids: ["b", "c"], added: true, reason: null });
  assert.deepEqual(addExtraContact([], "a", "a"), { ids: [], added: false, reason: "duplicate" });
  assert.deepEqual(addExtraContact(["b"], "b", "a"), { ids: ["b"], added: false, reason: "duplicate" });
  assert.equal(addExtraContact(["b", "c", "d"], "e", "a").ids.length, 4, "먼저 고른 상대 + 4명 = 5명까지");
  assert.deepEqual(addExtraContact(["b", "c", "d", "e"], "f", "a"), {
    ids: ["b", "c", "d", "e"],
    added: false,
    reason: "full",
  });
});

test("상대 더하기는 넘겨받은 목록을 바꾸지 않는다", () => {
  const before = ["b"];
  const next = addExtraContact(before, "c", "a");
  assert.deepEqual(before, ["b"]);
  assert.notEqual(next.ids, before);
});

test("더 고를 수 있는 상대는 이미 고른 상대를 뺀 나머지", () => {
  assert.deepEqual(extraCandidates(CONTACTS, "a", ["b"]), [MOM]);
  assert.deepEqual(extraCandidates(CONTACTS, "a", []), [FRIEND, MOM]);
  assert.deepEqual(extraCandidates(CONTACTS, "a", ["b", "c"]), []);
  assert.deepEqual(extraCandidates(null, "a", null), []);
});

// ---------- 항목 하나 ----------

test("추천 항목: 문장을 복사할 수 있다", () => {
  const view = batchItemView(ok("a", "팀장님, 금요일은 어렵습니다."), { contact: BOSS });
  assert.equal(view.kind, "recommended");
  assert.equal(view.contactId, "a");
  assert.equal(view.alias, "지현 팀장");
  assert.equal(view.text, "팀장님, 금요일은 어렵습니다.");
  assert.equal(view.copyable, true);
  assert.equal(view.rec.id, "rw-a", "복사 기록(feedback)을 남길 첨삭 id");
  assert.equal(view.error, null);
  assert.equal(view.fallback, null);
});

test("추천인데 문장이 비어 있으면 복사 버튼을 두지 않는다", () => {
  assert.equal(batchItemView(ok("a", "  \n"), { contact: BOSS }).copyable, false);
});

test("원문 유지 항목: 복사할 수 없고, 단건과 같은 안내를 쓴다", () => {
  const view = batchItemView(kept("b"), { contact: FRIEND });
  assert.equal(view.kind, "fallback");
  assert.equal(view.copyable, false);
  assert.equal(view.text, "");
  assert.equal(view.fallback.headline, "바꾼 문장이 초안의 사실을 지키지 못했어요");
  assert.deepEqual(view.fallback.items, ["날짜·시간 '금요일'이(가) 결과에 없습니다."]);
});

test("실패 항목: 그 상대의 오류만 담는다", () => {
  const view = batchItemView(failed("c", "model_unavailable"), { contact: MOM });
  assert.equal(view.kind, "error");
  assert.equal(view.rec, null);
  assert.equal(view.copyable, false);
  assert.equal(view.error.kind, "model");
  assert.equal(view.error.action, "retry");
});

test("계약 밖의 항목(둘 다 비었거나 둘 다 찼다)은 실패로 보여 준다 — 빈 카드를 두지 않는다", () => {
  const empty = batchItemView({ contact_id: "a", rewrite: null, error: null }, { contact: BOSS });
  assert.equal(empty.kind, "error");
  assert.equal(empty.error.kind, "server");
  const both = batchItemView({ ...ok("a", "문장"), error: { code: "not_found", message: "없음" } }, { contact: BOSS });
  assert.equal(both.kind, "error");
  assert.equal(both.error.kind, "missing");
});

test("목록에 없는 상대의 항목도 이름 자리를 비우지 않는다", () => {
  const view = batchItemView(ok("z", "문장"), {});
  assert.equal(view.alias, "상대");
  assert.equal(view.contactId, "z");
  assert.equal(view.contact, null);
});

// ---------- 항목의 오류 code → 안내 ----------

test("항목 오류: 단건 오류와 같은 code를 같은 안내로 바꾼다", () => {
  const kind = (code, opts) => batchItemErrorView({ code, message: "서버 문장" }, opts).kind;
  assert.equal(kind("not_found"), "missing");
  assert.equal(kind("model_unavailable"), "model");
  assert.equal(kind("budget_exceeded"), "budget");
  assert.equal(kind("usage_exceeded"), "usage");
  assert.equal(kind("quota_exceeded"), "quota");
  assert.equal(kind("rate_limited"), "rate");
  assert.equal(kind("internal_error"), "server");
  assert.equal(kind("처음 보는 코드"), "server");
  assert.equal(batchItemErrorView(null).kind, "server");
});

test("항목 오류: 오늘 첨삭 한도를 다 썼으면 한도를 숫자로 알려 준다", () => {
  const view = batchItemErrorView(
    { code: "usage_exceeded", message: "오늘 한도를 넘었습니다." },
    { usage: { rewrites: 30, rewrite_limit: 30 } },
  );
  assert.equal(view.kind, "usage");
  assert.match(view.body, /30회/);
});

test("항목 오류: 조건을 직접 골랐는데 그 상대만 말투 설정이 없으면 설정하러 가게 한다", () => {
  const error = { code: "invalid_input", message: "저장된 페르소나가 없습니다." };
  const picked = batchItemErrorView(error, { contact: FRIEND, condition: "B" });
  assert.equal(picked.kind, "no_persona");
  assert.equal(picked.action, "persona");
  assert.match(picked.title, /최유진/);
  // 말투 설정이 있는 상대의 422는 서버 문장을 그대로 보여 준다
  const other = batchItemErrorView(error, { contact: BOSS, condition: "B" });
  assert.equal(other.kind, "invalid");
  assert.equal(other.body, "저장된 페르소나가 없습니다.");
});

// ---------- 요청 전체가 실패했을 때 ----------

test("서버에 묶음 경로가 없으면 '상대를 찾을 수 없다'가 아니라 기능이 없다고 알린다", () => {
  const noRoute = new ApiError(normalizeError(404, { detail: "Not Found" }));
  assert.equal(batchRequestErrorView(noRoute).kind, "batch_unavailable");
  const wrongMethod = new ApiError(normalizeError(405, null));
  assert.equal(batchRequestErrorView(wrongMethod).kind, "batch_unavailable");
  assert.equal(batchRequestErrorView(wrongMethod).action, null);
});

test("요청 전체의 다른 실패는 단건과 같은 안내를 쓴다", () => {
  const invalid = new ApiError(
    normalizeError(422, {
      code: "invalid_input",
      message: "상대는 5명까지 고를 수 있습니다.",
      details: [{ loc: "body.contact_ids", msg: "상대는 5명까지 고를 수 있습니다." }],
    }),
  );
  const view = batchRequestErrorView(invalid, { condition: "B" });
  assert.equal(view.kind, "invalid", "요청 전체의 422는 한 상대의 말투 설정 문제로 보지 않는다");
  assert.equal(view.body, "상대는 5명까지 고를 수 있습니다.");
  assert.equal(batchRequestErrorView(new ApiError(normalizeError(0, null))).kind, "network");
  assert.equal(batchRequestErrorView(new ApiError(normalizeError(429, null, "12"))).kind, "rate");
});

// ---------- 응답 전체 ----------

test("응답 → 카드: 보낸 순서대로, 상대마다 하나씩", () => {
  const response = { items: [ok("a", "팀장님께"), kept("b"), failed("c", "not_found")] };
  const view = batchView(response, { requestedIds: ["a", "b", "c"], contacts: CONTACTS });
  assert.deepEqual(
    view.items.map((it) => [it.contactId, it.alias, it.kind]),
    [
      ["a", "지현 팀장", "recommended"],
      ["b", "최유진", "fallback"],
      ["c", "엄마", "error"],
    ],
  );
  assert.deepEqual(view.counts, { total: 3, recommended: 1, fallback: 1, error: 1 });
  assert.equal(view.summary, "3명 중 추천 1명, 원문 유지 1명, 실패 1명이에요.");
});

test("한 상대만 실패해도 나머지 카드는 정상이다", () => {
  const response = { items: [ok("a", "팀장님께"), failed("b", "usage_exceeded"), ok("c", "엄마에게")] };
  const view = batchView(response, { requestedIds: ["a", "b", "c"], contacts: CONTACTS });
  assert.deepEqual(view.items.map((it) => it.copyable), [true, false, true]);
  assert.equal(view.items[1].error.kind, "usage");
  assert.equal(view.items[2].text, "엄마에게");
  assert.equal(view.summary, "3명 중 추천 2명, 실패 1명이에요.");
});

test("응답 순서가 어긋나도 contact_id로 맞춘다 — 다른 상대의 문장이 엉뚱한 이름 아래 가지 않는다", () => {
  const response = { items: [ok("c", "엄마에게"), ok("a", "팀장님께")] };
  const view = batchView(response, { requestedIds: ["a", "c"], contacts: CONTACTS });
  assert.deepEqual(view.items.map((it) => [it.alias, it.text]), [
    ["지현 팀장", "팀장님께"],
    ["엄마", "엄마에게"],
  ]);
});

test("응답에 빠진 상대는 실패 카드로, 보내지 않은 상대의 항목은 버린다", () => {
  const response = { items: [ok("a", "팀장님께"), ok("z", "모르는 상대"), ok("a", "두 번째는 버린다")] };
  const view = batchView(response, { requestedIds: ["a", "b"], contacts: CONTACTS });
  assert.equal(view.items.length, 2);
  assert.equal(view.items[0].text, "팀장님께");
  assert.equal(view.items[1].kind, "error");
  assert.equal(view.items[1].alias, "최유진");
  assert.equal(view.items[1].error.kind, "server");
});

test("응답이 비었거나 모양이 틀려도 보낸 상대만큼 실패 카드를 만든다", () => {
  for (const response of [null, {}, { items: "x" }, { items: [null] }]) {
    const view = batchView(response, { requestedIds: ["a", "b"], contacts: CONTACTS });
    assert.deepEqual(view.items.map((it) => it.kind), ["error", "error"]);
    assert.equal(view.summary, "2명 중 실패 2명이에요.");
  }
});

test("조건과 사용량은 항목의 오류 안내까지 전해진다", () => {
  const response = { items: [ok("a", "팀장님께"), failed("b", "invalid_input", "저장된 페르소나가 없습니다.")] };
  const view = batchView(response, { requestedIds: ["a", "b"], contacts: CONTACTS, condition: "C" });
  assert.equal(view.items[1].error.kind, "no_persona");
});

test("요약 문장: 모두 됐을 때와 섞였을 때", () => {
  assert.equal(batchSummary({ total: 3, recommended: 3, fallback: 0, error: 0 }), "3명 모두 추천 문장이 준비됐어요.");
  assert.equal(batchSummary({ total: 2, recommended: 0, fallback: 2, error: 0 }), "2명 중 원문 유지 2명이에요.");
  assert.equal(batchSummary({ total: 2, recommended: 1, fallback: 0, error: 1 }), "2명 중 추천 1명, 실패 1명이에요.");
});
