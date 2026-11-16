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
        Form {
            Section("계정") {
                Text(session.username)
                if let usage {
                    Text("오늘 첨삭 \(usage.rewrites)/\(usage.rewrite_limit)회 · \(usage.input_tokens + usage.output_tokens) 토큰")
                        .foregroundStyle(.secondary)
                }
                Button("로그아웃") { logout() }
            }
            Section {
                SecureField("현재 비밀번호", text: $current).textContentType(.password)
                SecureField("새 비밀번호", text: $new).textContentType(.newPassword)
                Button("비밀번호 변경") { changePassword() }.disabled(current.isEmpty || new.isEmpty)
            } header: {
                Text("비밀번호")
            } footer: {
                Text("변경하면 다른 기기의 로그인은 모두 풀립니다.")
            }
            Section("키보드") {
                Text("설정 → 일반 → 키보드 → 키보드 → 새로운 키보드 추가에서 Persona_ize를 선택하세요. Persona_ize → ‘전체 접근 허용’을 켜면 메신저에서 지구본으로 전환해 입력창의 초안을 바로 다듬을 수 있습니다. 켜지 않으면 앱에서 ‘키보드에 준비’한 문장만 넣습니다. 키보드는 버튼을 누를 때만 입력창 글자를 읽어 서버로 보냅니다.")
                Button("준비한 문장 지우기") {
                    run {
                        try KeyboardMessage.clear()
                        run.notice = "키보드에 준비한 문장을 지웠습니다."
                    }
                }
            }
            Section {
                SecureField("비밀번호 확인", text: $deleting).textContentType(.password)
                Button("계정 삭제", role: .destructive) { confirming = true }.disabled(deleting.isEmpty)
            } header: {
                Text("계정 삭제")
            } footer: {
                Text("상대·대화·첨삭 이력·맥락 그래프를 모두 지웁니다. 되돌릴 수 없습니다.")
            }
        }
        .status(run)
        .navigationTitle("계정")
        .task { usage = try? await session.api?.usage() }
        .confirmationDialog("\(session.username) 계정과 모든 데이터를 지웁니다. 되돌릴 수 없습니다.",
                            isPresented: $confirming, titleVisibility: .visible) {
            Button("계정 삭제", role: .destructive) { deleteAccount() }
        }
    }

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
