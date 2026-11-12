import SwiftUI

@main
struct PersonaApp: App {
    var body: some Scene { WindowGroup { Composer() } }
}

private struct Composer: View {
    @State private var server = ""
    @State private var username = ""
    @State private var password = ""
    @State private var api: API?
    @State private var contacts: [Contact] = []
    @State private var contact = ""
    @State private var draft = ""
    @State private var result: Recommendation?
    @State private var key = UUID()
    @State private var busy = false
    @State private var notice = ""

    private var hasPersona: Bool {
        contacts.first { $0.id == contact }?.persona_version != nil
    }

    var body: some View {
        NavigationStack {
            Form {
                if api == nil {
                    Section("서버 로그인") {
                        TextField("HTTPS 서버 주소", text: $server).keyboardType(.URL)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                        TextField("아이디", text: $username).textContentType(.username)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                        SecureField("비밀번호", text: $password).textContentType(.password)
                        Button("로그인") { perform {
                            let client = try API(server: server)
                            let list = try await client.login(name: username, password: password)
                            password = ""
                            contacts = list
                            contact = list.first { $0.persona_version != nil }?.id ?? list.first?.id ?? ""
                            api = client
                        } }.disabled(server.isEmpty || username.isEmpty || password.isEmpty)
                    }
                } else {
                    Section("메시지 초안") {
                        if contacts.isEmpty { Text("관리 화면에서 상대를 등록한 뒤 다시 로그인하세요.") }
                        Picker("대화 상대", selection: $contact) {
                            ForEach(contacts) { Text($0.alias).tag($0.id) }
                        }
                        if !contacts.isEmpty && !hasPersona {
                            Text("관리 화면에서 이 상대의 페르소나를 저장한 뒤 다시 로그인하세요.")
                        }
                        TextEditor(text: $draft).frame(minHeight: 90).accessibilityLabel("합성 메시지 초안")
                        Text("시연에는 합성 대화만 입력하세요. 첨삭을 누르면 서버에서 AI가 초안을 처리합니다.")
                            .font(.footnote).foregroundStyle(.secondary)
                        Button("첨삭하기") { perform {
                            guard let api else { return }
                            let current = key
                            let response = try await api.rewrite(contact: contact, draft: draft, key: current)
                            if current == key { result = response; key = UUID() }
                        } }.disabled(!hasPersona || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                    if let result {
                        Section(result.status == .recommended ? "추천 문장" : "원문 유지") {
                            Text(result.status == .recommended ? result.result_text : draft)
                            if !result.preserved_items.isEmpty {
                                Text("보존 항목 · " + result.preserved_items.map(\.text).joined(separator: ", "))
                            }
                            ForEach(result.warnings.indices, id: \.self) { Text(result.warnings[$0]) }
                            if result.status == .recommended {
                                Button("초안에 적용") { draft = result.result_text; self.result = nil }
                                Button("키보드에 준비") { perform {
                                    try KeyboardMessage.save(result.result_text)
                                    notice = "Persona_ize 키보드에서 문장을 확인하고 넣으세요."
                                } }
                            }
                        }
                    }
                    Button("로그아웃") { perform {
                        try KeyboardMessage.clear()
                        try await api?.logout()
                        api = nil; contacts = []; draft = ""; result = nil
                    } }
                }
                Section("키보드") {
                    Text("설정 → 일반 → 키보드 → 키보드 → 새로운 키보드 추가에서 Persona_ize를 선택하세요.")
                    Button("준비한 문장 지우기") { perform {
                        try KeyboardMessage.clear()
                        notice = "키보드에 준비한 문장을 지웠습니다."
                    } }
                }
                if busy { ProgressView("처리 중") }
                if !notice.isEmpty { Text(notice).accessibilityLabel(notice) }
            }
            .disabled(busy)
            .navigationTitle("Persona_ize")
            .onChange(of: draft) { _ in invalidate() }
            .onChange(of: contact) { _ in invalidate() }
        }
    }

    private func invalidate() { key = UUID(); result = nil; notice = "" }

    private func perform(_ action: @escaping @MainActor () async throws -> Void) {
        guard !busy else { return }
        busy = true; notice = ""
        Task { @MainActor in
            defer { busy = false }
            do { try await action() } catch { notice = error.localizedDescription }
        }
    }
}
