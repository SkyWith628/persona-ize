import SwiftUI

/// 작성 → 추천/원문 유지 (와이어프레임 v2의 M1~M4를 앱 안 화면으로 옮긴 것).
/// 추천은 자동으로 보내지 않고 초안이나 키보드로만 옮긴다.
struct ComposeView: View {
    @EnvironmentObject private var session: Session
    @StateObject private var run = Runner()
    @AppStorage("lastContact") private var contact = ""
    @AppStorage("recentContacts") private var recentIDs = ""
    @State private var draft = ""
    @State private var situation = ""
    @State private var condition = "D"
    @State private var result: Rewrite?
    @State private var edited = ""
    @State private var fixing = false
    @State private var usage: Usage?
    @State private var key = UUID()
    @State private var answered = false
    @State private var rejecting = false
    @State private var reason = ""
    @FocusState private var editing: Bool

    private var alias: String { session.contacts.first { $0.id == contact }?.alias ?? "상대" }

    private var hasPersona: Bool {
        session.contacts.first { $0.id == contact }?.persona_version != nil
    }

    private var blocked: Bool {
        contact.isEmpty || (!hasPersona && condition != "A")
            || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// 최근에 첨삭한 상대 최대 3명. 기록이 없으면 목록 앞에서 채운다.
    private var recents: [Contact] {
        Recents.pick(stored: recentIDs, contacts: session.contacts)
    }

    var body: some View {
        ScrollViewReader { proxy in
            Form {
                Section("누구에게 보내나요?") {
                    if session.contacts.isEmpty {
                        Text("상대 탭에서 상대를 먼저 등록하세요.")
                    } else {
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 8) {
                                ForEach(recents) { item in
                                    Button(item.alias) { contact = item.id }
                                        .buttonStyle(.bordered)
                                        .tint(item.id == contact ? .accentColor : .secondary)
                                        .accessibilityAddTraits(item.id == contact ? .isSelected : [])
                                }
                            }
                        }
                        Picker("상대 바꾸기", selection: $contact) {
                            ForEach(session.contacts) { Text($0.alias).tag($0.id) }
                        }
                        if !hasPersona && condition != "A" {
                            Text("상대 탭에서 이 상대의 페르소나를 저장하면 첨삭할 수 있습니다.")
                        }
                    }
                }
                Section {
                    TextEditor(text: $draft).frame(minHeight: 90).accessibilityLabel("합성 메시지 초안").focused($editing)
                    Button { rewrite() } label: {
                        Label("\(alias)에게 맞게", systemImage: "sparkles").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent).disabled(blocked)
                } header: {
                    Text("초안")
                } footer: {
                    Text("시연에는 합성 대화만 입력하세요. 누르면 서버에서 AI가 초안을 처리합니다.")
                }
                if let result {
                    if result.result.status == .recommended { recommendation(result) } else { kept(result) }
                }
                Section {
                    DisclosureGroup("시연 설정") {
                        Picker("조건", selection: $condition) {
                            ForEach(Labels.conditions, id: \.0) { Text($0.1).tag($0.0) }
                        }
                        TextField("상황 (예: 일정 지연 양해)", text: $situation)
                    }
                }
            }
            .onChange(of: result?.id) { id in
                if id != nil { withAnimation { proxy.scrollTo("result", anchor: .top) } }
            }
        }
        .status(run)
        .navigationTitle("작성")
        .onAppear { pickContact() }
        .task { await loadUsage() }
        .onChange(of: session.contacts) { _ in pickContact() }
        .onChange(of: draft) { _ in invalidate() }
        .onChange(of: contact) { _ in invalidate(); session.syncKeyboard() }
        .onChange(of: recentIDs) { _ in session.syncKeyboard() }
        .onChange(of: situation) { _ in invalidate() }
        .onChange(of: condition) { _ in invalidate() }
        .alert("초안을 그대로 둘까요?", isPresented: $rejecting) {
            TextField("추천이 맞지 않은 이유 (선택)", text: $reason)
            Button("초안 그대로") { reject() }
            Button("취소", role: .cancel) {}
        }
    }

    // MARK: M3 — 추천: 문장 → 보존 항목 → 주의 → 넣기

    @ViewBuilder
    private func recommendation(_ rewrite: Rewrite) -> some View {
        Section {
            if fixing {
                TextEditor(text: $edited).frame(minHeight: 80).accessibilityLabel("추천 문장 고치기")
            } else {
                Text(edited).textSelection(.enabled)
            }
            if !rewrite.result.preserved_items.isEmpty {
                Text(rewrite.result.preserved_items.map { "✓ " + $0.text }.joined(separator: "   "))
                    .font(.footnote).foregroundStyle(.green)
                    .accessibilityLabel("지킨 내용: " + rewrite.result.preserved_items.map(\.text).joined(separator: ", "))
            }
            if !rewrite.result.warnings.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Label("확인해 주세요", systemImage: "exclamationmark.triangle.fill").font(.footnote.bold())
                    ForEach(rewrite.result.warnings.indices, id: \.self) {
                        Text(rewrite.result.warnings[$0]).font(.footnote)
                    }
                }
                .listRowBackground(Color.yellow.opacity(0.22))
            }
            HStack {
                Button("다시 만들기") { self.rewrite() }
                Spacer()
                Button(fixing ? "고치기 완료" : "직접 고치기") { fixing.toggle() }
                Spacer()
                Button("초안 그대로") { reason = ""; rejecting = true }
            }
            .buttonStyle(.borderless).font(.footnote)
            Button { prepare(rewrite) } label: { Text("키보드에 준비").frame(maxWidth: .infinity) }
                .buttonStyle(.borderedProminent)
                .disabled(edited.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            Button("초안에 적용") { apply(rewrite) }
            ForEach(rewrite.result.change_reasons.indices, id: \.self) {
                Text("바꾼 이유 · " + rewrite.result.change_reasons[$0]).font(.footnote).foregroundStyle(.secondary)
            }
        } header: {
            Text("\(alias)에게 맞게").id("result")
        } footer: {
            Text("넣기만 해요. 전송은 직접 누릅니다." + usageLine)
        }
    }

    // MARK: M4 — 추천하지 않는 경우

    @ViewBuilder
    private func kept(_ rewrite: Rewrite) -> some View {
        Section {
            Label("초안을 그대로 뒀어요", systemImage: "hand.raised.fill").font(.headline)
            let violations = rewrite.result.validation.violations
            if violations.isEmpty && rewrite.result.warnings.isEmpty {
                Text("사실 보존을 확인하지 못해 추천하지 않았습니다. 초안은 그대로 있어요.")
            }
            ForEach(violations.indices, id: \.self) { index in
                VStack(alignment: .leading, spacing: 2) {
                    Text(Labels.violation(violations[index].check)).font(.subheadline.bold())
                    Text(violations[index].detail).font(.footnote).foregroundStyle(.secondary)
                }
            }
            ForEach(rewrite.result.warnings.indices, id: \.self) { Text(rewrite.result.warnings[$0]).font(.footnote) }
            HStack {
                Button("닫기") { result = nil }
                Spacer()
                Button("다시 만들기") { self.rewrite() }
            }
            .buttonStyle(.borderless)
        } header: {
            Text("\(alias)에게 맞게").id("result")
        } footer: {
            Text("입력한 초안은 바뀌지 않았습니다." + usageLine)
        }
    }

    private var usageLine: String {
        usage.map { " 오늘 \($0.rewrites) / \($0.rewrite_limit)회 사용." } ?? ""
    }

    // MARK: 동작

    /// 고른 상대가 사라졌으면 페르소나가 있는 첫 상대로 옮긴다. 마지막에 고른 상대는 기기에 기억한다(M1).
    private func pickContact() {
        guard !session.contacts.contains(where: { $0.id == contact }) else { return }
        contact = session.contacts.first { $0.persona_version != nil }?.id ?? session.contacts.first?.id ?? ""
    }

    /// 입력이 바뀌면 이전 추천을 적용할 수 없게 하고 새 요청 키를 쓴다.
    private func invalidate() {
        key = UUID()
        result = nil
        answered = false
        fixing = false
        run.notice = ""
    }

    private func loadUsage() async {
        guard let latest = try? await session.api?.usage() else { return }
        usage = latest
    }

    /// 실패 뒤 다시 누르면 같은 Idempotency-Key로 재시도한다 (키는 성공하거나 입력이 바뀔 때만 바뀐다).
    private func rewrite() {
        editing = false
        run {
            guard let api = session.api else { return }
            let current = key
            defer { Task { await loadUsage() } }
            let response = try await api.rewrite(contact: contact, draft: draft, situation: situation,
                                                 condition: condition, key: current)
            guard current == key else { return }
            result = response
            edited = response.result.result_text
            fixing = false
            answered = false
            key = UUID()
            recentIDs = Recents.push(contact, onto: recentIDs)
        }
    }

    /// 피드백은 추천 하나에 한 번만 보낸다. 직접 고친 문장은 함께 보낸다.
    private func sendFeedback(_ rewrite: Rewrite, accepted: Bool, text: String? = nil, reason: String? = nil) async throws {
        guard let api = session.api, !answered else { return }
        answered = true
        let changed = text.flatMap { $0 == rewrite.result.result_text ? nil : $0 }
        try await api.feedback(rewrite: rewrite.id, accepted: accepted, edited: changed, reason: reason)
    }

    private func apply(_ rewrite: Rewrite) {
        let text = edited
        run {
            // 초안을 바꾸면 invalidate가 추천을 닫는다. 피드백은 그 전에 상태를 잡아 둔다.
            let first = !answered
            draft = text
            result = nil
            if first {
                let changed = text == rewrite.result.result_text ? nil : text
                try await session.api?.feedback(rewrite: rewrite.id, accepted: true, edited: changed)
            }
        }
    }

    private func prepare(_ rewrite: Rewrite) {
        let text = edited
        fixing = false
        run {
            try KeyboardMessage.save(text)
            try await sendFeedback(rewrite, accepted: true, text: text)
            run.notice = "Persona_ize 키보드에서 문장을 확인하고 넣으세요."
        }
    }

    private func reject() {
        guard let rewrite = result else { return }
        let why = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        run {
            try await sendFeedback(rewrite, accepted: false, reason: why.isEmpty ? nil : why)
            result = nil
        }
    }
}

/// 최근 상대 기록 (기기에 id만 저장, 최신이 앞).
enum Recents {
    static let limit = 3

    static func push(_ id: String, onto stored: String) -> String {
        guard !id.isEmpty else { return stored }
        let rest = stored.split(separator: ",").map(String.init).filter { $0 != id }
        return ([id] + rest).prefix(limit).joined(separator: ",")
    }

    /// 기록 순서대로 아직 있는 상대를 고르고, 모자라면 목록 앞에서 채운다.
    static func pick(stored: String, contacts: [Contact]) -> [Contact] {
        let remembered = stored.split(separator: ",").compactMap { id in contacts.first { $0.id == id } }
        let filler = contacts.filter { candidate in !remembered.contains { $0.id == candidate.id } }
        return Array((remembered + filler).prefix(limit))
    }
}
