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

    private let authBody = Data(#"""
    {"user_id":"7","username":"mina","is_admin":false,"consented_at":null,"consent_version":null,
     "consent_current":false,"email":null,"display_name":null,"role":"user","status":"active",
     "csrf_token":"tok-1","expires_at":"2026-12-01T00:00:00Z"}
    """#.utf8)
    private let pendingBody = Data(#"{"status":"pending","username":"mina","email":null}"#.utf8)

    func testRegister201StartsSession() throws {
        XCTAssertEqual(try SignInOutcome.read(status: 201, body: authBody), .session(csrf: "tok-1"))
    }

    func testLogin200StartsSession() throws {
        XCTAssertEqual(try SignInOutcome.read(status: 200, body: authBody), .session(csrf: "tok-1"))
    }

    func testRegister202IsPendingApproval() throws {
        XCTAssertEqual(try SignInOutcome.read(status: 202, body: pendingBody), .pendingApproval)
    }

    func test202IsPendingWhateverTheBodySays() throws {
        // 202에는 쿠키가 없다 — 본문이 비었거나 다른 모양이어도 세션을 시작하지 않는다.
        XCTAssertEqual(try SignInOutcome.read(status: 202, body: Data()), .pendingApproval)
        XCTAssertEqual(try SignInOutcome.read(status: 202, body: authBody), .pendingApproval)
    }

    func testPendingBodyWithoutTokenIsPendingEvenOnOtherStatus() throws {
        XCTAssertEqual(try SignInOutcome.read(status: 201, body: pendingBody), .pendingApproval)
        XCTAssertEqual(try SignInOutcome.read(status: 200, body: pendingBody), .pendingApproval)
    }

    func testSuccessWithoutTokenOrPendingStatusIsAnError() {
        for body in [Data(), Data("{}".utf8), Data(#"{"csrf_token":""}"#.utf8), Data(#"{"status":"active"}"#.utf8)] {
            XCTAssertThrowsError(try SignInOutcome.read(status: 201, body: body)) { error in
                XCTAssertEqual((error as? ServiceError)?.status, 201)
            }
        }
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
