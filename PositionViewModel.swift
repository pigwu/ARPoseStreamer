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
            return "For offline export on macOS, connect the iPhone and use Finder file sharing."
        case .windows:
            return "For offline export on Windows, connect the iPhone and use Apple Devices or iTunes file sharing."
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

struct CaptureShareRequest: Identifiable {
    let id = UUID()
    let recordID: UUID
    let kind: CaptureUploadKind
    let itemURLs: [URL]
    let title: String
}

struct ReuploadPrompt: Identifiable {
    let id = UUID()
    let recordID: UUID
    let kind: CaptureUploadKind
    let title: String
    let previousUploadDate: Date
}

@MainActor
final class PositionViewModel: ObservableObject {
    @Published var hostIP: String {
        didSet { UserDefaults.standard.set(hostIP, forKey: Self.hostIPKey) }
    }
    @Published var hostPort: String {
        didSet { UserDefaults.standard.set(hostPort, forKey: Self.hostPortKey) }
    }
    @Published var receiverPlatform: ReceiverPlatform {
        didSet { UserDefaults.standard.set(receiverPlatform.rawValue, forKey: Self.receiverPlatformKey) }
    }
    @Published var showPositionChart: Bool {
        didSet { UserDefaults.standard.set(showPositionChart, forKey: Self.showPositionChartKey) }
    }

    @Published private(set) var position: SIMD3<Float> = .zero
    @Published private(set) var positionHistory: [PositionHistorySample] = []
    @Published private(set) var sendStatus = "Idle"
    @Published private(set) var latestPacketSummary = "No packets yet"
    @Published private(set) var recordingStatus = VideoRecordingStatus.idle.message
    @Published private(set) var isSending = false
    @Published private(set) var isRecordingVideo = false
    @Published private(set) var lastSavedVideoURL: URL?
    @Published private(set) var lastSavedVideoName = "No saved video yet"
    @Published private(set) var lastCaptureSessionName = "No capture exported yet"
    @Published private(set) var captureRecords: [CaptureRecord] = []
    @Published var activeShareRequest: CaptureShareRequest?
    @Published var pendingReuploadPrompt: ReuploadPrompt?

    private let maxHistorySamples = 120
    private let captureLibraryStore = CaptureLibraryStore()
    private var sender: ARPoseUDPSender?

    var targetSummary: String {
        "\(receiverPlatform.displayName) receiver at \(hostIP):\(hostPort)"
    }

    var videoAccessHint: String {
        receiverPlatform.videoAccessHint
    }

    init() {
        let defaults = UserDefaults.standard
        hostIP = defaults.string(forKey: Self.hostIPKey) ?? "192.168.1.10"
        hostPort = defaults.string(forKey: Self.hostPortKey) ?? "5555"
        receiverPlatform = ReceiverPlatform(rawValue: defaults.string(forKey: Self.receiverPlatformKey) ?? ReceiverPlatform.macOS.rawValue) ?? .macOS
        showPositionChart = defaults.object(forKey: Self.showPositionChartKey) as? Bool ?? true
        captureRecords = captureLibraryStore.loadRecords().sorted { $0.createdAt > $1.createdAt }

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

    func renameCapture(_ record: CaptureRecord, to newName: String) {
        let trimmedName = newName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedName.isEmpty else { return }

        captureRecords = captureLibraryStore
            .renameRecord(id: record.id, to: trimmedName)
            .sorted { $0.createdAt > $1.createdAt }
    }

    func requestVideoUpload(for record: CaptureRecord) {
        guard captureLibraryStore.urlForVideo(record: record) != nil else { return }

        if let previousUploadDate = record.videoUploadedAt {
            pendingReuploadPrompt = ReuploadPrompt(
                recordID: record.id,
                kind: .video,
                title: "Video already uploaded",
                previousUploadDate: previousUploadDate
            )
        } else {
            startUpload(for: record, kind: .video)
        }
    }

    func requestPoseUpload(for record: CaptureRecord) {
        if let previousUploadDate = record.poseUploadedAt {
            pendingReuploadPrompt = ReuploadPrompt(
                recordID: record.id,
                kind: .pose,
                title: "Pose data already uploaded",
                previousUploadDate: previousUploadDate
            )
        } else {
            startUpload(for: record, kind: .pose)
        }
    }

    func confirmReupload(_ prompt: ReuploadPrompt) {
        guard let record = captureRecords.first(where: { $0.id == prompt.recordID }) else { return }
        pendingReuploadPrompt = nil
        startUpload(for: record, kind: prompt.kind)
    }

    func cancelReuploadPrompt() {
        pendingReuploadPrompt = nil
    }

    func markShareCompleted(for request: CaptureShareRequest, completed: Bool) {
        guard completed else { return }

        captureRecords = captureLibraryStore
            .markUploaded(id: request.recordID, kind: request.kind)
            .sorted { $0.createdAt > $1.createdAt }
    }

    private func startUpload(for record: CaptureRecord, kind: CaptureUploadKind) {
        let request: CaptureShareRequest?

        switch kind {
        case .video:
            guard let videoURL = captureLibraryStore.urlForVideo(record: record) else { return }
            request = CaptureShareRequest(
                recordID: record.id,
                kind: .video,
                itemURLs: [videoURL],
                title: "Upload Video"
            )
        case .pose:
            let poseURL = captureLibraryStore.urlForPoseCSV(record: record)
            let manifestURL = captureLibraryStore.urlForManifest(record: record)
            request = CaptureShareRequest(
                recordID: record.id,
                kind: .pose,
                itemURLs: [poseURL, manifestURL],
                title: "Upload Pose Data"
            )
        }

        activeShareRequest = request
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

        newSender?.onCaptureSessionSaved = { [weak self] artifact in
            Task { @MainActor [weak self] in
                guard let self else { return }

                if let record = self.captureLibraryStore.addCapture(from: artifact) {
                    self.captureRecords.insert(record, at: 0)
                } else {
                    self.captureRecords = self.captureLibraryStore.loadRecords().sorted { $0.createdAt > $1.createdAt }
                }

                self.lastCaptureSessionName = artifact.sessionDirectoryURL.lastPathComponent
                if let videoURL = artifact.videoURL {
                    self.lastSavedVideoURL = videoURL
                    self.lastSavedVideoName = videoURL.lastPathComponent
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

    private static let hostIPKey = "ARPoseStreamer.hostIP"
    private static let hostPortKey = "ARPoseStreamer.hostPort"
    private static let receiverPlatformKey = "ARPoseStreamer.receiverPlatform"
    private static let showPositionChartKey = "ARPoseStreamer.showPositionChart"
}
