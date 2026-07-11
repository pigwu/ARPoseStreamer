import Foundation
import simd

struct PoseCaptureArtifact {
    let sessionDirectoryURL: URL
    let poseCSVURL: URL
    let magneticCSVURL: URL?
    let manifestURL: URL
    let videoURL: URL?
    let warning: String?
}

private struct PoseCaptureManifest: Codable {
    let createdAtUnixTime: TimeInterval
    let poseCSVFileName: String
    let magneticCSVFileName: String?
    let videoFileName: String?
    let sessionStartFrameTime: TimeInterval
    let magneticStartOffsetSeconds: TimeInterval?
    let poseSampleCount: Int
    let magneticSampleCount: Int
    let videoStartOffsetSeconds: TimeInterval?
}

final class PoseDataSessionRecorder {
    var onSessionSaved: ((PoseCaptureArtifact) -> Void)?

    private var sessionDirectoryURL: URL?
    private var poseCSVURL: URL?
    private var magneticCSVURL: URL?
    private var videoURL: URL?
    private var fileHandle: FileHandle?
    private var magneticFileHandle: FileHandle?
    private var sessionStartFrameTime: TimeInterval?
    private var firstMagneticMonotonicTime: TimeInterval?
    private var videoStartFrameTime: TimeInterval?
    private var creationTime: Date?
    private var lastVideoArchiveWarning: String?
    private var sampleCount = 0
    private var magneticSampleCount = 0

    func startSessionIfNeeded() {
        guard sessionDirectoryURL == nil else { return }

        let sessionStamp = Self.timestampFormatter.string(from: Date())
        let documentsURL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let capturesURL = documentsURL.appendingPathComponent("Captures", isDirectory: true)
        let sessionURL = capturesURL.appendingPathComponent(sessionStamp, isDirectory: true)
        let poseCSVURL = sessionURL.appendingPathComponent("pose.csv")

        do {
            self.sessionDirectoryURL = sessionURL
            self.poseCSVURL = poseCSVURL
            try FileManager.default.createDirectory(at: sessionURL, withIntermediateDirectories: true)
            try Data(Self.csvHeader.utf8).write(to: poseCSVURL, options: .atomic)

            let fileHandle = try FileHandle(forWritingTo: poseCSVURL)
            try fileHandle.seekToEnd()

            self.fileHandle = fileHandle
            self.creationTime = Date()
        } catch {
            reset(deleteSessionDirectory: true)
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
            do {
                try fileHandle.write(contentsOf: data)
                sampleCount += 1
            } catch {
                return
            }
        }
    }

