import SwiftUI
import UniformTypeIdentifiers

/// 상대 목록과 등록. 상대를 누르면 말투(페르소나)·예전 대화로 들어간다.
struct ContactsView: View {
    @EnvironmentObject private var session: Session
    @StateObject private var run = Runner()
    @State private var alias = ""
    @State private var relationship = "colleague"
    @State private var adding = false

    var body: some View {
        List {
            if session.contacts.isEmpty {
                ContactsEmptyState(systemImage: "person.2",
                                   text: "아직 상대가 없어요. 아래 버튼으로 별칭과 관계를 넣고 추가해 주세요.")
            } else {
                Section {
                    ForEach(session.contacts) { contact in
                        NavigationLink(value: contact) { row(contact) }
                    }
                }
                .listRowBackground(Theme.surface)
            }
        }
        .screenBackground()
        .tint(Theme.accent)
        .contactsBottomBar {
            Button { adding = true } label: { Label("상대 추가", systemImage: "plus") }
                .buttonStyle(.primary)
        }
        .status(run)
        .navigationTitle("상대")
        .navigationDestination(for: Contact.self) { ContactDetailView(contact: $0) }
        .refreshable { try? await session.reload() }
        .sheet(isPresented: $adding) { addSheet }
    }

    /// 별칭을 크게, 관계는 꼬리표로. 말투를 아직 안 정한 상대는 다음에 할 일을 강조색 글자로 알린다.
    private func row(_ contact: Contact) -> some View {
        VStack(alignment: .leading, spacing: Theme.Space.s) {
            Text(contact.alias).font(.title3.weight(.semibold)).foregroundStyle(Theme.ink)
            HStack(spacing: Theme.Space.s) {
                Tag(text: Labels.relationship(contact.relationship_type))
                if contact.persona_version != nil {
                    Tag(text: "말투 준비됨", kind: .ok, systemImage: "checkmark.circle.fill")
                } else {
                    Text("말투 정하기").font(.footnote.weight(.semibold)).foregroundStyle(Theme.accentStrong)
                }
            }
        }
        .padding(.vertical, Theme.Space.xs)
    }

    /// 상대 추가는 화면을 옮기지 않고 시트에서 받는다. 주 버튼은 시트 아래쪽.
    private var addSheet: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("별칭 (예: 박지현)", text: $alias)
                    Picker("관계", selection: $relationship) {
                        ForEach(Labels.relationships, id: \.0) { Text($0.1).tag($0.0) }
                    }
                } footer: {
                    Text("실명 대신 별칭을 써도 돼요.")
                }
                .listRowBackground(Theme.surface)
            }
            .screenBackground()
            .tint(Theme.accent)
            .contactsBottomBar {
                Button("추가") { add() }
                    .buttonStyle(.primary)
                    .disabled(alias.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .status(run)
            .navigationTitle("상대 추가")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("닫기") { adding = false } }
            }
        }
        .presentationDetents([.medium, .large])
    }

    private func add() {
        run {
            try await session.api?.addContact(alias: String(alias.trimmingCharacters(in: .whitespaces).prefix(50)),
                                              relationship: relationship)
            alias = ""
            adding = false
            try await session.reload()
        }
    }
}

// MARK: - 이 파일 안에서만 쓰는 조각

private extension View {
    /// 화면의 주 버튼을 아래쪽(엄지가 닿는 곳)에 고정한다.
    func contactsBottomBar<Bar: View>(@ViewBuilder _ bar: () -> Bar) -> some View {
        safeAreaInset(edge: .bottom) {
            bar()
                .padding(.horizontal, Theme.Space.l)
                .padding(.vertical, Theme.Space.m)
                .background(Theme.bg)
        }
    }
}

/// 빈 상태: 큰 기호와 한 줄 안내. 주 버튼은 화면 아래쪽에 따로 있다.
private struct ContactsEmptyState: View {
    let systemImage: String
    let text: String

