import SwiftUI

struct GraphNode: Decodable, Identifiable, Equatable {
    let id: String
    let name: String
    let kind: String
    let summary: String
    let event_type: String?
    let is_contact: Bool
}

struct GraphEdge: Decodable, Identifiable, Equatable {
    let id: String
    let source: String
    let target: String
    let fact: String
    let valid_at: String?
    let current: Bool
}

struct ContactGraph: Decodable, Equatable {
    let enabled: Bool
    let center_id: String?
    let nodes: [GraphNode]
    let edges: [GraphEdge]
}

/// 상대를 가운데에 두고 직접 이어진 노드를 안쪽 원, 나머지를 바깥 원에 놓는다. 좌표는 0~1.
enum GraphLayout {
    static let innerRadius = 0.21
    static let outerRadius = 0.39

    static func positions(_ graph: ContactGraph) -> [String: CGPoint] {
        guard let center = graph.center_id ?? graph.nodes.first?.id else { return [:] }
        var neighbors: [String: Set<String>] = [:]
        for edge in graph.edges where edge.source != edge.target {
            neighbors[edge.source, default: []].insert(edge.target)
            neighbors[edge.target, default: []].insert(edge.source)
        }
        let others = graph.nodes.map(\.id).filter { $0 != center }
        let inner = others.filter { neighbors[center]?.contains($0) == true }
        var result = [center: CGPoint(x: 0.5, y: 0.5)]
        var angles: [String: Double] = [:]
        for (index, id) in inner.enumerated() {
            let angle = -Double.pi / 2 + 2 * Double.pi * Double(index) / Double(inner.count)
            angles[id] = angle
            result[id] = point(angle, innerRadius)
        }
        // 바깥 노드는 이어진 안쪽 노드 가까이 오도록 그 각도 순으로 늘어놓는다.
        let outer = others.filter { !inner.contains($0) }.sorted { anchor($0, neighbors, angles) < anchor($1, neighbors, angles) }
        for (index, id) in outer.enumerated() {
            let offset = inner.isEmpty ? 0 : Double.pi / Double(max(outer.count, 2))
            result[id] = point(-Double.pi / 2 + offset + 2 * Double.pi * Double(index) / Double(outer.count), outerRadius)
        }
        return result
    }

    private static func point(_ angle: Double, _ radius: Double) -> CGPoint {
        CGPoint(x: 0.5 + radius * cos(angle), y: 0.5 + radius * sin(angle))
    }

    private static func anchor(_ id: String, _ neighbors: [String: Set<String>], _ angles: [String: Double]) -> Double {
        let linked = (neighbors[id] ?? []).compactMap { angles[$0] }
        return linked.isEmpty ? 0 : linked.reduce(0, +) / Double(linked.count)
    }
}

/// 상대 한 명 주변의 맥락 그래프: 그림(노드를 누르면 그 노드의 사실만) + 사실 목록.
struct ContactGraphView: View {
    let contact: Contact
    @EnvironmentObject private var session: Session
    @StateObject private var run = Runner()
    @State private var graph: ContactGraph?
    @State private var selected: String?

    private var facts: [GraphEdge] {
        guard let graph else { return [] }
        guard let selected else { return graph.edges }
        return graph.edges.filter { $0.source == selected || $0.target == selected }
    }

