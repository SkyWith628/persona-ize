import UIKit

/// 자판 없이 첨삭 패널만 있는 보조 키보드 (와이어프레임 v2 M1~M4).
///
/// 입력창의 초안을 읽어 서버에서 다듬고, 사용자가 누르면 입력창 글자를 추천으로 바꾼다. 전송은 하지 않는다.
/// 네트워크와 앱 그룹 쓰기는 ‘전체 접근 허용’이 켜져 있어야 한다. 꺼져 있으면 앱에서 준비한 문장만 넣는다.
final class KeyboardController: UIInputViewController {
    private enum Phase {
        case idle
        case picking
        case loading
        case result(Rewrite)
        case kept(Rewrite)
        case failed(String)
    }

    private let content = UIStackView()
    private var phase = Phase.idle { didSet { render() } }
    private var link: KeyboardLink?
    /// 요청에 쓴 초안. 넣기 전에 입력창이 그대로인지 견준다.
    private var draft = ""
    /// 같은 초안을 다시 시도할 때는 같은 키를 쓴다. 성공하거나 초안이 바뀌면 새 키.
    private var key = UUID()
    private var usage: Usage?
    private var task: Task<Void, Never>?

    override func viewDidLoad() {
        super.viewDidLoad()
        content.axis = .vertical
        content.spacing = 8
        content.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(content)
        let height = view.heightAnchor.constraint(equalToConstant: 276)
        height.priority = .init(999)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: view.layoutMarginsGuide.leadingAnchor),
            content.trailingAnchor.constraint(equalTo: view.layoutMarginsGuide.trailingAnchor),
            content.topAnchor.constraint(equalTo: view.topAnchor, constant: 8),
            content.bottomAnchor.constraint(lessThanOrEqualTo: view.bottomAnchor, constant: -6),
            height
        ])
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        link = KeyboardLink.load()
        phase = .idle
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        task?.cancel()
    }

    /// 추천을 보는 동안 입력창이 바뀌면 그 추천은 더 쓸 수 없다.
    override func textDidChange(_ textInput: UITextInput?) {
        super.textDidChange(textInput)
        switch phase {
        case .result, .kept:
            if currentDraft() != draft { phase = .idle }
        default:
            break
        }
    }

    private func currentDraft() -> String {
        (textDocumentProxy.documentContextBeforeInput ?? "") + (textDocumentProxy.documentContextAfterInput ?? "")
    }

    // MARK: 화면

    private func render() {
        content.arrangedSubviews.forEach { $0.removeFromSuperview() }
        switch phase {
        case .idle: renderIdle()
        case .picking: renderPicking()
        case .loading:
            let spinner = UIActivityIndicatorView(style: .medium)
            spinner.startAnimating()
            content.addArrangedSubview(row([spinner, label("\(link?.current?.alias ?? "상대")에게 맞게 다듬는 중…")], equal: false))
            content.addArrangedSubview(row([button("취소") { [weak self] in self?.task?.cancel(); self?.phase = .idle }]))
        case .result(let rewrite): renderResult(rewrite)
        case .kept(let rewrite): renderKept(rewrite)
        case .failed(let message):
            content.addArrangedSubview(label(message, style: .headline))
            content.addArrangedSubview(label("입력창의 초안은 그대로 남아 있어요.", color: .secondaryLabel))
            content.addArrangedSubview(row([button("닫기") { [weak self] in self?.phase = .idle },
                                            button("다시 시도", primary: true) { [weak self] in self?.start() }]))
        }
    }

    /// M1: 맨 윗줄에 호출 버튼과 상대 바꾸기.
    private func renderIdle() {
        if !hasFullAccess {
            content.addArrangedSubview(label("설정 → 일반 → 키보드 → 키보드 → Persona_ize에서 ‘전체 접근 허용’을 켜면 입력창의 초안을 여기서 바로 다듬습니다.", color: .secondaryLabel))
        } else if let link, let person = link.current {
            content.addArrangedSubview(row([
                button("✦ \(person.alias)에게 맞게", primary: true) { [weak self] in self?.start() },
                button("상대 바꾸기") { [weak self] in self?.phase = .picking }
            ]))
        } else if link != nil {
            content.addArrangedSubview(label("Persona_ize 앱의 상대 탭에서 페르소나를 저장한 상대가 있어야 다듬을 수 있어요.", color: .secondaryLabel))
        } else {
            content.addArrangedSubview(label("Persona_ize 앱에서 로그인하면 입력창의 초안을 여기서 바로 다듬을 수 있어요.", color: .secondaryLabel))
        }
        if let prepared = KeyboardMessage.read(), !prepared.isEmpty {
            let preview = label("앱에서 준비한 문장: " + prepared)
            preview.numberOfLines = 2
            content.addArrangedSubview(preview)
            content.addArrangedSubview(row([button("준비한 문장 넣기") { [weak self] in
                guard KeyboardMessage.read() == prepared else { self?.render(); return }
                self?.textDocumentProxy.insertText(prepared)
            }]))
        }
        let next = button("🌐") {}
        next.accessibilityLabel = "다음 키보드"
        next.addTarget(self, action: #selector(handleInputModeList(from:with:)), for: .allTouchEvents)
        var keys = [next]
        for symbol in ["?", "!", "공백", "지우기"] {
            keys.append(button(symbol) { [weak self] in
                if symbol == "지우기" { self?.textDocumentProxy.deleteBackward() }
                else { self?.textDocumentProxy.insertText(symbol == "공백" ? " " : symbol) }
            })
        }
        content.addArrangedSubview(row(keys))
    }

    /// M2: 최근 상대가 먼저 오는 가로 목록.
    private func renderPicking() {
        let close = button("✕") { [weak self] in self?.phase = .idle }
        close.accessibilityLabel = "닫기"
        close.widthAnchor.constraint(equalToConstant: 48).isActive = true
        content.addArrangedSubview(row([label("누구에게 보내나요?", style: .headline), close], equal: false))
        let chips = UIStackView()
        chips.axis = .horizontal
        chips.spacing = 8
        for person in link?.ordered ?? [] {
            let chip = button(person.ready ? person.alias : "\(person.alias) (페르소나 없음)",
                              primary: person.id == link?.current?.id) { [weak self] in self?.choose(person.id) }
            chip.isEnabled = person.ready
            chips.addArrangedSubview(chip)
        }
        chips.translatesAutoresizingMaskIntoConstraints = false
        let scroll = UIScrollView()
        scroll.showsHorizontalScrollIndicator = false
        scroll.addSubview(chips)
        NSLayoutConstraint.activate([
            chips.leadingAnchor.constraint(equalTo: scroll.contentLayoutGuide.leadingAnchor),
            chips.trailingAnchor.constraint(equalTo: scroll.contentLayoutGuide.trailingAnchor),
            chips.topAnchor.constraint(equalTo: scroll.contentLayoutGuide.topAnchor),
            chips.bottomAnchor.constraint(equalTo: scroll.contentLayoutGuide.bottomAnchor),
            chips.heightAnchor.constraint(equalTo: scroll.frameLayoutGuide.heightAnchor),
            scroll.heightAnchor.constraint(equalToConstant: 48)
        ])
        content.addArrangedSubview(scroll)
        content.addArrangedSubview(label("상대를 새로 만들거나 말투를 바꾸려면 Persona_ize 앱을 여세요.", color: .secondaryLabel))
    }

    /// M3: 추천 문장 → 지킨 내용 → 주의 → 넣기.
    private func renderResult(_ rewrite: Rewrite) {
        // 긴 추천은 글자를 줄여 다섯 줄 안에 보인다. 넣은 뒤에는 입력창에서 전체를 볼 수 있다.
        let text = label(rewrite.result.result_text, style: .body)
        text.numberOfLines = 5
        text.adjustsFontSizeToFitWidth = true
        text.minimumScaleFactor = 0.7
        text.accessibilityLabel = "추천 문장: " + rewrite.result.result_text
        content.addArrangedSubview(text)
        if !rewrite.result.preserved_items.isEmpty {
            let kept = label(rewrite.result.preserved_items.map { "✓ " + $0.text }.joined(separator: "   "),
                             style: .footnote, color: .systemGreen)
            kept.numberOfLines = 2
            content.addArrangedSubview(kept)
        }
        if !rewrite.result.warnings.isEmpty {
            let warning = label("확인해 주세요 · " + rewrite.result.warnings.joined(separator: " "), style: .footnote)
            warning.numberOfLines = 3
            warning.backgroundColor = UIColor.systemYellow.withAlphaComponent(0.25)
            content.addArrangedSubview(warning)
        }
        content.addArrangedSubview(row([
            button("다시 만들기") { [weak self] in self?.start() },
            button("초안 그대로") { [weak self] in self?.phase = .idle },
            button("입력창에 넣기", primary: true) { [weak self] in self?.insert(rewrite) }
        ]))
        content.addArrangedSubview(label("넣기만 해요. 전송은 직접 누릅니다." + usageLine, style: .caption2, color: .secondaryLabel))
    }

    /// M4: 검증에 실패해 추천하지 않은 경우.
    private func renderKept(_ rewrite: Rewrite) {
        content.addArrangedSubview(label("초안을 그대로 뒀어요", style: .headline))
        let reasons = rewrite.result.validation.violations.map { ViolationText.title($0.check) + " — " + $0.detail }
            + rewrite.result.warnings
        let detail = label(reasons.isEmpty ? "사실 보존을 확인하지 못해 추천하지 않았습니다." : reasons.joined(separator: "\n"),
                           style: .footnote)
        detail.numberOfLines = 5
        content.addArrangedSubview(detail)
        content.addArrangedSubview(row([button("닫기") { [weak self] in self?.phase = .idle },
                                        button("다시 만들기", primary: true) { [weak self] in self?.start() }]))
        content.addArrangedSubview(label("입력창의 초안은 그대로 있어요." + usageLine, style: .caption2, color: .secondaryLabel))
    }

    private var usageLine: String {
        usage.map { " 오늘 \($0.rewrites) / \($0.rewrite_limit)회 사용." } ?? ""
    }

    // MARK: 동작

    private func choose(_ id: String) {
        link?.choose(id)
        try? link?.save()
        phase = .idle
    }

    private func start() {
        guard let link, let person = link.current else { phase = .idle; return }
        let text = currentDraft()
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            phase = .failed("입력창에 초안을 먼저 써 주세요.")
            return
        }
        if text != draft {
            draft = text
            key = UUID()
        }
        let requestKey = key
        let client = KeyboardClient(link: link)
        phase = .loading
        task?.cancel()
        task = Task { @MainActor [weak self] in
            do {
                let rewrite = try await client.rewrite(contact: person.id, draft: text, key: requestKey)
                let usage = await client.usage()
                guard let self, !Task.isCancelled else { return }
                self.key = UUID()
                self.usage = usage ?? self.usage
                self.phase = rewrite.result.status == .recommended ? .result(rewrite) : .kept(rewrite)
            } catch {
                guard let self, !Task.isCancelled else { return }
                self.phase = .failed(error is ServiceError ? error.localizedDescription : "인터넷 연결을 확인해 주세요.")
            }
        }
    }

    /// 입력창의 초안을 추천으로 바꾼다. 요청 뒤 입력창이 바뀌었으면 바꾸지 않는다.
    private func insert(_ rewrite: Rewrite) {
        let proxy = textDocumentProxy
        guard let plan = DraftReplacement.plan(requested: draft, before: proxy.documentContextBeforeInput,
                                               after: proxy.documentContextAfterInput) else {
            phase = .failed("그 사이 입력창의 글이 바뀌었어요. 다시 만들어 주세요.")
            return
        }
        let link = link
        let replace = { [weak self] in
            for _ in 0..<plan.deletions { proxy.deleteBackward() }
            proxy.insertText(rewrite.result.result_text)
            self?.draft = ""
            self?.phase = .idle
            if let link { Task { await KeyboardClient(link: link).accept(rewrite.id) } }
        }
        if plan.forward == 0 {
            replace()
        } else {
            // 커서가 초안 가운데 있으면 끝으로 옮긴 뒤 지운다. 이동이 반영될 틈을 준다.
            proxy.adjustTextPosition(byCharacterOffset: plan.forward)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.15, execute: replace)
        }
    }

    // MARK: 부품

    private func label(_ text: String, style: UIFont.TextStyle = .subheadline, color: UIColor = .label) -> UILabel {
        let label = UILabel()
        label.text = text
        label.font = .preferredFont(forTextStyle: style)
        label.adjustsFontForContentSizeCategory = true
        label.textColor = color
        label.numberOfLines = 0
        return label
    }

    private func button(_ title: String, primary: Bool = false, action: @escaping () -> Void) -> UIButton {
        var configuration = primary ? UIButton.Configuration.filled() : UIButton.Configuration.gray()
        configuration.title = title
        configuration.titleLineBreakMode = .byTruncatingTail
        configuration.contentInsets = NSDirectionalEdgeInsets(top: 6, leading: 8, bottom: 6, trailing: 8)
        let button = UIButton(configuration: configuration, primaryAction: UIAction { _ in action() })
        button.heightAnchor.constraint(greaterThanOrEqualToConstant: 44).isActive = true
        return button
    }

    private func row(_ views: [UIView], equal: Bool = true) -> UIStackView {
        let row = UIStackView(arrangedSubviews: views)
        row.spacing = 6
        row.alignment = .center
        row.distribution = equal ? .fillEqually : .fill
        return row
    }
}
