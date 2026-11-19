import SwiftUI

/// 첫 실행 흐름의 규칙. 화면은 한 번에 한 가지만 묻는다:
/// 예시로 체험(가입 전) → 로그인 → 첫 상대 만들기 → 키보드 안내.
enum Onboarding {
    /// 로그인한 뒤에 보일 준비 단계.
    enum Step: Equatable { case contact, keyboard }

    /// 상대가 이미 있으면 상대 만들기는 건너뛴다. 키보드 안내는 한 번은 보여 준다.
    static func steps(hasContacts: Bool) -> [Step] { hasContacts ? [.keyboard] : [.contact, .keyboard] }

    /// 가입 전에 보여 주는 준비된 예시. 서버를 부르지 않으므로 실제 추천이 아니라 예시라고 화면에 밝힌다.
    struct Sample: Identifiable, Equatable {
        let id: String
        let alias: String
        let relationship: String
        let text: String
    }

    static let sampleDraft = "내일 회의 못 갈 것 같아요 병원 예약이 있어서요"
    static let samples = [
        Sample(id: "boss", alias: "박 팀장", relationship: "상사",
               text: "팀장님, 죄송하지만 내일 회의는 참석이 어려울 것 같습니다. 병원 예약이 있어서요."),
        Sample(id: "friend", alias: "민지", relationship: "친구",
               text: "나 내일 회의 못 갈 듯ㅠ 병원 예약 있어서"),
    ]

    /// 상대 별칭: 앞뒤 공백을 떼고 서버 한도(50자)에 맞춘다. 비면 nil.
    static func alias(from input: String) -> String? {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : String(trimmed.prefix(50))
    }
}

/// 다듬는 동안 보여 줄 단계. 서버가 단계 정보를 주지 않아 걸린 시간으로 넘긴다.
enum RewriteStage: Int, CaseIterable {
    case looking, writing, checking

    var text: String {
        switch self {
        case .looking: return "예전 대화를 살펴보고 있어요"
        case .writing: return "문장을 다듬고 있어요"
        case .checking: return "내용이 바뀐 곳이 없는지 확인하고 있어요"
        }
    }

    /// 걸린 시간(초)에 맞는 단계. 음수는 처음 단계로 본다. 마지막 단계에서는 끝날 때까지 머문다.
    static func at(elapsed: TimeInterval) -> RewriteStage {
        if elapsed < 3 { return .looking }
        if elapsed < 9 { return .writing }
        return .checking
    }
}

// MARK: - 1단계: 예시로 체험

/// 가입 전에 앱이 무엇을 하는지 한 번 보여 준다. 같은 초안이 상대에 따라 달라지는 예시 두 개.
struct IntroView: View {
    let done: () -> Void
    @State private var picked = Onboarding.samples[0].id
    @State private var shown = false

    private var sample: Onboarding.Sample { Onboarding.samples.first { $0.id == picked } ?? Onboarding.samples[0] }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.xl) {
                VStack(alignment: .leading, spacing: Theme.Space.m) {
                    Text("같은 말도\n상대에 맞게")
                        .font(.largeTitle.bold()).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityAddTraits(.isHeader)
                    Text("보낼 글을 쓰면 받는 사람에 맞는 말투로 다듬어 드려요. 내용은 바꾸지 않아요.")
                        .font(.body).foregroundStyle(Theme.ink2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.horizontal, Theme.Space.xs)

                VStack(alignment: .leading, spacing: Theme.Space.s) {
                    caption("내가 쓴 글")
                    Text(Onboarding.sampleDraft)
                        .font(.body).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .card()
                }

                VStack(alignment: .leading, spacing: Theme.Space.s) {
                    caption("누구에게 보내나요?")
                    HStack(spacing: Theme.Space.s) {
                        ForEach(Onboarding.samples) { item in
                            Button("\(item.alias) · \(item.relationship)") { picked = item.id }
                                .buttonStyle(ChipStyle(selected: item.id == picked))
                                .accessibilityAddTraits(item.id == picked ? .isSelected : [])
                        }
                    }
                }

                if shown {
                    VStack(alignment: .leading, spacing: Theme.Space.s) {
                        caption("\(sample.alias)에게 맞게 다듬은 예시")
                        Text(sample.text)
                            .font(.title3.weight(.semibold)).foregroundStyle(Theme.ink)
                            .lineSpacing(Theme.Space.xs)
                            .fixedSize(horizontal: false, vertical: true)
                            .card(fill: Theme.accentSoft)
                        Text("미리 준비한 예시예요. 실제로는 내 예전 대화의 말버릇을 참고해 다듬어요.")
                            .font(.footnote).foregroundStyle(Theme.muted)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.horizontal, Theme.Space.xs)
                    }
                    .transition(.opacity)
                }
            }
            .padding(.horizontal, Theme.Space.l)
            .padding(.top, Theme.Space.xxl).padding(.bottom, Theme.Space.l)
        }
        .background(Theme.bg.ignoresSafeArea())
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: Theme.Space.s) {
                if shown {
                    Button("시작하기") { done() }.buttonStyle(.primary)
                } else {
                    Button("다듬어 보기") {
                        withAnimation { shown = true }
                        Haptic.notify(.success)
                    }
                    .buttonStyle(.primary)
                    Button("건너뛰기") { done() }.buttonStyle(QuietButtonStyle()).foregroundStyle(Theme.accentStrong)
                }
            }
            .padding(.horizontal, Theme.Space.l).padding(.vertical, Theme.Space.m)
            .background(Theme.bg)
        }
    }

    private func caption(_ text: String) -> some View {
        Text(text).font(.footnote.weight(.semibold)).foregroundStyle(Theme.muted)
            .padding(.horizontal, Theme.Space.xs)
            .accessibilityAddTraits(.isHeader)
    }
}

