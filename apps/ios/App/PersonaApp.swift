import SwiftUI

@main
struct PersonaApp: App {
    @StateObject private var session = Session()
    var body: some Scene { WindowGroup { Root().environmentObject(session) } }
}

/// 로그인한 동안의 API 연결과 상대 목록. 로그인 토큰은 키체인에 남겨 다음 실행 때 되살린다.
@MainActor
final class Session: ObservableObject {
    @Published private(set) var api: API?
    @Published private(set) var username = ""
    @Published private(set) var contacts: [Contact] = []
    /// 기기에 남은 로그인을 확인하는 중인가. 끝나기 전에는 로그인 화면을 보이지 않는다.
    @Published private(set) var restoring = true
    /// 로그인 화면이 한 번 보여 줄 안내 (토큰 만료 등). 보여 준 쪽이 비운다.
    @Published var notice = ""

    static let expiredNotice = "로그인이 만료됐어요. 다시 로그인해 주세요."
    static let offlineNotice = "서버에 연결하지 못했어요. 연결을 확인한 뒤 다시 로그인해 주세요."

    func start(_ client: API, username: String) async throws {
        contacts = try await client.contacts()
        self.username = username
        // 이후 요청에서 토큰이 거절되면(만료·폐기) 로그인 화면으로 돌아간다.
        client.onExpired = { [weak self] in self?.expire() }
        api = client
        // 키체인에 못 남겨도 이번 실행 동안은 그대로 쓴다.
        if let login = client.savedLogin(username: username) { try? TokenStore.save(login) }
        syncKeyboard()
    }

    /// 앱을 열 때 한 번: 키체인의 토큰으로 로그인 상태를 되살린다.
    /// 토큰이 거절되면(401) 지우고 로그인 화면으로, 연결 문제면 토큰은 두고 로그인 화면으로 보낸다.
    func restore() async {
        guard restoring else { return }
        defer { restoring = false }
        guard api == nil, let saved = TokenStore.load() else { return }
        guard let client = try? API(server: saved.server, token: saved.token) else {
            TokenStore.clear()
            return
        }
        do {
            try await start(client, username: saved.username)
        } catch let error as ServiceError where error.status == 401 {
            end()
            notice = Self.expiredNotice
        } catch {
            notice = Self.offlineNotice
        }
    }

    /// 새로 로그인하기 전에 기기에 남은 예전 토큰을 그 토큰을 준 서버에서 폐기하고 지운다.
    /// 서버 주소를 바꿔 로그인해도 예전 토큰이 다른 서버로 가지 않는다. 폐기에 실패해도 계속한다.
    func discardSaved() async {
        guard let saved = TokenStore.load() else { return }
        if let old = try? API(server: saved.server, token: saved.token) { try? await old.logout() }
        TokenStore.clear()
        KeyboardLink.clear()
    }

