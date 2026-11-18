import SwiftUI

/// 오늘 사용량, 비밀번호 변경, 키보드 안내, 실험 설정, 로그아웃, 계정 삭제.
struct AccountView: View {
    @EnvironmentObject private var session: Session
    @StateObject private var run = Runner()
    @State private var usage: Usage?
    @State private var current = ""
    @State private var new = ""
    @State private var deleting = ""
    @State private var confirming = false
    @AppStorage(Prefs.experiment) private var experiment = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.xl) {
                accountCard
                passwordCard
                keyboardCard
                experimentCard
                Button("로그아웃") { logout() }
                    .buttonStyle(.secondary)
                deleteCard
            }
            .padding(.horizontal, Theme.Space.l)
            .padding(.top, Theme.Space.s)
            .padding(.bottom, Theme.Space.xxl)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(Theme.bg.ignoresSafeArea())
        .status(run)
        .navigationTitle("계정")
        .task { usage = try? await session.api?.usage() }
        .confirmationDialog("\(session.username) 계정과 모든 데이터를 지워요. 되돌릴 수 없어요.",
                            isPresented: $confirming, titleVisibility: .visible) {
            Button("계정 삭제", role: .destructive) { deleteAccount() }
        }
    }

    // MARK: - 구역

    /// 누구로 로그인했는지와 오늘 사용량. 숫자를 크게, 진행 막대는 강조색 하나.
    private var accountCard: some View {
        VStack(alignment: .leading, spacing: Theme.Space.l) {
            Label {
                Text(session.username).font(.headline).foregroundStyle(Theme.ink)
            } icon: {
                Image(systemName: "person.crop.circle.fill").font(.title2).foregroundStyle(Theme.muted)
            }
            if let usage {
                VStack(alignment: .leading, spacing: Theme.Space.s) {
                    Text("오늘 다듬은 횟수").font(.footnote).foregroundStyle(Theme.muted)
                    (Text("\(usage.rewrites)").font(.largeTitle.weight(.bold)).foregroundColor(Theme.ink)
                        + Text(" / \(usage.rewrite_limit)회").font(.title3).foregroundColor(Theme.muted))
                        .monospacedDigit()
                    ProgressView(value: Self.fraction(usage))
                        .tint(Theme.accent)
                        .accessibilityHidden(true)
                    if experiment {
                        Text("\(usage.input_tokens + usage.output_tokens) 토큰")
                            .font(.footnote).foregroundStyle(Theme.muted)
                    }
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("오늘 \(usage.rewrite_limit)회 중 \(usage.rewrites)회 다듬었어요")
            }
        }
        .card()
    }

    private var passwordCard: some View {
        Block(title: "비밀번호", footer: "바꾸면 다른 기기의 로그인은 모두 풀려요.") {
            SecureField("현재 비밀번호", text: $current).textContentType(.password).modifier(FieldFace())
            SecureField("새 비밀번호", text: $new).textContentType(.newPassword).modifier(FieldFace())
            Button("비밀번호 바꾸기") { changePassword() }
                .buttonStyle(.secondary)
                .disabled(current.isEmpty || new.isEmpty)
        }
    }

    private var keyboardCard: some View {
        Block(title: "키보드") {
            VStack(alignment: .leading, spacing: Theme.Space.m) {
                KeyboardStep(number: 1, text: "설정 → 일반 → 키보드 → 키보드 → 새로운 키보드 추가에서 Persona_ize를 골라요.")
                KeyboardStep(number: 2, text: "Persona_ize → ‘전체 접근 허용’을 켜요. 메신저 입력창의 초안을 서버로 보내 다듬으려면 필요해요.")
                KeyboardStep(number: 3, text: "메신저에서 지구본을 눌러 Persona_ize로 바꾸면 입력창의 초안을 바로 다듬을 수 있어요.")
            }
            Text("전체 접근을 켜지 않아도 앱에서 ‘키보드에 준비’한 문장은 넣을 수 있어요. 키보드는 버튼을 누를 때만 입력창 글자를 읽어요.")
                .font(.footnote)
                .foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
            Button("준비한 문장 지우기") {
                run {
                    try KeyboardMessage.clear()
                    run.notice = "키보드에 준비한 문장을 지웠어요."
                }
            }
            .buttonStyle(.secondary)
        }
    }

    /// 평가·시연용 값(조건·상황·토큰 수)을 화면에 보일지. 평소에는 꺼 둔다.
    private var experimentCard: some View {
        Block(title: "실험 설정", footer: "켜면 작성 화면에 조건·상황 입력이, 이력에 조건이, 여기에 토큰 수가 보여요. 평가나 시연 때만 켜요.") {
            Toggle("실험용 값 보기", isOn: $experiment)
                .font(.body).foregroundStyle(Theme.ink).tint(Theme.accent)
                .frame(minHeight: Theme.minTouch)
        }
    }

    /// 맨 아래에 떨어뜨려 둔 위험 구역. 비밀번호 확인 + 확인 대화상자를 거친다.
    private var deleteCard: some View {
        Block(title: "계정 삭제", footer: "상대·대화·다듬은 글·기억하고 있는 일을 모두 지워요. 되돌릴 수 없어요.") {
            SecureField("비밀번호 확인", text: $deleting).textContentType(.password).modifier(FieldFace())
            Button(role: .destructive) { confirming = true } label: {
                Label("계정 삭제", systemImage: "trash")
            }
            .buttonStyle(DangerButtonStyle())
            .disabled(deleting.isEmpty)
        }
        .padding(.top, Theme.Space.xl)
    }

    private static func fraction(_ usage: Usage) -> Double {
        guard usage.rewrite_limit > 0 else { return 0 }
        return min(max(Double(usage.rewrites) / Double(usage.rewrite_limit), 0), 1)
    }

    // MARK: - 동작

    private func logout() {
        run {
            try KeyboardMessage.clear()
            try await session.api?.logout()
            session.end()
        }
    }

    private func changePassword() {
        run {
            try await session.api?.changePassword(current: current, new: new)
            current = ""
            new = ""
            run.notice = "비밀번호를 바꿨어요."
        }
    }

    private func deleteAccount() {
        run {
            try await session.api?.deleteAccount(password: deleting)
            deleting = ""
            session.end()
        }
    }
}

