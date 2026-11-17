// 관리자 화면 표시 규칙 검사. 실행: js.sh <이름> test "backend/app/web/tests/*.test.js"
import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, readError, requestInit } from "../js/admin/api.js";
import {
  ACTION_LABEL,
  CHECK_LABEL,
  REASON_LABEL,
  axisStep,
  costSummary,
  dateTime,
  dayLong,
  dayShort,
  duration,
  gateFor,
  groupAccess,
  healthView,
  heldView,
  label,
  num,
  percent,
  rangeText,
  retryConfirmText,
  retryErrorText,
  retryResultText,
  share,
  usd,
} from "../js/admin/format.js";

function overview(patch = {}) {
  return {
    range: { days: 7, from: "2026-11-05T00:00:00+09:00", to: "2026-11-12T00:00:00+09:00" },
    rewrites: { by_day: [{ day: "2026-11-05", total: 1, fallback: 0 }, { day: "2026-11-11", total: 2, fallback: 1 }] },
    tokens: { input: 403500, output: 1115, estimated_cost_usd: null },
    graph: { ingested_messages: 0, estimated_cost_usd: null },
    ...patch,
  };
}

test("표본이 없으면 0%가 아니라 null (화면은 '자료 없음'을 쓴다)", () => {
  assert.equal(percent(null), null);
  assert.equal(percent(undefined), null);
  assert.equal(percent(Number.NaN), null);
  assert.equal(share(0, 0), null);
  assert.equal(percent(share(0, 0)), null);
  // 표본이 있고 값이 0이면 0%가 맞다
  assert.equal(percent(0), "0%");
  assert.equal(percent(share(0, 4)), "0%");
  assert.equal(percent(0.962), "96.2%");
  assert.equal(percent(0.4286), "42.9%");
  assert.equal(percent(1), "100%");
  assert.equal(percent(0.7), "70%");
});

test("숫자에는 자릿수 구분과 단위", () => {
  assert.equal(num(1234567), "1,234,567");
  assert.equal(num(null), "0");
  assert.equal(duration(null), null);
  assert.equal(duration(12), "12ms");
  assert.equal(duration(999.4), "999ms");
  assert.equal(duration(1000), "1.0초");
  assert.equal(duration(7430), "7.4초");
  assert.equal(duration(12600), "13초");
});

test("금액: 단가가 없으면 null, 작은 금액을 0으로 뭉개지 않는다", () => {
  assert.equal(usd(null), null);
  assert.equal(usd(0), "$0");
  assert.equal(usd(0.0042), "$0.0042");
  assert.equal(usd(2.045375), "$2.05");
  assert.equal(usd(212), "$212.00");
  assert.equal(usd(12345.6), "$12,346");
});

test("날짜 표기", () => {
  assert.equal(dayShort("2026-11-05"), "11.05");
  assert.equal(dayLong("2026-11-05"), "11월 5일(목)");
  assert.equal(dayLong("2026-11-11"), "11월 11일(수)");
  const seen = new Date(2026, 10, 11, 14, 32); // 보는 사람의 시간대로 14:32
  assert.equal(dateTime(seen.toISOString(), new Date(2026, 10, 12)), "11.11 14:32");
  assert.equal(dateTime(seen.toISOString(), new Date(2027, 0, 2)), "2026.11.11 14:32");
  assert.equal(dateTime(null), "–");
  assert.equal(dateTime("날짜 아님"), "–");
});

test("기간 안내와 눈금 간격", () => {
  assert.equal(rangeText(overview()), "11월 5일(목) ~ 11월 11일(수) · 2일 · 한국 시간 기준");
  const today = overview({
    range: { days: 1, from: "2026-11-11T00:00:00+00:00", to: "2026-11-12T00:00:00+00:00" },
    rewrites: { by_day: [{ day: "2026-11-11", total: 0, fallback: 0 }] },
  });
  assert.equal(rangeText(today), "오늘 11월 11일(수) · 하루 경계 UTC+00:00");
  assert.equal(axisStep(1), 1);
  assert.equal(axisStep(7), 1);
  assert.equal(axisStep(30), 5);
});

test("코드 표기: 아는 코드는 문구로, 모르는 코드는 그대로, 없으면 줄표", () => {
  assert.equal(label(CHECK_LABEL, "preserve_missing"), "지켜야 할 내용이 빠짐");
  assert.equal(label(REASON_LABEL, "too_stiff"), "너무 딱딱함");
  assert.equal(label(REASON_LABEL, "brand_new_code"), "brand_new_code");
  assert.equal(label(REASON_LABEL, null), "–");
  assert.equal(label(CHECK_LABEL, "toString"), "toString"); // 객체에 원래 있는 이름에 속지 않는다
});

