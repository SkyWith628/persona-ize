import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiError, normalizeError } from "../js/lib/errors.js";
import {
  buildSituation,
  fallbackView,
  feedbackView,
  filterContacts,
  humanizeViolation,
  markSegments,
  mergeHistory,
  metaLine,
  preservedChips,
  pushRecent,
  recentContacts,
  resultMeta,
  rewriteErrorView,
  statusView,
  usageView,
} from "../js/lib/model.js";

// backend/app/domain/rewrite.py의 RewriteResult + 계약 §4.5의 추가 필드로 만든 예시
const PRESERVED = [
  { kind: "datetime", text: "이번 주 금요일", normalized: "이번주금요일" },
  { kind: "datetime", text: "다음 주 수요일", normalized: "다음주수요일" },
  { kind: "datetime", text: "금요일", normalized: "금요일" },
  { kind: "datetime", text: "수요일", normalized: "수요일" },
  { kind: "datetime", text: "이번 주", normalized: "이번주" },
  { kind: "datetime", text: "다음 주", normalized: "다음주" },
  { kind: "possibility", text: "어렵고", normalized: "불가" },
  { kind: "possibility", text: "가능합니다", normalized: "가능" },
];

const RECOMMENDED = {
  id: "0b0f6d0e-5d6e-4f43-9a55-3f5b0f1c2d10",
  contact_id: "c0ffee00-0000-4000-8000-000000000001",
  draft_version: 1,
  condition: "C",
  channel: "web",
  created_at: "2026-11-11T02:10:00Z",
  feedback: null,
  result: {
    status: "recommended",
    result_text: "지현 팀장님, 죄송하지만 이번 주 금요일은 어렵습니다. 다음 주 수요일까지는 가능합니다.",
    change_reasons: ["호칭을 설정에 맞췄습니다.", "과거에 쓰신 '죄송하지만'을 살렸습니다."],
    warnings: ["'금요일까지는'으로 읽히지 않게 문장을 나눴습니다. 뜻이 맞는지 확인해 주세요."],
    evidence_ids: ["ep-1"],
    intent: "refusal",
    preserved_items: PRESERVED,
    validation: { passed: true, violations: [] },
    attempts: 2,
    attempt_log: [
      {
        attempt: 1,
        passed: false,
        violations: [{ check: "fact_added", detail: "원문에 없는 person '서연 씨' 이(가) 추가되었습니다." }],
        rewritten_text: "…",
      },
      { attempt: 2, passed: true, violations: [], rewritten_text: "…" },
    ],
    model_version: "claude-opus-5-5",
    prompt_version: "rewrite-v3",
    retrieval_version: "vector-style-v1",
    token_usage: { input_tokens: 1200, output_tokens: 180 },
    latency_ms: 4400,
    adjust: null,
  },
};

const FALLBACK = {
  ...RECOMMENDED,
  id: "0b0f6d0e-5d6e-4f43-9a55-3f5b0f1c2d11",
  condition: "B",
  result: {
    ...RECOMMENDED.result,
    status: "fallback_original",
    result_text: "3%는 어렵고 5%면 수요일까지 됨",
    change_reasons: [],
    warnings: ["추천을 만들지 못해 원문을 유지했습니다.", "quantity '5%' 이(가) 결과에 없습니다."],
    evidence_ids: [],
    validation: {
      passed: false,
      violations: [
        { check: "preserve_missing", detail: "quantity '5%' 이(가) 결과에 없습니다." },
        { check: "preserve_missing", detail: "datetime '수요일' 이(가) 결과에 없습니다." },
      ],
    },
  },
};

test("보존 항목이 남은 구간을 표시한다 — 긴 항목이 먼저, 겹치면 한 번만", () => {
  const segs = markSegments(RECOMMENDED.result.result_text, PRESERVED);
  const marked = segs.filter((s) => s.mark).map((s) => s.text);
  assert.deepEqual(marked, ["이번 주 금요일", "다음 주 수요일", "가능합니다"]);
  assert.equal(segs.map((s) => s.text).join(""), RECOMMENDED.result.result_text, "조각을 이으면 원래 문장");
});

test("같은 항목이 두 번 나오면 둘 다 표시한다", () => {
  const segs = markSegments("금요일에 보고 금요일에 끝", [{ kind: "datetime", text: "금요일", normalized: "금요일" }]);
  assert.equal(segs.filter((s) => s.mark).length, 2);
});

