import Foundation

/// 붙여 넣은 대화를 가져오기 요청으로 바꾼다. 웹 관리 화면(`backend/app/web/app.js parseImport`)과 같은 규칙.
enum ImportParser {
    /// - Parameters:
    ///   - text: 한 줄에 `2026-10-05 09:25 | 화자 | 내용`, 또는 메시지 JSON 배열.
    ///   - me: 내 발화로 볼 화자 이름. 나머지 이름은 상대로 지정한다.
    ///   - contact: 상대 별칭 (JSON 배열 입력의 화자 지정에 쓴다).
    static func parse(_ text: String, me: String, contact: String) throws -> ImportPayload {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw ServiceError(message: "가져올 대화를 붙여 넣어 주세요.") }
        if trimmed.hasPrefix("{") { return try telegram(trimmed, me: me) }
        if trimmed.hasPrefix("[") {
            guard let messages = try? JSONDecoder().decode([ImportMessage].self, from: Data(trimmed.utf8)) else {
                throw ServiceError(message: "JSON 배열을 읽지 못했어요. id·speaker·sent_at·text가 필요해요.")
            }
            return ImportPayload(speaker_map: me.isEmpty ? [:] : [me: "me", contact: "contact"], messages: messages)
        }
        var messages: [ImportMessage] = []
        var speakerMap: [String: String] = [:]
        for (index, line) in trimmed.components(separatedBy: .newlines).enumerated() {
            if line.trimmingCharacters(in: .whitespaces).isEmpty { continue }
            let parts = line.split(separator: "|", omittingEmptySubsequences: false)
                .map { $0.trimmingCharacters(in: .whitespaces) }
            guard parts.count >= 3, !parts[0].isEmpty, !parts[1].isEmpty else {
                throw ServiceError(message: "\(index + 1)번째 줄: '날짜 시각 | 화자 | 내용' 형식이 아니에요.")
            }
            let speaker = parts[1]
            if speaker != "me", speaker != "contact" { speakerMap[speaker] = speaker == me ? "me" : "contact" }
            messages.append(ImportMessage(id: String(format: "m%02d", index + 1), speaker: speaker,
                                          sent_at: timestamp(parts[0]), text: parts[2...].joined(separator: " | ")))
        }
        return ImportPayload(speaker_map: speakerMap, messages: messages)
    }

    /// 시간대가 없으면 한국 시간(+09:00)으로 본다.
    static func timestamp(_ raw: String) -> String {
        var when = raw
        if let space = when.firstIndex(of: " ") { when.replaceSubrange(space...space, with: "T") }
        let time = when.dropFirst(10)
        if time.contains("+") || time.contains("Z") { return when }
        return when + (time.filter { $0 == ":" }.count < 2 ? ":00" : "") + "+09:00"
    }

    /// PC용 텔레그램의 대화 내보내기(result.json). 일반 메시지만 쓰고 서비스 메시지·빈 글은 건너뛴다.
    /// `text`는 문자열이거나 조각 배열(문자열 또는 `{type, text}`)이다.
    static func telegram(_ json: String, me: String) throws -> ImportPayload {
        guard let root = try? JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any],
              let items = root["messages"] as? [[String: Any]] else {
            throw ServiceError(message: "텔레그램 내보내기 파일(result.json)을 읽지 못했어요.")
        }
        guard !me.isEmpty else { throw ServiceError(message: "대화에 나오는 내 이름을 텔레그램 표시 이름 그대로 넣어 주세요.") }
        var messages: [ImportMessage] = []
        var speakerMap: [String: String] = [:]
        for item in items where item["type"] as? String == "message" {
            guard let speaker = item["from"] as? String, !speaker.isEmpty, let id = item["id"] else { continue }
            let text = telegramText(item["text"]).trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            let sentAt: String
            if let unix = (item["date_unixtime"] as? String).flatMap(TimeInterval.init) {
                sentAt = ISO8601DateFormatter().string(from: Date(timeIntervalSince1970: unix))
            } else if let date = item["date"] as? String {
                sentAt = timestamp(date)
            } else {
                continue
            }
            speakerMap[speaker] = speaker == me ? "me" : "contact"
            messages.append(ImportMessage(id: "tg\(id)", speaker: speaker, sent_at: sentAt, text: String(text.prefix(4000))))
        }
        guard !messages.isEmpty else { throw ServiceError(message: "가져올 메시지가 없어요.") }
        guard speakerMap[me] == "me" else {
            throw ServiceError(message: "‘\(me)’ 이름이 파일에 없어요. 파일에 있는 이름: \(speakerMap.keys.sorted().joined(separator: ", "))")
        }
        return ImportPayload(speaker_map: speakerMap, messages: messages)
    }

    private static func telegramText(_ value: Any?) -> String {
        if let text = value as? String { return text }
        guard let parts = value as? [Any] else { return "" }
        return parts.map { ($0 as? String) ?? (($0 as? [String: Any])?["text"] as? String) ?? "" }.joined()
    }
}
