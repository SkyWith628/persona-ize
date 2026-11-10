import Foundation

struct Contact: Decodable, Identifiable {
    let id: String
    let alias: String
    let persona_version: Int?
}

struct Recommendation: Decodable {
    struct Item: Decodable { let text: String }
    enum Status: String, Decodable { case recommended, fallback_original }
    let status: Status
    let result_text: String
    let preserved_items: [Item]
    let warnings: [String]
}

struct ServiceError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

private final class RedirectPolicy: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

final class API {
    private let base: URL
    private let session = URLSession(configuration: .ephemeral, delegate: RedirectPolicy(), delegateQueue: nil)
    private var csrf = ""

    init(server: String) throws {
        guard let parts = URLComponents(string: server.trimmingCharacters(in: .whitespacesAndNewlines)),
              parts.scheme?.lowercased() == "https", let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              let url = parts.url else { throw ServiceError(message: "HTTPS 서버 주소를 확인하세요.") }
        base = url
    }

    deinit { session.invalidateAndCancel() }

    private func request(_ path: String, body: [String: String]? = nil, key: UUID? = nil) async throws -> Data {
        var request = URLRequest(url: base.appendingPathComponent(path))
        if let body {
            request.httpMethod = "POST"
            request.httpBody = try JSONEncoder().encode(body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            if !csrf.isEmpty { request.setValue(csrf, forHTTPHeaderField: "X-CSRF-Token") }
            request.setValue(key?.uuidString, forHTTPHeaderField: "Idempotency-Key")
        }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            struct Failure: Decodable { let message: String }
            throw ServiceError(message: (try? JSONDecoder().decode(Failure.self, from: data))?.message ?? "서버 응답을 확인하세요.")
        }
        return data
    }

    func login(name: String, password: String) async throws -> [Contact] {
        struct Login: Decodable { let csrf_token: String }
        let data = try await request("v1/auth/login", body: ["username": name, "password": password])
        csrf = try JSONDecoder().decode(Login.self, from: data).csrf_token
        return try await JSONDecoder().decode([Contact].self, from: request("v1/contacts"))
    }

    func logout() async throws { _ = try await request("v1/auth/logout", body: [:]) }

    func rewrite(contact: String, draft: String, key: UUID) async throws -> Recommendation {
        struct Envelope: Decodable { let result: Recommendation }
        let data = try await request("v1/rewrites", body: ["contact_id": contact, "draft_text": draft, "condition": "D"], key: key)
        return try JSONDecoder().decode(Envelope.self, from: data).result
    }
}
