import SwiftUI

@main
struct PersonaApp: App {
    @StateObject private var session = Session()
    var body: some Scene { WindowGroup { Root().environmentObject(session) } }
}

/// 로그인한 동안의 API 연결과 상대 목록. 앱을 종료하면 사라진다.
@MainActor
final class Session: ObservableObject {
    @Published private(set) var api: API?
    @Published private(set) var username = ""
    @Published private(set) var contacts: [Contact] = []

    func start(_ client: API, username: String) async throws {
        contacts = try await client.contacts()
        self.username = username
        api = client
        syncKeyboard()
    }

    func reload() async throws {
        guard let api else { return }
        contacts = try await api.contacts()
        syncKeyboard()
    }

    /// 키보드 확장이 같은 로그인으로 바로 첨삭할 수 있게 연결 정보를 앱 그룹에 쓴다.
    /// 고른 상대·최근 상대는 작성 화면이 기기에 기억한 값을 따른다.
    func syncKeyboard() {
        guard let credentials = api?.keyboardCredentials() else { return }
        let defaults = UserDefaults.standard
        let link = KeyboardLink(
            server: credentials.server, cookie: credentials.cookie, csrf: credentials.csrf,
            contacts: contacts.map { .init(id: $0.id, alias: $0.alias, ready: $0.persona_version != nil) },
            selected: defaults.string(forKey: "lastContact") ?? "",
            recents: (defaults.string(forKey: "recentContacts") ?? "").split(separator: ",").map(String.init))
        try? link.save()
    }

    func alias(_ contact: String) -> String { contacts.first { $0.id == contact }?.alias ?? "삭제된 상대" }

    /// 준비한 문장을 지우고 화면을 로그인으로 돌린다. 서버 세션 정리는 호출한 쪽이 먼저 한다.
    func end() {
        try? KeyboardMessage.clear()
        KeyboardLink.clear()
        api = nil
        contacts = []
        username = ""
    }
}

/// 화면 하나의 요청 상태. 처리 중에는 중복 탭을 막고 실패 사유를 notice에 남긴다.
@MainActor
final class Runner: ObservableObject {
    @Published private(set) var busy = false
    @Published var notice = ""

    func callAsFunction(_ action: @escaping @MainActor () async throws -> Void) {
        guard !busy else { return }
        busy = true
        notice = ""
        Task { @MainActor in
            defer { busy = false }
            do { try await action() } catch { notice = error.localizedDescription }
        }
    }
}

/// 처리 중 표시와 결과·오류 안내를 화면 아래에 고정한다. 긴 폼에서도 스크롤 없이 보인다.
private struct StatusBar: ViewModifier {
    @ObservedObject var run: Runner

    func body(content: Content) -> some View {
        content.disabled(run.busy).safeAreaInset(edge: .bottom) {
            if run.busy || !run.notice.isEmpty {
                HStack(spacing: 8) {
                    if run.busy { ProgressView() }
                    Text(run.busy ? "처리 중" : run.notice).font(.footnote)
                }
                .frame(maxWidth: .infinity).padding(12).background(.regularMaterial)
                .accessibilityElement(children: .combine)
            }
        }
    }
}

extension View {
    func status(_ run: Runner) -> some View { modifier(StatusBar(run: run)) }
}

enum Labels {
    static let relationships = [("superior", "상사"), ("colleague", "동료·동기"), ("subordinate", "후배"), ("friend", "친구"),
                                ("family", "가족"), ("partner", "연인"), ("acquaintance", "지인·거래처")]
    static let conditions = [("D", "D 페르소나 + 과거 표현 + 맥락"), ("C", "C 페르소나 + 과거 표현"),
                             ("B", "B 페르소나"), ("A", "A 기준선(페르소나 없음)")]

    /// 페르소나 5단계의 말 풀이 (와이어프레임 M6). 값은 1~5.
    static let formality = ["반말·캐주얼", "편한 말", "보통", "정중", "매우 정중"]
    static let friendliness = ["거리감 있게", "낮음", "보통", "친근", "매우 친근"]
    static let directness = ["많이 돌려 말하기", "완곡", "보통", "분명하게", "결론부터"]

    static func step(_ names: [String], _ value: Int) -> String { names[min(max(value, 1), names.count) - 1] }

    static func violation(_ check: String) -> String { ViolationText.title(check) }

    static func relationship(_ value: String) -> String { relationships.first { $0.0 == value }?.1 ?? value }

    /// 서버 시각(ISO 8601, 소수 초 포함 가능)을 기기 형식으로. 못 읽으면 앞부분을 그대로 보인다.
    static func date(_ iso: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = formatter.date(from: iso)
        if date == nil {
            formatter.formatOptions = [.withInternetDateTime]
            date = formatter.date(from: iso)
        }
        guard let date else { return String(iso.prefix(16)).replacingOccurrences(of: "T", with: " ") }
        return date.formatted(date: .abbreviated, time: .shortened)
    }
}

private struct Root: View {
    @EnvironmentObject private var session: Session

    var body: some View {
        if session.api == nil {
            NavigationStack { LoginView() }
        } else {
            TabView {
                NavigationStack { ComposeView() }.tabItem { Label("작성", systemImage: "square.and.pencil") }
                NavigationStack { ContactsView() }.tabItem { Label("상대", systemImage: "person.2") }
                NavigationStack { HistoryView() }.tabItem { Label("이력", systemImage: "clock") }
                NavigationStack { AccountView() }.tabItem { Label("계정", systemImage: "gearshape") }
            }
        }
    }
}

private struct LoginView: View {
    @EnvironmentObject private var session: Session
    @StateObject private var run = Runner()
    @AppStorage("server") private var server = ""
    @AppStorage("username") private var username = ""
    @State private var password = ""

    private var incomplete: Bool { server.isEmpty || username.isEmpty || password.isEmpty }

    var body: some View {
        Form {
            Section {
                TextField("HTTPS 서버 주소", text: $server).keyboardType(.URL)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                TextField("아이디 (영문·숫자 3~32자)", text: $username).textContentType(.username)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                SecureField("비밀번호", text: $password).textContentType(.password)
                Button("로그인") { sign(register: false) }.disabled(incomplete)
                Button("새 계정으로 가입") { sign(register: true) }.disabled(incomplete)
            } header: {
                Text("서버 로그인")
            } footer: {
                Text("서버 주소와 아이디만 기기에 기억합니다. 비밀번호와 로그인 상태는 앱을 종료하면 사라집니다.")
            }
        }
        .status(run)
        .navigationTitle("Persona_ize")
    }

    private func sign(register: Bool) {
        run {
            let client = try API(server: server)
            let name = username.trimmingCharacters(in: .whitespaces)
            try await client.signIn(name: name, password: password, register: register)
            password = ""
            try await session.start(client, username: name)
        }
    }
}
