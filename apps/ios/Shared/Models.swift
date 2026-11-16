import Foundation

// 앱과 키보드 확장이 함께 쓰는 서버 응답 모델과 주소·오류 규칙.

struct Recommendation: Decodable {
    struct Item: Decodable { let text: String }
    enum Status: String, Decodable { case pending, recommended, fallback_original }
    struct Validation: Decodable {
        struct Violation: Decodable { let check: String; let detail: String }
        let violations: [Violation]
    }

    let status: Status
    let result_text: String
    let change_reasons: [String]
    let preserved_items: [Item]
    let warnings: [String]
    let validation: Validation
}

struct Rewrite: Decodable, Identifiable {
    let id: String
    let contact_id: String
    let condition: String
    let created_at: String
    let result: Recommendation
}

struct Usage: Decodable {
    let rewrites: Int
    let rewrite_limit: Int
    let input_tokens: Int
    let output_tokens: Int
}

struct ServiceError: LocalizedError {
    let message: String
    var status = 0
    var errorDescription: String? { message }

    /// 도메인 오류는 `{code, message}`, 입력 검증 오류는 FastAPI 기본형 `{detail: [{msg}]}`로 온다.
    static func message(from data: Data, status: Int) -> String {
        struct Failure: Decodable {
            struct Detail: Decodable { let msg: String }
            let message: String?
            let detail: [Detail]?
        }
        let failure = try? JSONDecoder().decode(Failure.self, from: data)
        if let message = failure?.message, !message.isEmpty { return message }
        if let detail = failure?.detail?.first?.msg { return "입력을 확인하세요: \(detail)" }
        return status == 401 ? "로그인이 만료되었습니다. 다시 로그인하세요." : "서버 응답을 확인하세요."
    }
}

enum ServerAddress {
    /// 사용자 정보·쿼리가 없는 HTTPS 주소만 받는다.
    static func url(from text: String) throws -> URL {
        guard let parts = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              parts.scheme?.lowercased() == "https", let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              let url = parts.url else { throw ServiceError(message: "HTTPS 서버 주소를 확인하세요.") }
        return url
    }
}

/// 리디렉션을 따르지 않는다. 세션 쿠키·CSRF 토큰이 다른 주소로 넘어가지 않게 한다.
final class NoRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

enum ViolationText {
    /// 원문 유지 사유(서버 검증 항목)를 사용자 말로.
    static func title(_ check: String) -> String {
        switch check {
        case "preserve_missing": return "지켜야 할 내용이 빠졌어요"
        case "fact_added": return "초안에 없던 내용이 들어갔어요"
        case "negation_changed": return "가능·불가가 뒤바뀌었어요"
        case "forbidden_phrase": return "금지 표현이 들어갔어요"
        case "refusal": return "모델이 다듬지 않았어요"
        default: return "검증을 통과하지 못했어요"
        }
    }
}
