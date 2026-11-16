import SwiftUI

/// 최근 첨삭 20건. 상대별로 좁혀 볼 수 있고, 지난 추천을 다시 키보드에 준비할 수 있다.
struct HistoryView: View {
    @EnvironmentObject private var session: Session
    @StateObject private var run = Runner()
    @State private var filter = ""
    @State private var rewrites: [Rewrite] = []
    @State private var loaded = false

    var body: some View {
        Form {
            Picker("상대", selection: $filter) {
                Text("전체").tag("")
                ForEach(session.contacts) { Text($0.alias).tag($0.id) }
            }
            Section("최근 첨삭 \(rewrites.count)건") {
                if loaded && rewrites.isEmpty { Text("아직 받은 추천이 없습니다.") }
                ForEach(rewrites) { rewrite in
                    NavigationLink {
                        RewriteDetailView(rewrite: rewrite, alias: session.alias(rewrite.contact_id))
                    } label: {
                        VStack(alignment: .leading) {
                            Text(rewrite.result.result_text).lineLimit(2)
                            Text([session.alias(rewrite.contact_id), Self.status(rewrite), "조건 \(rewrite.condition)",
                                  Labels.date(rewrite.created_at)].joined(separator: " · "))
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .status(run)
        .navigationTitle("이력")
        .task(id: filter) { run { try await load() } }
        .refreshable { try? await load() }
    }

    static func status(_ rewrite: Rewrite) -> String {
        rewrite.result.status == .recommended ? "추천" : "원문 유지"
    }

    private func load() async throws {
        guard let api = session.api else { return }
        rewrites = try await api.rewrites(contact: filter.isEmpty ? nil : filter)
        loaded = true
    }
}

private struct RewriteDetailView: View {
    let rewrite: Rewrite
    let alias: String
    @StateObject private var run = Runner()

    var body: some View {
        let result = rewrite.result
        Form {
            Section(HistoryView.status(rewrite)) {
                Text(result.result_text).textSelection(.enabled)
                Text("\(alias) · 조건 \(rewrite.condition) · \(Labels.date(rewrite.created_at))")
                    .font(.footnote).foregroundStyle(.secondary)
                if result.status == .recommended {
                    Button("키보드에 준비") {
                        run {
                            try KeyboardMessage.save(result.result_text)
                            run.notice = "Persona_ize 키보드에서 문장을 확인하고 넣으세요."
                        }
                    }
                }
            }
            if !result.warnings.isEmpty {
                Section("주의") { ForEach(result.warnings.indices, id: \.self) { Text(result.warnings[$0]) } }
            }
            if !result.preserved_items.isEmpty {
                Section("보존 항목") { Text(result.preserved_items.map(\.text).joined(separator: ", ")) }
            }
            if !result.change_reasons.isEmpty {
                Section("바꾼 이유") {
                    ForEach(result.change_reasons.indices, id: \.self) { Text(result.change_reasons[$0]) }
                }
            }
        }
        .status(run)
        .navigationTitle("첨삭 결과")
        .navigationBarTitleDisplayMode(.inline)
    }
}
