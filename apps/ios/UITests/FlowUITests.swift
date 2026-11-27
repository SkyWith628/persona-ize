import XCTest

/// 화면 흐름 검사: 예시 화면 → 가입 → 첫 상대 → 다듬기 → 이력 → 다시 열기(로그인 유지) → 로그아웃.
///
/// 로컬 서버가 떠 있어야 한다(README "화면 흐름 검사"). 주소는 `TEST_RUNNER_PERSONA_UITEST_SERVER`로
/// 바꿀 수 있고 기본은 `https://localhost:8443`이다. 빌드에 든 기본 서버(운영)로는 절대 가지 않게
/// 로컬 주소가 아니면 시작하지 않는다. 계정은 실행마다 새로 만들고 비밀번호는 그 자리에서 지어낸다.
final class FlowUITests: XCTestCase {
    private let wait: TimeInterval = 30
    private let alias = "박 팀장"
    private let draft = "내일 회의 못 갈 것 같아요"

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testSignUpRewriteAndStaySignedIn() throws {
        let server = try localServer()
        let name = "ui" + UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12).lowercased()
        let password = UUID().uuidString

        // 1. 처음 상태에서 연다: 예시 화면 → 건너뛰기 → 로그인 화면.
        var app = launch(server: server, reset: true)
        tap(app.buttons["건너뛰기"])

        // 2. 가입. 가입 응답에는 토큰이 없어 앱이 이어서 토큰을 받는다.
        type(name, into: app.textFields["아이디 (영문·숫자 3~32자)"])
        type(password, into: app.secureTextFields["비밀번호"])
        tap(app.buttons["새 계정으로 가입"])
        dismissPasswordPrompt()

        // 3. 준비 화면: 첫 상대를 만들면 기본 말투가 함께 생긴다. 키보드 안내는 건너뛴다.
        type(alias, into: app.textFields["예: 박 팀장"])
        tap(app.buttons["상대 만들기"])
        tap(app.buttons["나중에 하기"])

        // 4. 작성: 초안을 넣고 다듬는다. 추천이 나오면 '키보드에 준비'가 주 버튼이 된다.
        XCTAssertTrue(app.tabBars.buttons["작성"].waitForExistence(timeout: wait), "탭 화면이 뜨지 않았다")
        type(draft, into: app.textViews["초안"])
        tap(app.buttons["\(alias)에게 맞게"])
        XCTAssertTrue(app.staticTexts["\(alias)에게 맞게 다듬었어요"].waitForExistence(timeout: wait),
                      "다듬은 결과가 나오지 않았다")
        XCTAssertTrue(app.buttons["키보드에 준비"].waitForExistence(timeout: wait))

        // 5. 이력에 방금 다듬은 글이 남는다 (가짜 모델은 초안을 그대로 돌려준다).
        tap(app.tabBars.buttons["이력"])
        let saved = app.staticTexts.containing(NSPredicate(format: "label CONTAINS %@", draft)).firstMatch
        XCTAssertTrue(saved.waitForExistence(timeout: wait), "이력에 다듬은 글이 없다")

        // 6. 완전히 끄고 다시 열면 로그인 화면 없이 들어간다 (키체인의 토큰).
        app.terminate()
        app = launch(server: server, reset: false)
        XCTAssertTrue(app.tabBars.buttons["계정"].waitForExistence(timeout: wait), "다시 열었을 때 로그인이 풀렸다")

        // 7. 로그아웃하면 로그인 화면으로 돌아가고, 다시 열어도 로그인 화면이다.
        tap(app.tabBars.buttons["계정"])
        let logout = app.buttons["로그아웃"]
        XCTAssertTrue(logout.waitForExistence(timeout: wait))
        if !logout.isHittable { app.swipeUp() }
        logout.tap()
        XCTAssertTrue(app.buttons["로그인"].waitForExistence(timeout: wait), "로그아웃 뒤 로그인 화면이 아니다")

        app.terminate()
        app = launch(server: server, reset: false)
        XCTAssertTrue(app.buttons["로그인"].waitForExistence(timeout: wait), "로그아웃 뒤 다시 열었는데 로그인 화면이 아니다")
        XCTAssertFalse(app.tabBars.buttons["작성"].exists)
    }

    // MARK: 도구

    /// 검사에 쓸 서버 주소. 이 기기 안의 주소만 받는다.
    private func localServer() throws -> String {
        let text = ProcessInfo.processInfo.environment["PERSONA_UITEST_SERVER"] ?? "https://localhost:8443"
        let host = URLComponents(string: text)?.host ?? ""
        guard ["localhost", "127.0.0.1", "::1"].contains(host) || host.hasSuffix(".localhost") else {
            throw XCTSkip("화면 흐름 검사는 로컬 서버에서만 돈다: \(host)")
        }
        return text
    }

    /// 앱을 연다. `-server`는 로그인 화면의 서버 주소 설정을 이번 실행 동안 덮어쓴다.
    private func launch(server: String, reset: Bool) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-server", server] + (reset ? ["-uiTestReset"] : [])
        app.launch()
        return app
    }

    private func tap(_ element: XCUIElement, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertTrue(element.waitForExistence(timeout: wait), "화면에 없다: \(element)", file: file, line: line)
        element.tap()
    }

    private func type(_ text: String, into element: XCUIElement, file: StaticString = #filePath, line: UInt = #line) {
        tap(element, file: file, line: line)
        element.typeText(text)
    }

    /// 가입 직후 iOS가 띄우는 '비밀번호 저장' 물음이 있으면 닫는다.
    private func dismissPasswordPrompt() {
        let board = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        for label in ["나중에", "Not Now"] {
            let button = board.buttons[label]
            if button.waitForExistence(timeout: 3) {
                button.tap()
                return
            }
        }
    }
}
