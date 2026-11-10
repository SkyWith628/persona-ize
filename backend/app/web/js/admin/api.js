// 관리자 화면의 API 호출. 읽기(GET)만 한다 — 세션 쿠키로 인증하고 CSRF 토큰은 필요 없다.
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

/** GET으로 JSON을 읽는다. 실패하면 ApiError를 던진다. */
export async function getJson(path) {
  let res;
  try {
    res = await fetch(path, {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
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
