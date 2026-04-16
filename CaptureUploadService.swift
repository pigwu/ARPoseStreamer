import Foundation

enum CaptureUploadError: LocalizedError {
    case invalidBaseURL
    case httpStatus(Int)
    case invalidResponse

    var errorDescription: String? {
        switch self {
        case .invalidBaseURL:
            return "Invalid upload server URL"
        case .httpStatus(let code):
            return "Upload failed with HTTP status \(code)"
        case .invalidResponse:
            return "Upload server returned an invalid response"
        }
    }
}

struct UploadDescriptor {
    let fileURL: URL
    let component: String
}

final class CaptureUploadService {
    private let session: URLSession = .shared

    func upload(
        descriptors: [UploadDescriptor],
        captureID: String,
        serverBaseURL: URL,
        kind: CaptureUploadKind
    ) async throws {
        for descriptor in descriptors {
            try await uploadSingle(
                descriptor: descriptor,
                captureID: captureID,
                serverBaseURL: serverBaseURL,
                kind: kind
            )
        }
    }

    private func uploadSingle(
        descriptor: UploadDescriptor,
        captureID: String,
        serverBaseURL: URL,
        kind: CaptureUploadKind
    ) async throws {
        let uploadURL = serverBaseURL.appending(path: "upload")

        var request = URLRequest(url: uploadURL)
        request.httpMethod = "POST"
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.setValue(captureID, forHTTPHeaderField: "X-Capture-ID")
        request.setValue(descriptor.component, forHTTPHeaderField: "X-Capture-Component")
        request.setValue(kind == .video ? "video" : "pose", forHTTPHeaderField: "X-Upload-Kind")
        request.setValue(descriptor.fileURL.lastPathComponent, forHTTPHeaderField: "X-Original-Filename")

        let (_, response) = try await session.upload(for: request, fromFile: descriptor.fileURL)

        guard let httpResponse = response as? HTTPURLResponse else {
            throw CaptureUploadError.invalidResponse
        }

        guard (200...299).contains(httpResponse.statusCode) else {
            throw CaptureUploadError.httpStatus(httpResponse.statusCode)
        }
    }
}
