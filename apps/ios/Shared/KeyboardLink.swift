import Foundation

/// 앱이 로그인한 동안 키보드 확장에 넘겨 주는 연결 정보. 앱 그룹 컨테이너에 파일 하나로 둔다.
///
/// 세션 쿠키 값이 들어 있으므로 기기 잠금 중에는 읽을 수 없게 저장하고(`completeFileProtection`),
/// 로그아웃·계정 삭제 때 지운다. 비밀번호는 넣지 않는다.
struct KeyboardLink: Codable, Equatable {
    struct Person: Codable, Equatable, Identifiable {
        let id: String
        let alias: String
        /// 페르소나가 저장돼 있어 첨삭할 수 있는 상대인지.
        let ready: Bool
    }

    var server: String
    var cookie: String
    var csrf: String
    var contacts: [Person]
    /// 마지막에 고른 상대 (키보드는 지금 누구와 대화 중인지 알 수 없다).
    var selected: String
    /// 최근에 첨삭한 상대 id, 최신이 앞.
    var recents: [String]

    private static let name = "keyboard-link.json"

    static func load() -> KeyboardLink? {
        guard let url = try? SharedContainer.file(name), let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(KeyboardLink.self, from: data)
    }

    func save() throws {
        try JSONEncoder().encode(self).write(to: SharedContainer.file(Self.name), options: [.atomic, .completeFileProtection])
    }

    static func clear() {
        guard let url = try? SharedContainer.file(name) else { return }
        try? FileManager.default.removeItem(at: url)
    }

    /// 지금 고른 상대. 없거나 지워졌으면 첨삭할 수 있는 첫 상대.
    var current: Person? {
        contacts.first { $0.id == selected && $0.ready } ?? contacts.first { $0.ready }
    }

    /// 고르는 화면의 순서: 최근 상대 먼저, 나머지는 목록 순.
    var ordered: [Person] {
        let recent = recents.compactMap { id in contacts.first { $0.id == id } }
        return recent + contacts.filter { person in !recent.contains { $0.id == person.id } }
    }

    mutating func choose(_ id: String) {
        selected = id
        recents = Array(([id] + recents.filter { $0 != id }).prefix(3))
    }
}

enum SharedContainer {
    static func file(_ name: String) throws -> URL {
        guard let group = Bundle.main.object(forInfoDictionaryKey: "PersonaAppGroup") as? String,
              let directory = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: group)
        else { throw CocoaError(.fileNoSuchFile) }
        return directory.appendingPathComponent(name)
    }
}

/// 입력창 글자를 추천으로 바꾸는 계획. 키보드는 커서 앞뒤 글자만 읽고 지울 수 있다.
struct DraftReplacement: Equatable {
    /// 커서를 초안 끝으로 옮길 거리 (`adjustTextPosition`은 UTF-16 단위로 센다).
    let forward: Int
    /// 끝에서부터 지울 글자 수 (`deleteBackward` 한 번이 글자 하나).
    let deletions: Int

    /// 요청 때 읽은 초안과 지금 입력창이 같을 때만 계획을 낸다. 그 사이 바뀌었으면 nil.
    static func plan(requested: String, before: String?, after: String?) -> DraftReplacement? {
        let behind = after ?? ""
        guard (before ?? "") + behind == requested, !requested.isEmpty else { return nil }
        return DraftReplacement(forward: behind.utf16.count, deletions: requested.count)
    }
}

/// 키보드 확장이 쓰는 최소 클라이언트. 앱의 세션 쿠키와 CSRF 토큰을 요청마다 직접 붙인다.
struct KeyboardClient {
    let link: KeyboardLink

    private static let session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.timeoutIntervalForRequest = 40
        return URLSession(configuration: configuration, delegate: NoRedirects(), delegateQueue: nil)
    }()

    func request(_ method: String, _ path: String, body: [String: String]? = nil, key: UUID? = nil) throws -> URLRequest {
        var request = URLRequest(url: try ServerAddress.url(from: link.server).appendingPathComponent(path))
        request.httpMethod = method
        request.setValue("pi_session=\(link.cookie)", forHTTPHeaderField: "Cookie")
        if method != "GET" { request.setValue(link.csrf, forHTTPHeaderField: "X-CSRF-Token") }
        if let body {
            request.httpBody = try JSONEncoder().encode(body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if let key { request.setValue(key.uuidString, forHTTPHeaderField: "Idempotency-Key") }
        return request
    }

    private func send(_ request: URLRequest) async throws -> Data {
        let (data, response) = try await Self.session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw ServiceError(message: "서버 응답을 확인해 주세요.") }
        guard (200..<300).contains(http.statusCode) else {
            let message = http.statusCode == 401 ? "로그인이 풀렸어요. Persona_ize 앱에서 다시 로그인해 주세요."
                : ServiceError.message(from: data, status: http.statusCode)
            throw ServiceError(message: message, status: http.statusCode)
        }
        return data
    }

    func rewrite(contact: String, draft: String, key: UUID) async throws -> Rewrite {
        let body = ["contact_id": contact, "draft_text": draft, "condition": "D"]
        return try JSONDecoder().decode(Rewrite.self, from: await send(request("POST", "v1/rewrites", body: body, key: key)))
    }

    /// 넣은 추천을 '받아들임'으로 남긴다. 실패해도 입력에는 영향이 없다.
    func accept(_ rewrite: String) async {
        struct Feedback: Encodable { let accepted = true }
        guard var request = try? request("POST", "v1/rewrites/\(rewrite)/feedback") else { return }
        request.httpBody = try? JSONEncoder().encode(Feedback())
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        _ = try? await send(request)
    }

    func usage() async -> Usage? {
        guard let request = try? request("GET", "v1/usage"), let data = try? await send(request) else { return nil }
        return try? JSONDecoder().decode(Usage.self, from: data)
    }
}