test("한 글자 부정 표현은 강조하지 않는다('안녕'의 '안')", () => {
  const segs = markSegments("안녕하세요, 안 됩니다", [{ kind: "negation", text: "안", normalized: "부정" }]);
  assert.deepEqual(segs, [{ text: "안녕하세요, 안 됩니다", mark: false }]);
});

test("표시할 것이 없어도 문장 조각 하나는 돌려준다", () => {
  assert.deepEqual(markSegments("그대로", []), [{ text: "그대로", mark: false }]);
  assert.deepEqual(markSegments("", null), [{ text: "", mark: false }]);
});

test("모델 출력에 태그가 있어도 글자로만 다룬다", () => {
  const text = '<img src=x onerror="alert(1)"> 금요일';
  const segs = markSegments(text, [{ kind: "datetime", text: "금요일", normalized: "금요일" }]);
  assert.equal(segs[0].text, '<img src=x onerror="alert(1)"> ');
  assert.equal(segs[1].mark, true);
});

test("지킨 항목 칩: 긴 날짜만 남기고, 가능/불가는 방향으로 묶는다", () => {
  const labels = preservedChips(RECOMMENDED.result).map((c) => c.label);
  assert.deepEqual(labels, ["이번 주 금요일", "다음 주 수요일", "불가 표현", "가능합니다(가능)"]);
});

test("지킨 항목 칩: 약속·조건·부정", () => {
  const result = {
    status: "recommended",
    result_text: "목요일이면 제가 보내드리겠습니다. 안 늦겠습니다.",
    preserved_items: [
      { kind: "promise", text: "보낼게요", normalized: "약속" },
      { kind: "promise", text: "늦겠습니다", normalized: "약속" },
      { kind: "condition", text: "목요일이면", normalized: "목요일이면" },
      { kind: "condition", text: "비가 오면", normalized: "비가오면" },
      { kind: "negation", text: "안", normalized: "부정" },
    ],
  };
  const labels = preservedChips(result).map((c) => c.label);
  assert.deepEqual(labels, ["늦겠습니다(약속)", "목요일이면(조건)", "부정 표현"]);
  const none = preservedChips({ ...result, result_text: "제가 전달하겠습니다." });
  assert.deepEqual(none.map((c) => c.label), ["약속 표현", "부정 표현"], "글자 그대로 남지 않았으면 묶어서 하나만");
});

test("원문 유지 결과에는 지킨 항목 칩을 만들지 않는다", () => {
  assert.deepEqual(preservedChips(FALLBACK.result), []);
  assert.deepEqual(preservedChips(null), []);
});

test("검증 위반 문구의 영어 종류 이름을 우리말로 바꾼다", () => {
  assert.equal(humanizeViolation("datetime '수요일' 이(가) 결과에 없습니다."), "날짜·시간 '수요일'이(가) 결과에 없습니다.");
  assert.equal(
    humanizeViolation("원문에 없는 person '서연 씨' 이(가) 추가되었습니다."),
    "원문에 없는 사람 이름 '서연 씨'이(가) 추가되었습니다.",
  );
  assert.equal(humanizeViolation("금지 표현 'ㅋㅋ' 포함"), "금지 표현 'ㅋㅋ' 포함");
});

test("원문 유지: 사실을 못 지킨 경우의 안내와 항목", () => {
  const v = fallbackView(FALLBACK.result);
  assert.match(v.headline, /사실을 지키지 못했어요/);
  assert.deepEqual(v.items, ["숫자·수량 '5%'이(가) 결과에 없습니다.", "날짜·시간 '수요일'이(가) 결과에 없습니다."]);
});

test("원문 유지: 이력에서 볼 때는 '입력란' 이야기를 하지 않는다", () => {
  const v = fallbackView(FALLBACK.result, { history: true });
  assert.match(v.headline, /사실을 지키지 못했어요/);
  assert.doesNotMatch(v.lead, /입력란/);
  assert.match(v.lead, /초안을 그대로 뒀어요/);
  const down = fallbackView({ validation: { passed: false, violations: [{ check: "empty", detail: "x" }] } }, { history: true });
  assert.match(down.headline, /그때는/);
});