    var body: some View {
        VStack(spacing: Theme.Space.l) {
            Image(systemName: systemImage).font(.largeTitle).imageScale(.large)
                .foregroundStyle(Theme.muted).accessibilityHidden(true)
            Text(text).font(.body).foregroundStyle(Theme.ink2).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, Theme.Space.xxxl)
        .listRowBackground(Color.clear)
        .listRowSeparator(.hidden)
    }
}

/// 페르소나 5단계 한 줄: 항목 이름은 작게, 지금 값의 말 풀이는 크게, 단계는 막대 다섯 칸으로.
private struct PersonaStepRow: View {
    let title: String
    let names: [String]
    @Binding var value: Int

    private static let barWidth: CGFloat = 120

    var body: some View {
        Stepper(value: $value, in: 1...5) {
            VStack(alignment: .leading, spacing: Theme.Space.xs) {
                Text(title).font(.footnote).foregroundStyle(Theme.muted)
                Text(Labels.step(names, value)).font(.headline).foregroundStyle(Theme.ink)
                HStack(spacing: Theme.Space.xs) {
                    ForEach(1...5, id: \.self) { step in
                        Capsule().fill(step <= value ? Theme.accent : Theme.line).frame(height: Theme.Space.xs)
                    }
                }
                .frame(maxWidth: Self.barWidth)
                .accessibilityHidden(true)
            }
            .padding(.vertical, Theme.Space.xs)
        }
        .accessibilityLabel("\(title) · \(Labels.step(names, value))")
        .accessibilityValue("5단계 중 \(value)")
    }
}

/// 진행 상태 한 줄: 색만이 아니라 기호와 문구로 알린다.
private struct ImportStatusRow: View {
    let systemImage: String
    let tint: Color
    let title: String
    var detail: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Space.m) {
            Image(systemName: systemImage).foregroundStyle(tint).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: Theme.Space.xs) {
                Text(title).font(.headline).foregroundStyle(Theme.ink)
                if let detail { Text(detail).font(.footnote).foregroundStyle(Theme.ink2) }
            }
        }
        .padding(.vertical, Theme.Space.xs)
        .accessibilityElement(children: .combine)
    }
}

/// 삭제처럼 되돌릴 수 없는 행동의 줄. 확인 절차는 부르는 쪽이 그대로 가진다.
private struct DestructiveRow: View {
    let title: String
    let action: () -> Void

