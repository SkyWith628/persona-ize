import XCTest

/// 화면 흐름 검사 둘:
/// - 예시 화면 → 가입 → 첫 상대 → 다듬기 → 이력 → 다시 열기(로그인 유지) → 로그아웃.
/// - 가입 → 상대 화면에서 대화방 추가 → 카카오톡 대화 붙여넣기 → 가져오기 → 화자 안내(누가 나인지) →
///   내 이름을 넣고 다시 가져오기 → 말투 분석 완료.
/// - 토큰 만료 안내: 끈 사이 토큰이 폐기되면 다시 열 때, 쓰는 중 폐기되면 다음 요청에서 로그인 화면 + 안내.
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

        // 1~3. 처음 상태에서 가입하고 첫 상대를 만든다.
        var app = signUpWithFirstContact(server: server).app

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

    /// 카카오톡 내보내기(안드로이드 꼴) 붙여넣기. 제목 줄이 없어 서버가 누가 나인지 추천하지 못한다 —
    /// 그래서 앱이 화자 안내를 띄우고, 내 이름을 넣으면 가져온다. 대화는 지어낸 것이다.
    func testImportPastedKakaoWithSpeakerGuide() throws {
        let server = try localServer()
        let app = signUpWithFirstContact(server: server).app
        let me = "김하늘"
        let room = "회의 일정"
        let chat = [
            "2026년 11월 2일 오전 9:10, \(alias) : 내일 회의 자료 준비됐나요",
            "2026년 11월 2일 오전 9:12, \(me) : 네 오늘 오후까지 보내드릴게요",
            "2026년 11월 2일 오전 9:13, \(alias) : 좋아요 고마워요",
            "2026년 11월 2일 오전 9:15, \(me) : 확인했습니다",
        ].joined(separator: "\n")

        // 1. 상대 탭 → 상대 → 대화방 추가 → 그 대화방으로 들어간다.
        XCTAssertTrue(app.tabBars.buttons["상대"].waitForExistence(timeout: wait), "탭 화면이 뜨지 않았다")
        tap(app.tabBars.buttons["상대"])
        tap(app.staticTexts[alias])
        let roomName = app.textFields["대화방 이름"]
        reveal(roomName, in: app)
        type(room, into: roomName)
        let addRoom = app.buttons["대화방 추가"]
        reveal(addRoom, in: app)
        addRoom.tap()
        // 대화방 목록은 입력 칸보다 위에 있다. 이름 칸(값이 비워진다)이 아닌 목록의 줄을 찾는다.
        let roomLink = app.descendants(matching: .any).matching(NSPredicate(
            format: "label == %@ AND elementType != %d", room, XCUIElement.ElementType.textField.rawValue
        )).firstMatch
        reveal(roomLink, in: app, upward: false)
        roomLink.tap()

        // 2. 카카오톡 방법을 고르고 대화를 붙여 넣는다. 내 이름은 비워 둔 채 가져온다.
        tap(app.buttons["카카오톡"])
        type(chat, into: app.textViews["가져올 대화"])
        let importButton = app.buttons["가져오기"]
        reveal(importButton, in: app)
        importButton.tap()

        // 3. 화자 안내: 누가 나인지 모른다며 파일에 있는 이름을 보여 준다.
        let guide = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "누가 나인지 알 수 없어요")).firstMatch
        XCTAssertTrue(guide.waitForExistence(timeout: wait), "화자 안내가 뜨지 않았다")
        XCTAssertTrue(guide.label.contains(me) && guide.label.contains(alias), "안내에 파일의 이름이 없다: \(guide.label)")

        // 4. 내 이름을 넣고 다시 가져오면 끝까지 간다. 4건 중 내 말 2건.
        let meField = app.textFields["대화에 나오는 내 이름 (예: 김민수)"]
        reveal(meField, in: app)
        type(me, into: meField)
        reveal(importButton, in: app)
        importButton.tap()
        let done = app.staticTexts["말투 분석 완료 · 지금 바로 쓸 수 있어요"]
        XCTAssertTrue(done.waitForExistence(timeout: wait), "가져오기 완료 표시가 없다")
        let counts = app.staticTexts.containing(NSPredicate(format: "label CONTAINS %@", "내가 쓴 것 2개")).firstMatch
        XCTAssertTrue(counts.waitForExistence(timeout: wait), "가져온 건수가 맞지 않다")
    }

    /// 토큰 만료 안내 두 경로. 폐기는 실제 서버 기능으로 한다: 검사 실행기가 같은 계정으로 토큰을 하나 더 받아
    /// `POST /v1/auth/logout-all`(지금 세션만 남기고 다른 세션·토큰 전부 폐기)을 부르면 앱의 Bearer 토큰이 죽는다.
    func testExpiredTokenReturnsToSignInWithNotice() throws {
        let server = try localServer()
        let (first, name, password) = signUpWithFirstContact(server: server)
        var app = first
        XCTAssertTrue(app.tabBars.buttons["작성"].waitForExistence(timeout: wait), "탭 화면이 뜨지 않았다")

        // (b) 앱을 끈 사이 토큰이 폐기됨 → 다시 열면 restore()가 401 → 로그인 화면 + 만료 안내.
        app.terminate()
        try revokeOtherSessions(server: server, username: name, password: password)
        app = launch(server: server, reset: false)
        XCTAssertTrue(app.buttons["로그인"].waitForExistence(timeout: wait), "폐기된 토큰으로 다시 열었는데 로그인 화면이 아니다")
        XCTAssertTrue(expiredNotice(in: app).waitForExistence(timeout: wait), "다시 열 때 만료 안내가 없다")
        XCTAssertFalse(app.tabBars.buttons["작성"].exists)

        // 다시 로그인한다(아이디는 기기에 남아 있다).
        typeExact(password, into: app.secureTextFields["비밀번호"])
        tap(app.buttons["로그인"])
        dismissPasswordPrompt()
        XCTAssertTrue(app.tabBars.buttons["이력"].waitForExistence(timeout: wait), "다시 로그인하지 못했다")

        // (a) 쓰는 중 토큰이 폐기됨 → 다음 요청(이력 읽기)이 401 invalid_token → 로그인 화면 + 만료 안내.
        try revokeOtherSessions(server: server, username: name, password: password)
        tap(app.tabBars.buttons["이력"])
        XCTAssertTrue(app.buttons["로그인"].waitForExistence(timeout: wait), "토큰이 거절됐는데 로그인 화면으로 가지 않았다")
        XCTAssertTrue(expiredNotice(in: app).waitForExistence(timeout: wait), "쓰는 중 만료 안내가 없다")
        XCTAssertFalse(app.tabBars.buttons["작성"].exists)

        // 기기의 토큰도 지워졌다: 다시 열어도 로그인 화면이다(이번엔 안내 없이).
        app.terminate()
        app = launch(server: server, reset: false)
        XCTAssertTrue(app.buttons["로그인"].waitForExistence(timeout: wait), "만료 뒤 다시 열었는데 로그인 화면이 아니다")
        XCTAssertFalse(app.tabBars.buttons["작성"].exists)
    }

    // MARK: 도구

    /// 처음 상태에서 연다 → 예시 화면 건너뛰기 → 가입(가입 응답에는 토큰이 없어 앱이 이어서 토큰을 받는다)
    /// → 준비 화면에서 첫 상대를 만들고(기본 말투가 함께 생긴다) 키보드 안내는 건너뛴다.
    private func signUpWithFirstContact(server: String) -> (app: XCUIApplication, name: String, password: String) {
        let name = "ui" + UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12).lowercased()
        // 소문자·숫자만(시프트·기호 없이 친다).
        let password = "pw" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let app = launch(server: server, reset: true)
        // 막 뜬 앱에서는 첫 탭이 먹지 않을 때가 있다(11/27, 첫 검사): 로그인 화면이 안 보이면 한 번 더 누른다.
        let skip = app.buttons["건너뛰기"]
        let nameField = app.textFields["아이디 (영문·숫자 3~32자)"]
        tap(skip)
        if !nameField.waitForExistence(timeout: 5), skip.exists { skip.tap() }
        typeExact(name, into: nameField)
        typeExact(password, into: app.secureTextFields["비밀번호"])
        tap(app.buttons["새 계정으로 가입"])
        dismissPasswordPrompt()
        type(alias, into: app.textFields["예: 박 팀장"])
        tap(app.buttons["상대 만들기"])
        tap(app.buttons["나중에 하기"])
        return (app, name, password)
    }

    /// 로그인 화면 아래 안내 줄(StatusBar가 기호와 문구를 한 요소로 묶는다).
    private func expiredNotice(in app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "로그인이 만료됐어요. 다시 로그인해 주세요.")).firstMatch
    }

    /// 검사 실행기가 앱 밖에서 같은 계정으로 토큰을 받아 `logout-all`을 부른다 — 앱이 가진 토큰이 폐기된다.
    /// 실행기도 시뮬레이터 안에서 돌아 스크립트가 등록한 루트 인증서로 localhost HTTPS를 믿는다.
    private func revokeOtherSessions(server: String, username: String, password: String) throws {
        let base = try XCTUnwrap(URL(string: server))
        let login = try send(base.appendingPathComponent("v1/auth/token"), body: [
            "username": username, "password": password, "client": "api",
        ])
        let token = try XCTUnwrap(login["access_token"] as? String, "토큰을 받지 못했다")
        let out = try send(base.appendingPathComponent("v1/auth/logout-all"), body: [:], token: token)
        let revoked = try XCTUnwrap(out["revoked_sessions"] as? Int)
        XCTAssertGreaterThanOrEqual(revoked, 1, "폐기한 세션이 없다 — 앱의 토큰이 남아 있다")
    }

    private func send(_ url: URL, body: [String: String], token: String? = nil) throws -> [String: Any] {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let token { request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization") }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        var result: Result<(Data, Int), Error>?
        let done = expectation(description: url.path)
        URLSession.shared.dataTask(with: request) { data, response, error in
            if let error {
                result = .failure(error)
            } else {
                result = .success((data ?? Data(), (response as? HTTPURLResponse)?.statusCode ?? 0))
            }
            done.fulfill()
        }.resume()
        wait(for: [done], timeout: wait)
        let (data, status) = try XCTUnwrap(result).get()
        XCTAssertEqual(status, 200, "\(url.path) 응답 \(status)")
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    /// 긴 폼 아래쪽의 요소가 누를 수 있게 보일 때까지 위로 민다(목록은 보이는 줄만 만든다).
    /// `upward: false`면 거꾸로(아래로) 밀어 위쪽 요소를 찾는다. 못 찾으면 반대쪽으로도 민다.
    private func reveal(_ element: XCUIElement, in app: XCUIApplication, upward: Bool = true,
                        file: StaticString = #filePath, line: UInt = #line) {
        // 끝까지 밀어도 없으면 반대쪽으로도 민다 — 키보드가 떠 있었는지 등에 따라 요소가 반대편에 있을 때가 있다
        // (11/27, 대화방 줄이 아래쪽에 남아 위로만 찾다가 실패).
        for direction in [upward, !upward] {
            for _ in 0..<8 where !(element.exists && element.isHittable) {
                if direction { app.swipeUp() } else { app.swipeDown() }
            }
        }
        XCTAssertTrue(element.waitForExistence(timeout: wait) && element.isHittable, "화면에 없다: \(element)",
                      file: file, line: line)
    }

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

    /// 눌러서 입력 포커스를 잡은 뒤 친다. 화면이 막 바뀐 참에는(가입 직후 준비 화면 등) 첫 탭이 포커스를
    /// 못 잡을 때가 있어(11/27, 부팅 직후 첫 검사) 포커스가 올 때까지 몇 번 다시 누른다.
    private func type(_ text: String, into element: XCUIElement, file: StaticString = #filePath, line: UInt = #line) {
        tap(element, file: file, line: line)
        let focused = NSPredicate(format: "hasKeyboardFocus == true")
        for _ in 0..<3 {
            let check = XCTNSPredicateExpectation(predicate: focused, object: element)
            if XCTWaiter().wait(for: [check], timeout: 2) == .completed { break }
            element.tap()
        }
        element.typeText(text)
    }

    /// 친 값이 그대로 들어갔는지 보고, 글자가 빠졌으면 지우고 다시 친다(최대 세 번). 검사 실행기가 같은
    /// 아이디·비밀번호로 서버에 로그인하므로 앱에 들어간 값과 같아야 한다. 보안 칸은 가린 글자(•) 수로 본다.
    private func typeExact(_ text: String, into field: XCUIElement, file: StaticString = #filePath, line: UInt = #line) {
        let secure = field.elementType == .secureTextField
        for _ in 0..<3 {
            type(text, into: field, file: file, line: line)
            let value = field.value as? String ?? ""
            if secure ? value.count == text.count : value == text { return }
            field.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: value.count))
        }
        XCTFail("입력이 제대로 들어가지 않았다: \(field)", file: file, line: line)
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