    var body: some View {
        Form {
            if let graph {
                if !graph.enabled {
                    GraphEmptyState(systemImage: "point.3.connected.trianglepath.dotted",
                                    text: "이 서버는 맥락 그래프를 쓰지 않습니다. D 조건은 C와 같게 동작합니다.")
                } else if graph.nodes.isEmpty {
                    GraphEmptyState(systemImage: "point.3.connected.trianglepath.dotted",
                                    text: "아직 \(contact.alias)의 그래프가 없습니다. 과거 대화를 가져오면 서버가 수집합니다. 대화 속 이름이 상대 별칭과 같아야 이 상대의 그래프로 묶입니다.")
                } else {
                    Section {
                        GraphDiagram(graph: graph, selected: $selected)
                            .frame(height: GraphMetrics.diagramHeight)
                            .listRowInsets(EdgeInsets())
                    } footer: {
                        GraphLegend()
                    }
                    .listRowBackground(Theme.surface)
                    if let node = graph.nodes.first(where: { $0.id == selected }), !node.summary.isEmpty {
                        Section {
                            VStack(alignment: .leading, spacing: Theme.Space.s) {
                                HStack(spacing: Theme.Space.s) {
                                    Text(node.name).font(.headline).foregroundStyle(Theme.ink)
                                    Tag(text: node.kind == "event" ? "사건" : "사람",
                                        systemImage: node.kind == "event" ? "calendar" : "person.fill")
                                }
                                Text(node.summary).font(.subheadline).foregroundStyle(Theme.ink2)
                            }
                            .card(fill: Theme.accentSoft)
                            .listRowInsets(EdgeInsets())
                            .listRowBackground(Color.clear)
                        }
                    }
                    Section("사실 \(facts.count)건") {
                        ForEach(facts) { edge in
                            VStack(alignment: .leading, spacing: Theme.Space.xs) {
                                Text(edge.fact).font(.body).foregroundStyle(edge.current ? Theme.ink : Theme.muted)
                                Text(caption(edge, in: graph)).font(.footnote).foregroundStyle(Theme.muted)
                                if !edge.current {
                                    Tag(text: "지난 사실", systemImage: "clock.arrow.circlepath")
                                }
                            }
                            .padding(.vertical, Theme.Space.xs)
                            .accessibilityElement(children: .combine)
                        }
                    }
                    .listRowBackground(Theme.surface)
                }
            }
        }
        .screenBackground()
        .tint(Theme.accent)
        .status(run)
        .navigationTitle("\(contact.alias) 맥락 그래프")
        .navigationBarTitleDisplayMode(.inline)
        .task { run { try await load() } }
        .refreshable { try? await load() }
    }

    private func load() async throws {
        guard let api = session.api else { return }
        graph = try await api.graph(contact: contact.id)
        selected = nil
    }

    private func caption(_ edge: GraphEdge, in graph: ContactGraph) -> String {
        let name = { (id: String) in graph.nodes.first { $0.id == id }?.name ?? "?" }
        var parts = ["\(name(edge.source)) → \(name(edge.target))"]
        if let when = edge.valid_at { parts.append(Labels.date(when)) }
        return parts.joined(separator: " · ")
    }
}

// MARK: - 이 파일 안에서만 쓰는 조각

/// 그림에만 쓰는 크기. Theme에 없는 값이라 여기 둔다.
private enum GraphMetrics {
    static let diagramHeight: CGFloat = 340
    static let contactSize: CGFloat = 32
    static let nodeSize: CGFloat = 22
    static let badgeWidth: CGFloat = 78
    static let legendMark: CGFloat = 12
    static let legendLine: CGFloat = 28
    static let lineWidth: CGFloat = 1.5
    static let dash: [CGFloat] = [4, 4]
}

/// 빈 상태: 큰 기호와 한 줄 안내.
private struct GraphEmptyState: View {
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

/// 범례: 모양과 색, 선 종류의 뜻을 그림 그대로 보여 준다.
private struct GraphLegend: View {
    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Space.s) {
            HStack(spacing: Theme.Space.m) {
                item("상대") { Circle().fill(Theme.accent) }
                item("사람") { Circle().fill(Theme.ink2) }
                item("사건") { EventShape() }
            }
            HStack(spacing: Theme.Space.s) {
                DashLine().stroke(Theme.muted, style: StrokeStyle(lineWidth: GraphMetrics.lineWidth, dash: GraphMetrics.dash))
                    .frame(width: GraphMetrics.legendLine, height: GraphMetrics.legendMark)
                Text("점선은 지난 사실만 남은 연결")
            }
            Text("노드를 누르면 그 노드의 사실만 봅니다.")
        }
        .font(.footnote).foregroundStyle(Theme.muted)
        .padding(.top, Theme.Space.xs)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("● 사람 ■ 사건 · 점선은 지난 사실만 남은 연결 · 노드를 누르면 그 노드의 사실만 봅니다.")
    }

    private func item<Mark: View>(_ text: String, @ViewBuilder mark: () -> Mark) -> some View {
        HStack(spacing: Theme.Space.xs) {
            mark().frame(width: GraphMetrics.legendMark, height: GraphMetrics.legendMark)
            Text(text)
        }
    }
}