// MARK: - 3·4단계: 첫 상대, 키보드 안내

/// 로그인 직후 한 번 보이는 준비 화면. 단계마다 "나중에 하기"가 있어 막히지 않는다.
struct SetupView: View {
    @EnvironmentObject private var session: Session
    @StateObject private var run = Runner()
    @Environment(\.openURL) private var openURL
    let done: () -> Void
    /// 들어올 때 정한 단계. 상대를 만들면 목록이 바뀌지만 단계는 다시 계산하지 않는다.
    @State private var steps: [Onboarding.Step]
    @State private var index = 0
    @State private var alias = ""
    @State private var relationship = "colleague"
    @State private var openedSettings = false
    @FocusState private var typing: Bool

    init(hasContacts: Bool, done: @escaping () -> Void) {
        self.done = done
        _steps = State(initialValue: Onboarding.steps(hasContacts: hasContacts))
    }

    private var step: Onboarding.Step { steps[min(index, steps.count - 1)] }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.xl) {
                if steps.count > 1 {
                    Text("\(index + 1) / \(steps.count)")
                        .font(.footnote.weight(.semibold)).monospacedDigit().foregroundStyle(Theme.muted)
                        .padding(.horizontal, Theme.Space.xs)
                        .accessibilityLabel("\(steps.count)단계 중 \(index + 1)단계")
                }
                switch step {
                case .contact: contactStep
                case .keyboard: keyboardStep
                }
            }
            .padding(.horizontal, Theme.Space.l)
            .padding(.top, Theme.Space.xl).padding(.bottom, Theme.Space.l)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(Theme.bg.ignoresSafeArea())
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: Theme.Space.s) {
                switch step {
                case .contact:
                    Button("상대 만들기") { addContact() }
                        .buttonStyle(.primary)
                        .disabled(Onboarding.alias(from: alias) == nil)
                    Button("나중에 하기") { next() }.buttonStyle(QuietButtonStyle()).foregroundStyle(Theme.accentStrong)
                case .keyboard:
                    if openedSettings {
                        Button("다 했어요") { next() }.buttonStyle(.primary)
                    } else {
                        Button("설정 열기") {
                            openedSettings = true
                            if let url = URL(string: UIApplication.openSettingsURLString) { openURL(url) }
                        }
                        .buttonStyle(.primary)
                        Button("나중에 하기") { next() }.buttonStyle(QuietButtonStyle()).foregroundStyle(Theme.accentStrong)
                    }
                }
            }
            .padding(.horizontal, Theme.Space.l).padding(.vertical, Theme.Space.m)
            .background(Theme.bg)
        }
        .status(run)
        .navigationBarTitleDisplayMode(.inline)
    }

    private var contactStep: some View {
        VStack(alignment: .leading, spacing: Theme.Space.xl) {
            heading("처음 보낼 사람을\n알려 주세요", detail: "별칭과 관계만 있으면 돼요. 관계에 맞는 기본 말투로 시작하고, 나중에 고칠 수 있어요.")
            VStack(alignment: .leading, spacing: Theme.Space.s) {
                caption("별칭")
                TextField("예: 박 팀장", text: $alias)
                    .focused($typing)
                    .submitLabel(.done)
                    .font(.body).foregroundStyle(Theme.ink)
                    .frame(minHeight: Theme.minTouch)
                    .card(padding: Theme.Space.m)
            }
            VStack(alignment: .leading, spacing: Theme.Space.s) {
                caption("관계")
                FlowLayout(spacing: Theme.Space.s) {
                    ForEach(Labels.relationships, id: \.0) { item in
                        Button(item.1) { relationship = item.0 }
                            .buttonStyle(ChipStyle(selected: item.0 == relationship))
                            .accessibilityAddTraits(item.0 == relationship ? .isSelected : [])
                    }
                }
            }
            Text("실명 대신 나만 알아볼 별칭을 써 주세요.")
                .font(.footnote).foregroundStyle(Theme.muted)
                .padding(.horizontal, Theme.Space.xs)
        }
    }

    private var keyboardStep: some View {
        VStack(alignment: .leading, spacing: Theme.Space.xl) {
            heading("메신저에서 바로\n다듬을 수 있어요", detail: "키보드를 켜 두면 앱을 오가지 않아도 돼요. 지금 안 해도 앱 안에서 다듬을 수 있어요.")
            VStack(alignment: .leading, spacing: Theme.Space.m) {
                KeyboardStep(number: 1, text: "설정에서 키보드 → 새로운 키보드 추가 → Persona_ize를 골라요.")
                KeyboardStep(number: 2, text: "Persona_ize의 ‘전체 접근 허용’을 켜요.")
                KeyboardStep(number: 3, text: "메신저에서 지구본을 눌러 Persona_ize로 바꿔요.")
            }
            .card()
            Label("‘전체 접근’은 입력창의 초안을 서버로 보내 다듬는 데만 써요. 버튼을 누를 때만 글자를 읽어요.",
                  systemImage: "lock.shield")
                .font(.footnote).foregroundStyle(Theme.ink2)
                .fixedSize(horizontal: false, vertical: true)
                .card(padding: Theme.Space.m, fill: Theme.surface2)
        }
    }

    private func heading(_ title: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: Theme.Space.m) {
            Text(title).font(.largeTitle.bold()).foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityAddTraits(.isHeader)
            Text(detail).font(.body).foregroundStyle(Theme.ink2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, Theme.Space.xs)
    }

    private func caption(_ text: String) -> some View {
        Text(text).font(.footnote.weight(.semibold)).foregroundStyle(Theme.muted)
            .padding(.horizontal, Theme.Space.xs)
    }

    private func addContact() {
        guard let name = Onboarding.alias(from: alias) else { return }
        typing = false
        run {
            // 관계에 맞는 기본 말투를 함께 만들어, 준비가 끝나면 바로 다듬을 수 있게 한다.
            try await session.api?.addContact(alias: name, relationship: relationship, defaultPersona: true)
            try await session.reload()
            next()
        }
    }

    private func next() {
        if index + 1 < steps.count { withAnimation { index += 1 } } else { done() }
    }
}

