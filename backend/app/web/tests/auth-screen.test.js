// 첫 화면(로그인·가입·소셜 로그인)과 세션 처리의 순수 규칙 검사. DOM을 쓰지 않는 함수만 본다.
import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, normalizeError } from "../js/lib/errors.js";
import { isPendingSignup, oauthErrorText, safeAuthorizeUrl, splitAuthError } from "../js/session.js";
import { authErrorMessage, authOptions, looksLikeEmail } from "../js/views/landing.js";

const apiError = (status, body) => new ApiError(normalizeError(status, body));

const CONFIG = { allow_register: true, invite_required: false, require_consent: false, loaded: true };
const PROVIDERS = {
  signup_open: true,
  requires_approval: false,
  invite_required: false,
  require_consent: false,
  google: false,
  kakao: false,
};

test("가입 응답: 202 승인 대기는 세션이 아니다", () => {
  assert.equal(isPendingSignup({ status: "pending", username: "hong", email: "hong@example.com" }), true);
  // 로그인된 응답(201)에도 status가 있다 — 토큰이 있으면 세션이다
  assert.equal(isPendingSignup({ status: "active", username: "hong", csrf_token: "t" }), false);
  assert.equal(isPendingSignup({ status: "pending", username: "hong", csrf_token: "t" }), false);
  assert.equal(isPendingSignup({ user_id: "u", username: "hong", csrf_token: "t" }), false); // 옛 서버
  assert.equal(isPendingSignup(null), false);
});

test("auth_error: 주소에서 꺼내고 나머지 쿼리는 남긴다", () => {
  assert.deepEqual(splitAuthError("?auth_error=oauth_denied"), { code: "oauth_denied", search: "" });
  assert.deepEqual(splitAuthError("?a=1&auth_error=email_in_use&b=2"), { code: "email_in_use", search: "?a=1&b=2" });
  assert.deepEqual(splitAuthError("?auth_error="), { code: "oauth_failed", search: "" });
  assert.deepEqual(splitAuthError("?a=1"), { code: null, search: "?a=1" });
  assert.deepEqual(splitAuthError(""), { code: null, search: "" });
});

test("auth_error 코드마다 안내가 있고, 모르는 코드는 일반 실패 문장이다", () => {
  const codes = [
    "oauth_failed",
    "oauth_denied",
    "signup_closed",
    "invalid_invite",
    "consent_required",
    "account_pending",
    "account_suspended",
    "email_in_use",
  ];
  const texts = codes.map(oauthErrorText);
  assert.equal(new Set(texts).size, codes.length); // 코드마다 다른 문장
  for (const text of texts) assert.match(text, /요\.$/);
  assert.match(oauthErrorText("account_pending"), /승인/);
  assert.match(oauthErrorText("account_suspended"), /정지/);
  assert.match(oauthErrorText("invalid_invite"), /초대 코드/);
  assert.match(oauthErrorText("consent_required"), /동의/);
  // 주소에 적힌 글자를 화면에 옮기지 않는다
  assert.equal(oauthErrorText("<b>x</b>"), oauthErrorText("oauth_failed"));
  assert.equal(oauthErrorText("constructor"), oauthErrorText("oauth_failed"));
});

test("소셜 로그인 주소는 http(s)일 때만 쓴다", () => {
  assert.equal(safeAuthorizeUrl("https://accounts.example.com/o/oauth2/auth?state=abc"), "https://accounts.example.com/o/oauth2/auth?state=abc");
  assert.equal(safeAuthorizeUrl("javascript:alert(1)"), null);
  assert.equal(safeAuthorizeUrl("/relative"), null);
  assert.equal(safeAuthorizeUrl(""), null);
  assert.equal(safeAuthorizeUrl(undefined), null);
});

test("화면 구성: providers를 모르는 옛 서버는 예전과 같다(아이디 가입만, 소셜 없음)", () => {
  assert.deepEqual(authOptions({ ...CONFIG, providers: null }), {
    signupOpen: true,
    inviteRequired: false,
    requireConsent: false,
    requiresApproval: false,
    emailSignup: false,
    social: [],
  });
  // 설정을 하나도 못 읽었을 때(store의 기본값): 가입은 열려 있다고 본다
  assert.equal(authOptions({ allow_register: true, loaded: false }).signupOpen, true);
  assert.equal(authOptions(undefined).signupOpen, true);
});