/// 키보드 켜는 방법의 한 단계: 번호 동그라미와 한 문장.
private struct KeyboardStep: View {
    let number: Int
    let text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Space.m) {
            Text("\(number)")
                .font(.footnote.weight(.bold)).monospacedDigit()
                .foregroundStyle(Theme.accentStrong)
                .frame(width: Theme.Space.xl, height: Theme.Space.xl)
                .background(Theme.accentSoft, in: Circle())
                .accessibilityHidden(true)
            Text(text)
                .font(.subheadline).foregroundStyle(Theme.ink2)
                .lineSpacing(Theme.Space.xs)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(number)단계. \(text)")
    }
}

/// 제목 + 카드 + 조용한 설명으로 된 구역 하나.
private struct Block<Content: View>: View {
    let title: String
    var footer: String?
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Space.s) {
            Text(title)
                .font(.headline)
                .foregroundStyle(Theme.ink)
                .padding(.horizontal, Theme.Space.xs)
                .accessibilityAddTraits(.isHeader)
            VStack(alignment: .leading, spacing: Theme.Space.m) { content }
                .card()
            if let footer {
                Text(footer)
                    .font(.footnote)
                    .foregroundStyle(Theme.muted)
                    .padding(.horizontal, Theme.Space.xs)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// 카드 안 입력 칸: 테두리 없이 한 단계 어두운 면(surface2). 높이는 글자 크기에 따라 늘어난다.
private struct FieldFace: ViewModifier {
    func body(content: Content) -> some View {
        content
            .font(.body)
            .padding(.horizontal, Theme.Space.m)
            .frame(minHeight: Theme.controlHeight)
            .background(Theme.surface2, in: RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
    }
}

/// 되돌릴 수 없는 행동. 보조 버튼과 같은 모양에 위험색(bad)과 기호를 쓴다.
private struct DangerButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { Face(configuration: configuration) }

    private struct Face: View {
        let configuration: Configuration
        @Environment(\.isEnabled) private var enabled

        var body: some View {
            configuration.label
                .font(.subheadline.weight(.semibold))
                .frame(maxWidth: .infinity, minHeight: Theme.minTouch)
                .foregroundStyle(enabled ? Theme.bad : Theme.muted)
                .background(enabled ? Theme.badSoft : Theme.surface2,
                            in: RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
                .opacity(configuration.isPressed ? 0.7 : 1)
                .contentShape(Rectangle())
        }
    }
}