test("원문 유지: 모델 장애·거절은 다른 제목을 쓴다", () => {
  const down = fallbackView({ validation: { passed: false, violations: [{ check: "empty", detail: "모델 호출 실패" }] } });
  assert.match(down.headline, /지금은 추천 문장을 만들지 못했어요/);
  const refused = fallbackView({ validation: { passed: false, violations: [{ check: "refusal", detail: "거절" }] } });
  assert.match(refused.headline, /다듬지 않았어요/);
  assert.match(fallbackView({}).headline, /만들지 못했어요/);
});

test("부가 정보: 조건·시간·시도 횟수(시연 대본의 '2회 시도')", () => {
  const meta = resultMeta(RECOMMENDED, "auto");
  assert.equal(meta.condition, "C");
  assert.equal(meta.attempts, 2);
  assert.equal(meta.evidenceCount, 1);
  assert.equal(metaLine(meta), "조건 C(자동) · 4.4초 · 2회 시도");
  assert.equal(meta.attemptLog[0].violations[0], "원문에 없는 사람 이름 '서연 씨'이(가) 추가되었습니다.");
  assert.equal(meta.attemptLog[1].passed, true);
});

test("부가 정보: 조정을 적용했으면 한 줄에 붙인다", () => {
  const rec = { ...RECOMMENDED, result: { ...RECOMMENDED.result, adjust: "shorter", attempts: 1 } };
  assert.equal(metaLine(resultMeta(rec, "B")), "조건 C · 4.4초 · 1회 시도 · 더 짧게");
});

test("피드백 표시: 적용·수정·거절·없음, 그리고 필드가 없는 옛 서버", () => {
  assert.equal(feedbackView({ accepted: true, edited_text: null }).label, "적용");
  assert.equal(feedbackView({ accepted: true, edited_text: "고친 문장" }).kind, "edited");
  assert.equal(feedbackView({ accepted: false, reason_code: "too_stiff" }).label, "거절 · 너무 딱딱해요");
  assert.equal(feedbackView({ accepted: false, reason_code: null }).label, "거절");
  assert.equal(feedbackView(null).kind, "none");
  assert.equal(feedbackView(undefined).kind, "unknown");
});

test("상태 표시는 색과 함께 글자로도 구분한다", () => {
  assert.deepEqual(statusView(RECOMMENDED.result), { kind: "recommended", label: "추천", tone: "ok" });
  assert.equal(statusView(FALLBACK.result).label, "원문 유지");
});

test("이력 더 보기: 같은 id는 붙이지 않는다", () => {
  const first = [RECOMMENDED];
  const more = mergeHistory(first, [RECOMMENDED, FALLBACK]);
  assert.equal(more.rows.length, 2);
  assert.equal(more.added, 1);
  assert.equal(mergeHistory(first, [RECOMMENDED]).added, 0, "before를 무시하는 서버에서는 더 붙지 않는다");
});

test("상황: 칩과 자유 입력을 한 줄로", () => {
  assert.equal(buildSituation("거절", ""), "거절");
  assert.equal(buildSituation(null, "  일정이 겹쳐서 "), "일정이 겹쳐서");
  assert.equal(buildSituation("거절", "일정이 겹쳐서"), "거절 · 일정이 겹쳐서");
  assert.equal(buildSituation("", ""), "");
  assert.equal(buildSituation("보고", "가".repeat(600)).length, 500);
});

const err = (status, body, retryAfter) => new ApiError(normalizeError(status, body, retryAfter));
const CONTACT = { id: "c1", alias: "박지현", relationship_type: "superior", persona_version: null };

test("첨삭 오류: 페르소나가 없으면 설정으로 안내한다", () => {
  const e = err(422, { code: "invalid_input", message: "이 상대에게 저장된 페르소나가 없습니다." });
  const v = rewriteErrorView(e, { contact: CONTACT, condition: "B" });
  assert.equal(v.kind, "no_persona");
  assert.equal(v.action, "persona");
  assert.match(v.title, /박지현/);
  const withPersona = rewriteErrorView(e, { contact: { ...CONTACT, persona_version: 1 }, condition: "B" });
  assert.equal(withPersona.kind, "invalid");
  assert.equal(withPersona.body, "이 상대에게 저장된 페르소나가 없습니다.");
});

