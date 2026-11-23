import SwiftUI

/// 작성 → 추천/원문 유지 (와이어프레임 v2의 M1~M4를 앱 안 화면으로 옮긴 것).
/// 추천은 자동으로 보내지 않고 초안이나 키보드로만 옮긴다.
struct ComposeView: View {
    @EnvironmentObject private var session: Session
    @StateObject private var run = Runner()
    @AppStorage("lastContact") private var contact = ""
    @AppStorage("recentContacts") private var recentIDs = ""
    @AppStorage(Prefs.experiment) private var experiment = false
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
    /// 다듬기 요청을 보낸 시각. 값이 있는 동안 결과 자리에 기다리는 카드를 보인다.
    @State private var waitingSince: Date?
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

    /// 칩으로 보일 상대: 최근 상대에 지금 고른 상대가 없으면 맨 앞에 넣는다 (메뉴에서 고른 상대도 칩으로 보인다).
    private var chips: [Contact] {
        Recents.chips(recents: recents, selected: contact, contacts: session.contacts)
    }

    /// 칩에 없는 나머지 상대. "다른 상대" 메뉴에 들어간다.
    private var others: [Contact] {
        let shown = Set(chips.map(\.id))
        return session.contacts.filter { !shown.contains($0.id) }
    }

    private var hasWarnings: Bool { !(result?.result.warnings.isEmpty ?? true) }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: Theme.Space.xl) {
                    contactPicker
                    draftEditor
                    if let waitingSince {
                        // 다시 만드는 동안에는 앞선 결과 대신 기다리는 카드를 보인다.
                        WaitingCard(alias: alias, since: waitingSince).id("waiting")
                    } else if let result {
                        if result.result.status == .recommended { recommendation(result) } else { kept(result) }
                    }
                    if experiment { demoSettings }
                }
                .padding(.horizontal, Theme.Space.l)
                .padding(.vertical, Theme.Space.l)
            }
            .scrollDismissesKeyboard(.interactively)
            .screenBackground()
            .onChange(of: result?.id) { id in
                if id != nil { withAnimation { proxy.scrollTo("result", anchor: .top) } }
            }
            .onChange(of: waitingSince) { since in
                if since != nil { withAnimation { proxy.scrollTo("waiting", anchor: .top) } }
            }
        }
        // 주 행동은 엄지가 닿는 아래에 고정한다. .status 띠는 이 바깥에 붙으므로 버튼 아래(화면 맨 아래)에 놓인다.
        .safeAreaInset(edge: .bottom, spacing: 0) { actionBar }
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
        .onChange(of: experiment) { on in
            // 실험 설정을 끄면 숨은 값이 남아 결과를 바꾸지 않게 기본으로 되돌린다.
            if !on { condition = "D"; situation = "" }
        }
        .alert("초안을 그대로 둘까요?", isPresented: $rejecting) {
            TextField("추천이 맞지 않은 이유 (선택)", text: $reason)
            Button("초안 그대로") { reject() }
            Button("취소", role: .cancel) {}
        }
    }

    // MARK: M1 — 상대 고르기

    private var contactPicker: some View {
        VStack(alignment: .leading, spacing: Theme.Space.m) {
            Text("누구에게 보내나요?").font(.title2.bold()).foregroundStyle(Theme.ink)
                .accessibilityAddTraits(.isHeader)
            if session.contacts.isEmpty {
                Label("상대 탭에서 상대를 먼저 추가해 주세요.", systemImage: "person.crop.circle.badge.plus")
                    .font(.body).foregroundStyle(Theme.ink2)
                    .card()
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: Theme.Space.s) {
                        ForEach(chips) { item in
                            Button(item.alias) { contact = item.id }
                                .buttonStyle(ChipStyle(selected: item.id == contact))
                                .accessibilityAddTraits(item.id == contact ? .isSelected : [])
                        }
                        // 칩에 다 못 담은 상대는 같은 줄의 메뉴 칩 하나로 고른다.
                        if !others.isEmpty {
                            Menu {
                                ForEach(others) { item in Button(item.alias) { contact = item.id } }
                            } label: {
                                Label("다른 상대", systemImage: "chevron.down")
                                    .labelStyle(.titleAndIcon)
                                    .font(.subheadline.weight(.semibold))
                                    .foregroundStyle(Theme.accentStrong)
                                    .padding(.horizontal, Theme.Space.l)
                                    .frame(minHeight: Theme.minTouch)
                                    .background(Theme.surface2, in: Capsule())
                            }
                        }
                    }
                }
                if !hasPersona && condition != "A" {
                    Label("상대 탭에서 이 상대에게 쓸 말투를 정하면 다듬을 수 있어요.", systemImage: "info.circle.fill")
                        .font(.footnote).foregroundStyle(Theme.warn)
                        .card(padding: Theme.Space.m, fill: Theme.warnSoft)
                }
            }
        }
    }

    // MARK: M2 — 초안

    private var draftEditor: some View {
        VStack(alignment: .leading, spacing: Theme.Space.s) {
            Text("초안").font(.headline).foregroundStyle(Theme.ink).accessibilityAddTraits(.isHeader)
            TextEditor(text: $draft)
                .font(.body).foregroundStyle(Theme.ink)
                .scrollContentBackground(.hidden)
                .frame(minHeight: Metric.draftMinHeight)
                .accessibilityLabel("초안").focused($editing)
                .card(padding: Theme.Space.m)
            Text("버튼을 누르면 초안을 서버로 보내 AI가 다듬어요. 지금은 지어낸 대화만 넣어 주세요.")
                .font(.footnote).foregroundStyle(Theme.muted)
        }
    }

    // MARK: 실험 설정 — 조건·상황 (계정 탭에서 켰을 때만 보인다)

    private var demoSettings: some View {
        DisclosureGroup("실험 설정") {
            VStack(alignment: .leading, spacing: Theme.Space.m) {
                HStack {
                    Text("조건").font(.body).foregroundStyle(Theme.ink)
                    Spacer(minLength: Theme.Space.s)
                    Picker("조건", selection: $condition) {
                        ForEach(Labels.conditions, id: \.0) { Text($0.1).tag($0.0) }
                    }
                    .pickerStyle(.menu).tint(Theme.accentStrong)
                }
                .frame(minHeight: Theme.minTouch)
                TextField("상황 (예: 일정 지연 양해)", text: $situation)
                    .font(.body).foregroundStyle(Theme.ink)
                    .padding(.horizontal, Theme.Space.m)
                    .frame(minHeight: Theme.minTouch)
                    .background(Theme.surface2, in: RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
            }
            .padding(.top, Theme.Space.m)
        }
        .font(.subheadline).foregroundStyle(Theme.ink2).tint(Theme.muted)
        .card()
    }

    // MARK: 주 행동 — 화면 아래 고정

    /// 한 화면에 주 행동 하나: 추천이 떠 있으면 "키보드에 준비", 아니면 "…에게 맞게".
    /// 확인할 것(주의)이 있는 추천은 안전한 쪽("초안 그대로")이 주 버튼이고, 준비는 좁은 보조 버튼으로 물러난다.
    @ViewBuilder
    private var actionBar: some View {
        Group {
            if let result, result.result.status == .recommended {
                let empty = edited.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                if hasWarnings {
                    HStack(spacing: Theme.Space.s) {
                        Button("그래도 준비") { prepare(result) }
                            .buttonStyle(.secondary)
                            .frame(width: Metric.sideAction)
                            .disabled(empty)
                        Button("초안 그대로") { reason = ""; rejecting = true }
                            .buttonStyle(.primary)
                    }
                } else {
                    Button { prepare(result) } label: { Label("키보드에 준비", systemImage: "keyboard") }
                        .buttonStyle(.primary)
                        .disabled(empty)
                }
            } else {
                Button { rewrite() } label: { Label("\(alias)에게 맞게", systemImage: "sparkles") }
                    .buttonStyle(.primary)
                    .disabled(blocked)
            }
        }
        .padding(.horizontal, Theme.Space.l)
        .padding(.vertical, Theme.Space.s)
        .background(Theme.bg)
    }

    // MARK: M3 — 추천: 문장 → 보존 항목 → 주의 → 넣기

    @ViewBuilder
    private func recommendation(_ rewrite: Rewrite) -> some View {
        VStack(alignment: .leading, spacing: Theme.Space.m) {
            resultHeader
            Group {
                if fixing {
                    TextEditor(text: $edited)
                        .scrollContentBackground(.hidden)
                        .frame(minHeight: Metric.fixMinHeight).accessibilityLabel("추천 문장 고치기")
                } else {
                    Text(edited).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .font(.title3).foregroundStyle(Theme.ink)
            .card(fill: Theme.accentSoft)
            if !rewrite.result.preserved_items.isEmpty {
                FlowLayout(spacing: Theme.Space.s) {
                    ForEach(rewrite.result.preserved_items.indices, id: \.self) {
                        Tag(text: rewrite.result.preserved_items[$0].text, kind: .ok, systemImage: "checkmark")
                    }
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("지킨 내용: " + rewrite.result.preserved_items.map(\.text).joined(separator: ", "))
            }
            if !rewrite.result.warnings.isEmpty {
                VStack(alignment: .leading, spacing: Theme.Space.xs) {
                    Label("확인해 주세요", systemImage: "exclamationmark.triangle.fill")
                        .font(.footnote.bold()).foregroundStyle(Theme.warn)
                    ForEach(rewrite.result.warnings.indices, id: \.self) {
                        Text(rewrite.result.warnings[$0]).font(.footnote).foregroundStyle(Theme.ink)
                    }
                }
                .card(padding: Theme.Space.m, fill: Theme.warnSoft)
            }
            // 보조 행동은 한 줄로 조용하게. 주 행동은 화면 아래 버튼 하나다.
            HStack(spacing: Theme.Space.s) {
                Button { self.rewrite() } label: { Label("다시 만들기", systemImage: "arrow.clockwise") }
                Button { fixing.toggle() } label: {
                    Label(fixing ? "고치기 완료" : "직접 고치기", systemImage: fixing ? "checkmark" : "pencil")
                }
                Spacer(minLength: 0)
                Menu {
                    Button { apply(rewrite) } label: { Label("초안을 이 문장으로 바꾸기", systemImage: "arrow.up.doc") }
                    if !hasWarnings {
                        Button { reason = ""; rejecting = true } label: {
                            Label("초안 그대로 두기", systemImage: "arrow.uturn.backward")
                        }
                    }
                } label: {
                    Image(systemName: "ellipsis")
                        .frame(width: Theme.minTouch, height: Theme.minTouch)
                        .contentShape(Rectangle())
                }
                .accessibilityLabel("더 보기")
            }
            .buttonStyle(QuietButtonStyle())
            .foregroundStyle(Theme.accentStrong)
            if !rewrite.result.change_reasons.isEmpty {
                VStack(alignment: .leading, spacing: Theme.Space.xs) {
                    ForEach(rewrite.result.change_reasons.indices, id: \.self) {
                        Text("바꾼 이유 · " + rewrite.result.change_reasons[$0])
                    }
                }
                .font(.footnote).foregroundStyle(Theme.muted)
            }
            Text("넣기만 해요. 보내기는 직접 눌러요." + usageLine)
                .font(.footnote).foregroundStyle(Theme.muted)
        }
    }

    private var resultHeader: some View {
        Label("\(alias)에게 맞게 다듬었어요", systemImage: "sparkles")
            .font(.headline).foregroundStyle(Theme.ink)
            .accessibilityAddTraits(.isHeader)
            .id("result")
    }

    // MARK: M4 — 추천하지 않는 경우

    @ViewBuilder
    private func kept(_ rewrite: Rewrite) -> some View {
        VStack(alignment: .leading, spacing: Theme.Space.m) {
            resultHeader
            VStack(alignment: .leading, spacing: Theme.Space.m) {
                Label("초안을 그대로 뒀어요", systemImage: "hand.raised.fill")
                    .font(.headline).foregroundStyle(Theme.warn)
                let violations = rewrite.result.validation.violations
                if violations.isEmpty && rewrite.result.warnings.isEmpty {
                    Text("내용이 달라질 수 있어서 추천하지 않았어요. 초안은 그대로예요.")
                        .font(.body).foregroundStyle(Theme.ink)
                }
                ForEach(violations.indices, id: \.self) { index in
                    VStack(alignment: .leading, spacing: Theme.Space.xs) {
                        Text(Labels.violation(violations[index].check)).font(.subheadline.bold()).foregroundStyle(Theme.ink)
                        Text(violations[index].detail).font(.footnote).foregroundStyle(Theme.ink2)
                    }
                }
                ForEach(rewrite.result.warnings.indices, id: \.self) {
                    Text(rewrite.result.warnings[$0]).font(.footnote).foregroundStyle(Theme.ink)
                }
            }
            .card(fill: Theme.warnSoft)
            HStack(spacing: Theme.Space.s) {
                Button("닫기") { result = nil }
                Button("다시 만들기") { self.rewrite() }
            }
            .buttonStyle(.secondary)
            Text("입력한 초안은 바뀌지 않았어요." + usageLine)
                .font(.footnote).foregroundStyle(Theme.muted)
        }
    }

    private var usageLine: String {
        usage.map { " 오늘 \($0.rewrites) / \($0.rewrite_limit)회 썼어요." } ?? ""
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
            waitingSince = Date()
            defer {
                waitingSince = nil
                Task { await loadUsage() }
            }
            let response = try await api.rewrite(contact: contact, draft: draft, situation: situation,
                                                 condition: condition, key: current)
            guard current == key else { return }
            result = response
            edited = response.result.result_text
            fixing = false
            answered = false
            key = UUID()
            recentIDs = Recents.push(contact, onto: recentIDs)
            // 추천이면 가볍게, 초안을 그대로 둔 경우나 확인할 것이 있으면 주의 진동.
            Haptic.notify(response.result.status == .recommended && response.result.warnings.isEmpty ? .success : .warning)
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
            run.notice = "준비했어요. Persona_ize 키보드에서 확인하고 넣어 주세요."
            Haptic.notify(.success)
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

// MARK: - 이 화면만 쓰는 모양

/// Theme에 없는 치수 (입력 칸 최소 높이).
private enum Metric {
    static let draftMinHeight: CGFloat = 120
    static let fixMinHeight: CGFloat = 96
    /// 주의가 있을 때 주 버튼 옆으로 물러난 보조 버튼의 너비.
    static let sideAction: CGFloat = 120
}

/// 카드 안의 조용한 보조 행동: 면 없이 강조색 글자와 기호만. 누를 수 있는 높이는 44pt를 지킨다.
struct QuietButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.subheadline.weight(.semibold))
            .padding(.horizontal, Theme.Space.s)
            .frame(minHeight: Theme.minTouch)
            .opacity(configuration.isPressed ? 0.6 : 1)
            .contentShape(Rectangle())
    }
}

/// 최근 상대 칩. 44pt 이상, 고른 칩은 강조색 면 + 체크 기호로 구분한다.
struct ChipStyle: ButtonStyle {
    let selected: Bool

    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: Theme.Space.xs) {
            if selected { Image(systemName: "checkmark").font(.footnote.weight(.bold)) }
            configuration.label.lineLimit(1)
        }
        .font(.subheadline.weight(.semibold))
        .foregroundStyle(selected ? Theme.accentInk : Theme.ink2)
        .padding(.horizontal, Theme.Space.l)
        .frame(minHeight: Theme.minTouch)
        .background(selected ? Theme.accent : Theme.surface2, in: Capsule())
        .opacity(configuration.isPressed ? 0.8 : 1)
        .contentShape(Capsule())
    }
}

