import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_SETTINGS,
  SCALES,
  addPhrases,
  applySignals,
  cleanPhrases,
  describeSettings,
  normalizeSettings,
  settingsEqual,
  signalMatches,
  signalView,
  suggestionState,
  stepLabel,
} from "../js/lib/persona.js";

// 계약 §4.3: GET /v1/contacts/{id}/persona/suggestion
const SUGGESTION = {
  based_on_messages: 42,
  settings: { formality: 4, friendliness: 2, directness: 3, length: "same", emoji: "none", address_term: "팀장님", preferred_phrases: [], forbidden_phrases: [], notes: null },
  signals: [
    { field: "formality", value: 4, reason: "내 메시지 42개 중 31개가 '-습니다'체" },
    { field: "emoji", value: "none", reason: "이모지·ㅋㅋ가 한 번도 없음" },
    { field: "address_term", value: "팀장님", reason: "문장 첫머리 호칭 12회" },
  ],
};

test("척도는 세 가지이고 단계마다 말로 된 설명이 있다", () => {
  assert.deepEqual(SCALES.map((s) => s.field), ["formality", "friendliness", "directness"]);
  for (const scale of SCALES) {
    assert.equal(scale.steps.length, 5);
    for (const step of scale.steps) assert.ok(step.short && step.desc);
  }
  assert.equal(stepLabel("formality", 5), "아주 격식 있게");
  assert.equal(stepLabel("directness", 1), "많이 돌려서");
});

test("빈 입력은 서버 기본값과 같다", () => {
  assert.deepEqual(normalizeSettings(null), { ...DEFAULT_SETTINGS });
  assert.deepEqual(normalizeSettings({}), { ...DEFAULT_SETTINGS });
});

test("범위 밖 값·빈 글자·중복 표현을 바로잡는다", () => {
  const s = normalizeSettings({
    formality: 9,
    friendliness: "2",
    directness: 0,
    length: "huge",
    emoji: "often",
    address_term: "  팀장님 ",
    notes: "   ",
    preferred_phrases: [" 죄송하지만 ", "", "죄송하지만", "부탁드립니다"],
    forbidden_phrases: "ㅋㅋ",
  });
  assert.equal(s.formality, 3);
  assert.equal(s.friendliness, 2);
  assert.equal(s.directness, 3);
  assert.equal(s.length, "same");
  assert.equal(s.emoji, "often");
  assert.equal(s.address_term, "팀장님");
  assert.equal(s.notes, null);
  assert.deepEqual(s.preferred_phrases, ["죄송하지만", "부탁드립니다"]);
  assert.deepEqual(s.forbidden_phrases, []);
});

test("표현 개수 한도를 지킨다", () => {
  const many = Array.from({ length: 30 }, (_, i) => `표현${i}`);
  assert.equal(cleanPhrases(many, 20).length, 20);
  assert.equal(normalizeSettings({ preferred_phrases: many }).preferred_phrases.length, 20);
});

test("설정 비교는 빈 값의 표기 차이를 무시한다", () => {
  assert.ok(settingsEqual({ ...DEFAULT_SETTINGS, address_term: "" }, DEFAULT_SETTINGS));
  assert.ok(!settingsEqual({ ...DEFAULT_SETTINGS, formality: 4 }, DEFAULT_SETTINGS));
  assert.ok(!settingsEqual({ ...DEFAULT_SETTINGS, forbidden_phrases: ["ㅋㅋ"] }, DEFAULT_SETTINGS));
});

test("한 줄 요약", () => {
  const line = describeSettings({ formality: 5, friendliness: 2, directness: 3, address_term: "지현 팀장님", forbidden_phrases: ["ㅋㅋ", "ㅠ"] });
  assert.equal(line, "격식 아주 격식 있게 · 친근함 조금 거리 있게 · 직접성 보통 · 길이 초안만큼 · 이모지 안 써요 · 호칭 지현 팀장님 · 금지 2개");
  // 메모만 다른 버전도 구분된다
  assert.match(describeSettings({ notes: "업무 얘기는 결론부터" }), / · 메모 있음$/);
  assert.doesNotMatch(describeSettings({ notes: "  " }), /메모/);
});

test("제안 신호를 화면용으로 바꾼다", () => {
  const v = signalView(SUGGESTION.signals[0]);
  assert.equal(v.fieldLabel, "격식");
  assert.equal(v.valueLabel, "4단계 · 정중하게");
  assert.equal(v.reason, "내 메시지 42개 중 31개가 '-습니다'체");
  assert.ok(v.usable);
  assert.equal(signalView(SUGGESTION.signals[1]).valueLabel, "안 써요");
  assert.equal(signalView(SUGGESTION.signals[2]).valueLabel, "팀장님");
});

