import XCTest
@testable import PersonaIze

final class ImportParserTests: XCTestCase {
    func testLinesGetIdsKoreanTimeAndSpeakerRoles() throws {
        let text = """
        2026-10-05 09:25 | 박지현 | 민수씨, 개편은 언제까지 가능해요?

        2026-10-05 09:31 | 김민수 | 다음 주 수요일까지는 가능합니다. | 확인 부탁드립니다
        """
        let payload = try ImportParser.parse(text, me: "김민수", contact: "박지현")

        XCTAssertEqual(payload.speaker_map, ["박지현": "contact", "김민수": "me"])
        XCTAssertEqual(payload.messages.map(\.id), ["m01", "m03"])
        XCTAssertEqual(payload.messages[0].sent_at, "2026-10-05T09:25:00+09:00")
        XCTAssertEqual(payload.messages[1].text, "다음 주 수요일까지는 가능합니다. | 확인 부탁드립니다")
    }

    func testRoleKeywordsNeedNoSpeakerMap() throws {
        let payload = try ImportParser.parse("2026-10-05 09:25 | me | 넵\n2026-10-05 09:26 | contact | 네", me: "", contact: "박지현")

        XCTAssertEqual(payload.speaker_map, [:])
        XCTAssertEqual(payload.messages.map(\.speaker), ["me", "contact"])
    }

    func testTimestampKeepsExplicitZoneAndSeconds() {
        XCTAssertEqual(ImportParser.timestamp("2026-10-05 09:25:30"), "2026-10-05T09:25:30+09:00")
        XCTAssertEqual(ImportParser.timestamp("2026-10-05T00:25:00Z"), "2026-10-05T00:25:00Z")
        XCTAssertEqual(ImportParser.timestamp("2026-10-05 09:25+02:00"), "2026-10-05T09:25+02:00")
    }

    func testMalformedLineReportsItsNumber() {
        XCTAssertThrowsError(try ImportParser.parse("2026-10-05 09:25 | me | 넵\n형식 아님", me: "", contact: "박지현")) {
            XCTAssertTrue($0.localizedDescription.hasPrefix("2번째 줄"))
        }
    }

    func testRolesFollowServerSuggestionWhenMyNameIsBlank() throws {
        let speakers = [ImportPreview.Speaker(name: "김민수", count: 6, suggested_role: "me"),
                        ImportPreview.Speaker(name: "박지현", count: 5, suggested_role: "contact")]

        XCTAssertEqual(try ImportParser.roles(for: speakers, me: ""), ["김민수": "me", "박지현": "contact"])
    }

    func testRolesUseMyNameOverServerSuggestion() throws {
        let speakers = [ImportPreview.Speaker(name: "김민수", count: 6, suggested_role: nil),
                        ImportPreview.Speaker(name: "박지현", count: 5, suggested_role: "me"),
                        ImportPreview.Speaker(name: "최유진", count: 1, suggested_role: nil)]

        XCTAssertEqual(try ImportParser.roles(for: speakers, me: "김민수", contact: ["박지현"]),
                       ["김민수": "me", "박지현": "contact", "최유진": "skip"])
    }

    func testRolesNeedSomeoneToBeMe() {
        let speakers = [ImportPreview.Speaker(name: "김민수", count: 6, suggested_role: nil),
                        ImportPreview.Speaker(name: "박지현", count: 5, suggested_role: nil)]

        XCTAssertThrowsError(try ImportParser.roles(for: speakers, me: "")) {
            XCTAssertTrue($0.localizedDescription.contains("김민수, 박지현"))
        }
        XCTAssertThrowsError(try ImportParser.roles(for: speakers, me: "민수")) {
            XCTAssertTrue($0.localizedDescription.hasPrefix("‘민수’"))
        }
        XCTAssertThrowsError(try ImportParser.roles(for: [], me: "김민수"))
    }

    func testGroupChatKeepsOnlyThisContactAndSkipsTheRest() throws {
        let speakers = [ImportPreview.Speaker(name: "김민수", count: 6, suggested_role: nil),
                        ImportPreview.Speaker(name: "박지현", count: 5, suggested_role: nil),
                        ImportPreview.Speaker(name: "최유진", count: 1, suggested_role: nil)]

        XCTAssertEqual(try ImportParser.roles(for: speakers, me: "김민수", contact: ["", "박지현"].filter { !$0.isEmpty }),
                       ["김민수": "me", "박지현": "contact", "최유진": "skip"])
        // 적어 준 이름이 별칭보다 먼저다.
        XCTAssertEqual(try ImportParser.roles(for: speakers, me: "김민수", contact: ["최유진", "박지현"]),
                       ["김민수": "me", "박지현": "skip", "최유진": "contact"])
    }

