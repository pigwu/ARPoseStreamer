import Foundation
import simd

struct PoseCaptureArtifact {
    let sessionDirectoryURL: URL
    let poseCSVURL: URL
    let manifestURL: URL
    let videoURL: URL?
}

private struct PoseCaptureManifest: Codable {
    let createdAtUnixTime: TimeInterval
    let poseCSVFileName: String
    let videoFileName: String?
    let sessionStartFrameTime: TimeInterval
    let videoStartOffsetSeconds: TimeInterval?
}

final class PoseDataSessionRecorder {
    var onSessionSaved: ((PoseCaptureArtifact) -> Void)?

    private var sessionDirectoryURL: URL?
    private var poseCSVURL: URL?
    private var videoURL: URL?
    private var fileHandle: FileHandle?
    private var sessionStartFrameTime: TimeInterval?
    private var videoStartFrameTime: TimeInterval?
    private var creationTime: Date?

    func startSessionIfNeeded() {
        guard sessionDirectoryURL == nil else { return }

        let sessionStamp = Self.timestampFormatter.string(from: Date())
        let documentsURL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let capturesURL = documentsURL.appendingPathComponent("Captures", isDirectory: true)
        let sessionURL = capturesURL.appendingPathComponent(sessionStamp, isDirectory: true)
        let poseCSVURL = sessionURL.appendingPathComponent("pose.csv")

        do {
            try FileManager.default.createDirectory(at: sessionURL, withIntermediateDirectories: true)
            try Data(Self.csvHeader.utf8).write(to: poseCSVURL, options: .atomic)

            let fileHandle = try FileHandle(forWritingTo: poseCSVURL)
            try fileHandle.seekToEnd()

            self.sessionDirectoryURL = sessionURL
            self.poseCSVURL = poseCSVURL
            self.fileHandle = fileHandle
            self.creationTime = Date()
        } catch {
            reset()
        }
    }

    func append(sample: PoseSampleRecord) {
        startSessionIfNeeded()

        if sessionStartFrameTime == nil {
            sessionStartFrameTime = sample.frameTimestamp
        }

        guard let fileHandle, let sessionStartFrameTime else { return }

        let relativeTime = sample.frameTimestamp - sessionStartFrameTime
        let line = String(
            format: "%u,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f\n",
            locale: Locale(identifier: "en_US_POSIX"),
            sample.sequence,
            sample.senderTimestamp,
            sample.frameTimestamp,
            relativeTime,
            sample.position.x,
            sample.position.y,
            sample.position.z,
            sample.orientation.vector.x,
            sample.orientation.vector.y,
            sample.orientation.vector.z,
            sample.orientation.vector.w
        )

        if let data = line.data(using: .utf8) {
            try? fileHandle.write(contentsOf: data)
        }
    }

    func markVideoStarted(frameTimestamp: TimeInterval) {
        if videoStartFrameTime == nil {
            videoStartFrameTime = frameTimestamp
        }
    }

    func attachVideo(url: URL?) {
        videoURL = url
    }

    func finishSession() {
        guard
            let sessionDirectoryURL,
            let poseCSVURL,
            let sessionStartFrameTime,
            let creationTime
        else {
            reset()
            return
        }

        try? fileHandle?.close()

        let manifest = PoseCaptureManifest(
            createdAtUnixTime: creationTime.timeIntervalSince1970,
            poseCSVFileName: poseCSVURL.lastPathComponent,
            videoFileName: videoURL?.lastPathComponent,
            sessionStartFrameTime: sessionStartFrameTime,
            videoStartOffsetSeconds: videoStartFrameTime.map { $0 - sessionStartFrameTime }
        )

        let manifestURL = sessionDirectoryURL.appendingPathComponent("capture_manifest.json")
        if let data = try? JSONEncoder.pretty.encode(manifest) {
            try? data.write(to: manifestURL, options: .atomic)
        }

        onSessionSaved?(
            PoseCaptureArtifact(
                sessionDirectoryURL: sessionDirectoryURL,
                poseCSVURL: poseCSVURL,
                manifestURL: manifestURL,
                videoURL: videoURL
            )
        )

        reset()
    }

    private func reset() {
        try? fileHandle?.close()
        sessionDirectoryURL = nil
        poseCSVURL = nil
        videoURL = nil
        fileHandle = nil
        sessionStartFrameTime = nil
        videoStartFrameTime = nil
        creationTime = nil
    }

    private static let csvHeader = "sequence,sender_time,frame_time,relative_time,x,y,z,qx,qy,qz,qw\n"

    private static let timestampFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return formatter
    }()
}

private extension JSONEncoder {
    static var pretty: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return encoder
    }
}

struct PoseSampleRecord {
    let sequence: UInt32
    let senderTimestamp: TimeInterval
    let frameTimestamp: TimeInterval
    let position: SIMD3<Float>
    let orientation: simd_quatf
}
