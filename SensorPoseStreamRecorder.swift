import Foundation

final class SensorPoseStreamRecorder {
    private var fileURL: URL?
    private var fileHandle: FileHandle?
    private var eventFileURL: URL?
    private var eventFileHandle: FileHandle?

    var currentFileName: String {
        fileURL?.lastPathComponent ?? "No sensor log yet"
    }

    func startSessionIfNeeded() {
        guard fileURL == nil else { return }

        let fileName = "sensor_pose_\(Self.timestampFormatter.string(from: Date())).csv"
        let documentsURL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let logsURL = documentsURL.appendingPathComponent("SensorCaptures", isDirectory: true)
        let csvURL = logsURL.appendingPathComponent(fileName)
        let eventsURL = logsURL.appendingPathComponent("sensor_events_\(Self.timestampFormatter.string(from: Date())).csv")

        do {
            try FileManager.default.createDirectory(at: logsURL, withIntermediateDirectories: true)
            try Data(Self.csvHeader.utf8).write(to: csvURL, options: .atomic)
            try Data(Self.eventHeader.utf8).write(to: eventsURL, options: .atomic)

            let handle = try FileHandle(forWritingTo: csvURL)
            try handle.seekToEnd()
            let eventHandle = try FileHandle(forWritingTo: eventsURL)
            try eventHandle.seekToEnd()

            fileURL = csvURL
            fileHandle = handle
            eventFileURL = eventsURL
            eventFileHandle = eventHandle
        } catch {
            finishSession()
        }
    }

    func append(sample: SensorPoseSample) {
        startSessionIfNeeded()

        guard let fileHandle else { return }

        let sensorTime = sample.sensorTimestamp.map { Self.format($0) } ?? ""
        let line = [
            String(sample.sequence),
            sample.source,
            String(sample.protocolVersion),
            Self.format(sample.receivedTimestamp),
            sensorTime,
            Self.format(Double(sample.position.x)),
            Self.format(Double(sample.position.y)),
            Self.format(Double(sample.position.z)),
            Self.format(Double(sample.orientation.vector.x)),
            Self.format(Double(sample.orientation.vector.y)),
            Self.format(Double(sample.orientation.vector.z)),
            Self.format(Double(sample.orientation.vector.w)),
            sample.checksumValid.map { $0 ? "true" : "false" } ?? "",
            Self.escape(sample.rawLine ?? "")
        ].joined(separator: ",") + "\n"

        if let data = line.data(using: .utf8) {
            try? fileHandle.write(contentsOf: data)
        }
    }

    func appendEvent(kind: String, detail: String) {
        startSessionIfNeeded()

        guard let eventFileHandle else { return }
        let line = [
            Self.format(Date().timeIntervalSince1970),
            Self.escape(kind),
            Self.escape(detail)
        ].joined(separator: ",") + "\n"

        if let data = line.data(using: .utf8) {
            try? eventFileHandle.write(contentsOf: data)
        }
    }

    func finishSession() {
        try? fileHandle?.close()
        try? eventFileHandle?.close()
        fileHandle = nil
        fileURL = nil
        eventFileHandle = nil
        eventFileURL = nil
    }

    private static func format(_ value: Double) -> String {
        String(format: "%.6f", locale: Locale(identifier: "en_US_POSIX"), value)
    }

    private static func escape(_ value: String) -> String {
        let escaped = value.replacingOccurrences(of: "\"", with: "\"\"")
        return "\"\(escaped)\""
    }

    private static let csvHeader = "sequence,source,protocol_version,received_time,sensor_time,x,y,z,qx,qy,qz,qw,checksum_valid,raw_line\n"
    private static let eventHeader = "time,kind,detail\n"

    private static let timestampFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return formatter
    }()
}
