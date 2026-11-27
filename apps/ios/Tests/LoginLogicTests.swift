import XCTest
@testable import PersonaIze

final class LoginLogicTests: XCTestCase {
    // MARK: 서버 주소 결정

    func testDefaultServerAddsHTTPSToHost() {
        XCTAssertEqual(ServerChoice.defaultServer(host: "43-203-31-7.sslip.io"), "https://43-203-31-7.sslip.io")
        XCTAssertEqual(ServerChoice.defaultServer(host: " example.com\n"), "https://example.com")
    }

    func testDefaultServerIsMissingWhenHostIsEmpty() {
        XCTAssertNil(ServerChoice.defaultServer(host: nil))
        XCTAssertNil(ServerChoice.defaultServer(host: ""))
        XCTAssertNil(ServerChoice.defaultServer(host: "  "))
    }

    func testTypedServerWinsOverDefault() {
        XCTAssertEqual(ServerChoice.resolve(input: " https://demo.ngrok.app ", fallback: "https://default.example"),
                       "https://demo.ngrok.app")
    }

    func testEmptyInputFallsBackToDefault() {
        XCTAssertEqual(ServerChoice.resolve(input: "", fallback: "https://default.example"), "https://default.example")
        XCTAssertEqual(ServerChoice.resolve(input: " \n", fallback: "https://default.example"), "https://default.example")
    }

    func testNoInputAndNoDefaultIsNil() {
        XCTAssertNil(ServerChoice.resolve(input: "", fallback: nil))
        XCTAssertNil(ServerChoice.resolve(input: "   ", fallback: nil))
        XCTAssertEqual(ServerChoice.resolve(input: "https://only.example", fallback: nil), "https://only.example")
    }

    // MARK: 가입·로그인 응답 읽기

