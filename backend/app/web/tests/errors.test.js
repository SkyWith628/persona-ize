import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ApiError,
  describeError,
  formatWait,
  hasFieldError,
  isMissingEndpoint,
  isTemporary,
  isUnavailable,
  normalizeError,
} from "../js/lib/errors.js";

test("통일 오류 모양을 그대로 읽는다", () => {
  const e = normalizeError(429, { code: "usage_exceeded", message: "오늘 한도를 모두 썼습니다." });
  assert.equal(e.code, "usage_exceeded");
  assert.equal(e.message, "오늘 한도를 모두 썼습니다.");
  assert.equal(e.unavailable, false);
});

test("옛 401 모양(detail 안의 code·message)도 읽는다", () => {
  const e = normalizeError(401, { detail: { code: "unauthorized", message: "로그인이 필요합니다." } });
  assert.equal(e.code, "unauthorized");
  assert.equal(e.message, "로그인이 필요합니다.");
});

test("FastAPI 기본 검증 오류(detail 배열)를 details로 바꾼다", () => {
  const e = normalizeError(422, {
    detail: [{ type: "literal_error", loc: ["body", "condition"], msg: "Input should be 'A', 'B', 'C' or 'D'" }],
  });
  assert.equal(e.code, "invalid_input");
  assert.deepEqual(e.details, [{ loc: "body.condition", msg: "Input should be 'A', 'B', 'C' or 'D'" }]);
  assert.ok(hasFieldError(e, "condition"));
  assert.ok(!hasFieldError(e, "draft_text"));
});

test("통일된 422 모양의 details(loc이 문자열)도 필드를 찾는다", () => {
  const e = normalizeError(422, {
    code: "invalid_input",
    message: "입력을 확인하세요.",
    details: [{ loc: "body.password", msg: "8자 이상" }],
  });
  assert.equal(e.message, "입력을 확인하세요.");
  assert.ok(hasFieldError(e, "password"));
});

test("없는 경로의 404와 메서드 불일치 405는 '아직 없는 기능'이다", () => {
  // 옛 서버: detail만
  const old = normalizeError(404, { detail: "Not Found" });
  assert.ok(isUnavailable(old));
  assert.equal(old.message, "찾을 수 없어요.");
  // 지금 서버(core/http_errors.py): 최상위 code·message + 예전 detail도 함께
  const now = normalizeError(404, { code: "not_found", message: "요청한 주소를 찾을 수 없습니다.", detail: "Not Found" });
  assert.ok(isUnavailable(now));
  assert.equal(now.message, "요청한 주소를 찾을 수 없습니다.");
  assert.ok(isUnavailable(normalizeError(405, { code: "method_not_allowed", message: "쓸 수 없는 요청 방식", detail: "Method Not Allowed" })));
});

test("화면 주소가 없을 때의 404(detail이 객체)는 '아직 없는 기능'이 아니다", () => {
  const e = normalizeError(404, { code: "not_found", message: "없는 화면입니다.", detail: { code: "not_found", message: "없는 화면입니다." } });
  assert.ok(!isUnavailable(e));
});

test("지금 서버의 422: details(loc 문자열)와 FastAPI detail 목록이 함께 와도 details를 쓴다", () => {
  const e = normalizeError(422, {
    code: "invalid_input",
    message: "condition: 허용되는 값이 아닙니다 (가능한 값: 'A', 'B', 'C', 'D', 'auto').",
    details: [{ loc: "body.condition", msg: "허용되는 값이 아닙니다" }],
    detail: [{ type: "literal_error", loc: ["body", "condition"], msg: "Input should be 'A'" }],
  });
  assert.deepEqual(e.details, [{ loc: "body.condition", msg: "허용되는 값이 아닙니다" }]);
  assert.ok(hasFieldError(e, "condition"));
  assert.match(e.message, /허용되는 값이 아닙니다/);
});

test("프록시가 만든 502~504는 잠시 뒤 다시 하면 되는 상태다", () => {
  const e = new ApiError(normalizeError(502, { code: "service_unavailable", message: "서버를 다시 시작하는 중입니다." }, "10"));
  assert.ok(isTemporary(e));
  assert.equal(e.retryAfter, 10);
  assert.equal(describeError(e), "지금은 처리할 수 없어요. 10초쯤 뒤에 다시 시도해 주세요.");
  assert.equal(normalizeError(504, null).code, "service_unavailable");
  assert.ok(!isTemporary(new ApiError(normalizeError(500, null))));
});