// MARK: - 기다리는 화면

/// 다듬는 동안 결과 자리에 놓는 카드: 지금 단계 한 줄과 글 모양의 자리 표시.
struct WaitingCard: View {
    let alias: String
    let since: Date
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var dim = false

    var body: some View {
        TimelineView(.periodic(from: since, by: 1)) { context in
            let stage = RewriteStage.at(elapsed: context.date.timeIntervalSince(since))
            VStack(alignment: .leading, spacing: Theme.Space.l) {
                Text("\(alias)에게 맞게 다듬고 있어요")
                    .font(.title3.bold()).foregroundStyle(Theme.ink)
                    .accessibilityAddTraits(.isHeader)
                VStack(alignment: .leading, spacing: Theme.Space.m) {
                    ForEach(RewriteStage.allCases, id: \.rawValue) { item in
                        HStack(spacing: Theme.Space.s) {
                            Image(systemName: item.rawValue < stage.rawValue ? "checkmark.circle.fill"
                                  : item == stage ? "circle.dotted" : "circle")
                                .foregroundStyle(item.rawValue <= stage.rawValue ? Theme.accent : Theme.line)
                            Text(item.text)
                                .font(.subheadline.weight(item == stage ? .semibold : .regular))
                                .foregroundStyle(item == stage ? Theme.ink : Theme.muted)
                        }
                    }
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(stage.text)
                VStack(alignment: .leading, spacing: Theme.Space.s) {
                    bar(1); bar(0.92); bar(0.6)
                }
                .opacity(dim ? 0.45 : 1)
                .accessibilityHidden(true)
            }
            .card()
        }
        .onAppear {
            guard !reduceMotion else { return }
            withAnimation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true)) { dim = true }
        }
    }

    private func bar(_ fraction: CGFloat) -> some View {
        GeometryReader { proxy in
            RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous)
                .fill(Theme.surface2)
                .frame(width: proxy.size.width * fraction)
        }
        .frame(height: Theme.Space.l)
    }
}
