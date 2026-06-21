import Foundation

enum CaptureUploadError: LocalizedError {
    case invalidBaseURL
    case uploadFileMissing(String)
    case uploadFileInvalid(String)
    case uploadFileEmpty(String)
    case httpStatus(Int)
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
        case .httpStatus(let code):
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

final class CaptureUploadService {
    private let session: URLSession = .shared

    func upload(
        descriptors: [UploadDescriptor],
        captureID: String,
        serverBaseURL: URL,
        kind: CaptureUploadKind,
        progress: (@Sendable (UploadProgressSnapshot) async -> Void)? = nil
    ) async throws -> [UploadResponse] {
        var responses: [UploadResponse] = []

        for (index, descriptor) in descriptors.enumerated() {
            let response = try await uploadSingle(
                descriptor: descriptor,
                captureID: captureID,
                serverBaseURL: serverBaseURL,
                kind: kind
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
        kind: CaptureUploadKind
    ) async throws -> UploadResponse {
        try validateUploadFile(descriptor)

        let uploadURL = serverBaseURL.appending(path: "upload")

        var request = URLRequest(url: uploadURL)
        request.httpMethod = "POST"
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.setValue(captureID, forHTTPHeaderField: "X-Capture-ID")
        request.setValue(descriptor.component, forHTTPHeaderField: "X-Capture-Component")
        request.setValue(kind == .video ? "video" : "pose", forHTTPHeaderField: "X-Upload-Kind")
        request.setValue(descriptor.fileURL.lastPathComponent, forHTTPHeaderField: "X-Original-Filename")

        let (data, response) = try await session.upload(for: request, fromFile: descriptor.fileURL)

        guard let httpResponse = response as? HTTPURLResponse else {
            throw CaptureUploadError.invalidResponse
        }

        guard (200...299).contains(httpResponse.statusCode) else {
            throw CaptureUploadError.httpStatus(httpResponse.statusCode)
        }

        let decoder = JSONDecoder()
        guard let uploadResponse = try? decoder.decode(UploadResponse.self, from: data) else {
            throw CaptureUploadError.invalidResponseBody
        }

        return uploadResponse
    }

    private func validateUploadFile(_ descriptor: UploadDescriptor) throws {
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
    }
}
