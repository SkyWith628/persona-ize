import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_SETTINGS,
  LIMITS,
  SCALES,
  addPhrases,
  applySignals,
  cardSignals,
  cardView,
  cleanPhrases,
  describeSettings,
  evidenceText,
  importProposal,
  isFewMessages,
  normalizeSettings,
  personaSaveError,
  settingsEqual,
  signalMatches,
  signalView,
  suggestionState,
  stepLabel,
  visibleSignals,
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
  assert.deepEqual(addPhrases(["ㅋㅋ"], "ㅠ, ㅋㅋ ,  넵", 50), { list: ["ㅋㅋ", "ㅠ", "넵"], error: null, rejected: [] });
  const full = addPhrases(["a", "b"], "c", 2);
  assert.deepEqual(full.list, ["a", "b"]);
  assert.match(full.error, /2개까지/);
  const long = addPhrases([], "가".repeat(41), 20);
  assert.deepEqual(long.list, []);
  assert.match(long.error, /40자까지/);
  assert.deepEqual(addPhrases([], "  ", 20), { list: [], error: null, rejected: [] });
  // 서버 상한(MAX_PHRASE_CHARS)과 같은 경계: 딱 40자는 받고, 공백은 뺀 뒤에 센다
  assert.equal(LIMITS.phrase, 40);
  assert.deepEqual(addPhrases([], ` ${"가".repeat(40)} `, 20), { list: ["가".repeat(40)], error: null, rejected: [] });
  // 이모지는 한 글자로 센다(서버와 같다)
  assert.deepEqual(addPhrases([], "😆".repeat(40), 20), { list: ["😆".repeat(40)], error: null, rejected: [] });
  assert.match(addPhrases([], "😆".repeat(41), 20).error, /40자까지/);
  // 긴 것 하나가 섞여도 나머지는 들어간다
  assert.deepEqual(addPhrases([], `넵, ${"가".repeat(41)}, ㅠ`, 20).list, ["넵", "ㅠ"]);
});

test("표현 칩 입력: 넣지 못한 표현은 rejected로 돌려준다(입력 칸에 남겨 고쳐 쓰게)", () => {
  const long = "가".repeat(41);
  assert.deepEqual(addPhrases([], `넵, ${long}, ㅠ`, 20).rejected, [long]);
  assert.deepEqual(addPhrases([], long, 20).rejected, [long]);
  // 개수 한도에 걸리면 그 뒤의 것이 모두 남는다. 중복은 남기지 않는다
  assert.deepEqual(addPhrases(["a"], "a, b, c, d", 2), { list: ["a", "b"], error: "2개까지 넣을 수 있어요.", rejected: ["c", "d"] });
});

