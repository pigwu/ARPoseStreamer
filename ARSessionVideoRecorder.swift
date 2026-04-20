import Foundation
import AVFoundation
import CoreVideo

enum VideoRecordingStatus {
    case idle
    case recording
    case saving
    case saved(URL)
    case failed(String)

    var isRecording: Bool {
        if case .recording = self {
            return true
        }

        return false
    }

    var isTerminal: Bool {
        switch self {
        case .idle, .saved, .failed:
            return true
        case .recording, .saving:
            return false
        }
    }

    var message: String {
        switch self {
        case .idle:
            return "Video idle"
        case .recording:
            return "Recording video..."
        case .saving:
            return "Saving video..."
        case .saved(let url):
            return "Saved video: \(url.lastPathComponent)"
        case .failed(let message):
            return "Video error: \(message)"
        }
    }
}

final class ARSessionVideoRecorder {
    var onStatusChange: ((VideoRecordingStatus) -> Void)?

    private var assetWriter: AVAssetWriter?
    private var writerInput: AVAssetWriterInput?
    private var pixelBufferAdaptor: AVAssetWriterInputPixelBufferAdaptor?
    private var outputURL: URL?
    private var recordingStatus: VideoRecordingStatus = .idle

    func startRecording() {
        guard !recordingStatus.isRecording else { return }

        resetWriterState()
        updateStatus(.recording)
    }

    func appendFrame(pixelBuffer: CVPixelBuffer, at sourceTime: CMTime) {
        guard recordingStatus.isRecording else { return }

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
            _ = pixelBufferAdaptor.append(pixelBuffer, withPresentationTime: sourceTime)
        } catch {
            updateStatus(.failed(error.localizedDescription))
            resetWriterState()
        }
    }

    func stopRecording(completion: @escaping (VideoRecordingStatus) -> Void) {
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
            if assetWriter.status == .completed, let outputURL = self.outputURL {
                finalStatus = .saved(outputURL)
            } else {
                finalStatus = .failed(assetWriter.error?.localizedDescription ?? "Failed to finish recording")
            }

            self.resetWriterState()
            self.updateStatus(finalStatus)
            completion(finalStatus)
        }
    }

    private func configureWriter(using pixelBuffer: CVPixelBuffer) throws {
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let pixelFormat = CVPixelBufferGetPixelFormatType(pixelBuffer)

        let outputURL = Self.makeOutputURL()
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
                AVVideoExpectedSourceFrameRateKey: 60,
                AVVideoMaxKeyFrameIntervalKey: 60
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

    private static func makeOutputURL() -> URL {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"

        let fileName = "ARPoseStreamer-\(formatter.string(from: Date())).mp4"
        let documentsURL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]

        return documentsURL.appendingPathComponent(fileName)
    }
}
