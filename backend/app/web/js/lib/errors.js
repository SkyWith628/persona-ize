// API 오류를 한 가지 모양으로 맞춘다. 순수 함수만 둔다(테스트: tests/errors.test.js).
//
// 서버 오류 모양은 과도기다(계약 §4):
//   통일 모양     {"code": "...", "message": "..."}  (+ 422는 details[{loc,msg}], 5xx는 request_id)
//   옛 401        {"detail": {"code": "...", "message": "..."}}
//   FastAPI 기본  {"detail": [{"loc": [...], "msg": "..."}]} (요청 검증), {"detail": "Not Found"} (없는 경로)
// 화면은 셋 다 읽는다.

const DEFAULT_MESSAGE = {
  0: "서버에 연결하지 못했어요. 인터넷 연결을 확인해 주세요.",
  401: "로그인이 필요해요.",
  403: "이 작업은 할 수 없어요.",
  404: "찾을 수 없어요.",
  409: "다른 곳에서 먼저 바뀌었어요. 새로 고친 뒤 다시 시도해 주세요.",
  413: "보내는 내용이 너무 커요.",
  422: "입력한 내용을 확인해 주세요.",
  429: "요청이 너무 많아요. 잠시 뒤 다시 시도해 주세요.",
  503: "지금은 처리할 수 없어요. 잠시 뒤 다시 시도해 주세요.",
};

const DEFAULT_CODE = {
  0: "network_error",
  401: "unauthorized",
  403: "forbidden",
  404: "not_found",
  409: "conflict",
  413: "payload_too_large",
  422: "invalid_input",
  429: "rate_limited",
  503: "model_unavailable",
};

export class ApiError extends Error {
  constructor(info) {
    super(info.message);
    this.name = "ApiError";
    this.status = info.status;
    this.code = info.code;
    this.details = info.details || [];
    this.retryAfter = info.retryAfter ?? null;
    this.requestId = info.requestId ?? null;
    this.unavailable = Boolean(info.unavailable);
  }
}

function defaultMessage(status) {
  if (DEFAULT_MESSAGE[status]) return DEFAULT_MESSAGE[status];
  if (status >= 500) return "서버에 문제가 생겼어요. 잠시 뒤 다시 시도해 주세요.";
  return "요청을 처리하지 못했어요.";
}

function toDetails(data) {
  const raw = Array.isArray(data?.details) ? data.details : Array.isArray(data?.detail) ? data.detail : [];
  return raw
    .filter((d) => d && typeof d === "object")
    .map((d) => ({
      loc: Array.isArray(d.loc) ? d.loc.join(".") : String(d.loc ?? ""),
      msg: String(d.msg ?? ""),
    }));
}

function parseRetryAfter(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 ? Math.ceil(n) : null;
}

/**
 * 응답 상태·본문·Retry-After 헤더 → 오류 정보.
 * unavailable: 서버에 그 경로가 아직 없다는 뜻(없는 경로의 404, 메서드 불일치 405, 501).
 * 자원을 못 찾은 404({"code":"not_found"})와 구분한다.
 */
export function normalizeError(status, data, retryAfterHeader = null) {
  const body = data && typeof data === "object" ? data : {};
  const nested = body.detail && typeof body.detail === "object" && !Array.isArray(body.detail) ? body.detail : null;
  const details = toDetails(body);
  const code = body.code ?? nested?.code ?? DEFAULT_CODE[status] ?? (status >= 500 ? "internal_error" : "error");
  let message = body.message ?? nested?.message ?? null;
  if (!message && details.length) message = details.map((d) => d.msg).filter(Boolean).join(" / ");
  const routeMissing = status === 404 && typeof body.detail === "string" && !body.code;
  if (!message || routeMissing) message = routeMissing ? defaultMessage(404) : message || defaultMessage(status);
  return {
    status,
    code: String(code),
    message: String(message),
    details,
    retryAfter: parseRetryAfter(retryAfterHeader),
    requestId: body.request_id ?? null,
    unavailable: routeMissing || status === 405 || status === 501,
  };
}

/** 서버에 그 기능(경로)이 아직 없는가. */
export function isUnavailable(err) {
  return Boolean(err && err.unavailable);
}

/**
 * 계정 전체에 하나뿐인 경로(/v1/auth/config, /v1/data/summary 등)는 "자원 없음" 404가 있을 수 없으므로
 * 404면 모두 "아직 없는 기능"으로 본다.
 */
export function isMissingEndpoint(err) {
  return Boolean(err && (err.unavailable || err.status === 404));
}

export function isAbort(err) {
  return Boolean(err && err.name === "AbortError");
}

/** 요청 검증 오류(422)가 특정 필드 때문인지. loc은 "body.condition" 꼴. */
export function hasFieldError(err, field) {
  if (!err || !Array.isArray(err.details)) return false;
  return err.details.some((d) => d.loc.split(".").includes(field));
}