test("비용 카드: 단가가 없으면 금액을 만들지 않는다", () => {
  assert.deepEqual(costSummary(overview()), { value: "단가 미설정", known: false, sub: "토큰 404,615" });
  assert.deepEqual(
    costSummary(overview({ graph: { ingested_messages: 12, estimated_cost_usd: null } })),
    { value: "단가 미설정", known: false, sub: "토큰 404,615 · 사건 정리 12건" },
  );
  const both = costSummary(
    overview({
      tokens: { input: 403500, output: 1115, estimated_cost_usd: 2.045375 },
      graph: { ingested_messages: 3, estimated_cost_usd: 0.012 },
    }),
  );
  assert.deepEqual(both, { value: "$2.06", known: true, sub: "첨삭 $2.05 + 사건 정리 $0.01 (추정)" });
  const rewriteOnly = costSummary(
    overview({
      tokens: { input: 1000, output: 500, estimated_cost_usd: 0.0175 },
      graph: { ingested_messages: 5, estimated_cost_usd: null },
    }),
  );
  assert.equal(rewriteOnly.value, "$0.02");
  assert.match(rewriteOnly.sub, /사건 정리 5건은 단가 미설정/);
  const graphOnly = costSummary(overview({ graph: { ingested_messages: 5, estimated_cost_usd: 0.02 } }));
  assert.equal(graphOnly.value, "$0.02");
  assert.match(graphOnly.sub, /첨삭은 단가 미설정, 토큰 404,615/);
});

test("연결 상태", () => {
  assert.deepEqual(healthView("ok"), { kind: "ok", text: "정상" });
  assert.deepEqual(healthView("skipped"), { kind: "off", text: "사용 안 함" });
  assert.deepEqual(healthView("error: timeout"), { kind: "bad", text: "오류 (timeout)" });
  assert.deepEqual(healthView(undefined), { kind: "bad", text: "오류 (알 수 없음)" });
});

test("접근 기록: 같은 관리자가 1분 안에 이어서 한 호출은 한 줄", () => {
  const at = (seconds) => new Date(Date.UTC(2026, 10, 11, 5, 0, seconds)).toISOString();
  const rows = [
    { admin_username: "boss", action: "access_log", created_at: at(125) },
    { admin_username: "boss", action: "users", created_at: at(124) },
    { admin_username: "boss", action: "overview", created_at: at(123) },
    { admin_username: "ops", action: "overview", created_at: at(122) }, // 다른 관리자
    { admin_username: "boss", action: "overview", created_at: at(121) },
    { admin_username: "boss", action: "overview", created_at: at(30) }, // 1분 넘게 떨어짐
  ];
  const groups = groupAccess(rows);
  assert.deepEqual(
    groups.map((g) => [g.admin_username, g.count, g.actions]),
    [
      ["boss", 3, ["access_log", "users", "overview"]],
      ["ops", 1, ["overview"]],
      ["boss", 1, ["overview"]],
      ["boss", 1, ["overview"]],
    ],
  );
  assert.equal(groups[0].newest, at(125));
  assert.equal(groups[0].oldest, at(123));
  assert.equal(groups.reduce((sum, g) => sum + g.count, 0), rows.length); // 빠지는 줄이 없다
  assert.deepEqual(groupAccess([]), []);
});

test("접근이 막힌 경우의 안내와 오류 본문 읽기", () => {
  assert.equal(gateFor(new ApiError(401, "unauthorized", "x")).title, "로그인이 필요합니다");
  assert.match(gateFor(new ApiError(403, "forbidden", "x")).title, /관리자만/);
  assert.equal(
    gateFor(new ApiError(503, "admin_unavailable", "x")).title,
    "PostgreSQL 저장소에서만 볼 수 있습니다",
  );
  assert.equal(gateFor(new ApiError(503, "model_unavailable", "x")), null); // 다른 503은 안내 화면이 아니다
  assert.equal(gateFor(new ApiError(500, "internal_error", "x")), null);
  assert.equal(gateFor(new ApiError(0, "network", "x")), null);

  const flat = readError(403, { code: "forbidden", message: "관리자만 볼 수 있습니다." });
  assert.deepEqual([flat.status, flat.code, flat.message], [403, "forbidden", "관리자만 볼 수 있습니다."]);
  const nested = readError(401, { detail: { code: "unauthorized", message: "로그인이 필요합니다." } });
  assert.deepEqual([nested.status, nested.code], [401, "unauthorized"]);
  const validation = readError(422, { detail: [{ loc: ["query", "limit"], msg: "bad" }] });
  assert.deepEqual([validation.code, validation.message], [null, "요청을 처리하지 못했습니다 (422)."]);
  assert.equal(readError(502, null).code, null);
});

test("보류 줄: 보류가 있고 수집이 켜져 있을 때만 다시 시도 버튼을 낸다", () => {
  assert.deepEqual(heldView({ enabled: true, held_messages: 0 }), {
    count: 0,
    canRetry: false,
    tag: { kind: "ok", text: "없음" },
  });
  // 필드가 없던 예전 서버 응답, 이상한 값도 0건으로 본다
  for (const graph of [{ enabled: true }, { enabled: true, held_messages: null }, { enabled: true, held_messages: -2 }, undefined]) {
    assert.equal(heldView(graph).canRetry, false);
    assert.equal(heldView(graph).count, 0);
  }
  const held = heldView({ enabled: true, held_messages: 3 });
  assert.deepEqual([held.count, held.canRetry, held.tag.kind], [3, true, "warn"]);
  assert.doesNotMatch(held.tag.text, /graph_reset/); // 서버에서 스크립트를 돌리라는 안내는 더 없다
  // 그래프가 꺼진 서버는 다시 넣을 곳이 없다 — 건수만 보이고 버튼은 없다
  const off = heldView({ enabled: false, held_messages: 3 });
  assert.deepEqual([off.count, off.canRetry, off.tag.kind], [3, false, "off"]);
});

