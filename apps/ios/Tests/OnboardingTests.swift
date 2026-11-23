import XCTest
@testable import PersonaIze

final class OnboardingTests: XCTestCase {
    func testNewAccountMakesAContactThenSeesTheKeyboardGuide() {
        XCTAssertEqual(Onboarding.steps(hasContacts: false), [.contact, .keyboard])
    }

    func testAccountWithContactsSkipsStraightToTheKeyboardGuide() {
        XCTAssertEqual(Onboarding.steps(hasContacts: true), [.keyboard])
    }

    func testAliasIsTrimmedAndCutToTheServerLimit() {
        XCTAssertEqual(Onboarding.alias(from: "  박 팀장 \n"), "박 팀장")
        XCTAssertEqual(Onboarding.alias(from: String(repeating: "가", count: 80))?.count, 50)
    }

    func testBlankAliasIsRejected() {
        XCTAssertNil(Onboarding.alias(from: ""))
        XCTAssertNil(Onboarding.alias(from: "   \n"))
    }

    func testSamplesKeepTheFactsOfTheDraft() {
        // 예시도 앱의 약속(내용은 그대로)을 지켜야 한다: 회의·병원 예약이 모든 예시에 남아 있다.
        XCTAssertFalse(Onboarding.samples.isEmpty)
        for sample in Onboarding.samples {
            XCTAssertTrue(sample.text.contains("회의"), sample.id)
            XCTAssertTrue(sample.text.contains("병원 예약"), sample.id)
            XCTAssertNotEqual(sample.text, Onboarding.sampleDraft)
        }
        XCTAssertEqual(Set(Onboarding.samples.map(\.id)).count, Onboarding.samples.count)
    }

    func testWaitingStageAdvancesWithElapsedTimeAndStaysOnTheLast() {
        XCTAssertEqual(RewriteStage.at(elapsed: -1), .looking)
        XCTAssertEqual(RewriteStage.at(elapsed: 0), .looking)
        XCTAssertEqual(RewriteStage.at(elapsed: 2.9), .looking)
        XCTAssertEqual(RewriteStage.at(elapsed: 3), .writing)
        XCTAssertEqual(RewriteStage.at(elapsed: 8.9), .writing)
        XCTAssertEqual(RewriteStage.at(elapsed: 9), .checking)
        XCTAssertEqual(RewriteStage.at(elapsed: 600), .checking)
    }
}