/// 사건 노드: 네모난 면(surface2)에 ink2 윤곽. 사람(원)과는 모양으로도 구분된다.
private struct EventShape: View {
    var body: some View {
        RoundedRectangle(cornerRadius: Theme.Space.xs, style: .continuous).fill(Theme.surface2)
            .overlay {
                RoundedRectangle(cornerRadius: Theme.Space.xs, style: .continuous)
                    .strokeBorder(Theme.ink2, lineWidth: GraphMetrics.lineWidth)
            }
    }
}

private struct DashLine: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.minX, y: rect.midY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.midY))
        return path
    }
}

private struct GraphDiagram: View {
    let graph: ContactGraph
    @Binding var selected: String?

    /// 노드 쌍마다 선 하나. 유효한 사실이 하나라도 있으면 실선.
    private var links: [(a: String, b: String, current: Bool)] {
        var current: [[String]: Bool] = [:]
        for edge in graph.edges where edge.source != edge.target {
            let pair = [edge.source, edge.target].sorted()
            current[pair] = (current[pair] ?? false) || edge.current
        }
        return current.map { (a: $0.key[0], b: $0.key[1], current: $0.value) }
    }

    var body: some View {
        GeometryReader { geometry in
            let unit = GraphLayout.positions(graph)
            let place = { (id: String) -> CGPoint? in
                unit[id].map { CGPoint(x: $0.x * geometry.size.width, y: $0.y * geometry.size.height) }
            }
            ZStack {
                Canvas { context, _ in
                    for link in links {
                        guard let from = place(link.a), let to = place(link.b) else { continue }
                        var path = Path()
                        path.move(to: from)
                        path.addLine(to: to)
                        let touched = selected == nil || selected == link.a || selected == link.b
                        // 지금도 유효한 연결은 ink2 실선, 지난 사실만 남은 연결은 muted 점선.
                        let color = link.current ? Theme.ink2 : Theme.muted
                        context.stroke(path, with: .color(color.opacity(touched ? 0.7 : 0.2)),
                                       style: StrokeStyle(lineWidth: GraphMetrics.lineWidth,
                                                          dash: link.current ? [] : GraphMetrics.dash))
                    }
                }
                ForEach(graph.nodes) { node in
                    if let point = place(node.id) {
                        NodeBadge(node: node, selected: selected == node.id)
                            .position(point)
                            .onTapGesture { selected = selected == node.id ? nil : node.id }
                    }
                }
            }
        }
    }
}

private struct NodeBadge: View {
    let node: GraphNode
    let selected: Bool

    var body: some View {
        let size = node.is_contact ? GraphMetrics.contactSize : GraphMetrics.nodeSize
        VStack(spacing: Theme.Space.xs) {
            Group {
                if node.kind == "event" {
                    EventShape()
                } else {
                    Circle().fill(node.is_contact ? Theme.accent : Theme.ink2)
                }
            }
            .frame(width: size, height: size)
            .overlay {
                if selected {
                    Circle().stroke(Theme.accent, lineWidth: 2)
                        .frame(width: size + Theme.Space.m, height: size + Theme.Space.m)
                }
            }
            Text(node.name).font(.caption2).fontWeight(node.is_contact || selected ? .bold : .regular)
                .foregroundStyle(Theme.ink)
                .lineLimit(2).multilineTextAlignment(.center)
        }
        .frame(width: GraphMetrics.badgeWidth)
        .frame(minHeight: Theme.minTouch)
        .contentShape(Rectangle())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(node.kind == "event" ? "사건" : "사람") \(node.name)")
        .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
    }
}
