import Foundation

final class SensorPoseStreamRecorder {
    private var fileURL: URL?
    private var fileHandle: FileHandle?

    var currentFileName: String {
        fileURL?.lastPathComponent ?? "No sensor log yet"
    }

    func startSessionIfNeeded() {
        guard fileURL == nil else { return }

        let fileName = "sensor_pose_\(Self.timestampFormatter.string(from: Date())).csv"
        let documentsURL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let logsURL = documentsURL.appendingPathComponent("SensorCaptures", isDirectory: true)
        let csvURL = logsURL.appendingPathComponent(fileName)

        do {
            try FileManager.default.createDirectory(at: logsURL, withIntermediateDirectories: true)
            try Data(Self.csvHeader.utf8).write(to: csvURL, options: .atomic)

            let handle = try FileHandle(forWritingTo: csvURL)
            try handle.seekToEnd()

            fileURL = csvURL
            fileHandle = handle
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
            Self.format(sample.receivedTimestamp),
            sensorTime,
            Self.format(Double(sample.position.x)),
            Self.format(Double(sample.position.y)),
            Self.format(Double(sample.position.z)),
            Self.format(Double(sample.orientation.vector.x)),
            Self.format(Double(sample.orientation.vector.y)),
            Self.format(Double(sample.orientation.vector.z)),
            Self.format(Double(sample.orientation.vector.w))
        ].joined(separator: ",") + "\n"

        if let data = line.data(using: .utf8) {
            try? fileHandle.write(contentsOf: data)
        }
    }

    func finishSession() {
        try? fileHandle?.close()
        fileHandle = nil
        fileURL = nil
    }

    private static func format(_ value: Double) -> String {
        String(format: "%.6f", locale: Locale(identifier: "en_US_POSIX"), value)
    }

    private static let csvHeader = "sequence,received_time,sensor_time,x,y,z,qx,qy,qz,qw\n"

    private static let timestampFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return formatter
    }()
}
