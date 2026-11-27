import Foundation
import Security

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
    /// 말투 분석(묶음 생성) 상태: processing · completed · failed. 예전 서버 응답에는 없을 수 있다.
    let status: String?
    let imported: Int
    let duplicates: Int
    let episodes: Int
    let graph_status: String
    let graph_episodes: Int
    let graph_error: String?
    /// 서버가 따르지 않은 화자 지정. 원문 가져오기(`imports/raw`) 응답에만 채워지고,
    /// `GET /v1/imports/{id}`에서는 늘 빈 목록이며 예전 서버 응답에는 없다.
    let ignored_speaker_roles: [IgnoredSpeakerRole]?

    var ignoredRoles: [IgnoredSpeakerRole] { ignored_speaker_roles ?? [] }
    var graphActive: Bool { graph_status == "pending" || graph_status == "running" }
    /// 말투 분석이 아직 도는 중인가. 큰 파일은 가져오기 응답이 먼저 오고 분석은 뒤에서 끝난다.
    var styleActive: Bool { status == "processing" }
    /// 서버에서 아직 진행 중인 일이 있어 다시 확인해야 하는가.
    var active: Bool { styleActive || graphActive }
}

/// 이미 저장된 메시지의 화자를 다른 역할이나 제외로 지정해 서버가 따르지 않은 한 건.
/// `requested_role`은 me·contact·skip, `stored_role`은 me·contact. 모양이 다른 항목은 안내에서 뺀다.
struct IgnoredSpeakerRole: Decodable, Equatable {
    let name: String?
    let requested_role: String?
    let stored_role: String?
    let messages: Int?
}

/// 가져오기 결과에서 "화자 지정을 따르지 않았다"를 알리는 안내 (PC 웹 `importResultView`와 같은 취지).
/// 하나면 한 문단(`head`), 둘 이상이면 머리말(`head`) · 화자마다 한 줄(`items`, 5명까지) · `more`("외 N건") · 해법(`tail`).
/// 앱에는 화자별로 고치는 화면이 없어 해법은 PC 웹을 가리킨다.
struct IgnoredRolesNote: Equatable {
    struct Item: Equatable {
        let name: String
        let detail: String
    }

    let head: String
    let items: [Item]
    let more: String?
    let tail: String?

    /// 화면에 이름을 적는 화자 수.
    static let shown = 5
    static let kept = "이미 가져온 메시지의 화자는 바뀌지 않았어요"
    static let skippedKept = "이미 가져온 대화와 같은 내용이라 건너뛰었어요. 전에 저장한 메시지는 그대로 있고, 화자도 바뀌지 않았어요"
    static let how = "바꾸거나 빼려면 PC 웹에서 상대 화면의 과거 대화에 있는 '화자별로 고치기'를 써 주세요."
    static let noDelete = " 대화방을 지우지 않아도 돼요."

    private static let roleWords = ["me": "나", "contact": "상대", "skip": "제외"]

    private static func number(_ value: Int) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.locale = Locale(identifier: "ko_KR")
        return formatter.string(from: NSNumber(value: value)) ?? String(value)
    }

    /// 모양이 맞는 항목만 화면 글로 바꾼다.
    private static func line(_ role: IgnoredSpeakerRole) -> Item? {
        guard let name = role.name, !name.isEmpty,
              let requested = roleWords[role.requested_role ?? ""],
              let stored = roleWords[role.stored_role ?? ""] else { return nil }
        let count = number(role.messages ?? 0)
        return Item(name: name, detail: "'\(requested)'로 골랐지만 '\(stored)'로 저장된 메시지 \(count)개")
    }

    private static func inline(_ items: [Item], more: String?) -> String {
        let names = items.map { "\($0.name)(\($0.detail))" }.joined(separator: ", ")
        guard let more else { return names }
        return names + " " + more
    }

    /// `imported`는 이번에 새로 저장한 메시지 수. 0건이면 건너뛴 사정과 "대화방을 지우지 않아도 된다"를 함께 알린다.
    /// 따르지 않은 지정이 없으면 nil.
    static func make(imported: Int, ignored: [IgnoredSpeakerRole]) -> IgnoredRolesNote? {
        let all = ignored.compactMap { line($0) }
        guard !all.isEmpty else { return nil }
        let items = Array(all.prefix(shown))
        let rest = all.count - items.count
        let more: String? = rest > 0 ? "외 \(number(rest))건" : nil
        let nothingNew = imported == 0
        let lead = nothingNew ? skippedKept : kept
        let solution = nothingNew ? how + noDelete : how
        if items.count + (more == nil ? 0 : 1) > 1 {
            return IgnoredRolesNote(head: lead + ".", items: items, more: more, tail: solution)
        }
        let list = inline(items, more: more)
        return IgnoredRolesNote(head: "\(lead): \(list). \(solution)", items: [], more: nil, tail: nil)
    }

    /// 안내 전체를 한 문장으로 (화면 낭독용): "머리말 이름(…), 이름(…) 외 N건. 해법"
    var text: String {
        guard !items.isEmpty else { return head }
        let list = Self.inline(items, more: more)
        return "\(head) \(list). " + (tail ?? "")
    }
}