    func append(magneticSample: MagneticSensorSample) {
        startSessionIfNeeded()

        guard let magneticFileHandle = ensureMagneticFile() else { return }

        var fields = [
            String(magneticSample.sequence),
            String(magneticSample.mcuTimeUs),
            Self.format(magneticSample.receivedWallTime),
            Self.format(magneticSample.receivedMonotonicTime)
        ]

        for chip in magneticSample.chips {
            fields.append(Self.format(Double(chip.t)))
            fields.append(Self.format(Double(chip.x)))
            fields.append(Self.format(Double(chip.y)))
            fields.append(Self.format(Double(chip.z)))
        }

        let line = fields.joined(separator: ",") + "\n"
        guard let data = line.data(using: .utf8) else { return }

        do {
            try magneticFileHandle.write(contentsOf: data)
            if firstMagneticMonotonicTime == nil {
                firstMagneticMonotonicTime = magneticSample.receivedMonotonicTime
            }
            magneticSampleCount += 1
        } catch {
            return
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
            let creationTime
        else {
            reset(deleteSessionDirectory: true)
            return
        }

        guard sampleCount > 0 || magneticSampleCount > 0 || videoURL != nil else {
            reset(deleteSessionDirectory: true)
            return
        }

        try? fileHandle?.close()
        try? magneticFileHandle?.close()

        lastVideoArchiveWarning = nil
        let archivedVideoURL = archiveVideoIfNeeded(videoURL, into: sessionDirectoryURL)
        let manifestSessionStartFrameTime = sessionStartFrameTime ?? firstMagneticMonotonicTime ?? videoStartFrameTime ?? 0

        let manifest = PoseCaptureManifest(
            createdAtUnixTime: creationTime.timeIntervalSince1970,
            poseCSVFileName: poseCSVURL.lastPathComponent,
            magneticCSVFileName: magneticCSVURL?.lastPathComponent,
            videoFileName: archivedVideoURL?.lastPathComponent,
            sessionStartFrameTime: manifestSessionStartFrameTime,
            magneticStartOffsetSeconds: firstMagneticMonotonicTime.map { $0 - manifestSessionStartFrameTime },
            poseSampleCount: sampleCount,
            magneticSampleCount: magneticSampleCount,
            videoStartOffsetSeconds: videoStartFrameTime.map { $0 - manifestSessionStartFrameTime }
        )

        let manifestURL = sessionDirectoryURL.appendingPathComponent("capture_manifest.json")
        if let data = try? JSONEncoder.pretty.encode(manifest) {
            try? data.write(to: manifestURL, options: .atomic)
        }

        onSessionSaved?(
            PoseCaptureArtifact(
                sessionDirectoryURL: sessionDirectoryURL,
                poseCSVURL: poseCSVURL,
                magneticCSVURL: magneticCSVURL,
                manifestURL: manifestURL,
                videoURL: archivedVideoURL,
                warning: lastVideoArchiveWarning
            )
        )

        reset()
    }

    private func ensureMagneticFile() -> FileHandle? {
        if let magneticFileHandle {
            return magneticFileHandle
        }

        guard let sessionDirectoryURL else { return nil }
        let url = sessionDirectoryURL.appendingPathComponent("magnetic.csv")

        do {
            try Data(Self.magneticCSVHeader.utf8).write(to: url, options: .atomic)
            let handle = try FileHandle(forWritingTo: url)
            try handle.seekToEnd()
            magneticCSVURL = url
            magneticFileHandle = handle
            return handle
        } catch {
            magneticCSVURL = nil
            magneticFileHandle = nil
            return nil
        }
    }

    private func archiveVideoIfNeeded(_ sourceURL: URL?, into sessionDirectoryURL: URL) -> URL? {
        guard let sourceURL else { return nil }

        let fileManager = FileManager.default
        let destinationURL = sessionDirectoryURL.appendingPathComponent(sourceURL.lastPathComponent)

        guard let sourceSize = Self.usableRegularFileSize(sourceURL) else {
            lastVideoArchiveWarning = "Video file is missing or empty: \(sourceURL.lastPathComponent)"
            return nil
        }

        if sourceURL.standardizedFileURL == destinationURL.standardizedFileURL {
            return sourceURL
        }

        do {
            if fileManager.fileExists(atPath: destinationURL.path) {
                try fileManager.removeItem(at: destinationURL)
            }

            try fileManager.copyItem(at: sourceURL, to: destinationURL)
            guard Self.usableRegularFileSize(destinationURL) == sourceSize else {
                try? fileManager.removeItem(at: destinationURL)
                lastVideoArchiveWarning = "Copied video size check failed: \(sourceURL.lastPathComponent)"
                return nil
            }

            try? fileManager.removeItem(at: sourceURL)
            return destinationURL
        } catch {
            lastVideoArchiveWarning = "Could not archive video \(sourceURL.lastPathComponent): \(error.localizedDescription)"
            return nil
        }
    }

    private func reset(deleteSessionDirectory: Bool = false) {
        let directoryURL = sessionDirectoryURL

        try? fileHandle?.close()
        try? magneticFileHandle?.close()
        sessionDirectoryURL = nil
        poseCSVURL = nil
        magneticCSVURL = nil
        videoURL = nil
        fileHandle = nil
        magneticFileHandle = nil
        sessionStartFrameTime = nil
        firstMagneticMonotonicTime = nil
        videoStartFrameTime = nil
        creationTime = nil
        lastVideoArchiveWarning = nil
        sampleCount = 0
        magneticSampleCount = 0

        if deleteSessionDirectory, let directoryURL {
            try? FileManager.default.removeItem(at: directoryURL)
        }
    }

    private static let csvHeader = "sequence,sender_time,frame_time,relative_time,x,y,z,qx,qy,qz,qw\n"
    private static let magneticCSVHeader = "sequence,mcu_time_us,phone_receive_time,phone_monotonic_time,s0_t,s0_x,s0_y,s0_z,s1_t,s1_x,s1_y,s1_z,s2_t,s2_x,s2_y,s2_z,s3_t,s3_x,s3_y,s3_z,s4_t,s4_x,s4_y,s4_z\n"

    private static func format(_ value: Double) -> String {
        String(format: "%.9f", locale: Locale(identifier: "en_US_POSIX"), value)
    }

    private static func usableRegularFileSize(_ url: URL) -> Int? {
        guard
            let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey]),
            values.isRegularFile == true,
            let fileSize = values.fileSize,
            fileSize > 0
        else {
            return nil
        }

        return fileSize
    }

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
