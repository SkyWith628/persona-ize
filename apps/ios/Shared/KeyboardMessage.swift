import Foundation

enum KeyboardMessage {
    private static func file() throws -> URL {
        guard let group = Bundle.main.object(forInfoDictionaryKey: "PersonaAppGroup") as? String,
              let directory = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: group)
        else { throw CocoaError(.fileNoSuchFile) }
        return directory.appendingPathComponent("approved-message.txt")
    }

    static func read() -> String? { try? String(contentsOf: file(), encoding: .utf8) }

    static func save(_ text: String) throws {
        try Data(text.utf8).write(to: file(), options: [.atomic, .completeFileProtection])
    }

    static func clear() throws {
        let url = try file()
        if FileManager.default.fileExists(atPath: url.path) { try FileManager.default.removeItem(at: url) }
    }
}