/// 원문 미리보기(`POST /v1/imports/parse`) 응답에서 앱이 쓰는 부분: 화자와 서버가 추천한 역할.
struct ImportPreview: Decodable {
    struct Speaker: Decodable, Equatable {
        let name: String
        let count: Int
        let suggested_role: String?
    }

    let speakers: [Speaker]
}

/// 원문 가져오기(`POST /v1/conversations/{id}/imports/raw`): 서버가 원문을 읽는다 (카카오톡 내보내기).
struct RawImportPayload: Encodable, Equatable {
    let format: String
    let text: String
    let speaker_map: [String: String]
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
/// - 로그인: `POST /v1/auth/token` 200 `TokenOut` — 본문의 `access_token`. 쿠키는 내려오지 않는다.
/// - 가입: `POST /v1/auth/register` 201(계정이 바로 쓸 수 있다 — 응답에 토큰은 없어 이어서 토큰을 받는다)
///   또는 승인제 202 `{"status": "pending", "username", "email"}`.
enum SignInOutcome: Equatable {
    /// 로그인됐다. 이후 요청의 `Authorization: Bearer`에 실을 토큰.
    case session(token: String)
    /// 계정은 만들어졌지만 로그인되지 않았다. 관리자가 승인한 뒤 로그인한다.
    case pendingApproval

    /// 승인제 서버에 가입한 직후 로그인 화면에 보일 안내.
    static let pendingNotice = "가입됐어요. 관리자가 승인하면 로그인할 수 있어요."

    /// 토큰 발급의 2xx 응답을 읽는다. 202는 본문과 무관하게 승인 대기다(토큰이 없다).
    /// 그 밖에는 `access_token`이 있어야 로그인이고, 토큰 없이 `status: pending`이면 승인 대기로 본다.
    static func read(status: Int, body: Data) throws -> SignInOutcome {
        struct Body: Decodable { let access_token: String?; let status: String? }
        let parsed = try? JSONDecoder().decode(Body.self, from: body)
        if status == 202 { return .pendingApproval }
        if let token = parsed?.access_token, !token.isEmpty { return .session(token: token) }
        if parsed?.status == "pending" { return .pendingApproval }
        throw ServiceError(message: "서버 응답을 확인해 주세요.", status: status)
    }

    /// 가입의 2xx 응답: 바로 로그인할 수 있는 계정이면 참, 승인을 기다리면 거짓.
    /// 202이거나 본문이 `status: pending`이면 승인 대기다 — 이때는 토큰을 받으러 가지 않는다(403이 된다).
    static func signedUp(status: Int, body: Data) -> Bool {
        struct Body: Decodable { let status: String? }
        if status == 202 { return false }
        let parsed = try? JSONDecoder().decode(Body.self, from: body)
        return parsed?.status != "pending"
    }
}

/// 기기에 남기는 로그인 한 건: 토큰과 그 토큰을 준 서버·아이디. 비밀번호는 없다.
/// 토큰은 받은 서버에만 보낸다 — 주소가 바뀌면 이 값은 버리고 새로 로그인한다.
struct SavedLogin: Codable, Equatable {
    let server: String
    let username: String
    let token: String
}

/// 로그인 토큰을 키체인에 둔다 (이 앱만 읽는다 — 키보드 확장과 나누는 접근 그룹은 쓰지 않는다).
/// 잠금이 풀린 동안만 읽을 수 있고 다른 기기로 옮겨지지 않는다(`WhenUnlockedThisDeviceOnly`).
enum TokenStore {
    /// 키체인 항목은 앱을 지워도 남는다. 이 표시(UserDefaults, 앱과 함께 지워진다)가 없으면
    /// 남은 항목을 쓰지 않고 지운다.
    private static let marker = "loginSaved"

    private static var base: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "PersonaIze.login",
         kSecAttrAccount as String: "bearer"]
    }

    static func load() -> SavedLogin? {
        guard UserDefaults.standard.bool(forKey: marker) else {
            _ = SecItemDelete(base as CFDictionary)
            return nil
        }
        var find = base
        find[kSecReturnData as String] = true
        find[kSecMatchLimit as String] = kSecMatchLimitOne
        var found: CFTypeRef?
        guard SecItemCopyMatching(find as CFDictionary, &found) == errSecSuccess,
              let data = found as? Data else { return nil }
        return try? JSONDecoder().decode(SavedLogin.self, from: data)
    }

    static func save(_ login: SavedLogin) throws {
        let data = try JSONEncoder().encode(login)
        var item = base
        _ = SecItemDelete(item as CFDictionary)
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else {
            throw ServiceError(message: "로그인 상태를 기기에 저장하지 못했어요.")
        }
        UserDefaults.standard.set(true, forKey: marker)
    }

    static func clear() {
        _ = SecItemDelete(base as CFDictionary)
        UserDefaults.standard.removeObject(forKey: marker)
    }
}

@MainActor
final class API {
    private struct Empty: Encodable {}

