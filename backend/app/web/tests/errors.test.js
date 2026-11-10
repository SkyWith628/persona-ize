import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiError, hasFieldError, isMissingEndpoint, isUnavailable, normalizeError } from "../js/lib/errors.js";

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
  const missing = normalizeError(404, { detail: "Not Found" });
  assert.ok(isUnavailable(missing));
  assert.equal(missing.message, "찾을 수 없어요.");
  assert.ok(isUnavailable(normalizeError(405, { detail: "Method Not Allowed" })));
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
  assert.equal(normalizeError(503, null).code, "model_unavailable");
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
