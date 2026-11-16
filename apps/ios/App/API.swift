import Foundation

struct Contact: Decodable, Identifiable, Hashable {
    let id: String
    let alias: String
    let relationship_type: String
    let persona_version: Int?
}

/// 서버 `PersonaSettings`와 같은 모양. 수치 축은 1~5.
struct PersonaSettings: Codable, Equatable {
    var formality = 3
    var friendliness = 3
    var directness = 3
    var length = "same"
    var emoji = "none"
    var address_term: String?
    var preferred_phrases: [String] = []
    var forbidden_phrases: [String] = []
    var notes: String?
}

struct Persona: Decodable {
    let version: Int
    let settings: PersonaSettings
}

struct Conversation: Decodable, Identifiable, Hashable {
    let id: String
    let title: String
    let me_name: String?
}

struct Message: Decodable, Identifiable {
    let id: String
    let speaker_role: String
    let sent_at: String
    let text: String
}

struct ImportMessage: Codable, Equatable {
    let id: String
    let speaker: String
    let sent_at: String
    let text: String
}

struct ImportPayload: Encodable, Equatable {
    let speaker_map: [String: String]
    let messages: [ImportMessage]
}

struct ImportJob: Decodable {
    let id: String
    let imported: Int
    let duplicates: Int
    let episodes: Int
    let graph_status: String
    let graph_episodes: Int
    let graph_error: String?

    var graphActive: Bool { graph_status == "pending" || graph_status == "running" }
}

@MainActor
final class API {
    private struct Empty: Encodable {}

    private let base: URL
    private let session = URLSession(configuration: .ephemeral, delegate: NoRedirects(), delegateQueue: nil)
    private var csrf = ""

    init(server: String) throws { base = try ServerAddress.url(from: server) }

    deinit { session.invalidateAndCancel() }

    // MARK: 전송

    private func send<Body: Encodable>(_ method: String, _ path: String, query: [URLQueryItem] = [],
                                       body: Body?, key: UUID? = nil) async throws -> Data {
        var url = base.appendingPathComponent(path)
        if !query.isEmpty, var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            parts.queryItems = query
            url = parts.url ?? url
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        if method != "GET", !csrf.isEmpty { request.setValue(csrf, forHTTPHeaderField: "X-CSRF-Token") }
        if let body {
            request.httpBody = try JSONEncoder().encode(body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if let key { request.setValue(key.uuidString, forHTTPHeaderField: "Idempotency-Key") }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw ServiceError(message: "서버 응답을 확인하세요.") }
        guard (200..<300).contains(http.statusCode) else {
            throw ServiceError(message: Self.failureMessage(data, status: http.statusCode), status: http.statusCode)
        }
        return data
    }

    private func send(_ method: String, _ path: String, query: [URLQueryItem] = []) async throws -> Data {
        try await send(method, path, query: query, body: Empty?.none)
    }

    static func failureMessage(_ data: Data, status: Int) -> String { ServiceError.message(from: data, status: status) }

    /// 키보드 확장에 넘길 연결 값: 서버 주소, 세션 쿠키 값, CSRF 토큰. 로그인 전이면 nil.
    func keyboardCredentials() -> (server: String, cookie: String, csrf: String)? {
        let cookies = session.configuration.httpCookieStorage?.cookies(for: base) ?? []
        guard let cookie = cookies.first(where: { $0.name == "pi_session" })?.value, !csrf.isEmpty else { return nil }
        return (base.absoluteString, cookie, csrf)
    }

    private func decode<T: Decodable>(_ data: Data) throws -> T { try JSONDecoder().decode(T.self, from: data) }

    // MARK: 계정

    func signIn(name: String, password: String, register: Bool) async throws {
        struct Auth: Decodable { let csrf_token: String }
        let data = try await send("POST", register ? "v1/auth/register" : "v1/auth/login",
                                  body: ["username": name, "password": password])
        csrf = (try decode(data) as Auth).csrf_token
    }

    func logout() async throws { _ = try await send("POST", "v1/auth/logout") }

    func changePassword(current: String, new: String) async throws {
        _ = try await send("POST", "v1/auth/password", body: ["current_password": current, "new_password": new])
    }

    func deleteAccount(password: String) async throws {
        _ = try await send("DELETE", "v1/auth/me", body: ["password": password])
    }

    func usage() async throws -> Usage { try decode(await send("GET", "v1/usage")) }

    // MARK: 상대·페르소나

    func contacts() async throws -> [Contact] { try decode(await send("GET", "v1/contacts")) }

    func addContact(alias: String, relationship: String) async throws {
        _ = try await send("POST", "v1/contacts", body: ["alias": alias, "relationship_type": relationship])
    }

    func deleteContact(_ id: String) async throws { _ = try await send("DELETE", "v1/contacts/\(id)") }

    /// 저장된 페르소나가 없으면 nil.
    func persona(contact: String) async throws -> Persona? {
        do { return try decode(await send("GET", "v1/contacts/\(contact)/persona")) }
        catch let error as ServiceError where error.status == 404 { return nil }
    }

    func savePersona(contact: String, settings: PersonaSettings, expected: Int) async throws -> Persona {
        struct Put: Encodable { let settings: PersonaSettings; let expected_version: Int }
        return try decode(await send("PUT", "v1/contacts/\(contact)/persona",
                                     body: Put(settings: settings, expected_version: expected)))
    }

    func graph(contact: String) async throws -> ContactGraph {
        try decode(await send("GET", "v1/contacts/\(contact)/graph"))
    }

    // MARK: 과거 대화

    func conversations(contact: String) async throws -> [Conversation] {
        try decode(await send("GET", "v1/conversations", query: [URLQueryItem(name: "contact_id", value: contact)]))
    }

    func addConversation(contact: String, title: String, meName: String) async throws {
        var body = ["contact_id": contact]
        if !title.isEmpty { body["title"] = title }
        if !meName.isEmpty { body["me_name"] = meName }
        _ = try await send("POST", "v1/conversations", body: body)
    }

    func deleteConversation(_ id: String) async throws { _ = try await send("DELETE", "v1/conversations/\(id)") }

    func messages(conversation: String) async throws -> [Message] {
        try decode(await send("GET", "v1/conversations/\(conversation)/messages"))
    }

    func importMessages(conversation: String, payload: ImportPayload) async throws -> ImportJob {
        try decode(await send("POST", "v1/conversations/\(conversation)/imports", body: payload))
    }

    func importJob(_ id: String) async throws -> ImportJob { try decode(await send("GET", "v1/imports/\(id)")) }

    // MARK: 첨삭

    func rewrite(contact: String, draft: String, situation: String, condition: String, key: UUID) async throws -> Rewrite {
        let body = ["contact_id": contact, "draft_text": draft, "situation": situation, "condition": condition]
        return try decode(await send("POST", "v1/rewrites", body: body, key: key))
    }

    func rewrites(contact: String?, limit: Int = 20) async throws -> [Rewrite] {
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let contact { query.append(URLQueryItem(name: "contact_id", value: contact)) }
        return try decode(await send("GET", "v1/rewrites", query: query))
    }

    func feedback(rewrite: String, accepted: Bool, edited: String? = nil, reason: String? = nil) async throws {
        struct Feedback: Encodable { let accepted: Bool; let edited_text: String?; let reason: String? }
        _ = try await send("POST", "v1/rewrites/\(rewrite)/feedback",
                           body: Feedback(accepted: accepted, edited_text: edited, reason: reason))
    }
}
