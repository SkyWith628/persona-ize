import UIKit

final class KeyboardController: UIInputViewController {
    private let preview = UILabel()
    private let insert = UIButton(type: .system)
    private var displayed: String?

    override func viewDidLoad() {
        super.viewDidLoad()
        preview.numberOfLines = 3
        preview.font = .preferredFont(forTextStyle: .body)
        insert.setTitle("준비한 문장 넣기", for: .normal)
        insert.addTarget(self, action: #selector(insertMessage), for: .touchUpInside)
        let next = UIButton(type: .system)
        next.setTitle("다음 키보드", for: .normal)
        next.addTarget(self, action: #selector(handleInputModeList(from:with:)), for: .allTouchEvents)
        let row = UIStackView(arrangedSubviews: [next, insert])
        row.distribution = .fillEqually
        let editing = UIStackView()
        editing.distribution = .fillEqually
        for symbol in ["?", "!", "공백", "지우기"] {
            let button = UIButton(type: .system)
            button.setTitle(symbol, for: .normal)
            button.addAction(UIAction { [weak self] _ in
                if symbol == "지우기" { self?.textDocumentProxy.deleteBackward() }
                else { self?.textDocumentProxy.insertText(symbol == "공백" ? " " : symbol) }
            }, for: .touchUpInside)
            editing.addArrangedSubview(button)
        }
        let stack = UIStackView(arrangedSubviews: [preview, row, editing])
        stack.axis = .vertical
        stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.layoutMarginsGuide.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: view.layoutMarginsGuide.trailingAnchor),
            stack.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            row.heightAnchor.constraint(greaterThanOrEqualToConstant: 44),
            editing.heightAnchor.constraint(greaterThanOrEqualToConstant: 44)
        ])
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        refresh()
    }

    private func refresh() {
        displayed = KeyboardMessage.read()
        preview.text = displayed ?? "Persona_ize 앱에서 추천을 선택하고 ‘키보드에 준비’를 누르세요."
        insert.isEnabled = displayed?.isEmpty == false
    }

    @objc private func insertMessage() {
        guard let displayed, KeyboardMessage.read() == displayed else { refresh(); return }
        textDocumentProxy.insertText(displayed)
    }
}
