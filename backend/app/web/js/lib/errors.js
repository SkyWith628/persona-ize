// API 오류를 한 가지 모양으로 맞춘다. 순수 함수만 둔다(테스트: tests/errors.test.js).
//
// 서버 오류 모양(계약 §4, core/http_errors.py):
//   통일 모양     {"code": "...", "message": "..."}  (+ 422는 details[{loc:"body.x",msg}], 5xx는 request_id)
//   하위 호환     같은 본문에 예전 "detail"도 함께 온다: 없는 주소는 "Not Found", 요청 검증은 FastAPI 목록
//   옛 서버       {"detail": {"code","message"}} 또는 {"detail": [...]} 만 오는 경우도 읽는다
//   프록시(Caddy) 413 payload_too_large, 502~504 service_unavailable + Retry-After (배포·재시작 중)

const DEFAULT_MESSAGE = {
  0: "서버에 연결하지 못했어요. 인터넷 연결을 확인해 주세요.",
  401: "로그인이 필요해요.",
  403: "이 작업은 할 수 없어요.",
  404: "찾을 수 없어요.",
  409: "다른 곳에서 먼저 바뀌었어요. 새로 고친 뒤 다시 시도해 주세요.",
  413: "보내는 내용이 너무 커요.",
  422: "입력한 내용을 확인해 주세요.",
  429: "요청이 너무 많아요. 잠시 뒤 다시 시도해 주세요.",
  502: "지금은 처리할 수 없어요. 잠시 뒤 다시 시도해 주세요.",
  503: "지금은 처리할 수 없어요. 잠시 뒤 다시 시도해 주세요.",
  504: "지금은 처리할 수 없어요. 잠시 뒤 다시 시도해 주세요.",
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
  502: "service_unavailable",
  503: "service_unavailable",
  504: "service_unavailable",
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

const SERVER_ERROR_MESSAGE = "서버에 문제가 생겼어요. 잠시 뒤 다시 시도해 주세요.";

function defaultMessage(status) {
  if (DEFAULT_MESSAGE[status]) return DEFAULT_MESSAGE[status];
  if (status >= 500) return SERVER_ERROR_MESSAGE;
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
 * unavailable: 서버에 그 경로가 없다는 뜻(없는 주소의 404, 메서드 불일치 405, 501) — 화면보다 서버가 옛 버전일 때.
 * 없는 주소의 404는 본문에 "detail": "Not Found"가 있고, 자원을 못 찾은 404(상대·대화방 등)에는 없다.
 */
export function normalizeError(status, data, retryAfterHeader = null) {
  const body = data && typeof data === "object" ? data : {};
  const nested = body.detail && typeof body.detail === "object" && !Array.isArray(body.detail) ? body.detail : null;
  const details = toDetails(body);
  const code = body.code ?? nested?.code ?? DEFAULT_CODE[status] ?? (status >= 500 ? "internal_error" : "error");
  let message = body.message ?? nested?.message ?? null;
  if (!message && details.length) message = details.map((d) => d.msg).filter(Boolean).join(" / ");
  const routeMissing = status === 404 && body.detail === "Not Found";
  if (!message) message = defaultMessage(status);
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

/** 잠시 뒤 다시 하면 되는 상태인가(서버·프록시가 일시적으로 못 받는 중: 502·503·504). */
export function isTemporary(err) {
  return Boolean(err && [502, 503, 504].includes(err.status));
}

/** 기다릴 시간을 사람 말로: 45 → "45초", 870 → "약 15분". */
export function formatWait(seconds) {
  const n = Math.max(1, Math.ceil(Number(seconds) || 0));
  if (n < 90) return `${n}초`;
  return `약 ${Math.ceil(n / 60)}분`;
}

/**
 * 화면에 보여 줄 오류 문장.
 * - 잠깐 막힌 상태(429 rate_limited·too_many_attempts, 502~504)는 기다릴 시간(Retry-After)을 사람 말로 알려 준다.
 * - 서버 문제(5xx)에 요청 번호가 있으면 덧붙인다 — 운영자가 로그에서 그 번호로 찾는다.
 */
export function describeError(err, fallback = "잠시 뒤 다시 시도해 주세요.") {
  if (!err) return fallback;
  let message = err.message || fallback;
  if (err.retryAfter) {
    const wait = formatWait(err.retryAfter);
    if (isTemporary(err)) message = `지금은 처리할 수 없어요. ${wait}쯤 뒤에 다시 시도해 주세요.`;
    else if (err.status === 429 && err.code === "too_many_attempts") message = `시도가 너무 많아 잠시 막혔어요. ${wait} 뒤에 다시 시도해 주세요.`;
    else if (err.status === 429 && err.code === "rate_limited") message = `요청이 너무 많아요. ${wait} 뒤에 다시 시도해 주세요.`;
  }
  // 처리되지 않은 서버 오류는 화면의 말투로 알린다(서버 문장은 원인을 담지 않은 고정 문구다)
  if (err.status >= 500 && err.code === "internal_error") message = SERVER_ERROR_MESSAGE;
  if (err.status >= 500 && err.requestId) message += ` (문의할 때 알려 주세요: ${err.requestId})`;
  return message;
}

/** 요청 검증 오류(422)가 특정 필드 때문인지. loc은 "body.condition" 꼴. */
export function hasFieldError(err, field) {
  if (!err || !Array.isArray(err.details)) return false;
  return err.details.some((d) => d.loc.split(".").includes(field));
}