test("저장 422: 서버의 항목 경로를 화면의 항목 이름으로 바꿔 알린다", () => {
  // 실제 서버 응답을 normalizeError로 읽은 모양 (PUT /v1/contacts/{id}/persona, 41자 표현)
  const err = {
    status: 422,
    message: "settings.forbidden_phrases: 표현 하나는 40자까지 넣을 수 있습니다.",
    details: [{ loc: "body.settings.forbidden_phrases", msg: "표현 하나는 40자까지 넣을 수 있습니다" }],
  };
  assert.deepEqual(personaSaveError(err), {
    field: "forbidden_phrases",
    label: "쓰지 않을 표현",
    detail: "표현 하나는 40자까지 넣을 수 있습니다. 고친 뒤 다시 저장해 주세요.",
    message: "쓰지 않을 표현: 표현 하나는 40자까지 넣을 수 있습니다. 고친 뒤 다시 저장해 주세요.",
  });
  const pref = personaSaveError({ status: 422, details: [{ loc: "body.settings.preferred_phrases.3", msg: "너무 깁니다." }] });
  assert.equal(pref.message, "즐겨 쓰는 표현: 너무 깁니다. 고친 뒤 다시 저장해 주세요.");
  assert.equal(personaSaveError({ status: 422, details: [{ loc: "body.settings.notes", msg: "" }] }).message, "메모: 입력한 내용을 확인한 뒤 다시 저장해 주세요.");
  assert.doesNotMatch(personaSaveError(err).message, /settings|forbidden_phrases/);
  // 항목을 알 수 없거나 422가 아니면 null — 부르는 쪽이 describeError를 쓴다
  assert.equal(personaSaveError({ status: 422, details: [{ loc: "body.expected_version", msg: "x" }] }), null);
  assert.equal(personaSaveError({ status: 422, details: [{ loc: "body.settings.__proto__", msg: "x" }] }), null);
  assert.equal(personaSaveError({ status: 422, details: [] }), null);
  assert.equal(personaSaveError({ status: 409, details: err.details }), null);
  assert.equal(personaSaveError(null), null);
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

// 설계 5.1의 제안 카드 예 (이서연, c3)
const CARD = {
  contact_alias: "이서연",
  speech_previous: { level: "hae", count: 3, message_ids: ["c3:m02", "c3:m04", "c3:m06"] },
  speech_current: { level: "hapsyo", count: 2, message_ids: ["c3:m08", "c3:m10"], since: "2026-10-21" },
  two_valued: null,
  address: { term: "서연 파트장님", count: 1, since: "2026-10-21", status: "candidate", message_ids: ["c3:m08"] },
  trigger: { event: "파트장 발령", at: "2026-10-17", message_ids: ["c3:m05"] },
  scope_hints: [
    { text: "회사에선 존댓말 써야겠네", message_ids: ["c3:m06"] },
    { text: "사적으로는 편하게 하자", message_ids: ["c3:m07"] },
  ],
  ask: true,
  ask_reason: "공식 자리에서만 쓰는 말투일 수 있습니다.",
  proposed: { formality: 4, address_term: "서연 파트장님" },
};

test("제안: 카드나 말투 변화·그래프 신호는 표본이 적어도 보여 준다", () => {
  const register = { field: "formality", value: 4, reason: "최근 2건이 합쇼체", source: "register", since: "2026-10-21" };
  const graph = { field: "address_term", value: "파트장님", reason: "관계 사실", source: "graph", since: "2026-10-21" };
  // 내 메시지가 다섯 개에 못 미쳐도, 서버가 settings: null을 줘도 가리지 않는다
  assert.equal(suggestionState({ based_on_messages: 4, settings: null, signals: [register] }), "ok");
  assert.equal(suggestionState({ based_on_messages: 4, settings: {}, signals: [graph] }), "ok");
  assert.equal(suggestionState({ based_on_messages: 2, settings: null, signals: [], card: CARD }), "ok");
  // 표본이 적을 때 "내 메시지 전체" 신호는 여전히 믿지 않는다 — 섞여 와도 그것만 뺀다
  const few = { based_on_messages: 4, settings: {}, signals: [...SUGGESTION.signals, register, graph] };
  assert.deepEqual(visibleSignals(few), [register, graph]);
  assert.ok(isFewMessages(few));
  // 표본이 충분하면 전부 보여 준다. 쓸 수 없는 신호는 어느 쪽이든 뺀다
  const enough = { based_on_messages: 30, settings: {}, signals: [...SUGGESTION.signals, register, { field: "mood", value: 3, source: "graph" }] };
  assert.equal(visibleSignals(enough).length, 4);
  assert.ok(!isFewMessages(enough));
  // 내용이 없는 카드는 카드로 치지 않는다
  assert.equal(suggestionState({ based_on_messages: 4, settings: null, signals: [], card: { ask: true, proposed: {} } }), "few");
  assert.equal(suggestionState({ based_on_messages: 30, settings: {}, signals: [], card: null }), "same");
});

test("제안 신호의 출처와 시작일", () => {
  assert.equal(signalView(SUGGESTION.signals[0]).sourceLabel, "내 메시지 전체", "source가 없으면 예전 응답으로 본다");
  assert.equal(signalView({ field: "formality", value: 4, source: "register", since: "2026-10-21" }).sourceLabel, "말투 변화");
  const g = signalView({ field: "formality", value: 4, source: "graph", since: "2026-10-17" });
  assert.equal(g.sourceLabel, "그래프");
  assert.equal(g.since, "2026-10-17");
  assert.equal(signalView({ field: "formality", value: 4, source: "toString" }).source, "messages");
  for (const bad of [null, undefined, "", "어제", 20261021]) {
    assert.equal(signalView({ field: "formality", value: 4, since: bad }).since, "", String(bad));
  }
});

test("제안 카드를 화면용으로 바꾼다", () => {
  const v = cardView(CARD);
  assert.equal(v.title, "이서연 님에게 쓰는 말투가 바뀐 것으로 보여요.");
  assert.deepEqual(v.rows, [
    { key: "speech", label: "말투", text: "반말 (3건: c3:m02, c3:m04, c3:m06) → 합쇼체 (최근 2건: c3:m08, c3:m10, 2026-10-21부터)" },
    { key: "address", label: "호칭", text: "'서연 파트장님' (1회 관찰: c3:m08, 2026-10-21부터)" },
    { key: "trigger", label: "계기(그래프)", text: "파트장 발령 (2026-10-17, c3:m05)" },
    { key: "scope", label: "범위 단서(그래프)", text: '"회사에선 존댓말 써야겠네" (c3:m06) / "사적으로는 편하게 하자" (c3:m07)' },
  ]);
  assert.equal(v.ask, true);
  assert.equal(v.askReason, "공식 자리에서만 쓰는 말투일 수 있습니다.");
  assert.equal(v.applyLabel, "격식 4 + 호칭 추가");
});

test("제안 카드: 빠진 조각·두 가지 말투·확정된 호칭", () => {
  assert.equal(cardView(null), null);
  assert.equal(cardView({}), null);
  assert.equal(cardView({ speech_current: { level: "???", count: 2 }, ask: true }), null, "모르는 말투 단계는 그리지 않는다");
  // 이전 구간 없이 현재만
  const only = cardView({ speech_current: { level: "haeyo", count: 5, message_ids: [] }, proposed: { formality: 3 } }, "김유진");
  assert.equal(only.title, "김유진 님에게 보낸 내 메시지에서 읽은 말투예요.");
  assert.deepEqual(only.rows, [{ key: "speech", label: "말투", text: "해요체 (최근 5건)" }]);
  assert.equal(only.ask, false);
  assert.equal(only.applyLabel, "격식 3 적용");
  // 두 가지 말투가 섞이면 값을 정해 주지 않는다
  const two = cardView({ contact_alias: "이서연", two_valued: { hapsyo: 4, hae: 2 }, ask: true, ask_reason: "최근 말투가 섞여 있습니다.", proposed: { formality: null, address_term: null } });
  assert.equal(two.title, "이서연 님에게 두 가지 말투를 섞어 쓰고 있어요.");
  assert.deepEqual(two.rows, [{ key: "two_valued", label: "최근 말투", text: "합쇼체 4건 / 반말 2건이 섞여 있어요" }]);
  assert.equal(two.applyLabel, "");
  // 두 번 넘게 본 호칭은 "관찰"을 붙이지 않는다. 계기에 날짜가 없어도 된다
  const confirmed = cardView({ address: { term: "팀장님", count: 3, status: "confirmed", message_ids: ["c1:m01"] }, trigger: { event: "팀 이동", at: null, message_ids: [] }, proposed: { address_term: "팀장님" } });
  assert.deepEqual(confirmed.rows.map((r) => r.text), ["'팀장님' (3회: c1:m01)", "팀 이동"]);
  assert.equal(confirmed.applyLabel, "호칭 추가");
  // 근거 id가 많으면 앞의 몇 개만
  assert.equal(evidenceText(["a", "b", "c", "d", "e", "f"]), "a, b, c, d 외 2건");
  assert.equal(evidenceText(null), "");
  // 가져온 대화의 긴 id는 화면에 적지 않는다 — 건수와 날짜만 남는다
  const long = ["k3:2510172237-4fa2c0c633566a45", "k3:2510180013-4d2bdbc3a7f93cb6"];
  assert.equal(evidenceText(long), "");
  assert.equal(evidenceText(["c3:m08", long[0]]), "");
  const imported = cardView({
    speech_current: { level: "hapsyo", count: 2, message_ids: long, since: "2025-10-18" },
    trigger: { event: "장비 공동구매", at: "2025-10-18", message_ids: long },
    scope_hints: [{ text: "우비 사이즈 L 요청", message_ids: [long[0]] }],
  });
  assert.ok(imported.rows.every((r) => !r.text.includes("k3:")));
  assert.match(imported.rows[0].text, /2건/);
  assert.equal(imported.rows.at(-1).text, '"우비 사이즈 L 요청"');
});

test("제안 카드 적용은 격식과 호칭만 채운다", () => {
  const mine = { ...DEFAULT_SETTINGS, friendliness: 5, notes: "결론부터" };
  const { settings, applied } = applySignals(mine, cardSignals(CARD));
  assert.equal(settings.formality, 4);
  assert.equal(settings.address_term, "서연 파트장님");
  assert.equal(settings.friendliness, 5);
  assert.equal(settings.notes, "결론부터");
  assert.deepEqual(applied, ["formality", "address_term"]);
  // 계기·범위 단서는 근거일 뿐 설정으로 들어가지 않는다
  assert.ok(!JSON.stringify(settings).includes("발령") && !JSON.stringify(settings).includes("존댓말"));
  // 정해 준 값이 없거나 범위 밖이면 적용할 것이 없다
  assert.deepEqual(cardSignals({ proposed: { formality: null, address_term: null } }), []);
  assert.deepEqual(cardSignals({ proposed: { formality: 9, address_term: "  " } }), []);
  assert.deepEqual(cardSignals({}), []);
  assert.ok(cardSignals(CARD).every((sig) => signalMatches(settings, sig)));
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

test("가져오기 직후 제안: 서버가 합친 설정에서 지금 설정과 다른 항목만 보여 준다", () => {
  const current = { ...DEFAULT_SETTINGS, friendliness: 2 };
  const p = importProposal(SUGGESTION, current);
  assert.equal(p.state, "ok");
  assert.deepEqual(p.changes, [
    { field: "formality", label: "격식", from: "3단계 · 보통 존댓말", to: "4단계 · 정중하게" },
    { field: "address_term", label: "호칭", from: "없음", to: "팀장님" },
  ]);
  assert.equal(p.settings.formality, 4);
  assert.equal(p.settings.friendliness, 2, "제안하지 않은 항목은 서버가 준 값 그대로");
  assert.equal(p.asking, false);
  // 지금 설정이 이미 제안과 같으면 저장할 것이 없다
  const same = importProposal(SUGGESTION, SUGGESTION.settings);
  assert.equal(same.state, "same");
  assert.equal(same.settings, null);
});

test("가져오기 직후 제안: 표본이 적으면 말투 변화·그래프 신호만 지금 설정에 얹는다", () => {
  const register = { field: "formality", value: 4, reason: "최근 2건이 합쇼체", source: "register", since: "2026-10-21" };
  const graph = { field: "address_term", value: "파트장님", reason: "관계 사실", source: "graph" };
  const mine = { ...DEFAULT_SETTINGS, notes: "결론부터", emoji: "rare" };
  const p = importProposal({ based_on_messages: 4, settings: null, signals: [...SUGGESTION.signals, register, graph] }, mine);
  assert.equal(p.state, "ok");
  assert.deepEqual(p.changes.map((c) => c.field), ["formality", "address_term"]);
  assert.equal(p.settings.address_term, "파트장님", "'내 메시지 전체'의 호칭(팀장님)은 쓰지 않는다");
  assert.equal(p.settings.emoji, "rare");
  assert.equal(p.settings.notes, "결론부터");
});

test("가져오기 직후 제안: 카드가 물으면 카드가 정해 둔 값을 얹고, 왜 묻는지 함께 준다", () => {
  const register = [
    { field: "formality", value: 4, reason: "최근 2건이 합쇼체", source: "register" },
    { field: "address_term", value: "서연 파트장님", reason: "1회 관찰", source: "register" },
  ];
  // 서버는 묻는 항목을 settings에서 뺀다 — 저장 버튼이 그 물음의 답이므로 카드의 값을 다시 얹는다
  const asked = importProposal({ based_on_messages: 4, settings: null, signals: register, card: CARD }, DEFAULT_SETTINGS);
  assert.equal(asked.state, "ok");
  assert.equal(asked.asking, true);
  assert.equal(asked.askReason, "공식 자리에서만 쓰는 말투일 수 있습니다.");
  assert.deepEqual(asked.changes, [
    { field: "formality", label: "격식", from: "3단계 · 보통 존댓말", to: "4단계 · 정중하게" },
    { field: "address_term", label: "호칭", from: "없음", to: "서연 파트장님" },
  ]);
  // 근거 줄에는 건수와 날짜만 — 메시지 id는 넣지 않는다
  assert.deepEqual(asked.rows.map((r) => r.key), ["speech", "address", "trigger", "scope"]);
  assert.equal(asked.rows[0].text, "반말 (3건) → 합쇼체 (최근 2건, 2026-10-21부터)");
  assert.equal(asked.rows[1].text, "'서연 파트장님' (1회 관찰, 2026-10-21부터)");
  assert.ok(!asked.rows.some((r) => r.text.includes("c3:")));
  // 카드가 값을 정해 두지 않았으면 격식·호칭은 건드리지 않고 묻기만 한다
  const open = { ...CARD, proposed: {} };
  const noValue = importProposal({ based_on_messages: 4, settings: null, signals: register, card: open }, DEFAULT_SETTINGS);
  assert.equal(noValue.state, "ask");
  assert.equal(noValue.settings, null);
  // 그때도 그래프 신호는 반영한다(서버와 같은 규칙)
  const graph = { field: "address_term", value: "파트장님", reason: "관계 사실", source: "graph" };
  const mixed = importProposal({ based_on_messages: 4, settings: null, signals: [...register, graph], card: open }, DEFAULT_SETTINGS);
  assert.equal(mixed.state, "ok");
  assert.deepEqual(mixed.changes, [{ field: "address_term", label: "호칭", from: "없음", to: "파트장님" }]);
  assert.equal(mixed.settings.formality, 3);
  // 지금 설정이 이미 카드의 값과 같으면 저장할 것이 없다
  const done = importProposal({ based_on_messages: 4, settings: null, signals: register, card: CARD }, { ...DEFAULT_SETTINGS, formality: 4, address_term: "서연 파트장님" });
  assert.equal(done.state, "ask");
});

test("가져오기 직후 제안: 내 메시지가 없거나 적으면 제안하지 않는다", () => {
  assert.equal(importProposal(null, DEFAULT_SETTINGS).state, "none");
  assert.equal(importProposal({ based_on_messages: 3, settings: null, signals: SUGGESTION.signals }, DEFAULT_SETTINGS).state, "few");
  assert.equal(importProposal({ based_on_messages: 3, settings: null, signals: [] }, null).settings, null);
});
