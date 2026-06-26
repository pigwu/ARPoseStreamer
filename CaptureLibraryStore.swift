import Foundation

enum CaptureUploadKind {
    case video
    case pose
}

enum CaptureVideoFileState {
    case notRecorded
    case missing(URL)
    case empty(URL)
    case available(URL, Int)

    var canUpload: Bool {
        if case .available = self {
            return true
        }
        return false
    }

    var uploadURL: URL? {
        if case .available(let url, _) = self {
            return url
        }
        return nil
    }

    var fileSize: Int? {
        if case .available(_, let size) = self {
            return size
        }
        return nil
    }

    var statusText: String {
        switch self {
        case .notRecorded:
            return "Video: no video recorded in this capture"
        case .missing(_):
            return "Video: file missing"
        case .empty(_):
            return "Video: file is empty"
        case .available(_, let fileSize):
            return "Video: not uploaded yet (\(ByteCountFormatter.fileString(fromByteCount: Int64(fileSize))))"
        }
    }
}

struct CaptureRecord: Identifiable, Codable, Hashable {
    let id: UUID
    let createdAt: Date
    var displayName: String
    let sessionDirectoryName: String
    let poseCSVFileName: String
    let manifestFileName: String
    let videoFileName: String?
    var videoUploadedAt: Date?
    var poseUploadedAt: Date?

    var defaultDisplayName: String {
        sessionDirectoryName
    }
}

final class CaptureLibraryStore {
    private static let libraryFileName = "capture_library.json"
    private let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }()
    private let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }()

    func loadRecords() -> [CaptureRecord] {
        let libraryURL = Self.libraryURL()
        guard let data = try? Data(contentsOf: libraryURL) else {
            return []
        }

        return (try? decoder.decode([CaptureRecord].self, from: data)) ?? []
    }

    func addCapture(from artifact: PoseCaptureArtifact) -> CaptureRecord? {
        var records = loadRecords()

        let record = CaptureRecord(
            id: UUID(),
            createdAt: Date(),
            displayName: artifact.sessionDirectoryURL.lastPathComponent,
            sessionDirectoryName: artifact.sessionDirectoryURL.lastPathComponent,
            poseCSVFileName: artifact.poseCSVURL.lastPathComponent,
            manifestFileName: artifact.manifestURL.lastPathComponent,
            videoFileName: artifact.videoURL?.lastPathComponent,
            videoUploadedAt: nil,
            poseUploadedAt: nil
        )

        records.insert(record, at: 0)
        save(records: records)
        return record
    }

    func renameRecord(id: UUID, to newName: String) -> [CaptureRecord] {
        var records = loadRecords()

        if let index = records.firstIndex(where: { $0.id == id }) {
            records[index].displayName = newName
            save(records: records)
        }

        return records
    }

    func markUploaded(id: UUID, kind: CaptureUploadKind, at date: Date = Date()) -> [CaptureRecord] {
        var records = loadRecords()

        if let index = records.firstIndex(where: { $0.id == id }) {
            switch kind {
            case .video:
                records[index].videoUploadedAt = date
            case .pose:
                records[index].poseUploadedAt = date
            }
            save(records: records)
        }

        return records
    }

    func urlForPoseCSV(record: CaptureRecord) -> URL {
        Self.captureDirectory(for: record).appendingPathComponent(record.poseCSVFileName)
    }

    func urlForManifest(record: CaptureRecord) -> URL {
        Self.captureDirectory(for: record).appendingPathComponent(record.manifestFileName)
    }

    func urlForVideo(record: CaptureRecord) -> URL? {
        videoFileState(for: record).uploadURL
    }

    func videoFileState(for record: CaptureRecord) -> CaptureVideoFileState {
        guard let videoFileName = record.videoFileName else { return .notRecorded }

        let captureVideoURL = Self.captureDirectory(for: record).appendingPathComponent(videoFileName)
        if let state = Self.stateForExistingVideo(captureVideoURL), state.canUpload {
            return state
        }

        let legacyVideoURL = Self.documentsRootURL().appendingPathComponent(videoFileName)
        if let state = Self.stateForExistingVideo(legacyVideoURL), state.canUpload {
            return state
        }

        if let captureState = Self.stateForExistingVideo(captureVideoURL) {
            return captureState
        }

        if let legacyState = Self.stateForExistingVideo(legacyVideoURL) {
            return legacyState
        }

        return .missing(captureVideoURL)
    }

    private func save(records: [CaptureRecord]) {
        let capturesURL = Self.capturesRootURL()
        try? FileManager.default.createDirectory(at: capturesURL, withIntermediateDirectories: true)

        let libraryURL = Self.libraryURL()
        if let data = try? encoder.encode(records) {
            try? data.write(to: libraryURL, options: .atomic)
        }
    }

    private static func libraryURL() -> URL {
        capturesRootURL().appendingPathComponent(libraryFileName)
    }

    private static func captureDirectory(for record: CaptureRecord) -> URL {
        capturesRootURL().appendingPathComponent(record.sessionDirectoryName, isDirectory: true)
    }

    private static func capturesRootURL() -> URL {
        documentsRootURL().appendingPathComponent("Captures", isDirectory: true)
    }

    private static func documentsRootURL() -> URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }

    private static func stateForExistingVideo(_ url: URL) -> CaptureVideoFileState? {
        guard FileManager.default.fileExists(atPath: url.path) else {
            return nil
        }

        guard
            let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey]),
            values.isRegularFile == true
        else {
            return .missing(url)
        }

        guard let fileSize = values.fileSize, fileSize > 0 else {
            return .empty(url)
        }

        return .available(url, fileSize)
    }
}

private extension ByteCountFormatter {
    static func fileString(fromByteCount byteCount: Int64) -> String {
        let formatter = ByteCountFormatter()
        formatter.allowedUnits = [.useKB, .useMB, .useGB]
        formatter.countStyle = .file
        return formatter.string(fromByteCount: byteCount)
    }
}
