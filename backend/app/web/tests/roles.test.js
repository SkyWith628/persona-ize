import assert from "node:assert/strict";
import { test } from "node:test";

import {
  asksMeName,
  canFixBySpeaker,
  namedSpeakers,
  showsSpeakerNames,
  speakerRolesPlan,
  speakerRolesResultView,
  swapErrorText,
  swapNotes,
  swapPreview,
  swapRequest,
  swapResultView,
  unnamedMessages,
} from "../js/lib/roles.js";

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

// ---------- 화자별로 고치기 ----------

const SPEAKERS = [
  { name: "김민수", speaker_role: "me", messages: 120 },
  { name: "박지현", speaker_role: "contact", messages: 80 },
  { name: "최유진", speaker_role: "contact", messages: 30 },
];

test("이름이 있는 화자만 한 줄씩 — 역할이 섞인 이름은 다시 정하게 한다", () => {
  assert.deepEqual(namedSpeakers(SPEAKERS), [
    { name: "김민수", role: "me", messages: 120 },
    { name: "박지현", role: "contact", messages: 80 },
    { name: "최유진", role: "contact", messages: 30 },
  ]);
  const mixed = [...SPEAKERS, { name: "최유진", speaker_role: "me", messages: 2 }, { name: null, speaker_role: "contact", messages: 7 }];
  assert.deepEqual(namedSpeakers(mixed)[2], { name: "최유진", role: null, messages: 32 });
  assert.equal(unnamedMessages(mixed), 7);
  assert.deepEqual(namedSpeakers(null), []);
});

test("이름이 남은 화자가 있어야 화자별로 고칠 수 있다", () => {
  assert.equal(canFixBySpeaker(SPEAKERS), true);
  assert.equal(canFixBySpeaker([{ name: null, speaker_role: "me", messages: 3 }]), false);
  assert.equal(canFixBySpeaker([]), false);
  assert.equal(canFixBySpeaker(undefined), false);
});

test("말풍선의 화자 이름은 상대 쪽이 둘 이상일 때만", () => {
  assert.equal(showsSpeakerNames(SPEAKERS), true);
  assert.equal(showsSpeakerNames(SPEAKERS.slice(0, 2)), false);
  assert.equal(showsSpeakerNames([]), false);
});

test("요청에는 달라진 화자만, 맞는 상태로 적는다", () => {
  const same = { 김민수: "me", 박지현: "contact", 최유진: "contact" };
  assert.deepEqual(speakerRolesPlan(SPEAKERS, same), { body: null, removed: 0, removedNames: [], emptied: false });
  assert.equal(speakerRolesPlan(SPEAKERS, {}).body, null);
  assert.equal(speakerRolesPlan(SPEAKERS, null).body, null);

  const plan = speakerRolesPlan(SPEAKERS, { ...same, 최유진: "skip" });
  assert.deepEqual(plan, { body: { speakers: { 최유진: "skip" } }, removed: 30, removedNames: ["최유진"], emptied: false });
  // 같은 선택으로 다시 만들면 같은 요청이다
  assert.deepEqual(speakerRolesPlan(SPEAKERS, { ...same, 최유진: "skip" }), plan);

  const swap = speakerRolesPlan(SPEAKERS, { 김민수: "contact", 박지현: "me", 최유진: "nobody", 없는사람: "me" });
  assert.deepEqual(swap.body, { speakers: { 김민수: "contact", 박지현: "me" } });

  // 역할이 섞인 이름은 어느 쪽을 골라도 요청에 넣는다
  const mixed = [...SPEAKERS, { name: "최유진", speaker_role: "me", messages: 2 }];
  assert.deepEqual(speakerRolesPlan(mixed, same).body, { speakers: { 최유진: "contact" } });
});

test("전부 제외하면 보낼 수 없다 — 이름 없는 메시지가 남으면 된다", () => {
  const all = { 김민수: "skip", 박지현: "skip", 최유진: "skip" };
  assert.equal(speakerRolesPlan(SPEAKERS, all).emptied, true);
  assert.equal(speakerRolesPlan(SPEAKERS, all).removed, 230);
  const withUnnamed = [...SPEAKERS, { name: null, speaker_role: "me", messages: 1 }];
  assert.equal(speakerRolesPlan(withUnnamed, all).emptied, false);
});

test("화자별 결과 알림: 바꾼 것·지운 것·말투 자료·사건 정리", () => {
  const job = { status: "completed", graph_status: "skipped" };
  assert.deepEqual(speakerRolesResultView({ changed: true, messages: 0, messages_removed: 1200, my_messages: 40, job }), {
    text: "제외한 사람의 메시지 1,200개를 지웠어요 — 이제 내 메시지는 40개예요. 말투 자료도 다시 만들었어요.",
    kind: "ok",
  });
  const both = speakerRolesResultView({ changed: true, messages: 4, messages_removed: 2, my_messages: 2, job: { status: "processing", graph_status: "pending" } });
  assert.match(both.text, /^메시지 4개의 화자를 바꿨어요, 제외한 사람의 메시지 2개를 지웠어요 — /);
  assert.match(both.text, /말투 자료를 다시 만들고 있어요\. 지난 약속·사건은 바뀐 부분을 다시 정리하고 있어요\.$/);
  assert.deepEqual(speakerRolesResultView({ changed: false, messages: 0, messages_removed: 0, my_messages: 0, job: null }), {
    text: "이미 그렇게 되어 있어요. 바꾼 것은 없어요.",
    kind: "info",
  });
});
