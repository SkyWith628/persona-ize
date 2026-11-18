import SwiftUI

/// 최근 첨삭 20건. 상대별로 좁혀 볼 수 있고, 지난 추천을 다시 키보드에 준비할 수 있다.
struct HistoryView: View {
    @EnvironmentObject private var session: Session
    @StateObject private var run = Runner()
    @State private var filter = ""
    @State private var rewrites: [Rewrite] = []
    @State private var loaded = false

    var body: some View {
        List {
            Section {
                Picker("상대", selection: $filter) {
                    Text("전체").tag("")
                    ForEach(session.contacts) { Text($0.alias).tag($0.id) }
                }
                .tint(Theme.accent)
                .frame(minHeight: Theme.minTouch)
                .listRowBackground(Theme.surface)
            }
            Section {
                if loaded && rewrites.isEmpty {
                    EmptyHistory(filtered: !filter.isEmpty)
                        .listRowBackground(Theme.surface)
                }
                ForEach(rewrites) { rewrite in
                    NavigationLink {
                        RewriteDetailView(rewrite: rewrite, alias: session.alias(rewrite.contact_id))
                    } label: {
                        RewriteRow(rewrite: rewrite, alias: session.alias(rewrite.contact_id))
                    }
                    .listRowBackground(Theme.surface)
                    .listRowSeparatorTint(Theme.line)
                }
            } header: {
                Text("최근에 다듬은 글 \(rewrites.count)건")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(Theme.muted)
                    .textCase(nil)
            }
        }
        .listStyle(.insetGrouped)
        .screenBackground()
        .status(run)
        .navigationTitle("이력")
        .task(id: filter) { run { try await load() } }
        .refreshable { try? await load() }
    }

    static func status(_ rewrite: Rewrite) -> String {
        rewrite.result.status == .recommended ? "추천" : "초안 유지"
    }

    private func load() async throws {
        guard let api = session.api else { return }
        rewrites = try await api.rewrites(contact: filter.isEmpty ? nil : filter)
        loaded = true
    }
}

/// 결과 상태 꼬리표. 추천은 ok, 원문 유지는 warn — 색과 함께 기호·문구로 구분한다.
private struct StatusTag: View {
    let rewrite: Rewrite

    var body: some View {
        let recommended = rewrite.result.status == .recommended
        Tag(text: HistoryView.status(rewrite), kind: recommended ? .ok : .warn,
            systemImage: recommended ? "checkmark.circle.fill" : "arrow.uturn.backward.circle.fill")
    }
}

/// 이력 한 줄: 위에 상태와 상대·시각(조용하게), 아래에 문장.
private struct RewriteRow: View {
    let rewrite: Rewrite
    let alias: String
    @AppStorage(Prefs.experiment) private var experiment = false

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Space.s) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: Theme.Space.s) {
                    StatusTag(rewrite: rewrite)
                    meta.lineLimit(1)
                }
                // 글자가 커져 한 줄에 안 들어가면 꼬리표 아래로 내린다.
                VStack(alignment: .leading, spacing: Theme.Space.xs) {
                    StatusTag(rewrite: rewrite)
                    meta
                }
            }
            Text(rewrite.result.result_text)
                .font(.body)
                .foregroundStyle(Theme.ink)
                .lineSpacing(Theme.Space.xs / 2)
                .lineLimit(3)
                .multilineTextAlignment(.leading)
        }
        .padding(.vertical, Theme.Space.xs)
        .frame(maxWidth: .infinity, minHeight: Theme.minTouch, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    private var meta: some View {
        Text(Labels.meta(alias: alias, rewrite: rewrite, experiment: experiment))
            .font(.footnote)
            .foregroundStyle(Theme.muted)
    }
}

/// 빈 상태 안내. 상대로 좁힌 경우에는 좁힌 탓임을 알려 준다.
private struct EmptyHistory: View {
    let filtered: Bool

    var body: some View {
        VStack(spacing: Theme.Space.s) {
            Image(systemName: "clock.arrow.circlepath")
                .font(.largeTitle)
                .foregroundStyle(Theme.muted)
                .accessibilityHidden(true)
            Text("아직 다듬은 글이 없어요.")
                .font(.headline)
                .foregroundStyle(Theme.ink)
            Text(filtered ? "다른 상대를 고르거나 ‘전체’로 바꿔 보세요."
                          : "문장을 다듬으면 여기에 최근 20건이 쌓여요.")
                .font(.footnote)
                .foregroundStyle(Theme.muted)
        }
        .multilineTextAlignment(.center)
        .frame(maxWidth: .infinity)
        .padding(.vertical, Theme.Space.xl)
        .accessibilityElement(children: .combine)
    }
}

private struct RewriteDetailView: View {
    let rewrite: Rewrite
    let alias: String
    @StateObject private var run = Runner()
    @AppStorage(Prefs.experiment) private var experiment = false

    var body: some View {
        let result = rewrite.result
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.m) {
                VStack(alignment: .leading, spacing: Theme.Space.m) {
                    StatusTag(rewrite: rewrite)
                    Text(result.result_text)
                        .font(.title3)
                        .foregroundStyle(Theme.ink)
                        .lineSpacing(Theme.Space.xs)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(Labels.meta(alias: alias, rewrite: rewrite, experiment: experiment))
                        .font(.footnote)
                        .foregroundStyle(Theme.muted)
                }
                .card()

                if !result.warnings.isEmpty {
                    DetailBlock(title: "확인해 주세요", systemImage: "exclamationmark.triangle.fill", tint: Theme.warn,
                                fill: Theme.warnSoft, lines: result.warnings)
                }
                if !result.preserved_items.isEmpty {
                    DetailBlock(title: "지킨 내용", systemImage: "lock.fill", tint: Theme.ok,
                                lines: [result.preserved_items.map(\.text).joined(separator: ", ")])
                }
                if !result.change_reasons.isEmpty {
                    DetailBlock(title: "바꾼 이유", systemImage: "text.bubble.fill", tint: Theme.muted,
                                lines: result.change_reasons)
                }
            }
            .padding(Theme.Space.l)
        }
        .background(Theme.bg.ignoresSafeArea())
        .safeAreaInset(edge: .bottom) {
            // 주 행동 하나는 엄지가 닿는 아래쪽에 고정한다.
            if result.status == .recommended {
                Button("키보드에 준비") {
                    run {
                        try KeyboardMessage.save(result.result_text)
                        run.notice = "준비했어요. Persona_ize 키보드에서 확인하고 넣어 주세요."
                        Haptic.notify(.success)
                    }
                }
                .buttonStyle(.primary)
                .padding(.horizontal, Theme.Space.l)
                .padding(.vertical, Theme.Space.m)
                .background(Theme.bg)
            }
        }
        .status(run)
        .navigationTitle("다듬은 글")
        .navigationBarTitleDisplayMode(.inline)
    }
}

/// 첨삭 결과의 한 구역(주의·보존 항목·바꾼 이유). 테두리 없이 면으로만 나눈다.
private struct DetailBlock: View {
    let title: String
    let systemImage: String
    let tint: Color
    var fill: Color = Theme.surface
    let lines: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Space.s) {
            Label(title, systemImage: systemImage)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(tint)
            ForEach(lines.indices, id: \.self) { index in
                Text(lines[index])
                    .font(.body)
                    .foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .card(fill: fill)
    }
}