test("첨삭 오류: 서버가 자동 조건을 모르면 고급으로 안내한다", () => {
  const e = err(422, { detail: [{ loc: ["body", "condition"], msg: "Input should be 'A', 'B', 'C' or 'D'" }] });
  const v = rewriteErrorView(e, { contact: CONTACT, condition: "auto" });
  assert.equal(v.kind, "auto_unavailable");
  assert.equal(v.action, "advanced");
});

test("첨삭 오류: 429 세 가지를 따로 안내한다", () => {
  const usage = rewriteErrorView(err(429, { code: "usage_exceeded", message: "오늘 첨삭 요청 한도(200회)를 모두 썼습니다." }));
  assert.equal(usage.kind, "usage");
  assert.match(usage.body, /200회/);
  const budget = rewriteErrorView(err(429, { code: "budget_exceeded", message: "서비스 전체 한도" }));
  assert.equal(budget.kind, "budget");
  assert.match(budget.body, /내일/);
  const rate = rewriteErrorView(err(429, { code: "rate_limited", message: "잠시 뒤" }, "7"));
  assert.equal(rate.kind, "rate");
  assert.match(rate.body, /7초 뒤에/);
  assert.equal(rate.action, "retry");
});

test("첨삭 오류: 503·네트워크·401은 초안이 남아 있음을 알린다", () => {
  for (const e of [err(503, { code: "model_unavailable", message: "모델 장애" }), err(0, null), err(401, null)]) {
    assert.match(rewriteErrorView(e).body, /초안은 입력란에 그대로/);
  }
  assert.equal(rewriteErrorView(err(503, null)).action, "retry");
  assert.equal(rewriteErrorView(err(401, null)).action, "login");
  const server = rewriteErrorView(err(500, { code: "internal_error", message: "x", request_id: "req-9" }));
  assert.match(server.body, /req-9/);
});

const CONTACTS = [
  { id: "a", alias: "박지현", relationship_type: "superior" },
  { id: "b", alias: "이서연", relationship_type: "colleague" },
  { id: "c", alias: "김준호", relationship_type: "family" },
  { id: "d", alias: "최유진", relationship_type: "friend" },
];

test("최근 상대 칩: 최근 순서 → 모자라면 최근에 만든 상대로 채운다", () => {
  assert.deepEqual(recentContacts(CONTACTS, ["c", "a"]).map((c) => c.id), ["c", "a", "d"]);
  assert.deepEqual(recentContacts(CONTACTS, []).map((c) => c.id), ["d", "c", "b"]);
  assert.deepEqual(recentContacts(CONTACTS, ["zzz", "b"]).map((c) => c.id), ["b", "d", "c"], "지워진 상대는 건너뛴다");
});

test("최근 상대 칩: 고른 상대가 빠져 있으면 맨 앞에 넣는다", () => {
  assert.deepEqual(recentContacts(CONTACTS, ["d", "c", "b"], "a").map((c) => c.id), ["a", "d", "c"]);
  assert.deepEqual(recentContacts(CONTACTS, ["d", "c", "b"], "c").map((c) => c.id), ["d", "c", "b"]);
  assert.deepEqual(recentContacts([], ["a"], "a"), []);
});

test("최근 목록에 넣기: 중복 없이 맨 앞", () => {
  assert.deepEqual(pushRecent(["a", "b", "c"], "c"), ["c", "a", "b"]);
  assert.deepEqual(pushRecent(null, "x"), ["x"]);
  assert.equal(pushRecent(["1", "2", "3"], "4", 3).length, 3);
});

test("상대 찾기: 이름과 관계로 거른다", () => {
  assert.deepEqual(filterContacts(CONTACTS, "지현").map((c) => c.id), ["a"]);
  assert.deepEqual(filterContacts(CONTACTS, "친구").map((c) => c.id), ["d"]);
  assert.equal(filterContacts(CONTACTS, "  ").length, 4);
  assert.equal(filterContacts(CONTACTS, "없는이름").length, 0);
});

test("사용량 표시", () => {
  const u = usageView({ day: "2026-11-11", rewrites: 12, rewrite_limit: 200, remaining_rewrites: 188, input_tokens: 3500, output_tokens: 2600, token_limit: 400000 });
  assert.equal(u.rewrites, 12);
  assert.equal(u.tokens, 6100);
  assert.equal(u.exhausted, false);
  assert.equal(u.graphCap, null);
  assert.equal(usageView({ rewrites: 200, rewrite_limit: 200 }).exhausted, true);
  assert.equal(usageView(null), null);
});