test("5xx는 요청 번호를 덧붙여 보여 준다", () => {
  const e = new ApiError(normalizeError(500, { code: "internal_error", message: "서버에서 문제가 생겼습니다.", request_id: "a1b2c3d4" }));
  assert.equal(describeError(e), "서버에 문제가 생겼어요. 잠시 뒤 다시 시도해 주세요. (문의할 때 알려 주세요: a1b2c3d4)");
  // 서버가 뜻을 담아 준 5xx(모델 장애 등)는 그 문장을 그대로 쓴다
  const model = new ApiError(normalizeError(503, { code: "model_unavailable", message: "모델이 응답하지 않습니다.", request_id: "r9" }));
  assert.equal(describeError(model), "모델이 응답하지 않습니다. (문의할 때 알려 주세요: r9)");
  const plain = new ApiError(normalizeError(409, { code: "conflict", message: "충돌" }));
  assert.equal(describeError(plain), "충돌");
  assert.equal(describeError(null, "기본"), "기본");
});

test("잠깐 막힌 429는 기다릴 시간을 사람 말로 알려 준다", () => {
  assert.equal(formatWait(1), "1초");
  assert.equal(formatWait(45), "45초");
  assert.equal(formatWait(89), "89초");
  assert.equal(formatWait(90), "약 2분");
  assert.equal(formatWait(870), "약 15분");
  assert.equal(formatWait(0), "1초");
  const locked = new ApiError(
    normalizeError(429, { code: "too_many_attempts", message: "로그인 시도가 너무 많습니다. 870초 뒤에 다시 시도하세요." }, "870"),
  );
  assert.equal(describeError(locked), "시도가 너무 많아 잠시 막혔어요. 약 15분 뒤에 다시 시도해 주세요.");
  const rate = new ApiError(normalizeError(429, { code: "rate_limited", message: "요청이 너무 많습니다. 12초 뒤에 다시 시도하세요." }, "12"));
  assert.equal(describeError(rate), "요청이 너무 많아요. 12초 뒤에 다시 시도해 주세요.");
  // 하루 한도(usage_exceeded·budget_exceeded)는 기다려서 풀리는 것이 아니므로 서버 문장을 그대로 둔다
  const usage = new ApiError(normalizeError(429, { code: "usage_exceeded", message: "오늘 첨삭 요청 한도(3회)를 모두 썼습니다. 내일 다시 시도하세요." }));
  assert.equal(describeError(usage), "오늘 첨삭 요청 한도(3회)를 모두 썼습니다. 내일 다시 시도하세요.");
  // Retry-After가 없으면 서버 문장 그대로
  const bare = new ApiError(normalizeError(429, { code: "rate_limited", message: "요청이 너무 많습니다." }));
  assert.equal(describeError(bare), "요청이 너무 많습니다.");
});

test("자원을 못 찾은 404는 '아직 없는 기능'이 아니다", () => {
  const e = normalizeError(404, { code: "not_found", message: "상대를 찾을 수 없습니다." });
  assert.ok(!isUnavailable(e));
  assert.ok(isMissingEndpoint(e), "계정 전체 경로에서는 404면 없는 기능으로 본다");
  assert.equal(e.message, "상대를 찾을 수 없습니다.");
});

test("Retry-After와 request_id를 담는다", () => {
  const e = normalizeError(429, { code: "rate_limited", message: "잠시 뒤" }, "12");
  assert.equal(e.retryAfter, 12);
  const s = normalizeError(500, { code: "internal_error", message: "문제", request_id: "req-1" });
  assert.equal(s.requestId, "req-1");
});

test("본문이 없거나 JSON이 아니어도 기본 문구를 준다", () => {
  assert.equal(normalizeError(503, null).code, "service_unavailable");
  assert.equal(normalizeError(503, { code: "model_unavailable", message: "모델 장애" }).code, "model_unavailable");
  assert.match(normalizeError(500, null).message, /서버에 문제/);
  assert.equal(normalizeError(0, null).code, "network_error");
  assert.equal(normalizeError(418, "teapot").message, "요청을 처리하지 못했어요.");
});

test("ApiError는 정보를 그대로 가진다", () => {
  const err = new ApiError(normalizeError(409, { code: "conflict", message: "충돌" }));
  assert.ok(err instanceof Error);
  assert.equal(err.status, 409);
  assert.equal(err.code, "conflict");
  assert.equal(err.message, "충돌");
});
