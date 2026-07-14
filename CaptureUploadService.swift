import Foundation

enum CaptureUploadError: LocalizedError {
    case invalidBaseURL
    case uploadFileMissing(String)
    case uploadFileInvalid(String)
    case uploadFileEmpty(String)
    case httpStatus(Int, String?)
    case invalidResponse
    case invalidResponseBody

    var errorDescription: String? {
        switch self {
        case .invalidBaseURL:
            return "Invalid upload server URL"
        case .uploadFileMissing(let fileName):
            return "Upload file is missing: \(fileName)"
        case .uploadFileInvalid(let fileName):
            return "Upload path is not a regular file: \(fileName)"
        case .uploadFileEmpty(let fileName):
            return "Upload file is empty: \(fileName)"
        case .httpStatus(let code, let message):
            if let message, !message.isEmpty {
                return "Upload failed with HTTP status \(code): \(message)"
            }
            return "Upload failed with HTTP status \(code)"
        case .invalidResponse:
            return "Upload server returned an invalid response"
        case .invalidResponseBody:
            return "Upload server returned an unreadable response body"
        }
    }
}

struct UploadDescriptor {
    let fileURL: URL
    let component: String
}

struct UploadResponse: Decodable {
    let ok: Bool
    let capture_id: String?
    let component: String?
    let upload_kind: String?
    let saved_to: String?
}

struct UploadProgressSnapshot {
    let completedFiles: Int
    let totalFiles: Int
    let currentFileName: String
    let currentComponent: String
    let savedTo: String?
}

struct ExperimentControlPayload: Encodable {
    let event: String
    let experimentID: String
    let eventUnixTime: TimeInterval
    let eventMonotonicTime: TimeInterval
}

final class CaptureUploadService {
    private let session: URLSession

    init() {
        let configuration = URLSessionConfiguration.default
        configuration.waitsForConnectivity = true
        configuration.timeoutIntervalForRequest = 600
        configuration.timeoutIntervalForResource = 3600
        self.session = URLSession(configuration: configuration)
    }

    func upload(
        descriptors: [UploadDescriptor],
        captureID: String,
        serverBaseURL: URL,
        kind: CaptureUploadKind,
        experimentStartUnixTime: TimeInterval? = nil,
        progress: (@Sendable (UploadProgressSnapshot) async -> Void)? = nil
    ) async throws -> [UploadResponse] {
        var responses: [UploadResponse] = []

        for (index, descriptor) in descriptors.enumerated() {
            let response = try await uploadSingle(
                descriptor: descriptor,
                captureID: captureID,
                serverBaseURL: serverBaseURL,
                kind: kind,
                fileIndex: index + 1,
                totalFiles: descriptors.count,
                experimentStartUnixTime: experimentStartUnixTime
            )

            responses.append(response)

            if let progress {
                await progress(
                    UploadProgressSnapshot(
                        completedFiles: index + 1,
                        totalFiles: descriptors.count,
                        currentFileName: descriptor.fileURL.lastPathComponent,
                        currentComponent: descriptor.component,
                        savedTo: response.saved_to
                    )
                )
            }
        }

        return responses
    }

    private func uploadSingle(
        descriptor: UploadDescriptor,
        captureID: String,
        serverBaseURL: URL,
        kind: CaptureUploadKind,
        fileIndex: Int,
        totalFiles: Int,
        experimentStartUnixTime: TimeInterval?
    ) async throws -> UploadResponse {
        let fileSize = try validateUploadFile(descriptor)

        let uploadURL = serverBaseURL.appending(path: "upload")

        var request = URLRequest(url: uploadURL)
        request.httpMethod = "POST"
        request.timeoutInterval = (kind == .video || kind == .experiment) ? 600 : 120
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.setValue(captureID, forHTTPHeaderField: "X-Capture-ID")
        request.setValue(descriptor.component, forHTTPHeaderField: "X-Capture-Component")
        request.setValue(Self.uploadKindName(kind), forHTTPHeaderField: "X-Upload-Kind")
        request.setValue(descriptor.fileURL.lastPathComponent, forHTTPHeaderField: "X-Original-Filename")
        request.setValue(String(fileSize), forHTTPHeaderField: "X-Upload-File-Size")
        request.setValue(String(fileIndex), forHTTPHeaderField: "X-Experiment-File-Index")
        request.setValue(String(totalFiles), forHTTPHeaderField: "X-Experiment-File-Count")
        if let experimentStartUnixTime {
            request.setValue(
                String(format: "%.6f", experimentStartUnixTime),
                forHTTPHeaderField: "X-Experiment-Start-Unix-Time"
            )
        }

        let (data, response) = try await session.upload(for: request, fromFile: descriptor.fileURL)

        guard let httpResponse = response as? HTTPURLResponse else {
            throw CaptureUploadError.invalidResponse
        }

        guard (200...299).contains(httpResponse.statusCode) else {
            let serverMessage = String(data: data, encoding: .utf8)
            throw CaptureUploadError.httpStatus(httpResponse.statusCode, serverMessage)
        }

        let decoder = JSONDecoder()
        guard let uploadResponse = try? decoder.decode(UploadResponse.self, from: data) else {
            throw CaptureUploadError.invalidResponseBody
        }

        return uploadResponse
    }

    func sendExperimentEvent(
        experimentID: UUID,
        event: String,
        eventUnixTime: TimeInterval,
        eventMonotonicTime: TimeInterval,
        serverBaseURL: URL
    ) async throws {
        let url = serverBaseURL.appending(path: "experiment/control")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 10
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(
            ExperimentControlPayload(
                event: event,
                experimentID: experimentID.uuidString,
                eventUnixTime: eventUnixTime,
                eventMonotonicTime: eventMonotonicTime
            )
        )

        let (_, response) = try await session.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw CaptureUploadError.invalidResponse
        }
        guard (200...299).contains(httpResponse.statusCode) else {
            throw CaptureUploadError.httpStatus(httpResponse.statusCode, nil)
        }
    }

    private static func uploadKindName(_ kind: CaptureUploadKind) -> String {
        switch kind {
        case .video:
            return "video"
        case .pose:
            return "pose"
        case .experiment:
            return "experiment"
        }
    }

    private func validateUploadFile(_ descriptor: UploadDescriptor) throws -> Int {
        let fileURL = descriptor.fileURL
        let fileName = fileURL.lastPathComponent

        guard FileManager.default.fileExists(atPath: fileURL.path) else {
            throw CaptureUploadError.uploadFileMissing(fileName)
        }

        let values = try fileURL.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
        guard values.isRegularFile == true else {
            throw CaptureUploadError.uploadFileInvalid(fileName)
        }

        guard let fileSize = values.fileSize, fileSize > 0 else {
            throw CaptureUploadError.uploadFileEmpty(fileName)
        }

        return fileSize
    }
}
