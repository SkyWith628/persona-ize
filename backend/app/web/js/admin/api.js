// 관리자 화면의 API 호출. 읽기(GET)는 세션 쿠키만으로 하고, 쓰기(보류 다시 시도, 회원관리)는 CSRF 토큰을 싣는다.
// 오류 모양은 {"code","message"}가 기준이고, 통일 전 모양({"detail":{"code","message"}})도 읽는다.

export class ApiError extends Error {
  /**
   * @param {number} status HTTP 상태 (네트워크 실패는 0)
   * @param {string|null} code 서버가 준 오류 코드
   * @param {string} message 사람이 읽는 문장
   */
  constructor(status, code, message) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

/** 오류 응답 본문에서 코드와 문장을 꺼낸다. */
export function readError(status, data) {
  const detail = data && typeof data.detail === "object" && !Array.isArray(data.detail) ? data.detail : null;
  const code = data?.code ?? detail?.code ?? null;
  const message = data?.message ?? detail?.message ?? `요청을 처리하지 못했습니다 (${status}).`;
  return new ApiError(status, typeof code === "string" ? code : null, String(message));
}

/** 요청 설정. 쓰기 요청에는 CSRF 토큰을 싣는다 (쿠키 인증의 쓰기 요청 규칙). body가 있으면 JSON으로 보낸다. */
export function requestInit(method, csrfToken = null, body = undefined) {
  const headers = { Accept: "application/json" };
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const init = { method, credentials: "same-origin", cache: "no-store", headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  return init;
}

/** GET으로 JSON을 읽는다. 실패하면 ApiError를 던진다. */
export function getJson(path) {
  return send(path, requestInit("GET"));
}

/** 본문 없는 POST. 토큰은 `/v1/auth/me`의 `csrf_token`을 쓴다. 실패하면 ApiError를 던진다. */
export function postJson(path, csrfToken) {
  return send(path, requestInit("POST", csrfToken));
}

/** 쓰기 요청(PATCH·POST·DELETE). body는 없어도 된다. 실패하면 ApiError를 던진다. */
export function writeJson(method, path, csrfToken, body = undefined) {
  return send(path, requestInit(method, csrfToken, body));
}

async function send(path, init) {
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(0, "network", "서버에 연결하지 못했습니다. 잠시 뒤에 다시 시도하세요.");
  }
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) throw readError(res.status, data);
  return data;
}