/// 꼬리표를 왼쪽부터 채우고 넘치면 다음 줄로 내린다.
struct FlowLayout: Layout {
    var spacing: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        let rows = arrange(subviews, width: width)
        let used = rows.frames.map(\.maxX).max() ?? 0
        return CGSize(width: proposal.width ?? used, height: rows.height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let frames = arrange(subviews, width: bounds.width).frames
        for (subview, frame) in zip(subviews, frames) {
            subview.place(at: CGPoint(x: bounds.minX + frame.minX, y: bounds.minY + frame.minY),
                          proposal: ProposedViewSize(frame.size))
        }
    }

    private func arrange(_ subviews: Subviews, width: CGFloat) -> (frames: [CGRect], height: CGFloat) {
        var frames: [CGRect] = []
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0
        for subview in subviews {
            let ideal = subview.sizeThatFits(.unspecified)
            let size = CGSize(width: min(ideal.width, width), height:
                ideal.width > width ? subview.sizeThatFits(ProposedViewSize(width: width, height: nil)).height : ideal.height)
            if x > 0 && x + size.width > width {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            frames.append(CGRect(origin: CGPoint(x: x, y: y), size: size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return (frames, y + rowHeight)
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

    /// 칩 줄에 보일 상대. 고른 상대가 최근 목록에 없으면 맨 앞에 넣고 개수는 그대로 둔다.
    static func chips(recents: [Contact], selected: String, contacts: [Contact]) -> [Contact] {
        guard !recents.contains(where: { $0.id == selected }),
              let chosen = contacts.first(where: { $0.id == selected }) else { return recents }
        return [chosen] + recents.prefix(limit - 1)
    }

    /// 기록 순서대로 아직 있는 상대를 고르고, 모자라면 목록 앞에서 채운다.
    static func pick(stored: String, contacts: [Contact]) -> [Contact] {
        let remembered = stored.split(separator: ",").compactMap { id in contacts.first { $0.id == id } }
        let filler = contacts.filter { candidate in !remembered.contains { $0.id == candidate.id } }
        return Array((remembered + filler).prefix(limit))
    }
}
