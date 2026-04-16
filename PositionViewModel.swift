import Foundation
import Combine
import simd

enum ReceiverPlatform: String, CaseIterable, Identifiable {
    case macOS
    case windows

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .macOS:
            return "macOS"
        case .windows:
            return "Windows"
        }
    }

    var receiverCommand: String {
        switch self {
        case .macOS:
            return "python3 udp_pose_receiver.py --host 0.0.0.0 --port 5555 --encoding binary"
        case .windows:
            return "py udp_pose_receiver.py --host 0.0.0.0 --port 5555 --encoding binary"
        }
    }

    var ipHint: String {
        switch self {
        case .macOS:
            return "Find host IP with: ipconfig getifaddr en0"
        case .windows:
            return "Find host IP with: ipconfig"
        }
    }

    var videoAccessHint: String {
        switch self {
        case .macOS:
            return "To copy saved videos on macOS, connect the iPhone and use Finder file sharing."
        case .windows:
            return "To copy saved videos on Windows, connect the iPhone and use Apple Devices or iTunes file sharing."
        }
    }
}

struct PositionHistorySample: Identifiable {
    let id = UUID()
    let sequence: Int
    let x: Double
    let y: Double
    let z: Double
}

@MainActor
final class PositionViewModel: ObservableObject {
    @Published var hostIP = "192.168.1.10"
    @Published var hostPort = "5555"
    @Published var receiverPlatform: ReceiverPlatform = .macOS

    @Published private(set) var position: SIMD3<Float> = .zero
    @Published private(set) var positionHistory: [PositionHistorySample] = []
    @Published private(set) var sendStatus = "Idle"
    @Published private(set) var latestPacketSummary = "No packets yet"
    @Published private(set) var recordingStatus = VideoRecordingStatus.idle.message
    @Published private(set) var isSending = false
    @Published private(set) var isRecordingVideo = false
    @Published private(set) var lastSavedVideoURL: URL?
    @Published private(set) var lastSavedVideoName = "No saved video yet"

    private let maxHistorySamples = 120
    private var sender: ARPoseUDPSender?

    var targetSummary: String {
        "\(receiverPlatform.displayName) receiver at \(hostIP):\(hostPort)"
    }

    var videoAccessHint: String {
        receiverPlatform.videoAccessHint
    }

    init() {
        configureSender()
    }

    func startSending() {
        guard let port = normalizedPort() else { return }

        if sender == nil {
            configureSender()
        } else {
            sender?.updateDestination(hostIP: hostIP, port: port)
        }

        sender?.start()
        isSending = true
        sendStatus = "Streaming pose to \(hostIP):\(port)"
    }

    func stopSending() {
        sender?.stop()
        isSending = false
        sendStatus = "Stopped"
    }

    func startRecording() {
        if sender == nil {
            configureSender()
        }

        sender?.startRecording()
        isRecordingVideo = true
    }

    func stopRecording() {
        sender?.stopRecording()
        isRecordingVideo = false
    }

    func resetOrigin() {
        sender?.resetOrigin()
    }

    func shutdown() {
        if isRecordingVideo {
            stopRecording()
        }

        if isSending {
            stopSending()
        }
    }

    func formattedValue(for value: Float) -> String {
        String(format: "%.3f m", value)
    }

    private func configureSender() {
        let senderPort = UInt16(hostPort) ?? 5555
        let newSender = ARPoseUDPSender(hostIP: hostIP, port: senderPort)
        sender = newSender

        newSender?.onSampleUpdated = { [weak self] sample in
            Task { @MainActor [weak self] in
                guard let self else { return }

                self.position = sample.position
                self.latestPacketSummary = String(
                    format: "#%u  t=%.3f  x=%.3f  y=%.3f  z=%.3f",
                    sample.sequence,
                    sample.timestamp,
                    sample.position.x,
                    sample.position.y,
                    sample.position.z
                )
                self.appendHistory(sample)
            }
        }

        newSender?.onSampleSent = { [weak self] sample in
            Task { @MainActor [weak self] in
                self?.latestPacketSummary = String(
                    format: "#%u  sent  x=%.3f  y=%.3f  z=%.3f",
                    sample.sequence,
                    sample.position.x,
                    sample.position.y,
                    sample.position.z
                )
            }
        }

        newSender?.onError = { [weak self] error in
            Task { @MainActor [weak self] in
                self?.sendStatus = "Send error: \(error.localizedDescription)"
                self?.isSending = false
            }
        }

        newSender?.onRecordingStatusChange = { [weak self] status in
            Task { @MainActor [weak self] in
                self?.recordingStatus = status.message
                self?.isRecordingVideo = status.isRecording

                if case .saved(let url) = status {
                    self?.lastSavedVideoURL = url
                    self?.lastSavedVideoName = url.lastPathComponent
                }
            }
        }
    }

    private func normalizedPort() -> UInt16? {
        guard let port = UInt16(hostPort) else {
            sendStatus = "Invalid port. Use a number between 0 and 65535."
            return nil
        }

        return port
    }

    private func appendHistory(_ sample: ARPoseUDPSender.PoseSample) {
        positionHistory.append(
            PositionHistorySample(
                sequence: Int(sample.sequence),
                x: Double(sample.position.x),
                y: Double(sample.position.y),
                z: Double(sample.position.z)
            )
        )

        if positionHistory.count > maxHistorySamples {
            positionHistory.removeFirst(positionHistory.count - maxHistorySamples)
        }
    }
}
