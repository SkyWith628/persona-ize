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

/// 로그인에 쓸 서버 주소를 정한다: 사용자가 적은 값이 먼저, 비어 있으면 빌드에 든 기본 주소.
enum ServerChoice {
    /// 빌드 설정(`PERSONA_DEFAULT_SERVER_HOST`)이 Info.plist에 남긴 기본 서버. 없으면 nil.
    static var bundled: String? {
        defaultServer(host: Bundle.main.object(forInfoDictionaryKey: "PersonaDefaultServerHost") as? String)
    }

    /// 호스트 이름을 HTTPS 주소로. 비어 있으면 기본 주소가 없는 빌드다.
    static func defaultServer(host: String?) -> String? {
        let host = (host ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return host.isEmpty ? nil : "https://" + host
    }

    /// 입력값이 있으면 그 값, 비어 있으면 기본 주소, 둘 다 없으면 nil.
    static func resolve(input: String, fallback: String?) -> String? {
        let input = input.trimmingCharacters(in: .whitespacesAndNewlines)
        return input.isEmpty ? fallback : input
    }
}

/// 로그인을 막는 계정 상태의 오류 code (`POST /v1/auth/login` → 403).
/// 서버는 비밀번호가 맞은 뒤에만 이 응답을 주고, 세션은 열지 않는다.
enum AccountState: String {
    case pending = "account_pending"
    case suspended = "account_suspended"

    init?(code: String?) {
        guard let code, let state = AccountState(rawValue: code) else { return nil }
        self = state
    }

    /// 오류 응답 `{code, message}`에서 계정 상태를 읽는다. 다른 code나 다른 모양이면 nil.
    static func from(_ data: Data) -> AccountState? {
        struct Failure: Decodable { let code: String? }
        return AccountState(code: (try? JSONDecoder().decode(Failure.self, from: data))?.code)
    }

    /// 로그인 화면에 보일 안내.
    var notice: String {
        switch self {
        case .pending: return "아직 승인 전이에요. 관리자가 승인하면 로그인할 수 있어요."
        case .suspended: return "정지된 계정이에요. 관리자에게 문의해 주세요."
        }
    }
}

/// 계정 상태로 막힌 요청. 문구는 서버 메시지 그대로이고, 로그인 화면만 `state.notice`로 바꿔 보인다.
struct AccountStateError: LocalizedError {
    let state: AccountState
    let message: String
    let status: Int
    var errorDescription: String? { message }
}

/// 가입·로그인 응답을 읽은 결과 (서버 `routes/auth.py`).
/// - 로그인 200·가입 201: `AuthOut` — 세션 쿠키와 `csrf_token`.
/// - 승인제 가입 202: `{"status": "pending", "username", "email"}` — 쿠키도 CSRF 토큰도 없다.
enum SignInOutcome: Equatable {
    /// 세션이 열렸다. 쓰기 요청에 실을 CSRF 토큰.
    case session(csrf: String)
    /// 계정은 만들어졌지만 로그인되지 않았다. 관리자가 승인한 뒤 로그인한다.
    case pendingApproval

    /// 승인제 서버에 가입한 직후 로그인 화면에 보일 안내.
    static let pendingNotice = "가입됐어요. 관리자가 승인하면 로그인할 수 있어요."

    /// 2xx 응답을 상태 코드와 본문으로 판단한다. 202는 본문과 무관하게 승인 대기다(세션이 없다).
    /// 그 밖에는 CSRF 토큰이 있어야 세션이고, 토큰 없이 `status: pending`이면 승인 대기로 본다.
    static func read(status: Int, body: Data) throws -> SignInOutcome {
        struct Body: Decodable { let csrf_token: String?; let status: String? }
        let parsed = try? JSONDecoder().decode(Body.self, from: body)
        if status == 202 { return .pendingApproval }
        if let csrf = parsed?.csrf_token, !csrf.isEmpty { return .session(csrf: csrf) }
        if parsed?.status == "pending" { return .pendingApproval }
        throw ServiceError(message: "서버 응답을 확인해 주세요.", status: status)
    }
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

    /// 요청을 보내고 2xx 응답의 본문과 상태 코드를 돌려준다. 그 밖의 응답은 `failure`로 바꿔 던진다.
    private func exchange<Body: Encodable>(_ method: String, _ path: String, query: [URLQueryItem] = [],
                                           body: Body?, key: UUID? = nil) async throws -> (data: Data, status: Int) {
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
        guard let http = response as? HTTPURLResponse else { throw ServiceError(message: "서버 응답을 확인해 주세요.") }
        guard (200..<300).contains(http.statusCode) else { throw Self.failure(data, status: http.statusCode) }
        return (data, http.statusCode)
    }

    private func send<Body: Encodable>(_ method: String, _ path: String, query: [URLQueryItem] = [],
                                       body: Body?, key: UUID? = nil) async throws -> Data {
        try await exchange(method, path, query: query, body: body, key: key).data
    }

    private func send(_ method: String, _ path: String, query: [URLQueryItem] = []) async throws -> Data {
        try await send(method, path, query: query, body: Empty?.none)
    }

    nonisolated static func failureMessage(_ data: Data, status: Int) -> String { ServiceError.message(from: data, status: status) }

    /// 2xx가 아닌 응답을 오류로 바꾼다. 403 `account_pending`·`account_suspended`만 `AccountStateError`,
    /// 나머지(401·403 forbidden/invalid_invite·409·422·429 …)는 서버 문구를 실은 `ServiceError`.
    nonisolated static func failure(_ data: Data, status: Int) -> Error {
        let message = failureMessage(data, status: status)
        if status == 403, let state = AccountState.from(data) {
            return AccountStateError(state: state, message: message, status: status)
        }
        return ServiceError(message: message, status: status)
    }

    /// 키보드 확장에 넘길 연결 값: 서버 주소, 세션 쿠키 값, CSRF 토큰. 로그인 전이면 nil.
    func keyboardCredentials() -> (server: String, cookie: String, csrf: String)? {
        let cookies = session.configuration.httpCookieStorage?.cookies(for: base) ?? []
        guard let cookie = cookies.first(where: { $0.name == "pi_session" })?.value, !csrf.isEmpty else { return nil }
        return (base.absoluteString, cookie, csrf)
    }

    private func decode<T: Decodable>(_ data: Data) throws -> T { try JSONDecoder().decode(T.self, from: data) }

    // MARK: 계정

    /// 가입(`register`) 또는 로그인. 세션이 열리면 CSRF 토큰을 기억한다.
    /// 승인제 서버의 가입(202)은 `.pendingApproval` — 세션이 없으니 다른 요청을 보내면 안 된다.
    func signIn(name: String, password: String, register: Bool) async throws -> SignInOutcome {
        let response = try await exchange("POST", register ? "v1/auth/register" : "v1/auth/login",
                                          body: ["username": name, "password": password])
        let outcome = try SignInOutcome.read(status: response.status, body: response.data)
        if case .session(let token) = outcome { csrf = token }
        return outcome
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

    /// `defaultPersona`가 참이면 서버가 관계 유형의 기본 말투로 v1을 함께 만든다 → 바로 다듬을 수 있다 (웹과 같다).
    func addContact(alias: String, relationship: String, defaultPersona: Bool = false) async throws {
        struct Post: Encodable { let alias: String; let relationship_type: String; let use_default_persona: Bool }
        _ = try await send("POST", "v1/contacts",
                           body: Post(alias: alias, relationship_type: relationship, use_default_persona: defaultPersona))
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
