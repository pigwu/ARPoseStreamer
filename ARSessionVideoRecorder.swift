import Foundation
import AVFoundation
import CoreVideo

enum VideoRecordingStatus {
    case idle
    case preparing
    case recording
    case saving
    case saved(URL)
    case failed(String)

    var isIdle: Bool {
        if case .idle = self {
            return true
        }

        return false
    }

    var isPreparing: Bool {
        if case .preparing = self {
            return true
        }

        return false
    }

    var isRecording: Bool {
        if case .recording = self {
            return true
        }

        return false
    }

    var isSaving: Bool {
        if case .saving = self {
            return true
        }

        return false
    }

    var isStoppable: Bool {
        switch self {
        case .preparing, .recording:
            return true
        case .idle, .saving, .saved, .failed:
            return false
        }
    }

    var isActive: Bool {
        switch self {
        case .preparing, .recording, .saving:
            return true
        case .idle, .saved, .failed:
            return false
        }
    }

    var isTerminal: Bool {
        switch self {
        case .idle, .saved, .failed:
            return true
        case .preparing, .recording, .saving:
            return false
        }
    }

    var message: String {
        switch self {
        case .idle:
            return "Idle"
        case .preparing:
            return "Preparing..."
        case .recording:
            return "Recording..."
        case .saving:
            return "Saving..."
        case .saved(let url):
            return "Saved: \(url.lastPathComponent)"
        case .failed(let message):
            return "Record failed: \(message)"
        }
    }
}

final class ARSessionVideoRecorder {
    var onStatusChange: ((VideoRecordingStatus) -> Void)?

    private let fileNamePrefix: String
    private let expectedFrameRate: Int
    private var assetWriter: AVAssetWriter?
    private var writerInput: AVAssetWriterInput?
    private var pixelBufferAdaptor: AVAssetWriterInputPixelBufferAdaptor?
    private var outputURL: URL?
    private var recordingStatus: VideoRecordingStatus = .idle

    init(fileNamePrefix: String = "ARPoseStreamer", expectedFrameRate: Int = 60) {
        self.fileNamePrefix = fileNamePrefix
        self.expectedFrameRate = max(1, expectedFrameRate)
    }

    func startRecording() {
        guard recordingStatus.isTerminal else { return }

        resetWriterState()
        updateStatus(.preparing)
    }

    func appendFrame(pixelBuffer: CVPixelBuffer, at sourceTime: CMTime) {
        guard recordingStatus.isPreparing || recordingStatus.isRecording else { return }
        let isStartingFirstFrame = recordingStatus.isPreparing

        do {
            if assetWriter == nil {
                try configureWriter(using: pixelBuffer)
            }

            guard
                let assetWriter,
                let writerInput,
                let pixelBufferAdaptor
            else {
                updateStatus(.failed("Recorder was not configured correctly"))
                return
            }

            if assetWriter.status == .unknown {
                guard assetWriter.startWriting() else {
                    updateStatus(.failed(assetWriter.error?.localizedDescription ?? "Failed to start writing"))
                    resetWriterState()
                    return
                }

                assetWriter.startSession(atSourceTime: sourceTime)
            }

            guard assetWriter.status == .writing else {
                if assetWriter.status == .failed {
                    updateStatus(.failed(assetWriter.error?.localizedDescription ?? "Writer failed"))
                    resetWriterState()
                }
                return
            }

            guard writerInput.isReadyForMoreMediaData else { return }
            let didAppendFrame = pixelBufferAdaptor.append(pixelBuffer, withPresentationTime: sourceTime)

            if didAppendFrame {
                if isStartingFirstFrame {
                    updateStatus(.recording)
                }
            } else if isStartingFirstFrame {
                updateStatus(.failed("Could not write first frame"))
                resetWriterState()
            }
        } catch {
            updateStatus(.failed(error.localizedDescription))
            resetWriterState()
        }
    }

    func stopRecording(completion: @escaping (VideoRecordingStatus) -> Void) {
        if recordingStatus.isPreparing {
            let status = VideoRecordingStatus.failed("No valid frame was recorded")
            assetWriter?.cancelWriting()
            resetWriterState()
            updateStatus(status)
            completion(status)
            return
        }

        guard recordingStatus.isRecording else {
            completion(recordingStatus)
            return
        }

        guard let assetWriter, let writerInput else {
            let status = VideoRecordingStatus.failed("No active recording to stop")
            updateStatus(status)
            completion(status)
            resetWriterState()
            return
        }

        updateStatus(.saving)
        writerInput.markAsFinished()

        assetWriter.finishWriting { [weak self] in
            guard let self else { return }

            let finalStatus: VideoRecordingStatus
            if
                assetWriter.status == .completed,
                let outputURL = self.outputURL,
                Self.usableRegularFileSize(outputURL) != nil
            {
                finalStatus = .saved(outputURL)
            } else if assetWriter.status == .completed {
                finalStatus = .failed("Recorded video file is empty or missing")
            } else {
                finalStatus = .failed(assetWriter.error?.localizedDescription ?? "Failed to finish recording")
            }

            self.resetWriterState()
            self.updateStatus(finalStatus)
            completion(finalStatus)
        }
    }

    func cancelRecording(reason: String) {
        guard recordingStatus.isActive else { return }

        assetWriter?.cancelWriting()
        resetWriterState()
        updateStatus(.failed(reason))
    }

    func failPreparing(_ message: String) {
        guard recordingStatus.isPreparing else { return }

        assetWriter?.cancelWriting()
        resetWriterState()
        updateStatus(.failed(message))
    }

    private func configureWriter(using pixelBuffer: CVPixelBuffer) throws {
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let pixelFormat = CVPixelBufferGetPixelFormatType(pixelBuffer)

        let outputURL = Self.makeOutputURL(prefix: fileNamePrefix)
        if FileManager.default.fileExists(atPath: outputURL.path) {
            try FileManager.default.removeItem(at: outputURL)
        }

        let assetWriter = try AVAssetWriter(url: outputURL, fileType: .mp4)
        let videoSettings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: width * height * 6,
                AVVideoExpectedSourceFrameRateKey: expectedFrameRate,
                AVVideoMaxKeyFrameIntervalKey: expectedFrameRate
            ]
        ]

        let writerInput = AVAssetWriterInput(mediaType: .video, outputSettings: videoSettings)
        writerInput.expectsMediaDataInRealTime = true

        let adaptorAttributes: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: pixelFormat,
            kCVPixelBufferWidthKey as String: width,
            kCVPixelBufferHeightKey as String: height
        ]

        let pixelBufferAdaptor = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: writerInput,
            sourcePixelBufferAttributes: adaptorAttributes
        )

        guard assetWriter.canAdd(writerInput) else {
            throw NSError(domain: "ARSessionVideoRecorder", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "Unable to add video writer input"
            ])
        }

        assetWriter.add(writerInput)

        self.assetWriter = assetWriter
        self.writerInput = writerInput
        self.pixelBufferAdaptor = pixelBufferAdaptor
        self.outputURL = outputURL
    }

    private func resetWriterState() {
        assetWriter = nil
        writerInput = nil
        pixelBufferAdaptor = nil
        outputURL = nil
    }

    private func updateStatus(_ status: VideoRecordingStatus) {
        recordingStatus = status
        onStatusChange?(status)
    }

    private static func makeOutputURL(prefix: String) -> URL {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"

        let fileName = "\(prefix)-\(formatter.string(from: Date())).mp4"
        let documentsURL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]

        return documentsURL.appendingPathComponent(fileName)
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
}