    /// 서버가 토큰을 거절했다(만료·폐기·정지). 기기의 로그인을 지우고 로그인 화면으로 돌린다.
    func expire() {
        guard api != nil else { return }
        end()
        notice = Self.expiredNotice
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
            server: credentials.server, token: credentials.token,
            contacts: contacts.map { .init(id: $0.id, alias: $0.alias, ready: $0.persona_version != nil) },
            selected: defaults.string(forKey: "lastContact") ?? "",
            recents: (defaults.string(forKey: "recentContacts") ?? "").split(separator: ",").map(String.init))
        try? link.save()
    }

    func alias(_ contact: String) -> String { contacts.first { $0.id == contact }?.alias ?? "삭제된 상대" }

    /// 준비한 문장과 기기에 남긴 토큰을 지우고 화면을 로그인으로 돌린다. 서버의 토큰 폐기는 호출한 쪽이 먼저 한다.
    func end() {
        try? KeyboardMessage.clear()
        KeyboardLink.clear()
        TokenStore.clear()
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
/// 처리 중은 중립 면, 안내·오류는 강조색 옅은 면 — 색만이 아니라 기호와 문구로도 구분한다.
private struct StatusBar: ViewModifier {
    @ObservedObject var run: Runner

    func body(content: Content) -> some View {
        content.disabled(run.busy).safeAreaInset(edge: .bottom, spacing: 0) {
            if run.busy || !run.notice.isEmpty {
                HStack(alignment: .firstTextBaseline, spacing: Theme.Space.s) {
                    if run.busy {
                        ProgressView()
                    } else {
                        Image(systemName: "info.circle.fill").foregroundStyle(Theme.accent)
                    }
                    Text(run.busy ? "처리하고 있어요" : run.notice)
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(run.busy ? Theme.ink2 : Theme.ink)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .frame(maxWidth: .infinity, minHeight: Theme.minTouch, alignment: run.busy ? .center : .leading)
                .padding(.horizontal, Theme.Space.l).padding(.vertical, Theme.Space.s)
                .background(run.busy ? Theme.surface2 : Theme.accentSoft,
                            in: RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
                .padding(.horizontal, Theme.Space.l).padding(.vertical, Theme.Space.s)
                .background(Theme.bg)
                .accessibilityElement(children: .combine)
            }
        }
    }
}

extension View {
    func status(_ run: Runner) -> some View { modifier(StatusBar(run: run)) }
}

/// 기기에 기억하는 설정 이름.
enum Prefs {
    /// 실험 설정(조건·상황·토큰 수)을 화면에 보일지. 평가·시연 때만 켠다.
    static let experiment = "experiment"
    /// 가입 전 예시 화면을 봤는지.
    static let seenIntro = "seenIntro"
    /// 로그인 뒤 준비 화면(첫 상대·키보드 안내)을 마쳤는지.
    static let setupDone = "setupDone"
}

/// 결과를 손끝으로 알리는 짧은 진동.
enum Haptic {
    @MainActor static func notify(_ kind: UINotificationFeedbackGenerator.FeedbackType) {
        UINotificationFeedbackGenerator().notificationOccurred(kind)
    }
}

enum Labels {
    static let relationships = [("superior", "상사"), ("colleague", "동료·동기"), ("subordinate", "후배"), ("friend", "친구"),
                                ("family", "가족"), ("partner", "연인"), ("acquaintance", "지인·거래처")]
    /// 첨삭 요청의 기본 조건: 서버가 이 상대에게 쓸 수 있는 가장 풍부한 조건을 고른다 (웹과 같다).
    static let defaultCondition = "auto"
    static let conditions = [("auto", "자동 (서버가 고름)"), ("D", "D 말투 + 예전 표현 + 지난 일"),
                             ("C", "C 말투 + 예전 표현"), ("B", "B 말투"), ("A", "A 기준선(말투 없음)")]

    /// 직접 고른 B~D는 저장된 말투 설정이 있어야 한다(없으면 서버가 422). 자동과 A는 없어도 된다.
    static func needsPersona(_ condition: String) -> Bool { ["B", "C", "D"].contains(condition) }

    /// 페르소나 5단계의 말 풀이 (와이어프레임 M6). 값은 1~5.
    static let formality = ["반말·캐주얼", "편한 말", "보통", "정중", "매우 정중"]
    static let friendliness = ["거리감 있게", "낮음", "보통", "친근", "매우 친근"]
    static let directness = ["많이 돌려 말하기", "완곡", "보통", "분명하게", "결론부터"]

    static func step(_ names: [String], _ value: Int) -> String { names[min(max(value, 1), names.count) - 1] }

    static func violation(_ check: String) -> String { ViolationText.title(check) }

    /// 이력 한 줄의 부가 정보: 상대 · 시각. 실험 설정을 켠 경우에만 조건(A~D)을 함께 보인다.
    static func meta(alias: String, rewrite: Rewrite, experiment: Bool) -> String {
        let parts = [alias, experiment ? "조건 \(rewrite.condition)" : nil, date(rewrite.created_at)]
        return parts.compactMap { $0 }.joined(separator: " · ")
    }

    static func relationship(_ value: String) -> String { relationships.first { $0.0 == value }?.1 ?? value }

    /// 서버 시각(ISO 8601, 소수 초 포함 가능)을 한국어 표기로. 못 읽으면 앞부분을 그대로 보인다.
    /// 화면 문구가 모두 한국어라 날짜도 기기 언어와 상관없이 한국어로 쓴다 (앱의 기본 언어가 영어라 그대로 두면 영어로 나왔다).
    static func date(_ iso: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = formatter.date(from: iso)
        if date == nil {
            formatter.formatOptions = [.withInternetDateTime]
            date = formatter.date(from: iso)
        }
        guard let date else { return String(iso.prefix(16)).replacingOccurrences(of: "T", with: " ") }
        return date.formatted(Date.FormatStyle(date: .abbreviated, time: .shortened, locale: Locale(identifier: "ko_KR")))
    }
}

private struct Root: View {
    @EnvironmentObject private var session: Session
    @AppStorage(Prefs.seenIntro) private var seenIntro = false
    @AppStorage(Prefs.setupDone) private var setupDone = false

    var body: some View {
        Group {
            if session.api == nil {
                if session.restoring {
                    // 기기에 남은 로그인을 확인하는 동안. 토큰이 없으면 바로 지나간다.
                    ProgressView("로그인 상태를 확인하고 있어요")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .background(Theme.bg.ignoresSafeArea())
                } else if seenIntro {
                    NavigationStack { LoginView() }
                } else {
                    // 처음 한 번은 가입 전에 예시부터 보여 준다.
                    IntroView { seenIntro = true }
                }
            } else if !setupDone {
                NavigationStack { SetupView(hasContacts: !session.contacts.isEmpty) { setupDone = true } }
            } else {
                TabView {
                    NavigationStack { ComposeView() }.tabItem { Label("작성", systemImage: "square.and.pencil") }
                    NavigationStack { ContactsView() }.tabItem { Label("상대", systemImage: "person.2") }
                    NavigationStack { HistoryView() }.tabItem { Label("이력", systemImage: "clock") }
                    NavigationStack { AccountView() }.tabItem { Label("계정", systemImage: "gearshape") }
                }
            }
        }
        .tint(Theme.accent)
        .task { await session.restore() }
    }
}

private struct LoginView: View {
    @EnvironmentObject private var session: Session
    @StateObject private var run = Runner()
    @AppStorage("server") private var server = ""
    @AppStorage("username") private var username = ""
    @State private var password = ""
    /// 예전에 적어 둔 주소가 있으면 펼쳐서 보여 준다 — 기본 주소 대신 그 값으로 연결되기 때문이다.
    @State private var showServer = !(UserDefaults.standard.string(forKey: "server") ?? "").isEmpty
    /// 가입은 됐지만 승인을 기다리는 아이디. 같은 아이디로 다시 가입하지 않게 가입 버튼을 감춘다.
    @State private var pendingName = ""
    /// 새로 가입할 때만 쓰는 값. 기기에 저장하지 않는다.
    @State private var inviteCode = ""
    @State private var consent = false

    private let defaultServer = ServerChoice.bundled

    /// 지금 연결할 주소: 입력값이 먼저, 비어 있으면 기본 주소.
    private var address: String? { ServerChoice.resolve(input: server, fallback: defaultServer) }
    private var name: String { username.trimmingCharacters(in: .whitespaces) }
    private var incomplete: Bool { address == nil || name.isEmpty || password.isEmpty }
    private var awaitingApproval: Bool { !pendingName.isEmpty && pendingName == name }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.xl) {
                intro
                fields
                if !awaitingApproval { signupFields }
                if defaultServer != nil { serverSettings }
                Text("비밀번호는 기기에 저장하지 않아요. 로그인 상태는 이 기기에만 안전하게 보관하고, 로그아웃하면 지워요.")
                    .font(.footnote).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, Theme.Space.xs)
            }
            .padding(.horizontal, Theme.Space.l)
            .padding(.top, Theme.Space.xl).padding(.bottom, Theme.Space.l)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(Theme.bg.ignoresSafeArea())
        // 주 행동은 엄지가 닿는 아래쪽에. 키보드가 올라오면 함께 올라와 가려지지 않는다.
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: Theme.Space.s) {
                Button("로그인") { sign(register: false) }.buttonStyle(.primary)
                if !awaitingApproval {
                    Button("새 계정으로 가입") { sign(register: true) }.buttonStyle(.secondary)
                }
            }
            .disabled(incomplete)
            .padding(.horizontal, Theme.Space.l).padding(.vertical, Theme.Space.m)
            .background(Theme.bg)
        }
        .status(run)
        .navigationTitle("Persona_ize")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            // 토큰이 만료돼 돌아온 경우 등: 왜 로그인 화면인지 한 번 알린다.
            if !session.notice.isEmpty {
                run.notice = session.notice
                session.notice = ""
            }
        }
    }

    /// 첫인상: 큰 제목 하나와 한 줄 소개.
    private var intro: some View {
        VStack(alignment: .leading, spacing: Theme.Space.m) {
            Image(systemName: "text.bubble.fill")
                .font(.title2)
                .foregroundStyle(Theme.accent)
                .frame(width: Theme.controlHeight, height: Theme.controlHeight)
                .background(Theme.accentSoft, in: RoundedRectangle(cornerRadius: Theme.Radius.l, style: .continuous))
                .accessibilityHidden(true)
            Text("사실은 그대로,\n말투만 상대에 맞게")
                .font(.largeTitle.bold()).foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityAddTraits(.isHeader)
            Text("보낼 글을 받는 사람에 맞는 말투로 다듬어 드려요.")
                .font(.body).foregroundStyle(Theme.ink2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, Theme.Space.xs)
    }

    /// 입력칸: 면(surface) 하나 위에 아이디·비밀번호. 줄 사이는 옅은 선으로만 나눈다.
    /// 기본 주소가 없는 빌드에서는 서버 주소 줄이 맨 위에 함께 놓인다.
    private var fields: some View {
        VStack(alignment: .leading, spacing: Theme.Space.s) {
            Text(defaultServer == nil ? "서버 로그인" : "로그인")
                .font(.footnote.weight(.semibold)).foregroundStyle(Theme.muted)
                .padding(.horizontal, Theme.Space.xs)
                .accessibilityAddTraits(.isHeader)
            VStack(spacing: 0) {
                if defaultServer == nil {
                    serverField
                    separator
                }
                row("person") {
                    TextField("아이디 (영문·숫자 3~32자)", text: $username).textContentType(.username)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                }
                separator
                row("lock") {
                    SecureField("비밀번호", text: $password).textContentType(.password)
                }
            }
            .card(padding: 0)
        }
    }

    /// 가입할 때만 필요한 값: 초대 코드(초대제 서버)와 안내 동의(동의 필수 서버). 로그인에는 쓰지 않는다.
    private var signupFields: some View {
        VStack(alignment: .leading, spacing: Theme.Space.s) {
            Text("새로 가입할 때만")
                .font(.footnote.weight(.semibold)).foregroundStyle(Theme.muted)
                .padding(.horizontal, Theme.Space.xs)
                .accessibilityAddTraits(.isHeader)
            VStack(spacing: 0) {
                row("ticket") {
                    TextField("초대 코드 (받았다면)", text: $inviteCode)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                }
                separator
                Toggle(isOn: $consent) {
                    Text("개인정보 처리방침과 이용약관을 읽었고 동의해요")
                        .font(.body).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .tint(Theme.accent)
                .padding(.horizontal, Theme.Space.l).padding(.vertical, Theme.Space.s)
                .frame(minHeight: Theme.controlHeight)
            }
            .card(padding: 0)
            if let address, let base = URL(string: address) {
                HStack(spacing: Theme.Space.l) {
                    Link("개인정보 처리방침", destination: base.appendingPathComponent("privacy"))
                    Link("이용약관", destination: base.appendingPathComponent("terms"))
                }
                .font(.footnote)
                .padding(.horizontal, Theme.Space.xs)
            }
        }
    }

    private var serverField: some View {
        row("link") {
            TextField("HTTPS 서버 주소", text: $server).keyboardType(.URL)
                .textInputAutocapitalization(.never).autocorrectionDisabled()
        }
    }

    /// 서버 설정: 평소에는 접어 둔다. 펼치면 주소 입력칸과 지금 쓰는 주소가 나온다.
    private var serverSettings: some View {
        VStack(alignment: .leading, spacing: Theme.Space.s) {
            Button {
                withAnimation { showServer.toggle() }
            } label: {
                HStack(spacing: Theme.Space.s) {
                    Text("서버 설정")
                    Image(systemName: showServer ? "chevron.down" : "chevron.right").accessibilityHidden(true)
                    Spacer()
                }
                .font(.footnote.weight(.semibold)).foregroundStyle(Theme.muted)
                .frame(minHeight: Theme.minTouch)
                .padding(.horizontal, Theme.Space.xs)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(showServer ? "펼침" : "접힘")
            if showServer {
                serverField.card(padding: 0)
                Group {
                    Text("비워 두면 기본 주소로 연결해요.")
                    if let address { Text("지금 쓰는 주소: \(address)") }
                }
                .font(.footnote).foregroundStyle(Theme.muted)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, Theme.Space.xs)
                if address != defaultServer {
                    Button("기본 주소로 되돌리기") { server = "" }.buttonStyle(.secondary)
                }
            }
        }
    }

    private var separator: some View {
        Rectangle().fill(Theme.line).frame(height: 1).padding(.leading, Theme.Space.l)
    }

    private func row<Field: View>(_ systemImage: String, @ViewBuilder field: () -> Field) -> some View {
        HStack(spacing: Theme.Space.m) {
            Image(systemName: systemImage)
                .font(.body).foregroundStyle(Theme.muted)
                .frame(width: Theme.Space.xl)
                .accessibilityHidden(true)
            field().font(.body).foregroundStyle(Theme.ink)
        }
        .padding(.horizontal, Theme.Space.l)
        .frame(minHeight: Theme.controlHeight)
    }

    private func sign(register: Bool) {
        let name = name
        let code = inviteCode.trimmingCharacters(in: .whitespaces)
        run {
            guard let address else { throw ServiceError(message: "HTTPS 서버 주소를 확인해 주세요.") }
            let client = try API(server: address)
            // 기기에 남은 예전 토큰(다른 서버의 것일 수도 있다)은 새 로그인 전에 폐기한다.
            await session.discardSaved()
            do {
                switch try await client.signIn(name: name, password: password, register: register,
                                                    inviteCode: code, consent: consent) {
                case .session:
                    password = ""
                    do {
                        try await session.start(client, username: name)
                    } catch {
                        // 토큰은 받았는데 첫 요청이 실패했다. 쓰지 못할 토큰을 서버에 남기지 않는다.
                        try? await client.logout()
                        throw error
                    }
                    pendingName = ""
                case .pendingApproval:
                    // 승인제 가입(202): 계정만 만들어졌고 토큰은 없다. 상대 목록을 읽지 않고 로그인으로 유도한다.
                    password = ""
                    pendingName = name
                    run.notice = SignInOutcome.pendingNotice
                }
            } catch let error as AccountStateError {
                // 로그인 403: 서버가 토큰을 주지 않았다. 승인 대기면 같은 아이디로 다시 가입하지 않게 한다.
                if error.state == .pending { pendingName = name }
                throw ServiceError(message: error.state.notice, status: error.status)
            }
        }
    }
}