    func testGroupChatNeedsToKnowWhichSpeakerIsThisContact() {
        let speakers = [ImportPreview.Speaker(name: "김민수", count: 6, suggested_role: "me"),
                        ImportPreview.Speaker(name: "박지현", count: 5, suggested_role: nil),
                        ImportPreview.Speaker(name: "최유진", count: 1, suggested_role: nil)]

        XCTAssertThrowsError(try ImportParser.roles(for: speakers, me: "", contact: ["팀장님"])) {
            XCTAssertTrue($0.localizedDescription.contains("상대 이름"))
            XCTAssertTrue($0.localizedDescription.contains("김민수, 박지현, 최유진"))
        }
        // 내 이름은 상대가 될 수 없다.
        XCTAssertThrowsError(try ImportParser.roles(for: speakers, me: "", contact: ["김민수"]))
    }

    func testEmptyInputIsRejected() {
        XCTAssertThrowsError(try ImportParser.parse(" \n ", me: "", contact: "박지현"))
    }

    func testJsonArrayPassesThroughWithSpeakerMap() throws {
        let json = #"[{"id":"x1","speaker":"김민수","sent_at":"2026-10-05T09:25:00+09:00","text":"넵"}]"#
        let payload = try ImportParser.parse(json, me: "김민수", contact: "박지현")

        XCTAssertEqual(payload.messages, [ImportMessage(id: "x1", speaker: "김민수", sent_at: "2026-10-05T09:25:00+09:00", text: "넵")])
        XCTAssertEqual(payload.speaker_map, ["김민수": "me", "박지현": "contact"])
        XCTAssertThrowsError(try ImportParser.parse("[{\"id\":1}]", me: "", contact: "박지현"))
    }
}

@MainActor
final class APITests: XCTestCase {
    func testServerMustBePlainHttpsOrigin() {
        XCTAssertNoThrow(try API(server: " https://example.test "))
        for bad in ["http://example.test", "example.test", "https://user:pw@example.test", "https://example.test/?a=1", ""] {
            XCTAssertThrowsError(try API(server: bad), bad)
        }
    }