test("다시 시도: 확인 문구는 건수와 비용을 말하고, 결과·오류를 문장으로 알린다", () => {
  assert.equal(
    retryConfirmText(3),
    "보류 3건을 다시 시도합니다. 모델 호출 비용이 3건만큼 듭니다.\n계속할까요?",
  );
  assert.match(retryConfirmText(1234), /보류 1,234건.*1,234건만큼/);

  assert.match(retryResultText({ released: 3, users: 2, started: true }), /^보류 3건\(사용자 2명\)을 풀었습니다\./);
  assert.match(retryResultText({ released: 3, users: 2, started: true }), /다시 시작했습니다/);
  assert.equal(retryResultText({ released: 0, users: 0, started: false }), "다시 시도할 보류 메시지가 없습니다.");
  assert.equal(retryResultText(null), "다시 시도할 보류 메시지가 없습니다.");

  assert.match(retryErrorText(new ApiError(409, "graph_disabled", "x")), /꺼진 서버/);
  assert.match(retryErrorText(new ApiError(403, "forbidden", "x")), /새로 고친 뒤/);
  assert.equal(
    retryErrorText(new ApiError(0, "network", "서버에 연결하지 못했습니다.")),
    "다시 시도하지 못했습니다. 서버에 연결하지 못했습니다.",
  );
  assert.equal(label(ACTION_LABEL, "graph_retry_held"), "보류 다시 시도");
  assert.equal(label(ACTION_LABEL, "graph_retry_held_done"), "보류 다시 시도 결과");
});

test("쓰기 요청에만 CSRF 토큰을 싣는다", () => {
  assert.deepEqual(requestInit("GET", "tok").headers, { Accept: "application/json" });
  const post = requestInit("POST", "tok");
  assert.deepEqual(post.headers, { Accept: "application/json", "X-CSRF-Token": "tok" });
  assert.deepEqual([post.method, post.credentials, post.cache, "body" in post], ["POST", "same-origin", "no-store", false]);
  assert.equal("X-CSRF-Token" in requestInit("POST", null).headers, false); // 토큰이 없으면 서버가 403으로 막는다
});

// ---------- 회원관리 ----------

const { memberActions, memberResultText, providersText } = await import("../js/admin/format.js");

const member = (over = {}) => ({
  user_id: "11111111-2222-3333-4444-555555555555",
  username: "minsu",
  email: "minsu@example.com",
  role: "user",
  status: "active",
  ...over,
});

test("회원 한 줄의 할 일: 상태와 권한에 따라 달라진다", () => {
  const keys = (m, self = "someone-else") => memberActions(m, self).map((a) => a.key);
  assert.deepEqual(keys(member({ status: "pending" })), ["approve", "suspend", "logout", "delete"]);
  assert.deepEqual(keys(member()), ["suspend", "promote", "logout", "delete"]);
  assert.deepEqual(keys(member({ role: "admin" })), ["suspend", "demote", "logout", "delete"]);
  assert.deepEqual(keys(member({ status: "suspended" })), ["restore", "logout", "delete"]);
});

test("자기 자신에게는 정지·강등·삭제를 내지 않는다", () => {
  const me = member({ role: "admin" });
  assert.deepEqual(memberActions(me, me.user_id), []);
});

test("회원 작업은 대상을 주소에 싣고, 확인 문장에 누구인지 적는다", () => {
  const [approve, , , remove] = memberActions(member({ status: "pending" }), "x");
  assert.equal(approve.method, "PATCH");
  assert.equal(approve.path, "/v1/admin/members/11111111-2222-3333-4444-555555555555");
  assert.deepEqual(approve.body, { status: "active" });
  assert.match(approve.confirm, /minsu\(minsu@example\.com\)/);
  assert.equal(remove.method, "DELETE");
  assert.equal(remove.danger, true);
  assert.match(remove.confirm, /되돌릴 수 없습니다/);
  // 이메일이 없는(소셜, 이메일 미동의) 회원은 아이디만 적는다
  assert.match(memberActions(member({ email: null }), "x")[0].confirm, /^minsu 님/);
});

test("로그인 수단과 결과 문장", () => {
  assert.equal(providersText(["password", "google"]), "비밀번호 · Google");
  assert.equal(providersText([]), "–");
  assert.equal(providersText(undefined), "–");
  const [suspend, , logout] = memberActions(member(), "x");
  assert.equal(memberResultText(suspend, member(), {}), "minsu 님: 정지했습니다.");
  assert.equal(memberResultText(logout, member(), { revoked: 2 }), "minsu 님을 로그아웃시켰습니다 (세션 2개).");
});
