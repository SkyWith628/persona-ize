import SwiftUI
import UniformTypeIdentifiers

/// 상대 목록과 등록. 상대를 누르면 페르소나·과거 대화로 들어간다.
struct ContactsView: View {
    @EnvironmentObject private var session: Session
    @StateObject private var run = Runner()
    @State private var alias = ""
    @State private var relationship = "colleague"

    var body: some View {
        Form {
            Section("상대") {
                if session.contacts.isEmpty { Text("아직 상대가 없습니다. 아래에서 별칭과 관계를 넣고 추가하세요.") }
                ForEach(session.contacts) { contact in
                    NavigationLink(value: contact) {
                        VStack(alignment: .leading) {
                            Text(contact.alias)
                            Text(Labels.relationship(contact.relationship_type)
                                 + (contact.persona_version.map { " · 페르소나 v\($0)" } ?? " · 페르소나 없음"))
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            Section {
                TextField("별칭 (예: 박지현)", text: $alias)
                Picker("관계", selection: $relationship) {
                    ForEach(Labels.relationships, id: \.0) { Text($0.1).tag($0.0) }
                }
                Button("추가") { add() }.disabled(alias.trimmingCharacters(in: .whitespaces).isEmpty)
            } header: {
                Text("상대 추가")
            } footer: {
                Text("실명 대신 별칭을 써도 됩니다.")
            }
        }
        .status(run)
        .navigationTitle("상대")
        .navigationDestination(for: Contact.self) { ContactDetailView(contact: $0) }
        .refreshable { try? await session.reload() }
    }

    private func add() {
        run {
            try await session.api?.addContact(alias: String(alias.trimmingCharacters(in: .whitespaces).prefix(50)),
                                              relationship: relationship)
            alias = ""
            try await session.reload()
        }
    }
}

/// 상대 한 명의 페르소나 편집, 과거 대화방 목록, 삭제.
private struct ContactDetailView: View {
    let contact: Contact
    @EnvironmentObject private var session: Session
    @Environment(\.dismiss) private var dismiss
    @StateObject private var run = Runner()
    @State private var version: Int?
    @State private var settings = PersonaSettings()
    @State private var address = ""
    @State private var preferred = ""
    @State private var forbidden = ""
    @State private var notes = ""
    @State private var conversations: [Conversation] = []
    @State private var title = ""
    @State private var meName = ""
    @State private var confirming = false

    var body: some View {
        Form {
            Section {
                Stepper("격식 · \(Labels.step(Labels.formality, settings.formality))", value: $settings.formality, in: 1...5)
                Stepper("친근함 · \(Labels.step(Labels.friendliness, settings.friendliness))", value: $settings.friendliness, in: 1...5)
                Stepper("돌려 말하기 ↔ 직설 · \(Labels.step(Labels.directness, settings.directness))", value: $settings.directness, in: 1...5)
                Picker("길이", selection: $settings.length) {
                    Text("짧게").tag("shorter"); Text("원문만큼").tag("same"); Text("길게").tag("longer")
                }
                Picker("이모지", selection: $settings.emoji) {
                    Text("없음").tag("none"); Text("가끔").tag("rare"); Text("자주").tag("often")
                }
                TextField("호칭 (예: 팀장님, 형)", text: $address)
                TextField("선호 표현 (쉼표로 구분)", text: $preferred)
                TextField("금지 표현 (예: ㅋㅋ, ㅠ)", text: $forbidden)
                TextField("메모 (예: 업무 얘기는 결론부터)", text: $notes, axis: .vertical)
                Button(version == nil ? "저장" : "저장 (새 버전)") { save() }
            } header: {
                Text(version.map { "페르소나 v\($0)" } ?? "페르소나 (저장 전)")
            } footer: {
                Text("각 항목은 5단계입니다. 직접 고른 값이며 서버가 추정하지 않습니다. 호칭은 여기 넣은 것만 씁니다.")
            }
            Section {
                if conversations.isEmpty { Text("대화방이 없습니다. 없어도 A·B 조건 첨삭은 됩니다.") }
                ForEach(conversations) { conversation in
                    NavigationLink(conversation.title) {
                        ConversationView(conversation: conversation, contact: contact) { await loadConversations() }
                    }
                }
                TextField("대화방 이름", text: $title)
                TextField("내 이름 (그래프용, 예: 김민수)", text: $meName)
                Button("대화방 추가") { addConversation() }
            } header: {
                Text("과거 대화")
            } footer: {
                Text("C·D 조건이 참고하는 재료입니다. 합성 대화만 넣으세요.")
            }
            Section {
                NavigationLink("맥락 그래프 보기") { ContactGraphView(contact: contact) }
            } footer: {
                Text("가져온 대화에서 서버가 뽑은 사람·사건·사실입니다. D 조건 첨삭이 참고합니다.")
            }
            Section {
                Button("이 상대 삭제", role: .destructive) { confirming = true }
            }
        }
        .status(run)
        .navigationTitle(contact.alias)
        .navigationBarTitleDisplayMode(.inline)
        .task { run { try await load() } }
        .confirmationDialog("\(contact.alias)의 대화방·페르소나·첨삭 이력을 모두 지웁니다. 되돌릴 수 없습니다.",
                            isPresented: $confirming, titleVisibility: .visible) {
            Button("삭제", role: .destructive) { remove() }
        }
    }

    private func load() async throws {
        guard let api = session.api else { return }
        if let persona = try await api.persona(contact: contact.id) { show(persona) }
        conversations = try await api.conversations(contact: contact.id)
    }

    private func loadConversations() async {
        guard let list = try? await session.api?.conversations(contact: contact.id) else { return }
        conversations = list
    }

    private func show(_ persona: Persona) {
        version = persona.version
        settings = persona.settings
        address = persona.settings.address_term ?? ""
        preferred = persona.settings.preferred_phrases.joined(separator: ", ")
        forbidden = persona.settings.forbidden_phrases.joined(separator: ", ")
        notes = persona.settings.notes ?? ""
    }

    private func phrases(_ text: String) -> [String] {
        text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }

    /// 서버가 버전을 비교한다. 다른 기기에서 먼저 저장했으면 409 메시지가 표시된다.
    private func save() {
        run {
            guard let api = session.api else { return }
            var next = settings
            let term = address.trimmingCharacters(in: .whitespaces)
            let memo = notes.trimmingCharacters(in: .whitespacesAndNewlines)
            next.address_term = term.isEmpty ? nil : term
            next.preferred_phrases = phrases(preferred)
            next.forbidden_phrases = phrases(forbidden)
            next.notes = memo.isEmpty ? nil : memo
            show(try await api.savePersona(contact: contact.id, settings: next, expected: version ?? 0))
            try await session.reload()
            run.notice = "페르소나를 저장했습니다."
        }
    }

    private func addConversation() {
        run {
            try await session.api?.addConversation(contact: contact.id,
                                                   title: title.trimmingCharacters(in: .whitespaces),
                                                   meName: meName.trimmingCharacters(in: .whitespaces))
            title = ""
            await loadConversations()
        }
    }

    private func remove() {
        run {
            try await session.api?.deleteContact(contact.id)
            try await session.reload()
            dismiss()
        }
    }
}

/// 대화방 하나: 대화 가져오기(M5), 분석 두 단계 표시(M6), 저장된 메시지, 삭제.
private struct ConversationView: View {
    let conversation: Conversation
    let contact: Contact
    let changed: () async -> Void
    @EnvironmentObject private var session: Session
    @Environment(\.dismiss) private var dismiss
    @StateObject private var run = Runner()
    @State private var messages: [Message] = []
    @State private var method = Method.paste
    @State private var text = ""
    @State private var meName = ""
    @State private var job: ImportJob?
    @State private var picking = false
    @State private var confirming = false
    @FocusState private var editing: Bool

    private enum Method: String, CaseIterable { case paste = "붙여넣기", telegram = "텔레그램", kakao = "카카오톡" }

    var body: some View {
        Form {
            Section {
                Picker("가져오는 방법", selection: $method) {
                    ForEach(Method.allCases, id: \.self) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                switch method {
                case .kakao:
                    Text("카카오톡 내보내기(.txt)는 기기와 버전에 따라 형식이 달라 아직 자동으로 읽지 못합니다. 대화를 복사해 ‘붙여넣기’ 형식으로 넣어 주세요.")
                case .telegram:
                    Text("PC용 텔레그램에서만 만들 수 있어요. 대화방 메뉴 → 채팅 기록 내보내기 → JSON으로 만든 result.json을 첨부하세요.")
                    inputs
                case .paste:
                    Text("한 줄에 한 메시지: 2026-10-05 09:25 | 박지현 | 내용. 이름 대신 me / contact도 됩니다.").font(.footnote)
                    inputs
                }
            } header: {
                Text("상대와 나눈 대화를 올려주세요")
            } footer: {
                Text("🔒 올린 대화는 내 계정에만 저장되고 말투 분석에 씁니다. 상대별로 언제든 지울 수 있어요. 시연에는 합성 대화만 넣으세요.")
            }
            if let job { analysis(job) }
            Section("메시지 \(messages.count)건") {
                ForEach(messages) { message in
                    VStack(alignment: .leading) {
                        Text((message.speaker_role == "me" ? "나" : contact.alias) + " · " + Labels.date(message.sent_at))
                            .font(.footnote).foregroundStyle(.secondary)
                        Text(message.text)
                    }
                }
            }
            Section {
                Button("이 대화방 삭제", role: .destructive) { confirming = true }
            }
        }
        .status(run)
        .navigationTitle(conversation.title)
        .navigationBarTitleDisplayMode(.inline)
        .task {
            meName = conversation.me_name ?? ""
            run { try await loadMessages() }
        }
        .task(id: job?.id) { await followGraph() }
        .fileImporter(isPresented: $picking, allowedContentTypes: [.plainText, .json]) { picked in
            run { text = try Self.read(try picked.get()) }
        }
        .confirmationDialog("이 대화방의 원문과 문체 묶음을 지웁니다. 되돌릴 수 없습니다.",
                            isPresented: $confirming, titleVisibility: .visible) {
            Button("삭제", role: .destructive) { remove() }
        }
    }

    @ViewBuilder
    private var inputs: some View {
        TextEditor(text: $text).frame(minHeight: 110).accessibilityLabel("가져올 합성 대화").focused($editing)
            .font(.footnote.monospaced())
        Button("대화 파일 첨부하기 (.txt · .json)") { editing = false; picking = true }
        TextField("내 이름으로 쓸 화자 (예: 김민수)", text: $meName)
        Button("가져오기") { importText() }
            .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }

    /// 말투(문체 묶음)는 가져오는 즉시 쓸 수 있고, 사건 정리(그래프)는 서버에서 뒤이어 돈다.
    @ViewBuilder
    private func analysis(_ job: ImportJob) -> some View {
        Section {
            Label("말투 분석 완료 · 지금 바로 쓸 수 있어요", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
            Text("메시지 \(messages.count)개 중 내 발화 \(messages.filter { $0.speaker_role == "me" }.count)개 · 이번에 \(job.imported)건 추가, \(job.duplicates)건 중복, 문체 묶음 \(job.episodes)개")
                .font(.footnote)
            switch job.graph_status {
            case "pending", "running":
                ProgressView(value: Double(min(job.graph_episodes, job.imported)), total: Double(max(job.imported, 1))) {
                    Text("지난 약속·사건 정리 중 \(job.graph_episodes) / \(job.imported)").font(.footnote)
                }
                Text("화면을 닫아도 서버에서 계속됩니다. 끝나면 지난번에 약속한 내용까지 참고합니다.").font(.footnote).foregroundStyle(.secondary)
            case "completed":
                Label("지난 약속·사건 정리 완료 (\(job.graph_episodes)개)", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
            case "failed":
                Text("지난 약속·사건 정리 실패: \(job.graph_error ?? "사유 없음")")
            default:
                EmptyView()
            }
        } header: {
            Text("분석")
        } footer: {
            Text("이 상대에게 쓰는 말투는 이전 화면의 페르소나에서 직접 정합니다.")
        }
    }

    /// 고른 파일을 글로 읽는다. 큰 파일은 요청 한 번에 담기 어려워 막는다.
    private static func read(_ url: URL) throws -> String {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let data = try Data(contentsOf: url)
        guard data.count <= 2_000_000 else { throw ServiceError(message: "파일이 너무 큽니다(2MB 초과). 기간을 나눠 내보내 주세요.") }
        guard let text = String(data: data, encoding: .utf8) else { throw ServiceError(message: "UTF-8 글 파일만 읽을 수 있습니다.") }
        return text
    }

    private func loadMessages() async throws {
        guard let api = session.api else { return }
        messages = try await api.messages(conversation: conversation.id)
    }

    private func importText() {
        editing = false
        run {
            guard let api = session.api else { return }
            let payload = try ImportParser.parse(text, me: meName.trimmingCharacters(in: .whitespaces),
                                                 contact: contact.alias)
            job = try await api.importMessages(conversation: conversation.id, payload: payload)
            text = ""
            try await loadMessages()
        }
    }

    /// 그래프 수집 진행을 4초마다 확인한다. 화면을 떠나면 확인만 멈춘다(수집은 계속된다).
    private func followGraph() async {
        guard let api = session.api else { return }
        while let current = job, current.graphActive {
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            if Task.isCancelled { return }
            if let next = try? await api.importJob(current.id) { job = next }
        }
    }

    private func remove() {
        run {
            try await session.api?.deleteConversation(conversation.id)
            await changed()
            dismiss()
        }
    }
}