test("모르는 항목이나 범위 밖 값은 적용하지 않는다", () => {
  assert.equal(signalView({ field: "personality", value: "내향적", reason: "x" }).usable, false);
  assert.equal(signalView({ field: "formality", value: 7, reason: "x" }).usable, false);
  assert.equal(signalView({ field: "emoji", value: "always", reason: "x" }).usable, false);
  assert.equal(signalView({ field: "notes", value: "메모를 바꿔라", reason: "x" }).usable, false);
  const { settings, applied } = applySignals(DEFAULT_SETTINGS, [{ field: "formality", value: 7 }, { field: "x", value: 1 }]);
  assert.deepEqual(settings, { ...DEFAULT_SETTINGS });
  assert.deepEqual(applied, []);
});

test("제안 적용은 신호가 가리키는 항목만 바꾸고 나머지는 그대로 둔다", () => {
  const mine = { ...DEFAULT_SETTINGS, friendliness: 5, notes: "결론부터", forbidden_phrases: ["ㅋㅋ"] };
  const { settings, applied } = applySignals(mine, SUGGESTION.signals);
  assert.equal(settings.formality, 4);
  assert.equal(settings.address_term, "팀장님");
  assert.equal(settings.friendliness, 5, "신호에 없는 항목은 건드리지 않는다");
  assert.equal(settings.notes, "결론부터");
  assert.deepEqual(settings.forbidden_phrases, ["ㅋㅋ"]);
  assert.deepEqual(applied, ["formality", "emoji", "address_term"]);
  assert.equal(mine.formality, 3, "원래 객체는 바꾸지 않는다");
});

test("표현 목록 제안은 기존 목록에 더한다", () => {
  const { settings } = applySignals({ ...DEFAULT_SETTINGS, preferred_phrases: ["부탁드립니다"] }, [
    { field: "preferred_phrases", value: ["죄송하지만", "부탁드립니다"], reason: "자주 씀" },
  ]);
  assert.deepEqual(settings.preferred_phrases, ["부탁드립니다", "죄송하지만"]);
});

test("이미 같은 값이면 적용할 필요가 없다", () => {
  assert.ok(signalMatches({ ...DEFAULT_SETTINGS, formality: 4 }, SUGGESTION.signals[0]));
  assert.ok(!signalMatches(DEFAULT_SETTINGS, SUGGESTION.signals[0]));
  assert.ok(signalMatches(DEFAULT_SETTINGS, SUGGESTION.signals[1]));
});

test("표현 칩 입력: 쉼표로 여러 개, 중복·한도·길이 검사", () => {
  assert.deepEqual(addPhrases(["ㅋㅋ"], "ㅠ, ㅋㅋ ,  넵", 50), { list: ["ㅋㅋ", "ㅠ", "넵"], error: null });
  const full = addPhrases(["a", "b"], "c", 2);
  assert.deepEqual(full.list, ["a", "b"]);
  assert.match(full.error, /2개까지/);
  const long = addPhrases([], "가".repeat(41), 20);
  assert.deepEqual(long.list, []);
  assert.match(long.error, /40자까지/);
  assert.deepEqual(addPhrases([], "  ", 20), { list: [], error: null });
});

test("제안: 표본이 적으면 보여 주지 않는다", () => {
  assert.equal(suggestionState({ based_on_messages: 0, signals: [] }), "none");
  assert.equal(suggestionState(null), "none");
  // 내 메시지가 다섯 개에 못 미치면 신호가 있어도 믿지 않는다
  assert.equal(suggestionState({ based_on_messages: 4, settings: {}, signals: SUGGESTION.signals }), "few");
  // 서버가 표본이 적다고 settings: null을 준 경우
  assert.equal(suggestionState({ based_on_messages: 12, settings: null, signals: [] }), "few");
  assert.equal(suggestionState({ based_on_messages: 5, settings: {}, signals: SUGGESTION.signals }), "ok");
  assert.equal(suggestionState({ based_on_messages: 30, settings: {}, signals: [] }), "same");
  assert.equal(suggestionState({ based_on_messages: 30, settings: {}, signals: [{ field: "mood", value: 3 }] }), "same");
});

test("제안 값은 숫자와 글자가 섞여 온다", () => {
  assert.equal(signalView({ field: "formality", value: "4" }).value, 4);
  assert.equal(signalView({ field: "formality", value: 4 }).valueLabel, signalView({ field: "formality", value: "4" }).valueLabel);
  assert.ok(signalMatches({ formality: 4 }, { field: "formality", value: "4" }));
  // 숫자로 볼 수 없는 값은 쓰지 않는다
  for (const bad of [true, "", "  ", null, undefined, "높음", 9]) {
    assert.equal(signalView({ field: "formality", value: bad }).usable, false, String(bad));
  }
  assert.equal(signalView({ field: "emoji", value: "none" }).usable, true);
  assert.equal(signalView({ field: "emoji", value: 0 }).usable, false);
});
