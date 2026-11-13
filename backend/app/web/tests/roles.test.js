import assert from "node:assert/strict";
import { test } from "node:test";

import { asksMeName, swapErrorText, swapNotes, swapPreview, swapRequest, swapResultView } from "../js/lib/roles.js";

const MESSAGES = [
  { id: "a1", speaker_role: "me", text: "보고서 언제 돼요?" },
  { id: "a2", speaker_role: "contact", text: "수요일까지 드릴게요." },
  { id: "a3", speaker_role: "me", text: "화요일은 어려워요?" },
  { id: "a4", speaker_role: "contact", text: "네, 수요일로 부탁드려요." },
];

test("맞바꾸기 요청은 첫 메시지를 반대쪽의 말이라고 적는다", () => {
  assert.deepEqual(swapRequest(MESSAGES), { message_id: "a1", speaker_role: "contact" });
  assert.deepEqual(swapRequest(MESSAGES.slice(1)), { message_id: "a2", speaker_role: "me" });
  // 같은 목록으로 다시 만들면 같은 요청이다 — 두 번 보내도 서버가 도로 뒤집지 않는다
  assert.deepEqual(swapRequest(MESSAGES), swapRequest(MESSAGES));
});

test("내 이름은 다듬어 넣고, 비어 있으면 넣지 않는다", () => {
  assert.equal(swapRequest(MESSAGES, "  김민수 ").me_name, "김민수");
  assert.equal("me_name" in swapRequest(MESSAGES, "   "), false);
  assert.equal(swapRequest(MESSAGES, "가".repeat(80)).me_name.length, 50);
});

test("메시지가 없거나 역할을 모르면 요청을 만들지 않는다", () => {
  assert.equal(swapRequest([]), null);
  assert.equal(swapRequest(null), null);
  assert.equal(swapRequest([{ id: "x", speaker_role: "skip" }]), null);
  assert.deepEqual(swapRequest([{ id: "x", speaker_role: "skip" }, MESSAGES[1]]), { message_id: "a2", speaker_role: "me" });
});

test("미리보기는 바꾼 뒤 누가 한 말이 되는지 보여 준다", () => {
  const view = swapPreview(MESSAGES, "박지현", 2);
  assert.equal(view.mineNow, 2);
  assert.equal(view.mineAfter, 2);
  assert.deepEqual(view.sample, [
    { now: "나", after: "박지현", text: "화요일은 어려워요?" },
    { now: "박지현", after: "나", text: "네, 수요일로 부탁드려요." },
  ]);
  const lopsided = swapPreview([MESSAGES[0], MESSAGES[2], MESSAGES[1]], "");
  assert.deepEqual([lopsided.mineNow, lopsided.mineAfter], [2, 1]);
  assert.equal(lopsided.sample[2].now, "상대");
  assert.deepEqual(swapPreview(null, "박지현"), { mineNow: 0, mineAfter: 0, sample: [] });
});

test("그래프가 켜진 서버에서만 다시 정리하느라 시간이 걸린다고 알린다", () => {
  const off = swapNotes({ graph_enabled: false, graph_ingested: 0 });
  assert.equal(off.length, 2);
  assert.ok(off.every((line) => !line.includes("사건")));
  assert.equal(swapNotes(null).length, 2);

  const pending = swapNotes({ graph_enabled: true, graph_ingested: 0 });
  assert.equal(pending.length, 3);
  assert.match(pending[2], /지난 약속·사건 정리를 다시 해야 해서 시간이 걸려요\.$/);

  const done = swapNotes({ graph_enabled: true, graph_ingested: 1234 });
  assert.match(done[2], /시간이 걸려요/);
  assert.match(done[2], /1,234개는 지우고/);

  assert.equal(asksMeName({ graph_enabled: true }), true);
  assert.equal(asksMeName({ graph_enabled: false }), false);
  assert.equal(asksMeName(null), false);
});

test("결과 알림: 바뀐 것, 말투 자료, 사건 정리", () => {
  const job = { status: "completed", graph_status: "skipped" };
  assert.deepEqual(swapResultView({ changed: true, my_messages: 1200, job }), {
    text: "나와 상대를 맞바꿨어요 — 이제 내 메시지는 1,200개예요. 말투 자료도 다시 만들었어요.",
    kind: "ok",
  });
  const slow = swapResultView({ changed: true, my_messages: 2, job: { status: "processing", graph_status: "pending" } });
  assert.match(slow.text, /말투 자료를 다시 만들고 있어요\./);
  assert.match(slow.text, /지난 약속·사건은 다시 정리하고 있어요\. 시간이 걸려요\.$/);
  assert.deepEqual(swapResultView({ changed: false, my_messages: 0, job: null }), {
    text: "이미 바뀌어 있어요. 바꾼 것은 없어요.",
    kind: "info",
  });
});

test("오류 문구: 정리 중(409)·그래프 장애(503)·사라진 대화방(404)만 따로 알린다", () => {
  assert.match(swapErrorText({ status: 409, code: "conflict" }), /바로 멈추지 않아/);
  assert.match(swapErrorText({ status: 503, code: "graph_unavailable" }), /바꾸지 않았어요/);
  assert.match(swapErrorText({ status: 404, code: "not_found" }), /새로 고친 뒤/);
  assert.equal(swapErrorText({ status: 503, code: "unavailable" }), null);
  assert.equal(swapErrorText({ status: 500, code: "internal_error" }), null);
  assert.equal(swapErrorText(null), null);
});