    var body: some View {
        Button(role: .destructive, action: action) {
            Label(title, systemImage: "trash").foregroundStyle(Theme.bad)
                .frame(maxWidth: .infinity, minHeight: Theme.minTouch, alignment: .leading)
                .contentShape(Rectangle())
        }
        .listRowBackground(Theme.surface)
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
                PersonaStepRow(title: "격식", names: Labels.formality, value: $settings.formality)
                PersonaStepRow(title: "친근함", names: Labels.friendliness, value: $settings.friendliness)
                PersonaStepRow(title: "돌려 말하기 ↔ 직설", names: Labels.directness, value: $settings.directness)
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
            } header: {
                HStack(spacing: Theme.Space.s) {
                    Text("이 사람에게 쓰는 말투")
                    if version != nil {
                        Tag(text: "저장됨", kind: .ok, systemImage: "checkmark.circle.fill")
                    } else {
                        Tag(text: "저장 전", kind: .warn, systemImage: "exclamationmark.circle")
                    }
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(version == nil ? "이 사람에게 쓰는 말투 (저장 전)" : "이 사람에게 쓰는 말투 (저장됨)")
            } footer: {
                Text("각 항목은 5단계예요. 직접 고른 값만 쓰고, 호칭은 여기 넣은 것만 써요.")
            }
            .listRowBackground(Theme.surface)
            Section {
                if conversations.isEmpty {
                    Text("아직 올린 대화가 없어요. 없어도 다듬을 수 있어요.").font(.subheadline).foregroundStyle(Theme.ink2)
                }
                ForEach(conversations) { conversation in
                    NavigationLink {
                        ConversationView(conversation: conversation, contact: contact) { await loadConversations() }
                    } label: {
                        Label(conversation.title, systemImage: "bubble.left.and.bubble.right")
                    }
                }
                TextField("대화방 이름", text: $title)
                TextField("대화에 나오는 내 이름 (예: 김민수)", text: $meName)
                Button { addConversation() } label: { Label("대화방 추가", systemImage: "plus") }
                    .buttonStyle(.secondary)
                    .listRowSeparator(.hidden)
            } header: {
                Text("예전 대화")
            } footer: {
                Text("예전에 쓴 표현과 약속을 참고해 다듬어요. 지금은 지어낸 대화만 넣어 주세요.")
            }
            .listRowBackground(Theme.surface)
            Section {
                NavigationLink { ContactGraphView(contact: contact) } label: {
                    Label("기억하고 있는 일 보기", systemImage: "point.3.connected.trianglepath.dotted")
                }
            } footer: {
                Text("올린 대화에서 뽑은 사람·사건·약속이에요. 다듬을 때 참고해요.")
            }
            .listRowBackground(Theme.surface)
            Section {
                DestructiveRow(title: "이 상대 삭제") { confirming = true }
            }
        }
        .screenBackground()
        .tint(Theme.accent)
        .contactsBottomBar {
            Button("말투 저장") { save() }.buttonStyle(.primary)
        }
        .status(run)
        .navigationTitle(contact.alias)
        .navigationBarTitleDisplayMode(.inline)
        .task { run { try await load() } }
        .confirmationDialog("\(contact.alias)의 대화·말투·다듬은 글을 모두 지워요. 되돌릴 수 없어요.",
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
            run.notice = "말투를 저장했어요."
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
    @State private var contactName = ""
    @State private var job: ImportJob?
    /// 이번 가져오기에서 서버가 따르지 않은 화자 지정. 가져오기 응답에만 오고 진행 확인(GET) 응답에는 없어 따로 둔다.
    @State private var ignoredRoles: [IgnoredSpeakerRole] = []
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
                .listRowSeparator(.hidden)
                switch method {
                case .kakao:
                    guide("채팅방 메뉴 → 대화 내용 내보내기로 만든 파일(.txt, PC는 .csv)을 첨부해 주세요. 1:1 대화는 내 이름을 비워 둬도 누가 나인지 알아서 정해요. 여러 명이 있는 대화는 이 상대와 내 말만 가져와요.",
                          systemImage: "info.circle")
                    inputs
                case .telegram:
                    guide("PC용 텔레그램에서만 만들 수 있어요. 대화방 메뉴 → 채팅 기록 내보내기 → JSON으로 만든 result.json을 첨부해 주세요.",
                          systemImage: "info.circle")
                    inputs
                case .paste:
                    guide("한 줄에 메시지 하나씩 써요. 예: 2026-10-05 09:25 | 박지현 | 내용. 이름 자리에 me(나)나 contact(상대)를 써도 돼요.",
                          systemImage: "info.circle")
                    inputs
                }
            } header: {
                Text("상대와 나눈 대화를 올려주세요")
            } footer: {
                Label("올린 대화는 내 계정에만 저장되고 말투 분석에 써요. 상대별로 언제든 지울 수 있어요. 지금은 지어낸 대화만 넣어 주세요.",
                      systemImage: "lock.fill")
            }
            .listRowBackground(Theme.surface)
            if let job { analysis(job) }
            Section("메시지 \(messages.count)건") {
                ForEach(messages) { message in
                    VStack(alignment: .leading, spacing: Theme.Space.xs) {
                        Text((message.speaker_role == "me" ? "나" : contact.alias) + " · " + Labels.date(message.sent_at))
                            .font(.footnote).foregroundStyle(Theme.muted)
                        Text(message.text).font(.body).foregroundStyle(Theme.ink)
                    }
                    .padding(.vertical, Theme.Space.xs)
                }
            }
            .listRowBackground(Theme.surface)
            Section {
                DestructiveRow(title: "이 대화방 삭제") { confirming = true }
            }
        }
        .screenBackground()
        .tint(Theme.accent)
        .status(run)
        .navigationTitle(conversation.title)
        .navigationBarTitleDisplayMode(.inline)
        .task {
            meName = conversation.me_name ?? ""
            run { try await loadMessages() }
        }
        .task(id: job?.id) { await followGraph() }
        .fileImporter(isPresented: $picking, allowedContentTypes: [.plainText, .commaSeparatedText, .json]) { picked in
            run { text = try Self.read(try picked.get()) }
        }
        .confirmationDialog("이 대화방의 대화와 말투 분석을 지워요. 되돌릴 수 없어요.",
                            isPresented: $confirming, titleVisibility: .visible) {
            Button("삭제", role: .destructive) { remove() }
        }
    }

    private static let pasteHeight: CGFloat = 140

    /// 형식 안내: 면(surface2) 위에 기호와 함께.
    private func guide(_ text: String, systemImage: String) -> some View {
        Label(text, systemImage: systemImage)
            .font(.footnote).foregroundStyle(Theme.ink2)
            .card(padding: Theme.Space.m, fill: Theme.surface2)
            .listRowSeparator(.hidden)
    }

    @ViewBuilder
    private var inputs: some View {
        TextEditor(text: $text).accessibilityLabel("가져올 대화").focused($editing)
            .font(.footnote.monospaced())
            .scrollContentBackground(.hidden)
            .frame(minHeight: Self.pasteHeight)
            .padding(Theme.Space.s)
            .background(Theme.surface2, in: RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
            .listRowSeparator(.hidden)
        Button { editing = false; picking = true } label: {
            Label("대화 파일 첨부하기 (.txt · .csv · .json)", systemImage: "paperclip")
        }
        .buttonStyle(.secondary)
        .listRowSeparator(.hidden)
        TextField("대화에 나오는 내 이름 (예: 김민수)", text: $meName)
        if method == .kakao {
            TextField("상대 이름 (여러 명이 있는 대화일 때)", text: $contactName)
        }
        Button("가져오기") { importText() }
            .buttonStyle(.primary)
            .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            .listRowSeparator(.hidden)
    }

    /// 말투(문체 묶음)는 가져오는 즉시 쓸 수 있고, 사건 정리(그래프)는 서버에서 뒤이어 돈다.
    @ViewBuilder
    private func analysis(_ job: ImportJob) -> some View {
        Section {
            let counts = "메시지 \(messages.count)개 중 내가 쓴 것 \(messages.filter { $0.speaker_role == "me" }.count)개 · 이번에 \(job.imported)건 추가, \(job.duplicates)건 중복"
            // 큰 파일은 가져오기 응답이 먼저 오고 말투 분석은 서버에서 뒤이어 끝난다 — 끝나기 전에는 완료라고 하지 않는다.
            switch job.status ?? "completed" {
            case "processing":
                ImportStatusRow(systemImage: "arrow.triangle.2.circlepath", tint: Theme.accent,
                                title: "말투 분석 중 · 조금 뒤부터 쓸 수 있어요", detail: counts)
            case "failed":
                ImportStatusRow(systemImage: "exclamationmark.triangle.fill", tint: Theme.bad,
                                title: "말투 분석 실패", detail: "대화는 저장됐어요. 잠시 뒤 다시 시도해 주세요.")
            default:
                ImportStatusRow(systemImage: "checkmark.circle.fill", tint: Theme.ok,
                                title: "말투 분석 완료 · 지금 바로 쓸 수 있어요", detail: counts)
            }
            switch job.graph_status {
            case "pending", "running":
                VStack(alignment: .leading, spacing: Theme.Space.s) {
                    ImportStatusRow(systemImage: "arrow.triangle.2.circlepath", tint: Theme.accent,
                                    title: "지난 약속·사건 정리 중 \(job.graph_episodes) / \(job.imported)")
                    ProgressView(value: Double(min(job.graph_episodes, job.imported)), total: Double(max(job.imported, 1)))
                        .tint(Theme.accent)
                        .accessibilityLabel("지난 약속·사건 정리 진행률")
                    Text("화면을 닫아도 계속돼요. 끝나면 지난번에 약속한 내용까지 참고해요.")
                        .font(.footnote).foregroundStyle(Theme.muted)
                }
                .padding(.vertical, Theme.Space.xs)
            case "completed":
                ImportStatusRow(systemImage: "checkmark.circle.fill", tint: Theme.ok,
                                title: "지난 약속·사건 정리 완료 (\(job.graph_episodes)개)")
            case "failed":
                ImportStatusRow(systemImage: "exclamationmark.triangle.fill", tint: Theme.bad,
                                title: "지난 약속·사건 정리 실패", detail: job.graph_error ?? "사유 없음")
            case "limited":
                // 서버의 수집 한도에 닿았다. 말없이 줄을 지우지 않고 어디까지 했는지 알린다.
                ImportStatusRow(systemImage: "exclamationmark.circle.fill", tint: Theme.warn,
                                title: "지난 약속·사건 정리는 한도까지만 했어요 (\(job.graph_episodes)개)",
                                detail: "최근 메시지부터 정리했어요. 말투 맞추기는 그대로 쓸 수 있어요.")
            default:
                EmptyView()
            }
            if let note = IgnoredRolesNote.make(imported: job.imported, ignored: ignoredRoles) {
                rolesNote(note)
            }
        } header: {
            Text("분석")
        } footer: {
            Text("이 상대에게 쓰는 말투는 이전 화면에서 직접 정해요.")
        }
        .listRowBackground(Theme.surface)
    }

    /// 서버가 따르지 않은 화자 지정 안내: 하나면 한 문단, 둘 이상이면 머리말 · 화자마다 한 줄 · 해법.
    /// 색만이 아니라 기호와 문구로 알리고, 화면 낭독기에는 한 문장으로 읽힌다.
    private func rolesNote(_ note: IgnoredRolesNote) -> some View {
        VStack(alignment: .leading, spacing: Theme.Space.s) {
            Label(note.head, systemImage: "exclamationmark.circle.fill")
            ForEach(note.items.indices, id: \.self) { index in
                Text("· \(note.items[index].name) — \(note.items[index].detail)")
            }
            if let more = note.more { Text(more) }
            if let tail = note.tail { Text(tail) }
        }
        .font(.footnote).foregroundStyle(Theme.ink)
        .fixedSize(horizontal: false, vertical: true)
        .card(padding: Theme.Space.m, fill: Theme.warnSoft)
        .listRowSeparator(.hidden)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(note.text)
    }

    /// 고른 파일을 글로 읽는다. 큰 파일은 요청 한 번에 담기 어려워 막는다.
    private static func read(_ url: URL) throws -> String {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let data = try Data(contentsOf: url)
        guard data.count <= 2_000_000 else { throw ServiceError(message: "파일이 너무 커요(2MB 초과). 기간을 나눠 내보내 주세요.") }
        guard let text = String(data: data, encoding: .utf8) else { throw ServiceError(message: "UTF-8 글 파일만 읽을 수 있어요.") }
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
            let me = meName.trimmingCharacters(in: .whitespaces)
            if method == .kakao {
                // 카카오톡 내보내기는 서버가 읽는다 (웹과 같은 파서). 앱은 화자만 지정한다.
                let preview = try await api.previewImport(text: text, format: "kakao")
                // 단체방이면 이 상대 한 명만 남긴다: 적어 준 이름이 먼저, 없으면 상대 별칭.
                let partner = [contactName.trimmingCharacters(in: .whitespaces), contact.alias].filter { !$0.isEmpty }
                let roles = try ImportParser.roles(for: preview.speakers, me: me, contact: partner)
                let payload = RawImportPayload(format: "kakao", text: text, speaker_map: roles)
                let done = try await api.importRaw(conversation: conversation.id, payload: payload)
                ignoredRoles = done.ignoredRoles
                job = done
            } else {
                let payload = try ImportParser.parse(text, me: me, contact: contact.alias)
                let done = try await api.importMessages(conversation: conversation.id, payload: payload)
                ignoredRoles = []
                job = done
            }
            text = ""
            try await loadMessages()
        }
    }

    /// 말투 분석과 그래프 수집 진행을 4초마다 확인한다. 화면을 떠나면 확인만 멈춘다(서버 일은 계속된다).
    private func followGraph() async {
        guard let api = session.api else { return }
        while let current = job, current.active {
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
