// 관리자 화면 표시 규칙 검사. 실행: js.sh <이름> test "backend/app/web/tests/*.test.js"
import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, readError } from "../js/admin/api.js";
import {
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
  label,
  num,
  percent,
  rangeText,
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
