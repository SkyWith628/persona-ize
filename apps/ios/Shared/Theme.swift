import SwiftUI
import UIKit

/// 앱과 키보드가 함께 쓰는 디자인 토큰. 색 값은 웹(`backend/app/web/css/tokens.css`)과 같다.
/// 화면 코드에는 색·간격 숫자를 직접 적지 않고 여기 이름만 쓴다.
///
/// 의미 색: accent = 주 행동(하나), ok = 지킨 사실, warn = 확인할 것·원문 유지, bad = 삭제·오류.
/// 글자는 시스템 글자 스타일(.largeTitle, .body …)을 써서 사용자의 글자 크기 설정을 따른다.
enum Theme {
    /// 밝은·어두운 모드에 따라 바뀌는 색 (UIKit용 — 키보드 확장이 쓴다).
    enum Palette {
        static let bg = dynamic(0xF6F6F4, 0x111318)
        static let surface = dynamic(0xFFFFFF, 0x1A1D24)
        static let surface2 = dynamic(0xF0F1F3, 0x22262F)
        static let ink = dynamic(0x16171A, 0xE8EAEF)
        static let ink2 = dynamic(0x3D4049, 0xC5C9D3)
        static let muted = dynamic(0x5C606B, 0x9AA1AE)
        static let line = dynamic(0xDCDEE4, 0x2C313B)
        static let accent = dynamic(0x3B5BDB, 0x8AA2FF)
        static let accentStrong = dynamic(0x2F49B3, 0xA9BBFF)
        static let accentInk = dynamic(0xFFFFFF, 0x0D1330)
        static let accentSoft = dynamic(0xE8ECFB, 0x1F2A52)
        static let ok = dynamic(0x1D6B3A, 0x6FD69A)
        static let okSoft = dynamic(0xE3F4E8, 0x15321F)
        static let warn = dynamic(0x8A5300, 0xF2BD5B)
        static let warnSoft = dynamic(0xFFF3D6, 0x3A2B0E)
        static let bad = dynamic(0xB3261E, 0xFF8F85)
        static let badSoft = dynamic(0xFDE7E5, 0x40201C)

        static func dynamic(_ light: UInt32, _ dark: UInt32) -> UIColor {
            UIColor { $0.userInterfaceStyle == .dark ? rgb(dark) : rgb(light) }
        }

        private static func rgb(_ hex: UInt32) -> UIColor {
            UIColor(red: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                    blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
        }
    }

    // SwiftUI용 색
    static let bg = Color(uiColor: Palette.bg)
    static let surface = Color(uiColor: Palette.surface)
    static let surface2 = Color(uiColor: Palette.surface2)
    static let ink = Color(uiColor: Palette.ink)
    static let ink2 = Color(uiColor: Palette.ink2)
    static let muted = Color(uiColor: Palette.muted)
    static let line = Color(uiColor: Palette.line)
    static let accent = Color(uiColor: Palette.accent)
    static let accentStrong = Color(uiColor: Palette.accentStrong)
    static let accentInk = Color(uiColor: Palette.accentInk)
    static let accentSoft = Color(uiColor: Palette.accentSoft)
    static let ok = Color(uiColor: Palette.ok)
    static let okSoft = Color(uiColor: Palette.okSoft)
    static let warn = Color(uiColor: Palette.warn)
    static let warnSoft = Color(uiColor: Palette.warnSoft)
    static let bad = Color(uiColor: Palette.bad)
    static let badSoft = Color(uiColor: Palette.badSoft)

    /// 간격 (4의 배수).
    enum Space {
        static let xs: CGFloat = 4
        static let s: CGFloat = 8
        static let m: CGFloat = 12
        static let l: CGFloat = 16
        static let xl: CGFloat = 24
        static let xxl: CGFloat = 32
        static let xxxl: CGFloat = 48
    }

    /// 모서리.
    enum Radius {
        static let s: CGFloat = 8
        static let m: CGFloat = 12
        static let l: CGFloat = 16
    }

    /// 화면의 주 버튼 높이. 누를 수 있는 것은 44pt보다 작게 만들지 않는다 (HIG).
    static let controlHeight: CGFloat = 52
    static let minTouch: CGFloat = 44
}

// MARK: - 버튼

/// 화면의 주 행동 하나. 가로를 꽉 채우고 엄지가 닿는 아래쪽에 둔다.
struct PrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { Face(configuration: configuration) }

    private struct Face: View {
        let configuration: Configuration
        @Environment(\.isEnabled) private var enabled

        var body: some View {
            configuration.label
                .font(.headline)
                .frame(maxWidth: .infinity, minHeight: Theme.controlHeight)
                .foregroundStyle(enabled ? Theme.accentInk : Theme.muted)
                .background(enabled ? Theme.accent : Theme.surface2,
                            in: RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
                .opacity(configuration.isPressed ? 0.85 : 1)
                .contentShape(Rectangle())
        }
    }
}

/// 보조 행동. 면(surface2) 위의 강조색 글자.
struct SecondaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { Face(configuration: configuration) }

    private struct Face: View {
        let configuration: Configuration
        @Environment(\.isEnabled) private var enabled

        var body: some View {
            configuration.label
                .font(.subheadline.weight(.semibold))
                .frame(maxWidth: .infinity, minHeight: Theme.minTouch)
                .foregroundStyle(enabled ? Theme.accentStrong : Theme.muted)
                .background(Theme.surface2, in: RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
                .opacity(configuration.isPressed ? 0.7 : 1)
                .contentShape(Rectangle())
        }
    }
}

extension ButtonStyle where Self == PrimaryButtonStyle {
    static var primary: PrimaryButtonStyle { PrimaryButtonStyle() }
}

extension ButtonStyle where Self == SecondaryButtonStyle {
    static var secondary: SecondaryButtonStyle { SecondaryButtonStyle() }
}

// MARK: - 면·꼬리표

extension View {
    /// 카드: 테두리·그림자 없이 면(surface)과 넉넉한 안쪽 여백으로 구분한다.
    func card(padding: CGFloat = Theme.Space.l, fill: Color = Theme.surface) -> some View {
        self.padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(fill, in: RoundedRectangle(cornerRadius: Theme.Radius.l, style: .continuous))
    }

    /// 화면 바탕색(bg). List·Form에 쓰면 시스템 회색 대신 앱 바탕이 깔린다.
    func screenBackground() -> some View {
        self.scrollContentBackground(.hidden).background(Theme.bg.ignoresSafeArea())
    }
}

/// 상태 꼬리표. 색만이 아니라 기호(SF Symbol)와 문구를 함께 쓴다.
struct Tag: View {
    enum Kind { case ok, warn, bad, neutral }

    let text: String
    var kind: Kind = .neutral
    var systemImage: String?

    private var colors: (ink: Color, fill: Color) {
        switch kind {
        case .ok: return (Theme.ok, Theme.okSoft)
        case .warn: return (Theme.warn, Theme.warnSoft)
        case .bad: return (Theme.bad, Theme.badSoft)
        case .neutral: return (Theme.ink2, Theme.surface2)
        }
    }

    var body: some View {
        HStack(spacing: Theme.Space.xs) {
            if let systemImage { Image(systemName: systemImage) }
            Text(text)
        }
        .font(.caption.weight(.medium))
        .foregroundStyle(colors.ink)
        .padding(.horizontal, Theme.Space.s).padding(.vertical, Theme.Space.xs)
        .background(colors.fill, in: RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous))
    }
}