    private let base: URL
    private let session: URLSession
    private var token: String
    /// 서버가 토큰을 거절했을 때(만료·폐기) 한 번 부른다. 화면을 로그인으로 돌리는 일은 받는 쪽이 한다.
    var onExpired: (@MainActor () -> Void)?

    /// `token`은 기기에 남겨 둔 로그인을 되살릴 때만 준다. 새 로그인은 `signIn`이 채운다.
    /// `configuration`은 단위 테스트가 가짜 전송(`protocolClasses`)을 넣을 때만 바꾼다. 앱은 늘 `.ephemeral`.
    init(server: String, token: String = "", configuration: URLSessionConfiguration = .ephemeral) throws {
        base = try ServerAddress.url(from: server)
        self.token = token
        session = URLSession(configuration: configuration, delegate: NoRedirects(), delegateQueue: nil)
    }

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
        // Bearer 인증은 CSRF 헤더가 필요 없다 (서버 `require_csrf`는 쿠키로 들어온 요청만 검사한다).
        if !token.isEmpty { BearerAuth.apply(token, to: &request) }
        if let body {
            request.httpBody = try JSONEncoder().encode(body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if let key { request.setValue(key.uuidString, forHTTPHeaderField: "Idempotency-Key") }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw ServiceError(message: "서버 응답을 확인해 주세요.") }
        guard (200..<300).contains(http.statusCode) else {
            let challenge = http.value(forHTTPHeaderField: "WWW-Authenticate")
            if !token.isEmpty, BearerAuth.rejected(status: http.statusCode, challenge: challenge) {
                // 만료·폐기된 토큰은 다시 보내지 않는다.
                token = ""
                onExpired?()
            }
            throw Self.failure(data, status: http.statusCode)
        }
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

    /// 키보드 확장에 넘길 연결 값: 서버 주소와 토큰. 로그인 전이면 nil.
    func keyboardCredentials() -> (server: String, token: String)? {
        guard !token.isEmpty else { return nil }
        return (base.absoluteString, token)
    }

    /// 키체인에 남길 로그인. 로그인 전이면 nil.
    func savedLogin(username: String) -> SavedLogin? {
        guard !token.isEmpty else { return nil }
        return SavedLogin(server: base.absoluteString, username: username, token: token)
    }

    private func decode<T: Decodable>(_ data: Data) throws -> T { try JSONDecoder().decode(T.self, from: data) }

    // MARK: 계정

    /// 가입(`register`) 또는 로그인. 로그인되면 토큰을 기억한다(`POST /v1/auth/token`, 쿠키 없음).
    /// 가입 응답에는 토큰이 없어 가입 → 토큰 발급 두 단계다. 승인제 서버의 가입(202)은 `.pendingApproval` —
    /// 로그인되지 않았으니 다른 요청을 보내면 안 된다.
    /// 가입할 때만 초대 코드와 동의를 함께 보낸다 — 초대제·동의 필수 서버는 없으면 가입을 거절한다.
    func signIn(name: String, password: String, register: Bool,
                inviteCode: String = "", consent: Bool = false) async throws -> SignInOutcome {
        struct Signup: Encodable {
            let username: String
            let password: String
            let invite_code: String?
            let consent: Bool
        }
        if register {
            let body = Signup(username: name, password: password,
                              invite_code: inviteCode.isEmpty ? nil : inviteCode, consent: consent)
            let created = try await exchange("POST", "v1/auth/register", body: body)
            guard SignInOutcome.signedUp(status: created.status, body: created.data) else { return .pendingApproval }
            // 가입 201은 쿠키 세션을 연다. 앱은 쿠키를 쓰지 않으니 그 세션을 닫고(쿠키만 실린 로그아웃) 쿠키를 지운다.
            _ = try? await send("POST", "v1/auth/logout")
            dropCookies()
        }
        let credentials = Self.tokenRequest(name: name, password: password)
        let issued = try await exchange("POST", "v1/auth/token", body: credentials)
        let outcome = try SignInOutcome.read(status: issued.status, body: issued.data)
        if case .session(let value) = outcome { token = value }
        return outcome
    }

    /// 토큰 발급 요청 본문. `client`는 서버 기록용 값이다(mobile · desktop · api).
    nonisolated static func tokenRequest(name: String, password: String) -> [String: String] {
        ["username": name, "password": password, "client": "mobile"]
    }

    private func dropCookies() {
        guard let storage = session.configuration.httpCookieStorage else { return }
        for cookie in storage.cookies ?? [] { storage.deleteCookie(cookie) }
    }

    /// 서버에서 이 토큰을 폐기한다. 성공하면 더는 보내지 않는다.
    func logout() async throws {
        _ = try await send("POST", "v1/auth/logout")
        token = ""
    }

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

    /// 서버 파서로 원문을 읽어 본다 (저장 없음). 읽지 못하면 서버 문구가 오류로 온다.
    func previewImport(text: String, format: String) async throws -> ImportPreview {
        let body = ["format": format, "text": text]
        return try decode(await send("POST", "v1/imports/parse", body: body))
    }

    func importRaw(conversation: String, payload: RawImportPayload) async throws -> ImportJob {
        try decode(await send("POST", "v1/conversations/\(conversation)/imports/raw", body: payload))
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