    func testFailureMessagePrefersServerMessageThenValidationDetail() {
        XCTAssertEqual(API.failureMessage(Data(#"{"code":"conflict","message":"버전이 다릅니다."}"#.utf8), status: 409), "버전이 다릅니다.")
        XCTAssertEqual(API.failureMessage(Data(#"{"detail":[{"msg":"too short"}]}"#.utf8), status: 422), "입력을 확인해 주세요: too short")
        XCTAssertEqual(API.failureMessage(Data(), status: 401), "로그인이 만료됐어요. 다시 로그인해 주세요.")
        XCTAssertEqual(API.failureMessage(Data("<html>".utf8), status: 502), "서버 응답을 확인해 주세요.")
    }
}

final class GraphLayoutTests: XCTestCase {
    private func node(_ id: String, contact: Bool = false) -> GraphNode {
        GraphNode(id: id, name: id, kind: "person", summary: "", event_type: nil, is_contact: contact)
    }

    private func edge(_ source: String, _ target: String) -> GraphEdge {
        GraphEdge(id: source + target, source: source, target: target, fact: "", valid_at: nil, current: true)
    }

    func testContactIsCenteredNeighborsInsideOthersOutside() {
        let graph = ContactGraph(enabled: true, center_id: "c",
                                 nodes: [node("c", contact: true), node("me"), node("event"), node("far")],
                                 edges: [edge("me", "c"), edge("c", "event"), edge("me", "far")])
        let points = GraphLayout.positions(graph)
        let distance = { (id: String) in hypot(points[id]!.x - 0.5, points[id]!.y - 0.5) }

        XCTAssertEqual(points["c"], CGPoint(x: 0.5, y: 0.5))
        XCTAssertEqual(distance("me"), GraphLayout.innerRadius, accuracy: 1e-9)
        XCTAssertEqual(distance("event"), GraphLayout.innerRadius, accuracy: 1e-9)
        XCTAssertEqual(distance("far"), GraphLayout.outerRadius, accuracy: 1e-9)
        XCTAssertEqual(Set(points.values.map { "\($0.x),\($0.y)" }).count, 4, "겹치는 노드가 없어야 한다")
        XCTAssertTrue(points.values.allSatisfy { (0.05...0.95).contains($0.x) && (0.05...0.95).contains($0.y) })
    }

    func testEmptyGraphHasNoPositions() {
        XCTAssertTrue(GraphLayout.positions(ContactGraph(enabled: true, center_id: nil, nodes: [], edges: [])).isEmpty)
    }
}

final class TelegramImportTests: XCTestCase {
    private let export = #"""
    {"name":"박지현","type":"personal_chat","messages":[
      {"id":1,"type":"service","date":"2026-10-05T09:00:00","action":"phone_call"},
      {"id":2,"type":"message","date":"2026-10-05T09:25:00","date_unixtime":"1791159900","from":"박지현","text":"개편은 언제까지 가능해요?"},
      {"id":3,"type":"message","date":"2026-10-05T09:31:00","from":"김민수","text":["다음 주 ",{"type":"bold","text":"수요일"},"까지 가능합니다."]},
      {"id":4,"type":"message","date":"2026-10-05T09:32:00","from":"김민수","text":""}
    ]}
    """#

    func testTelegramExportKeepsPlainMessagesAndJoinsTextParts() throws {
        let payload = try ImportParser.parse(export, me: "김민수", contact: "박지현")

        XCTAssertEqual(payload.messages.map(\.id), ["tg2", "tg3"])
        XCTAssertEqual(payload.messages[0].sent_at, "2026-10-05T00:25:00Z")
        XCTAssertEqual(payload.messages[1].sent_at, "2026-10-05T09:31:00+09:00")
        XCTAssertEqual(payload.messages[1].text, "다음 주 수요일까지 가능합니다.")
        XCTAssertEqual(payload.speaker_map, ["박지현": "contact", "김민수": "me"])
    }

    func testTelegramExportNeedsMyDisplayName() {
        XCTAssertThrowsError(try ImportParser.parse(export, me: "", contact: "박지현"))
        XCTAssertThrowsError(try ImportParser.parse(export, me: "민수", contact: "박지현")) {
            XCTAssertTrue($0.localizedDescription.contains("김민수"))
        }
        XCTAssertThrowsError(try ImportParser.parse(#"{"messages":[]}"#, me: "김민수", contact: "박지현"))
    }
}

final class RecentsTests: XCTestCase {
    private func contact(_ id: String) -> Contact {
        Contact(id: id, alias: id, relationship_type: "friend", persona_version: 1)
    }

    func testPushKeepsNewestFirstWithoutDuplicatesUpToThree() {
        var stored = ""
        for id in ["a", "b", "a", "c", "d"] { stored = Recents.push(id, onto: stored) }
        XCTAssertEqual(stored, "d,c,a")
        XCTAssertEqual(Recents.push("", onto: stored), stored)
    }

    func testPickSkipsDeletedContactsAndFillsFromList() {
        let contacts = ["a", "b", "c", "d"].map(contact)
        XCTAssertEqual(Recents.pick(stored: "x,c", contacts: contacts).map(\.id), ["c", "a", "b"])
        XCTAssertEqual(Recents.pick(stored: "", contacts: Array(contacts.prefix(2))).map(\.id), ["a", "b"])
    }

    func testChipsShowTheSelectedContactEvenWhenItIsNotRecent() {
        let contacts = ["a", "b", "c", "d", "e"].map(contact)
        let recents = Recents.pick(stored: "a,b,c", contacts: contacts)
        // 최근 목록에 있는 상대를 고르면 칩 줄은 그대로다.
        XCTAssertEqual(Recents.chips(recents: recents, selected: "b", contacts: contacts).map(\.id), ["a", "b", "c"])
        // 메뉴에서 고른 상대는 맨 앞 칩이 되고, 칩 수는 늘지 않는다.
        XCTAssertEqual(Recents.chips(recents: recents, selected: "e", contacts: contacts).map(\.id), ["e", "a", "b"])
        // 없는 상대·빈 값은 최근 목록을 그대로 둔다.
        XCTAssertEqual(Recents.chips(recents: recents, selected: "x", contacts: contacts).map(\.id), ["a", "b", "c"])
        XCTAssertEqual(Recents.chips(recents: [], selected: "", contacts: []).map(\.id), [])
    }

    func testDateIsWrittenInKoreanRegardlessOfDeviceLanguage() {
        let text = Labels.date("2026-11-18T02:05:00Z")
        XCTAssertTrue(text.contains("2026년") && text.contains("11월"), text)
        XCTAssertTrue(text.contains("오전") || text.contains("오후"), text)
        // 읽을 수 없는 값은 앞부분을 그대로 보인다.
        XCTAssertEqual(Labels.date("2026-11-18 soon"), "2026-11-18 soon")
    }

    func testViolationAndStepLabels() {
        XCTAssertEqual(Labels.violation("preserve_missing"), "지켜야 할 내용이 빠졌어요")
        XCTAssertEqual(Labels.violation("new_kind"), "검증을 통과하지 못했어요")
        XCTAssertEqual(Labels.step(Labels.formality, 5), "매우 정중")
        XCTAssertEqual(Labels.step(Labels.formality, 9), "매우 정중")
        XCTAssertEqual(Labels.step(Labels.formality, 0), "반말·캐주얼")
    }
}

final class KeyboardLinkTests: XCTestCase {
    private func link(selected: String = "b", recents: [String] = []) -> KeyboardLink {
        KeyboardLink(server: "https://example.test", cookie: "token", csrf: "csrf",
                     contacts: [.init(id: "a", alias: "A", ready: false), .init(id: "b", alias: "B", ready: true),
                                .init(id: "c", alias: "C", ready: true)],
                     selected: selected, recents: recents)
    }

    func testCurrentFallsBackToFirstContactWithPersona() {
        XCTAssertEqual(link().current?.id, "b")
        XCTAssertEqual(link(selected: "a").current?.id, "b", "페르소나 없는 상대는 고른 상태로 두지 않는다")
        XCTAssertEqual(link(selected: "gone").current?.id, "b")
        XCTAssertNil(KeyboardLink(server: "", cookie: "", csrf: "", contacts: [], selected: "", recents: []).current)
    }

    func testChooseRemembersUpToThreeRecentsAndOrdersThemFirst() {
        var value = link(recents: ["a", "gone", "c"])
        value.choose("c")
        XCTAssertEqual(value.selected, "c")
        XCTAssertEqual(value.recents, ["c", "a", "gone"])
        XCTAssertEqual(value.ordered.map(\.id), ["c", "a", "b"])
    }

    func testLinkSurvivesEncoding() throws {
        let value = link(recents: ["c"])
        XCTAssertEqual(try JSONDecoder().decode(KeyboardLink.self, from: JSONEncoder().encode(value)), value)
    }

    func testRequestCarriesSessionCookieCsrfAndIdempotencyKey() throws {
        let key = UUID()
        let post = try KeyboardClient(link: link()).request("POST", "v1/rewrites", body: ["draft_text": "넵"], key: key)
        XCTAssertEqual(post.url?.absoluteString, "https://example.test/v1/rewrites")
        XCTAssertEqual(post.value(forHTTPHeaderField: "Cookie"), "pi_session=token")
        XCTAssertEqual(post.value(forHTTPHeaderField: "X-CSRF-Token"), "csrf")
        XCTAssertEqual(post.value(forHTTPHeaderField: "Idempotency-Key"), key.uuidString)

        let get = try KeyboardClient(link: link()).request("GET", "v1/usage")
        XCTAssertNil(get.value(forHTTPHeaderField: "X-CSRF-Token"))
        var plain = link()
        plain.server = "http://example.test"
        XCTAssertThrowsError(try KeyboardClient(link: plain).request("GET", "v1/usage"), "HTTPS가 아니면 쿠키를 보내지 않는다")
    }
}

final class DraftReplacementTests: XCTestCase {
    func testPlanDeletesWholeDraftFromItsEnd() {
        XCTAssertEqual(DraftReplacement.plan(requested: "넹 보낼게여", before: "넹 보낼게여", after: nil),
                       DraftReplacement(forward: 0, deletions: 6))
        XCTAssertEqual(DraftReplacement.plan(requested: "넹 보낼게여", before: "넹 ", after: "보낼게여"),
                       DraftReplacement(forward: 4, deletions: 6))
    }

    func testEmojiCountsOnceForDeletionAndTwiceForCursor() {
        XCTAssertEqual(DraftReplacement.plan(requested: "네😊", before: "네", after: "😊"),
                       DraftReplacement(forward: 2, deletions: 2))
    }

    func testChangedOrEmptyInputHasNoPlan() {
        XCTAssertNil(DraftReplacement.plan(requested: "넹", before: "넹!", after: nil))
        XCTAssertNil(DraftReplacement.plan(requested: "", before: nil, after: nil))
    }
}

final class ImportJobTests: XCTestCase {
    private func job(_ fields: String) throws -> ImportJob {
        let json = #"{"id":"j1","imported":3,"duplicates":0,"episodes":2,"graph_episodes":0,"graph_error":null,"# + fields + "}"
        return try JSONDecoder().decode(ImportJob.self, from: Data(json.utf8))
    }

    func testProcessingJobKeepsPollingEvenWhenGraphIsOff() throws {
        let processing = try job(#""status":"processing","graph_status":"skipped""#)
        XCTAssertTrue(processing.styleActive)
        XCTAssertTrue(processing.active)
    }

    func testFinishedJobStopsPolling() throws {
        for graph in ["completed", "failed", "limited", "skipped"] {
            let done = try job(#""status":"completed","graph_status":""# + graph + #"""#)
            XCTAssertFalse(done.active, graph)
        }
        XCTAssertTrue(try job(#""status":"completed","graph_status":"running""#).active)
    }

    func testStatusMayBeMissing() throws {
        let old = try job(#""graph_status":"pending""#)
        XCTAssertNil(old.status)
        XCTAssertFalse(old.styleActive)
        XCTAssertTrue(old.active)
    }
}