    /// 가입 201 `AuthOut` — 쿠키 세션용 값(`csrf_token`)이 있지만 앱은 쓰지 않는다.
    private let authBody = Data(#"""
    {"user_id":"7","username":"mina","is_admin":false,"consented_at":null,"consent_version":null,
     "consent_current":false,"email":null,"display_name":null,"role":"user","status":"active",
     "csrf_token":"csrf-1","expires_at":"2026-12-01T00:00:00Z"}
    """#.utf8)
    /// 토큰 발급 200 `TokenOut`.
    private let tokenBody = Data(#"""
    {"access_token":"tok-1","token_type":"bearer","expires_at":"2026-12-09T00:00:00Z","user_id":"7","username":"mina"}
    """#.utf8)
    private let pendingBody = Data(#"{"status":"pending","username":"mina","email":null}"#.utf8)

    func testToken200StartsSession() throws {
        XCTAssertEqual(try SignInOutcome.read(status: 200, body: tokenBody), .session(token: "tok-1"))
    }

    func testCookieLoginBodyIsNotASession() {
        // 쿠키 로그인 응답(`csrf_token`만 있고 `access_token`이 없다)으로는 로그인하지 않는다.
        XCTAssertThrowsError(try SignInOutcome.read(status: 200, body: authBody))
    }

    func test202IsPendingWhateverTheBodySays() throws {
        // 202에는 토큰이 없다 — 본문이 비었거나 다른 모양이어도 로그인하지 않는다.
        XCTAssertEqual(try SignInOutcome.read(status: 202, body: pendingBody), .pendingApproval)
        XCTAssertEqual(try SignInOutcome.read(status: 202, body: Data()), .pendingApproval)
        XCTAssertEqual(try SignInOutcome.read(status: 202, body: tokenBody), .pendingApproval)
    }

    func testPendingBodyWithoutTokenIsPendingEvenOnOtherStatus() throws {
        XCTAssertEqual(try SignInOutcome.read(status: 201, body: pendingBody), .pendingApproval)
        XCTAssertEqual(try SignInOutcome.read(status: 200, body: pendingBody), .pendingApproval)
    }

    func testSuccessWithoutTokenOrPendingStatusIsAnError() {
        for body in [Data(), Data("{}".utf8), Data(#"{"access_token":""}"#.utf8), Data(#"{"status":"active"}"#.utf8)] {
            XCTAssertThrowsError(try SignInOutcome.read(status: 200, body: body)) { error in
                XCTAssertEqual((error as? ServiceError)?.status, 200)
            }
        }
    }

    func testRegister201CanGoOnToToken() {
        XCTAssertTrue(SignInOutcome.signedUp(status: 201, body: authBody))
        // 본문을 못 읽어도 201이면 계정은 만들어졌다 — 토큰 발급이 결과를 가린다.
        XCTAssertTrue(SignInOutcome.signedUp(status: 201, body: Data()))
    }

    func testRegister202OrPendingBodyStopsBeforeToken() {
        XCTAssertFalse(SignInOutcome.signedUp(status: 202, body: pendingBody))
        XCTAssertFalse(SignInOutcome.signedUp(status: 202, body: Data()))
        XCTAssertFalse(SignInOutcome.signedUp(status: 201, body: pendingBody))
    }

    func testTokenRequestNamesTheMobileClient() {
        XCTAssertEqual(API.tokenRequest(name: "mina", password: "pw-123456"),
                       ["username": "mina", "password": "pw-123456", "client": "mobile"])
    }

    // MARK: Bearer 토큰

    func testBearerHeader() {
        var request = URLRequest(url: URL(string: "https://example.test/v1/contacts")!)
        BearerAuth.apply("tok-1", to: &request)
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer tok-1")
    }

    func testOnlyInvalidTokenChallengeMeansExpired() {
        XCTAssertTrue(BearerAuth.rejected(status: 401, challenge: #"Bearer error="invalid_token""#))
        // 비밀번호 변경·계정 삭제에서 비밀번호가 틀린 401에는 이 헤더가 없다 — 로그아웃시키지 않는다.
        XCTAssertFalse(BearerAuth.rejected(status: 401, challenge: nil))
        XCTAssertFalse(BearerAuth.rejected(status: 401, challenge: ""))
        XCTAssertFalse(BearerAuth.rejected(status: 403, challenge: #"Bearer error="invalid_token""#))
    }

    func testSavedLoginSurvivesEncoding() throws {
        let login = SavedLogin(server: "https://example.test", username: "mina", token: "tok-1")
        XCTAssertEqual(try JSONDecoder().decode(SavedLogin.self, from: JSONEncoder().encode(login)), login)
    }

    @MainActor
    func testClientHandsOutCredentialsOnlyWhenItHasAToken() throws {
        let fresh = try API(server: "https://example.test")
        XCTAssertNil(fresh.keyboardCredentials())
        XCTAssertNil(fresh.savedLogin(username: "mina"))

        let restored = try API(server: "https://example.test", token: "tok-1")
        XCTAssertEqual(restored.keyboardCredentials()?.server, "https://example.test")
        XCTAssertEqual(restored.keyboardCredentials()?.token, "tok-1")
        XCTAssertEqual(restored.savedLogin(username: "mina"),
                       SavedLogin(server: "https://example.test", username: "mina", token: "tok-1"))
    }

    // MARK: 첨삭 조건

    func testDefaultConditionIsAutoLikeTheWeb() {
        XCTAssertEqual(Labels.defaultCondition, "auto")
        XCTAssertEqual(Labels.conditions.first?.0, "auto")
        XCTAssertFalse(Labels.needsPersona("auto"))
        XCTAssertFalse(Labels.needsPersona("A"))
        for condition in ["B", "C", "D"] { XCTAssertTrue(Labels.needsPersona(condition), condition) }
    }

    func testPendingSignupNoticePointsToLogin() {
        XCTAssertEqual(SignInOutcome.pendingNotice, "가입됐어요. 관리자가 승인하면 로그인할 수 있어요.")
    }

    // MARK: 오류 응답 → 오류

    func testLogin403PendingBecomesAccountStateError() {
        let body = Data(#"{"code":"account_pending","message":"관리자 승인을 기다리는 계정입니다. 승인되면 로그인할 수 있습니다."}"#.utf8)
        let error = API.failure(body, status: 403) as? AccountStateError
        XCTAssertEqual(error?.state, .pending)
        XCTAssertEqual(error?.status, 403)
        XCTAssertEqual(error?.message, "관리자 승인을 기다리는 계정입니다. 승인되면 로그인할 수 있습니다.")
    }

    func testLogin403SuspendedBecomesAccountStateError() {
        let body = Data(#"{"code":"account_suspended","message":"정지된 계정입니다. 관리자에게 문의하세요."}"#.utf8)
        XCTAssertEqual((API.failure(body, status: 403) as? AccountStateError)?.state, .suspended)
    }

    func testOtherFailuresKeepServerMessageAndStatus() {
        let cases: [(Int, String, String)] = [
            (401, #"{"code":"unauthorized","message":"이름 또는 비밀번호가 맞지 않습니다."}"#, "이름 또는 비밀번호가 맞지 않습니다."),
            (403, #"{"code":"forbidden","message":"회원가입이 닫혀 있습니다."}"#, "회원가입이 닫혀 있습니다."),
            (403, #"{"code":"invalid_invite","message":"초대 코드가 맞지 않습니다."}"#, "초대 코드가 맞지 않습니다."),
            (409, #"{"code":"conflict","message":"이미 사용 중인 이름입니다."}"#, "이미 사용 중인 이름입니다."),
            (422, #"{"code":"invalid_input","message":"비밀번호는 8자 이상이어야 합니다."}"#, "비밀번호는 8자 이상이어야 합니다."),
            (422, #"{"code":"consent_required","message":"약관과 개인정보 처리 안내에 동의해야 가입할 수 있습니다."}"#,
             "약관과 개인정보 처리 안내에 동의해야 가입할 수 있습니다."),
            (429, #"{"code":"too_many_attempts","message":"로그인 시도가 너무 많습니다. 30초 뒤에 다시 시도하세요."}"#,
             "로그인 시도가 너무 많습니다. 30초 뒤에 다시 시도하세요."),
        ]
        for (status, body, message) in cases {
            let error = API.failure(Data(body.utf8), status: status)
            XCTAssertFalse(error is AccountStateError, body)
            XCTAssertEqual((error as? ServiceError)?.status, status, body)
            XCTAssertEqual((error as? ServiceError)?.message, message, body)
        }
    }

    func testAccountStateCodeOutsideOf403IsAnOrdinaryFailure() {
        let body = Data(#"{"code":"account_pending","message":"승인 대기"}"#.utf8)
        XCTAssertTrue(API.failure(body, status: 500) is ServiceError)
    }

    func testUnreadableFailureBodyFallsBackToGenericMessage() {
        XCTAssertEqual((API.failure(Data("<html>".utf8), status: 502) as? ServiceError)?.message, "서버 응답을 확인해 주세요.")
    }

    // MARK: 계정 상태 안내

    func testPendingCodeMapsToApprovalNotice() {
        let state = AccountState(code: "account_pending")
        XCTAssertEqual(state, .pending)
        XCTAssertEqual(state?.notice, "아직 승인 전이에요. 관리자가 승인하면 로그인할 수 있어요.")
    }

    func testSuspendedCodeMapsToContactAdminNotice() {
        let state = AccountState(code: "account_suspended")
        XCTAssertEqual(state, .suspended)
        XCTAssertEqual(state?.notice, "정지된 계정이에요. 관리자에게 문의해 주세요.")
    }

    func testOtherCodesAreNotAccountStates() {
        XCTAssertNil(AccountState(code: nil))
        XCTAssertNil(AccountState(code: ""))
        XCTAssertNil(AccountState(code: "invalid_credentials"))
    }

    func testAccountStateIsReadFromErrorBody() {
        XCTAssertEqual(AccountState.from(Data(#"{"code":"account_pending","message":"승인 대기"}"#.utf8)), .pending)
        XCTAssertEqual(AccountState.from(Data(#"{"code":"account_suspended","message":"정지"}"#.utf8)), .suspended)
        XCTAssertNil(AccountState.from(Data(#"{"code":"not_found","message":"없음"}"#.utf8)))
        XCTAssertNil(AccountState.from(Data(#"{"detail":[{"msg":"too short"}]}"#.utf8)))
        XCTAssertNil(AccountState.from(Data()))
    }

    func testAccountStateErrorKeepsServerMessage() {
        let error = AccountStateError(state: .pending, message: "승인 대기 중인 계정입니다.", status: 403)
        XCTAssertEqual(error.localizedDescription, "승인 대기 중인 계정입니다.")
    }
}

// MARK: - 토큰 만료 처리 (가짜 전송)

/// 요청을 네트워크로 보내지 않고 `StubTransport.respond`로 정한 응답을 돌려준다.
/// 받은 요청은 `requests`에 남는다 (Authorization 헤더 확인용).
final class StubTransport: URLProtocol {
    enum Reply {
        case http(status: Int, headers: [String: String], body: Data)
        case failure(URLError.Code)
    }

    private static let lock = NSLock()
    private static var reply: Reply = .failure(.notConnectedToInternet)
    private static var seen: [URLRequest] = []

    static func respond(_ next: Reply) {
        lock.lock(); defer { lock.unlock() }
        reply = next
    }

    static var requests: [URLRequest] {
        lock.lock(); defer { lock.unlock() }
        return seen
    }

    static func reset() {
        lock.lock(); defer { lock.unlock() }
        reply = .failure(.notConnectedToInternet)
        seen = []
    }

    static func configuration() -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubTransport.self]
        return configuration
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.lock.lock()
        Self.seen.append(request)
        let reply = Self.reply
        Self.lock.unlock()
        switch reply {
        case .http(let status, let headers, let body):
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
                                           headerFields: headers)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: body)
            client?.urlProtocolDidFinishLoading(self)
        case .failure(let code):
            client?.urlProtocol(self, didFailWithError: URLError(code))
        }
    }

    override func stopLoading() {}
}

@MainActor
final class TokenExpiryTests: XCTestCase {
    private let server = "https://expiry.test"
    private let rejected = ["WWW-Authenticate": #"Bearer error="invalid_token""#]
    private let failureBody = Data(#"{"code":"unauthorized","message":"로그인이 필요해요."}"#.utf8)

    override func setUp() async throws {
        StubTransport.reset()
        Self.clearDevice()
    }

    override func tearDown() async throws {
        StubTransport.reset()
        Self.clearDevice()
    }

    private static func clearDevice() {
        TokenStore.clear()
        KeyboardLink.clear()
        try? KeyboardMessage.clear()
    }

    private func client(token: String = "t1") throws -> API {
        try API(server: server, token: token, configuration: StubTransport.configuration())
    }

    private func authorization(_ index: Int) -> String? {
        StubTransport.requests[index].value(forHTTPHeaderField: "Authorization")
    }

    // MARK: API

    func testInvalidTokenChallengeClearsTokenAndCallsOnExpiredOnce() async throws {
        let api = try client()
        var expired = 0
        api.onExpired = { expired += 1 }
        StubTransport.respond(.http(status: 401, headers: rejected, body: failureBody))

        do {
            _ = try await api.contacts()
            XCTFail("401은 오류여야 한다")
        } catch let error as ServiceError {
            XCTAssertEqual(error.status, 401)
        }
        XCTAssertEqual(expired, 1)
        XCTAssertEqual(authorization(0), "Bearer t1")
        XCTAssertNil(api.keyboardCredentials(), "거절된 토큰은 비운다")
        XCTAssertNil(api.savedLogin(username: "mina"))

        // 비운 토큰은 다시 보내지 않고, 같은 401이 또 와도 다시 부르지 않는다.
        _ = try? await api.contacts()
        XCTAssertEqual(StubTransport.requests.count, 2)
        XCTAssertNil(authorization(1))
        XCTAssertEqual(expired, 1)
    }

    func testOtherFailuresKeepTokenAndDoNotExpire() async throws {
        let cases: [(Int, [String: String])] = [
            (401, [:]),                                 // 헤더 없는 401
            (401, ["WWW-Authenticate": "Bearer"]),      // invalid_token이 아닌 도전
            (403, rejected),                            // 403은 만료가 아니다
            (500, [:]),
        ]
        for (status, headers) in cases {
            StubTransport.reset()
            let api = try client()
            var expired = 0
            api.onExpired = { expired += 1 }
            StubTransport.respond(.http(status: status, headers: headers, body: failureBody))

            do {
                _ = try await api.contacts()
                XCTFail("\(status)은 오류여야 한다")
            } catch let error as ServiceError {
                XCTAssertEqual(error.status, status)
            }
            XCTAssertEqual(expired, 0, "\(status) \(headers)")
            XCTAssertEqual(api.keyboardCredentials()?.token, "t1", "\(status) \(headers)")
            _ = try? await api.contacts()
            XCTAssertEqual(authorization(1), "Bearer t1", "토큰을 그대로 계속 보낸다")
        }
    }

    func testConnectionFailureDoesNotExpire() async throws {
        let api = try client()
        var expired = 0
        api.onExpired = { expired += 1 }
        StubTransport.respond(.failure(.cannotConnectToHost))

        do {
            _ = try await api.contacts()
            XCTFail("연결 실패는 오류여야 한다")
        } catch {
            XCTAssertTrue(error is URLError)
        }
        XCTAssertEqual(expired, 0)
        XCTAssertEqual(api.keyboardCredentials()?.token, "t1")
    }

    // MARK: Session.restore

    private func saveLogin() throws -> SavedLogin {
        let saved = SavedLogin(server: server, username: "mina", token: "t1")
        try TokenStore.save(saved)
        XCTAssertEqual(TokenStore.load(), saved, "테스트 호스트에서 키체인 왕복이 돼야 한다")
        return saved
    }

    func testRestoreWithRejectedTokenEndsAndShowsExpiredNotice() async throws {
        _ = try saveLogin()
        StubTransport.respond(.http(status: 401, headers: rejected, body: failureBody))
        let session = Session(transport: StubTransport.configuration)

        await session.restore()

        XCTAssertEqual(StubTransport.requests.count, 1)
        XCTAssertEqual(authorization(0), "Bearer t1")
        XCTAssertNil(session.api)
        XCTAssertFalse(session.restoring)
        XCTAssertEqual(session.notice, Session.expiredNotice)
        XCTAssertNil(TokenStore.load(), "거절된 토큰은 기기에서 지운다")
        XCTAssertNil(KeyboardLink.load())
    }

    func testRestoreOfflineKeepsTokenAndShowsOfflineNotice() async throws {
        let saved = try saveLogin()
        StubTransport.respond(.failure(.notConnectedToInternet))
        let session = Session(transport: StubTransport.configuration)

        await session.restore()

        XCTAssertNil(session.api)
        XCTAssertFalse(session.restoring)
        XCTAssertEqual(session.notice, Session.offlineNotice)
        XCTAssertEqual(TokenStore.load(), saved, "연결 문제면 토큰을 남겨 다음에 다시 쓴다")
    }

    func testRestoreWithoutSavedLoginSendsNothing() async {
        let session = Session(transport: StubTransport.configuration)
        await session.restore()
        XCTAssertTrue(StubTransport.requests.isEmpty)
        XCTAssertNil(session.api)
        XCTAssertFalse(session.restoring)
        XCTAssertEqual(session.notice, "")
    }

    // MARK: Session.expire

    func testLaterRejectionExpiresAndClearsKeyboardLinkAndPreparedMessage() async throws {
        _ = try saveLogin()
        let json = ["Content-Type": "application/json"]
        StubTransport.respond(.http(status: 200, headers: json, body: Data("[]".utf8)))
        let session = Session(transport: StubTransport.configuration)
        await session.restore()
        let api = try XCTUnwrap(session.api, "저장된 토큰으로 되살아나야 한다")
        XCTAssertEqual(session.username, "mina")
        XCTAssertEqual(session.notice, "")

        // 키보드 확장과 나누는 앱 그룹 파일이 테스트 호스트에서도 왕복돼야 이 검사가 뜻이 있다.
        XCTAssertEqual(KeyboardLink.load()?.token, "t1", "start가 키보드 링크를 쓴다")
        try KeyboardMessage.save("내일 회의는 3시예요.")
        XCTAssertEqual(KeyboardMessage.read(), "내일 회의는 3시예요.")

        // 이후 요청에서 토큰이 거절되면 onExpired → expire.
        StubTransport.respond(.http(status: 401, headers: rejected, body: failureBody))
        _ = try? await api.contacts()

        XCTAssertNil(session.api)
        XCTAssertEqual(session.notice, Session.expiredNotice)
        XCTAssertEqual(session.username, "")
        XCTAssertTrue(session.contacts.isEmpty)
        XCTAssertNil(KeyboardLink.load(), "키보드가 거절된 토큰을 계속 쓰지 않게 지운다")
        XCTAssertNil(KeyboardMessage.read(), "준비한 문장도 지운다")
        XCTAssertNil(TokenStore.load())
    }

    func testExpireWithoutLoginDoesNothing() {
        let session = Session(transport: StubTransport.configuration)
        session.expire()
        XCTAssertEqual(session.notice, "")
    }
}