test("화면 구성: 켜진 소셜 로그인만, 승인제·초대 코드·동의를 반영한다", () => {
  const options = authOptions({
    ...CONFIG,
    invite_required: true,
    require_consent: true,
    providers: { ...PROVIDERS, requires_approval: true, invite_required: true, require_consent: true, kakao: true },
  });
  assert.deepEqual(options, {
    signupOpen: true,
    inviteRequired: true,
    requireConsent: true,
    requiresApproval: true,
    emailSignup: true,
    social: ["kakao"],
  });
  assert.deepEqual(authOptions({ ...CONFIG, providers: { ...PROVIDERS, google: true, kakao: true } }).social, ["google", "kakao"]);
  // 가입이 닫힌 서버
  assert.equal(authOptions({ ...CONFIG, allow_register: false, providers: { ...PROVIDERS, signup_open: false } }).signupOpen, false);
  // /config는 못 읽고 /providers만 읽은 경우에는 그 값을 쓴다
  const onlyProviders = authOptions({
    allow_register: true,
    loaded: false,
    providers: { ...PROVIDERS, signup_open: false, invite_required: true },
  });
  assert.deepEqual([onlyProviders.signupOpen, onlyProviders.inviteRequired], [false, true]);
});

test("이메일 모양", () => {
  assert.equal(looksLikeEmail("hong@example.com"), true);
  assert.equal(looksLikeEmail("hong"), false);
  assert.equal(looksLikeEmail("hong@example"), false);
  assert.equal(looksLikeEmail("ho ng@example.com"), false);
  assert.equal(looksLikeEmail("a@b@example.com"), false);
  assert.equal(looksLikeEmail(`${"a".repeat(250)}@example.com`), false);
  assert.equal(looksLikeEmail(null), false);
});

test("로그인·가입 오류 문장", () => {
  const pending = apiError(403, { code: "account_pending", message: "x" });
  assert.match(authErrorMessage(pending, "login"), /아직 승인되지 않은 계정/);
  const suspended = apiError(403, { code: "account_suspended", message: "x" });
  assert.match(authErrorMessage(suspended, "login"), /정지된 계정/);
  assert.match(authErrorMessage(apiError(401, { code: "unauthorized", message: "x" }), "login"), /아이디\(또는 이메일\)나 비밀번호/);
  assert.match(authErrorMessage(apiError(409, { code: "conflict", message: "x" }), "signup"), /이미 가입된 이메일/);
  assert.match(authErrorMessage(apiError(409, { code: "conflict", message: "x" }), "register"), /이미 쓰고 있는 아이디/);
  assert.match(authErrorMessage(apiError(403, { code: "invalid_invite", message: "x" }), "signup"), /초대 코드가 맞지 않아요/);
  assert.match(authErrorMessage(apiError(422, { code: "consent_required", message: "x" }), "register"), /동의해야/);
  // 가입이 닫힌 서버(403 forbidden)
  assert.match(authErrorMessage(apiError(403, { code: "forbidden", message: "x" }), "signup"), /지금은 새로 가입할 수 없어요/);
  // 꺼진 소셜 로그인(404)
  assert.match(authErrorMessage(apiError(404, { code: "not_found", message: "x" }), "social"), /지금 쓸 수 없어요/);
  // 그 밖에는 서버 문장
  assert.equal(
    authErrorMessage(apiError(422, { code: "invalid_input", message: "비밀번호는 8자 이상이어야 합니다." }), "signup"),
    "비밀번호는 8자 이상이어야 합니다.",
  );
  // 화면이 만든 오류(주소를 못 받음)는 그 문장 그대로
  assert.equal(authErrorMessage(new Error("소셜 로그인을 시작하지 못했어요."), "social"), "소셜 로그인을 시작하지 못했어요.");
});
