import SwiftUI

/// 오늘 사용량, 비밀번호 변경, 키보드 안내, 로그아웃, 계정 삭제.
struct AccountView: View {
    @EnvironmentObject private var session: Session
    @StateObject private var run = Runner()
    @State private var usage: Usage?
    @State private var current = ""
    @State private var new = ""
    @State private var deleting = ""
    @State private var confirming = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.xl) {
                accountCard
                passwordCard
                keyboardCard
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
        .confirmationDialog("\(session.username) 계정과 모든 데이터를 지웁니다. 되돌릴 수 없습니다.",
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
                    Text("오늘 첨삭").font(.footnote).foregroundStyle(Theme.muted)
                    (Text("\(usage.rewrites)").font(.largeTitle.weight(.bold)).foregroundColor(Theme.ink)
                        + Text(" / \(usage.rewrite_limit)회").font(.title3).foregroundColor(Theme.muted))
                        .monospacedDigit()
                    ProgressView(value: Self.fraction(usage))
                        .tint(Theme.accent)
                        .accessibilityHidden(true)
                    Text("\(usage.input_tokens + usage.output_tokens) 토큰")
                        .font(.footnote).foregroundStyle(Theme.muted)
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("오늘 첨삭 \(usage.rewrites)/\(usage.rewrite_limit)회 · \(usage.input_tokens + usage.output_tokens) 토큰")
            }
        }
        .card()
    }

    private var passwordCard: some View {
        Block(title: "비밀번호", footer: "변경하면 다른 기기의 로그인은 모두 풀립니다.") {
            SecureField("현재 비밀번호", text: $current).textContentType(.password).modifier(FieldFace())
            SecureField("새 비밀번호", text: $new).textContentType(.newPassword).modifier(FieldFace())
            Button("비밀번호 변경") { changePassword() }
                .buttonStyle(.secondary)
                .disabled(current.isEmpty || new.isEmpty)
        }
    }

    private var keyboardCard: some View {
        Block(title: "키보드") {
            Text("설정 → 일반 → 키보드 → 키보드 → 새로운 키보드 추가에서 Persona_ize를 선택하세요. Persona_ize → ‘전체 접근 허용’을 켜면 메신저에서 지구본으로 전환해 입력창의 초안을 바로 다듬을 수 있습니다. 켜지 않으면 앱에서 ‘키보드에 준비’한 문장만 넣습니다. 키보드는 버튼을 누를 때만 입력창 글자를 읽어 서버로 보냅니다.")
                .font(.subheadline)
                .foregroundStyle(Theme.ink2)
                .lineSpacing(Theme.Space.xs)
                .fixedSize(horizontal: false, vertical: true)
            Button("준비한 문장 지우기") {
                run {
                    try KeyboardMessage.clear()
                    run.notice = "키보드에 준비한 문장을 지웠습니다."
                }
            }
            .buttonStyle(.secondary)
        }
    }

    /// 맨 아래에 떨어뜨려 둔 위험 구역. 비밀번호 확인 + 확인 대화상자를 거친다.
    private var deleteCard: some View {
        Block(title: "계정 삭제", footer: "상대·대화·첨삭 이력·맥락 그래프를 모두 지웁니다. 되돌릴 수 없습니다.") {
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
            run.notice = "비밀번호를 바꿨습니다."
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
