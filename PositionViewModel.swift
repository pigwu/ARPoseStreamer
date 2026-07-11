import Foundation
import Combine
import simd
import ARKit

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

    var uploadServerCommand: String {
        switch self {
        case .macOS:
            return "python3 capture_upload_server.py --host 0.0.0.0 --port 8000"
        case .windows:
            return "py capture_upload_server.py --host 0.0.0.0 --port 8000"
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

struct VideoStreamStatsViewState: Equatable {
    var state = "Video off"
    var encodedFPS = 0.0
    var sentFPS = 0.0
    var bitrateMbps = 0.0
    var encodedFrames: UInt64 = 0
    var sentFrames: UInt64 = 0
    var droppedFrames: UInt64 = 0
    var keyFrames: UInt64 = 0
    var sentBytes: UInt64 = 0

    static let idle = VideoStreamStatsViewState()

    init() {}

    init(from stats: LowLatencyVideoStats) {
        state = stats.state
        encodedFPS = stats.encodedFPS
        sentFPS = stats.sentFPS
        bitrateMbps = stats.bitrateMbps
        encodedFrames = stats.encodedFrames
        sentFrames = stats.sentFrames
        droppedFrames = stats.droppedFrames
        keyFrames = stats.keyFrames
        sentBytes = stats.sentBytes
    }
}

struct PositionHistorySample: Identifiable {
    let id = UUID()
    let timestamp: TimeInterval
    let sequence: Int
    let x: Double
    let y: Double
    let z: Double
}

struct ReuploadPrompt: Identifiable {
    let id = UUID()
    let recordID: UUID
    let kind: CaptureUploadKind
    let title: String
    let previousUploadDate: Date
}

struct WiredSensorStatsViewState {
    var bytesRead = 0
    var linesRead = 0
    var parsedSamples = 0
    var parseFailures = 0
    var lastRawLine = ""
    var lastParseFailure = ""
    var connectedAccessoryName = ""
}

struct UploadStatusViewState {
    var currentFileName = ""
    var currentComponent = ""
    var completedFiles = 0
    var totalFiles = 0
    var savedPaths: [String] = []

    var isActive: Bool {
        totalFiles > 0
    }

    var progressText: String {
        guard isActive else { return "" }
        let componentLabel = currentComponent.replacingOccurrences(of: "_", with: " ")
        return "\(completedFiles)/\(totalFiles) files - \(componentLabel) - \(currentFileName)"
    }

    var latestSavedPath: String? {
        savedPaths.last
    }
}

@MainActor
final class PositionViewModel: ObservableObject {
    @Published var hostIP: String {
        didSet {
            UserDefaults.standard.set(hostIP, forKey: Self.hostIPKey)
            applySenderConfigurationIfNeeded()
        }
    }
    @Published var hostPort: String {
        didSet {
            UserDefaults.standard.set(hostPort, forKey: Self.hostPortKey)
            applySenderConfigurationIfNeeded()
        }
    }
    @Published var uploadPort: String {
        didSet { UserDefaults.standard.set(uploadPort, forKey: Self.uploadPortKey) }
    }
    @Published var sensorPort: String {
        didSet { UserDefaults.standard.set(sensorPort, forKey: Self.sensorPortKey) }
    }
    @Published var sensorAccessoryProtocol: String {
        didSet { UserDefaults.standard.set(sensorAccessoryProtocol, forKey: Self.sensorAccessoryProtocolKey) }
    }
    @Published var receiverPlatform: ReceiverPlatform {
        didSet { UserDefaults.standard.set(receiverPlatform.rawValue, forKey: Self.receiverPlatformKey) }
    }
    @Published var isVideoStreamingEnabled: Bool {
        didSet {
            UserDefaults.standard.set(isVideoStreamingEnabled, forKey: Self.isVideoStreamingEnabledKey)
            applySenderConfigurationIfNeeded()
        }
    }
    @Published var videoPort: String {
        didSet {
            UserDefaults.standard.set(videoPort, forKey: Self.videoPortKey)
            applySenderConfigurationIfNeeded()
        }
    }
    @Published var videoFrameRate: String {
        didSet {
            UserDefaults.standard.set(videoFrameRate, forKey: Self.videoFrameRateKey)
            applySenderConfigurationIfNeeded()
        }
    }
    @Published var videoBitrateMbps: String {
        didSet {
            UserDefaults.standard.set(videoBitrateMbps, forKey: Self.videoBitrateKey)
            applySenderConfigurationIfNeeded()
        }
    }
    @Published var videoResolution: VideoStreamResolution {
        didSet {
            UserDefaults.standard.set(videoResolution.rawValue, forKey: Self.videoResolutionKey)
            applySenderConfigurationIfNeeded()
        }
    }
    @Published var showPositionChart: Bool {
        didSet { UserDefaults.standard.set(showPositionChart, forKey: Self.showPositionChartKey) }
    }

    @Published private(set) var position: SIMD3<Float> = .zero
    @Published private(set) var sensorPosition: SIMD3<Float> = .zero
    @Published private(set) var positionHistory: [PositionHistorySample] = []
    @Published private(set) var sendStatus = "Idle"
    @Published private(set) var sensorStatus = "Sensor idle"
    @Published private(set) var uploadStatus = "Upload idle"
    @Published private(set) var trackingStatus = "AR tracking idle"
    @Published private(set) var uploadDetails = UploadStatusViewState()
    @Published private(set) var latestPacketSummary = "No packets yet"
    @Published private(set) var latestSensorSummary = "No sensor packets yet"
    @Published private(set) var wiredSensorStats = WiredSensorStatsViewState()
    @Published private(set) var videoStatus = "Video off"
    @Published private(set) var videoStats = VideoStreamStatsViewState.idle
    @Published private(set) var connectedAccessories: [WiredSensorAccessoryInfo] = []
    @Published private(set) var recordingStatus = VideoRecordingStatus.idle.message
    @Published private(set) var recordingPhase = VideoRecordingStatus.idle
    @Published private(set) var isSending = false
    @Published private(set) var isSensorStreaming = false
    @Published private(set) var isRecordingVideo = false
    @Published private(set) var lastSavedVideoURL: URL?
    @Published private(set) var lastSavedVideoName = "No saved video yet"
    @Published private(set) var lastCaptureSessionName = "No capture exported yet"
    @Published private(set) var lastSensorLogName = "No sensor log yet"
    @Published private(set) var captureRecords: [CaptureRecord] = []
    @Published private(set) var uploadingRecordIDs: Set<UUID> = []
    @Published var pendingReuploadPrompt: ReuploadPrompt?

    private let maxHistorySamples = 120
    private let captureLibraryStore = CaptureLibraryStore()
    private let captureUploadService = CaptureUploadService()
    private let sensorRecorder = SensorPoseStreamRecorder()
    private var sender: ARPoseUDPSender?
    private var sensorBridge: WiredSensorPoseBridge?

    var previewSession: ARSession? {
        sender?.session
    }

    var targetSummary: String {
        "\(receiverPlatform.displayName) receiver at \(hostIP):\(hostPort)"
    }

    var videoTargetSummary: String {
        "Low-latency video at \(hostIP):\(videoPort)"
    }

    var videoAccessHint: String {
        receiverPlatform.videoAccessHint
    }

    var videoReceiverCommand: String {
        switch receiverPlatform {
        case .macOS:
            return "python3 udp_video_debug_ui.py --bind 0.0.0.0 --video-port \(normalizedPort(videoPort) ?? 5560) --pose-port \(normalizedPort(hostPort) ?? 5555)"
        case .windows:
            return "py udp_video_debug_ui.py --bind 0.0.0.0 --video-port \(normalizedPort(videoPort) ?? 5560) --pose-port \(normalizedPort(hostPort) ?? 5555)"
        }
    }

    var hasVideoStreamingEnabled: Bool {
        isVideoStreamingEnabled
    }

    var videoEncodedFPSText: String {
        String(format: "%.1f", videoStats.encodedFPS)
    }

    var videoSentFPSText: String {
        String(format: "%.1f", videoStats.sentFPS)
    }

    var videoBitrateText: String {
        String(format: "%.1f", videoStats.bitrateMbps)
    }

    var videoDroppedFramesText: String {
        "\(videoStats.droppedFrames)"
    }

    var canStartRecording: Bool {
        recordingPhase.isTerminal
    }

    var canStopRecording: Bool {
        recordingPhase.isStoppable
    }

    var isSavingRecording: Bool {
        recordingPhase.isSaving
    }

    var uploadServerSummary: String {
        "HTTP upload server at \(hostIP):\(uploadPort)"
    }

    var sensorTargetSummary: String {
        "Wired sensor mirror at \(hostIP):\(sensorPort)"
    }

    init() {
        let defaults = UserDefaults.standard
        hostIP = defaults.string(forKey: Self.hostIPKey) ?? "192.168.1.10"
        hostPort = defaults.string(forKey: Self.hostPortKey) ?? "5555"
        uploadPort = defaults.string(forKey: Self.uploadPortKey) ?? "8000"
        sensorPort = defaults.string(forKey: Self.sensorPortKey) ?? "5556"
        sensorAccessoryProtocol = defaults.string(forKey: Self.sensorAccessoryProtocolKey) ?? "com.example.sensor.pose"
        receiverPlatform = ReceiverPlatform(rawValue: defaults.string(forKey: Self.receiverPlatformKey) ?? ReceiverPlatform.macOS.rawValue) ?? .macOS
        isVideoStreamingEnabled = defaults.object(forKey: Self.isVideoStreamingEnabledKey) as? Bool ?? false
        videoPort = defaults.string(forKey: Self.videoPortKey) ?? "5560"
        videoFrameRate = defaults.string(forKey: Self.videoFrameRateKey) ?? "60"
        videoBitrateMbps = defaults.string(forKey: Self.videoBitrateKey) ?? "6.0"
        videoResolution = VideoStreamResolution(rawValue: defaults.string(forKey: Self.videoResolutionKey) ?? VideoStreamResolution.hd720p.rawValue) ?? .hd720p
        showPositionChart = defaults.object(forKey: Self.showPositionChartKey) as? Bool ?? true
        captureRecords = captureLibraryStore.loadRecords().sorted { $0.createdAt > $1.createdAt }
        videoStatus = isVideoStreamingEnabled ? "Video ready" : "Video off"
        videoStats = VideoStreamStatsViewState()
        videoStats.state = videoStatus

        configureSender()
        refreshConnectedAccessories()
    }

    func refreshConnectedAccessories() {
        connectedAccessories = WiredSensorAccessoryScanner.currentAccessories()
    }

    func startSending() {
        guard let port = normalizedPort(hostPort) else {
            sendStatus = "Invalid UDP port"
            return
        }

        if sender == nil {
            configureSender()
        } else {
            sender?.updateDestination(hostIP: hostIP, port: port)
            sender?.updateVideoStreamingConfiguration(makeVideoConfiguration())
        }

        sender?.start()
        isSending = true
        if isVideoStreamingEnabled {
            sendStatus = "Streaming pose to \(hostIP):\(port) and video to \(hostIP):\(normalizedPort(videoPort) ?? 5560)"
        } else {
            sendStatus = "Streaming pose to \(hostIP):\(port)"
        }
    }

    func stopSending() {
        sender?.stop()
        isSending = false
        sendStatus = "Stopped"
    }

    func startWiredSensor() {
        guard let port = normalizedPort(sensorPort) else {
            sensorStatus = "Invalid sensor UDP port"
            return
        }

        let trimmedProtocol = sensorAccessoryProtocol.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedProtocol.isEmpty else {
            sensorStatus = "External accessory protocol is empty"
            return
        }

        if sensorBridge == nil {
            configureSensorBridge(port: port)
        } else {
            sensorBridge?.updateDestination(hostIP: hostIP, port: port)
        }

        guard let sensorBridge else {
            sensorStatus = "Could not create sensor bridge"
            return
        }

        sensorRecorder.startSessionIfNeeded()
        sensorRecorder.appendEvent(kind: "sensor_start", detail: "protocol=\(trimmedProtocol) host=\(hostIP) port=\(port)")
        lastSensorLogName = sensorRecorder.currentFileName
        sensorBridge.start(accessoryProtocol: trimmedProtocol)
        isSensorStreaming = true
        sensorStatus = "Waiting for wired sensor"
    }

    func stopWiredSensor() {
        sensorBridge?.stop()
        sensorRecorder.appendEvent(kind: "sensor_stop", detail: sensorStatus)
        sensorRecorder.finishSession()
        isSensorStreaming = false
        sensorStatus = "Sensor idle"
    }

    func startRecording() {
        guard canStartRecording else { return }

        if sender == nil {
            configureSender()
        }

        sender?.startRecording()
    }

    func stopRecording() {
        guard canStopRecording else { return }

        sender?.stopRecording()
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

        if isSensorStreaming {
            stopWiredSensor()
        }

        sender?.stopPreview()
    }

    func activatePreview() {
        if sender == nil {
            configureSender()
        }

        sender?.updateVideoStreamingConfiguration(makeVideoConfiguration())
        sender?.startPreview()
    }

    func deactivatePreviewIfPossible() {
        sender?.stopPreview()
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

    func isUploading(_ record: CaptureRecord) -> Bool {
        uploadingRecordIDs.contains(record.id)
    }

    func requestVideoUpload(for record: CaptureRecord) {
        let videoState = captureLibraryStore.videoFileState(for: record)
        guard videoState.canUpload else {
            uploadStatus = videoState.statusText
            return
        }

        if let previousUploadDate = record.videoUploadedAt {
            pendingReuploadPrompt = ReuploadPrompt(
                recordID: record.id,
                kind: .video,
                title: "Video already uploaded",
                previousUploadDate: previousUploadDate
            )
        } else {
            upload(record: record, kind: .video)
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
            upload(record: record, kind: .pose)
        }
    }

    func confirmReupload(_ prompt: ReuploadPrompt) {
        guard let record = captureRecords.first(where: { $0.id == prompt.recordID }) else { return }
        pendingReuploadPrompt = nil
        upload(record: record, kind: prompt.kind)
    }

    func cancelReuploadPrompt() {
        pendingReuploadPrompt = nil
    }

    private func upload(record: CaptureRecord, kind: CaptureUploadKind) {
        guard let uploadPort = normalizedPort(uploadPort) else {
            uploadStatus = "Invalid upload port"
            return
        }

        let descriptors: [UploadDescriptor]
        switch kind {
        case .video:
            let videoState = captureLibraryStore.videoFileState(for: record)
            guard let videoURL = videoState.uploadURL else {
                uploadStatus = videoState.statusText
                return
            }
            descriptors = [UploadDescriptor(fileURL: videoURL, component: "video")]
        case .pose:
            descriptors = [
                UploadDescriptor(fileURL: captureLibraryStore.urlForPoseCSV(record: record), component: "pose_csv"),
                UploadDescriptor(fileURL: captureLibraryStore.urlForManifest(record: record), component: "manifest")
            ]
        }

        guard let baseURL = URL(string: "http://\(hostIP):\(uploadPort)") else {
            uploadStatus = "Invalid upload server URL"
            return
        }

        uploadingRecordIDs.insert(record.id)
        uploadStatus = "Uploading \(kind == .video ? "video" : "pose") for \(record.displayName)..."
        uploadDetails = UploadStatusViewState(
            currentFileName: descriptors.first?.fileURL.lastPathComponent ?? "",
            currentComponent: descriptors.first?.component ?? "",
            completedFiles: 0,
            totalFiles: descriptors.count,
            savedPaths: []
        )

        Task {
            do {
                let responses = try await captureUploadService.upload(
                    descriptors: descriptors,
                    captureID: record.sessionDirectoryName,
                    serverBaseURL: baseURL,
                    kind: kind,
                    progress: { [weak self] snapshot in
                        await MainActor.run {
                            guard let self else { return }
                            self.uploadDetails = UploadStatusViewState(
                                currentFileName: snapshot.currentFileName,
                                currentComponent: snapshot.currentComponent,
                                completedFiles: snapshot.completedFiles,
                                totalFiles: snapshot.totalFiles,
                                savedPaths: self.uploadDetails.savedPaths + (snapshot.savedTo.map { [$0] } ?? [])
                            )
                            let kindLabel = kind == .video ? "video" : "pose"
                            self.uploadStatus = "Uploading \(kindLabel) for \(record.displayName): \(snapshot.completedFiles)/\(snapshot.totalFiles)"
                        }
                    }
                )

                await MainActor.run {
                    uploadingRecordIDs.remove(record.id)
                    captureRecords = captureLibraryStore
                        .markUploaded(id: record.id, kind: kind)
                        .sorted { $0.createdAt > $1.createdAt }
                    let savedPaths = responses.compactMap(\.saved_to)
                    uploadDetails = UploadStatusViewState(
                        currentFileName: descriptors.last?.fileURL.lastPathComponent ?? "",
                        currentComponent: descriptors.last?.component ?? "",
                        completedFiles: descriptors.count,
                        totalFiles: descriptors.count,
                        savedPaths: savedPaths
                    )
                    let suffix = savedPaths.last.map { " -> \($0)" } ?? ""
                    uploadStatus = "Uploaded \(kind == .video ? "video" : "pose") for \(record.displayName)\(suffix)"
                }
            } catch {
                await MainActor.run {
                    uploadingRecordIDs.remove(record.id)
                    uploadDetails = UploadStatusViewState()
                    uploadStatus = "Upload failed: \(error.localizedDescription)"
                }
            }
        }
    }

    private func configureSender() {
        let senderPort = UInt16(hostPort) ?? 5555
        let newSender = ARPoseUDPSender(
            hostIP: hostIP,
            port: senderPort,
            videoConfiguration: makeVideoConfiguration()
        )
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
                self?.sendStatus = "Transport error: \(error.localizedDescription)"
            }
        }

        newSender?.onTrackingStatusChange = { [weak self] status in
            Task { @MainActor [weak self] in
                self?.trackingStatus = status
            }
        }

        newSender?.onVideoStateChange = { [weak self] state in
            Task { @MainActor [weak self] in
                self?.videoStatus = state
                self?.videoStats.state = state
            }
        }

        newSender?.onVideoStatsChange = { [weak self] stats in
            Task { @MainActor [weak self] in
                self?.videoStats = VideoStreamStatsViewState(from: stats)
                self?.videoStatus = stats.state
            }
        }

        newSender?.onRecordingStatusChange = { [weak self] status in
            Task { @MainActor [weak self] in
                self?.recordingStatus = status.message
                self?.recordingPhase = status
                self?.isRecordingVideo = status.isActive

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
                if let warning = artifact.warning {
                    self.recordingStatus = warning
                }
            }
        }
    }

    private func configureSensorBridge(port: UInt16) {
        let newBridge = WiredSensorPoseBridge(hostIP: hostIP, port: port)
        sensorBridge = newBridge

        newBridge?.onSampleReceived = { [weak self] sample in
            Task { @MainActor [weak self] in
                guard let self else { return }

                self.sensorPosition = sample.position
                self.sensorRecorder.append(sample: sample)
                self.lastSensorLogName = self.sensorRecorder.currentFileName
                self.latestSensorSummary = String(
                    format: "#%u  rx  x=%.3f  y=%.3f  z=%.3f",
                    sample.sequence,
                    sample.position.x,
                    sample.position.y,
                    sample.position.z
                )
            }
        }

        newBridge?.onSampleForwarded = { [weak self] sample in
            Task { @MainActor [weak self] in
                self?.latestSensorSummary = String(
                    format: "#%u  forwarded  x=%.3f  y=%.3f  z=%.3f",
                    sample.sequence,
                    sample.position.x,
                    sample.position.y,
                    sample.position.z
                )
            }
        }

        newBridge?.onStatusChanged = { [weak self] status in
            Task { @MainActor [weak self] in
                self?.sensorStatus = status
                self?.sensorRecorder.appendEvent(kind: "status", detail: status)
            }
        }

        newBridge?.onError = { [weak self] error in
            Task { @MainActor [weak self] in
                if let wiredError = error as? WiredSensorError, case .noMatchingAccessory = wiredError {
                    self?.sensorStatus = "Waiting for wired sensor"
                } else {
                    self?.sensorStatus = "Sensor error: \(error.localizedDescription)"
                }
                self?.sensorRecorder.appendEvent(kind: "error", detail: error.localizedDescription)
            }
        }

        newBridge?.onStatsChanged = { [weak self] stats in
            Task { @MainActor [weak self] in
                self?.wiredSensorStats = WiredSensorStatsViewState(
                    bytesRead: stats.bytesRead,
                    linesRead: stats.linesRead,
                    parsedSamples: stats.parsedSamples,
                    parseFailures: stats.parseFailures,
                    lastRawLine: stats.lastRawLine,
                    lastParseFailure: stats.lastParseFailure,
                    connectedAccessoryName: stats.connectedAccessoryName
                )
            }
        }

        newBridge?.onParseFailure = { [weak self] line in
            Task { @MainActor [weak self] in
                self?.sensorRecorder.appendEvent(kind: "parse_failure", detail: line)
            }
        }
    }

    private func applySenderConfigurationIfNeeded() {
        guard let sender else { return }

        if isSending, let posePort = normalizedPort(hostPort) {
            sender.updateDestination(hostIP: hostIP, port: posePort)
        }

        sender.updateVideoStreamingConfiguration(makeVideoConfiguration())
    }

    private func makeVideoConfiguration() -> LowLatencyVideoConfiguration {
        LowLatencyVideoConfiguration(
            isEnabled: isVideoStreamingEnabled,
            hostIP: hostIP,
            port: normalizedPort(videoPort) ?? LowLatencyVideoConfiguration.defaults.port,
            resolution: videoResolution,
            frameRate: Int(videoFrameRate) ?? LowLatencyVideoConfiguration.defaults.frameRate,
            bitrateMbps: Double(videoBitrateMbps) ?? LowLatencyVideoConfiguration.defaults.bitrateMbps
        )
    }

    private func normalizedPort(_ value: String) -> UInt16? {
        UInt16(value)
    }

    private func appendHistory(_ sample: ARPoseUDPSender.PoseSample) {
        positionHistory.append(
            PositionHistorySample(
                timestamp: sample.timestamp,
                sequence: Int(sample.sequence),
                x: Double(sample.position.x),
                y: Double(sample.position.y),
                z: Double(sample.position.z)
            )
        )

        let cutoff = sample.timestamp - 5.0
        positionHistory.removeAll { $0.timestamp < cutoff }

        if positionHistory.count > maxHistorySamples {
            positionHistory.removeFirst(positionHistory.count - maxHistorySamples)
        }
    }

    private static let hostIPKey = "ARPoseStreamer.hostIP"
    private static let hostPortKey = "ARPoseStreamer.hostPort"
    private static let uploadPortKey = "ARPoseStreamer.uploadPort"
    private static let sensorPortKey = "ARPoseStreamer.sensorPort"
    private static let sensorAccessoryProtocolKey = "ARPoseStreamer.sensorAccessoryProtocol"
    private static let receiverPlatformKey = "ARPoseStreamer.receiverPlatform"
    private static let isVideoStreamingEnabledKey = "ARPoseStreamer.video.enabled"
    private static let videoPortKey = "ARPoseStreamer.video.port"
    private static let videoFrameRateKey = "ARPoseStreamer.video.frameRate"
    private static let videoBitrateKey = "ARPoseStreamer.video.bitrateMbps"
    private static let videoResolutionKey = "ARPoseStreamer.video.resolution"
    private static let showPositionChartKey = "ARPoseStreamer.showPositionChart"
}
