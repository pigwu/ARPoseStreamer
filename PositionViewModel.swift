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

struct MagneticHistorySample: Identifiable {
    let id = UUID()
    let timestamp: TimeInterval
    let sequence: UInt32
    let magnitudes: [Double]
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

struct ActiveExperimentSession {
    let id: UUID
    let startUnixTime: TimeInterval
    let startMonotonicTime: TimeInterval
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
    @Published var autoUploadExperiments: Bool {
        didSet { UserDefaults.standard.set(autoUploadExperiments, forKey: Self.autoUploadExperimentsKey) }
    }
    @Published var sensorPort: String {
        didSet { UserDefaults.standard.set(sensorPort, forKey: Self.sensorPortKey) }
    }
    @Published var sensorAccessoryProtocol: String {
        didSet { UserDefaults.standard.set(sensorAccessoryProtocol, forKey: Self.sensorAccessoryProtocolKey) }
    }
    @Published var magneticListenPort: String {
        didSet {
            UserDefaults.standard.set(magneticListenPort, forKey: Self.magneticListenPortKey)
            applyMagneticConfigurationIfNeeded()
        }
    }
    @Published var computerRegistrationPort: String {
        didSet {
            UserDefaults.standard.set(computerRegistrationPort, forKey: Self.computerRegistrationPortKey)
            applyMagneticConfigurationIfNeeded()
        }
    }
    @Published var combinedStreamPort: String {
        didSet { UserDefaults.standard.set(combinedStreamPort, forKey: Self.combinedStreamPortKey) }
    }
    @Published var autoStartMagneticSensor: Bool {
        didSet {
            UserDefaults.standard.set(autoStartMagneticSensor, forKey: Self.autoStartMagneticSensorKey)
            if autoStartMagneticSensor {
                startMagneticSensor()
            }
        }
    }
    @Published var selectedMagneticChip: Int {
        didSet {
            let normalized = min(max(selectedMagneticChip, 0), MagneticSensorSample.chipCount - 1)
            if normalized != selectedMagneticChip {
                selectedMagneticChip = normalized
                return
            }
            UserDefaults.standard.set(selectedMagneticChip, forKey: Self.selectedMagneticChipKey)
        }
    }
    @Published var showMagneticChart: Bool {
        didSet { UserDefaults.standard.set(showMagneticChart, forKey: Self.showMagneticChartKey) }
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
    @Published var isUltraWideVideoStreamingEnabled: Bool {
        didSet {
            UserDefaults.standard.set(
                isUltraWideVideoStreamingEnabled,
                forKey: Self.isUltraWideVideoStreamingEnabledKey
            )
            applySenderConfigurationIfNeeded()
        }
    }
    @Published var ultraWideVideoPort: String {
        didSet {
            UserDefaults.standard.set(ultraWideVideoPort, forKey: Self.ultraWideVideoPortKey)
            applySenderConfigurationIfNeeded()
        }
    }
    @Published var showPositionChart: Bool {
        didSet { UserDefaults.standard.set(showPositionChart, forKey: Self.showPositionChartKey) }
    }

    @Published private(set) var position: SIMD3<Float> = .zero
    @Published private(set) var sensorPosition: SIMD3<Float> = .zero
    @Published private(set) var positionHistory: [PositionHistorySample] = []
    @Published private(set) var magneticHistory: [MagneticHistorySample] = []
    @Published private(set) var latestMagneticChips: [MagneticSensorChipValues] = Array(
        repeating: MagneticSensorChipValues(t: 0, x: 0, y: 0, z: 0),
        count: MagneticSensorSample.chipCount
    )
    @Published private(set) var sendStatus = "Idle"
    @Published private(set) var sensorStatus = "Sensor idle"
    @Published private(set) var magneticStatus = "Magnetic sensor idle"
    @Published private(set) var computerGatewayStatus = "Computer offline; recording locally"
    @Published private(set) var uploadStatus = "Upload idle"
    @Published private(set) var trackingStatus = "AR tracking idle"
    @Published private(set) var uploadDetails = UploadStatusViewState()
    @Published private(set) var latestPacketSummary = "No packets yet"
    @Published private(set) var latestSensorSummary = "No sensor packets yet"
    @Published private(set) var latestMagneticSummary = "No magnetic packets yet"
    @Published private(set) var wiredSensorStats = WiredSensorStatsViewState()
    @Published private(set) var magneticStats = MagneticGatewayStats()
    @Published private(set) var videoStatus = "Video off"
    @Published private(set) var videoStats = VideoStreamStatsViewState.idle
    @Published private(set) var ultraWideVideoStatus = "0.5x video off"
    @Published private(set) var ultraWideVideoStats = VideoStreamStatsViewState.idle
    @Published private(set) var connectedAccessories: [WiredSensorAccessoryInfo] = []
    @Published private(set) var recordingStatus = VideoRecordingStatus.idle.message
    @Published private(set) var recordingPhase = VideoRecordingStatus.idle
    @Published private(set) var isSending = false
    @Published private(set) var isSensorStreaming = false
    @Published private(set) var isMagneticListening = false
    @Published private(set) var isComputerConnected = false
    @Published private(set) var isRecordingVideo = false
    @Published private(set) var lastSavedVideoURL: URL?
    @Published private(set) var lastSavedVideoName = "No saved video yet"
    @Published private(set) var lastSavedUltraWideVideoURL: URL?
    @Published private(set) var lastSavedUltraWideVideoName = "No saved 0.5x video yet"
    @Published private(set) var lastCaptureSessionName = "No capture exported yet"
    @Published private(set) var lastSensorLogName = "No sensor log yet"
    @Published private(set) var captureRecords: [CaptureRecord] = []
    @Published private(set) var uploadingRecordIDs: Set<UUID> = []
    @Published var pendingReuploadPrompt: ReuploadPrompt?

    private let maxHistorySamples = 120
    private let maxMagneticHistorySamples = 160
    private let captureLibraryStore = CaptureLibraryStore()
    private let captureUploadService = CaptureUploadService()
    private let sensorRecorder = SensorPoseStreamRecorder()
    private var sender: ARPoseUDPSender?
    private var sensorBridge: WiredSensorPoseBridge?
    private var magneticGateway: MagneticSensorHotspotGateway?
    private var activeExperiment: ActiveExperimentSession?

    var previewSession: ARSession? {
        sender?.session
    }

    var targetSummary: String {
        "\(receiverPlatform.displayName) receiver at \(hostIP):\(hostPort)"
    }

    var videoTargetSummary: String {
        "1x at \(hostIP):\(videoPort); 0.5x ArUco at \(hostIP):\(ultraWideVideoPort)"
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

    var magneticListenSummary: String {
        "Phone hotspot gateway listens on UDP \(magneticListenPort)"
    }

    var combinedReceiverCommand: String {
        let port = normalizedPort(combinedStreamPort) ?? 5558
        let registrationPort = normalizedPort(computerRegistrationPort) ?? 5559
        switch receiverPlatform {
        case .macOS:
            return "python3 pose_magnetic_receiver.py --port \(port) --phone-ip 172.20.10.1 --registration-port \(registrationPort)"
        case .windows:
            return "py pose_magnetic_receiver.py --port \(port) --phone-ip 172.20.10.1 --registration-port \(registrationPort)"
        }
    }

    var magneticReceiveRateText: String {
        String(format: "%.1f", magneticStats.receiveRateHz)
    }

    var magneticLossText: String {
        String(format: "%.2f%%", magneticStats.lossPercent)
    }

    var magneticSequenceText: String {
        magneticStats.lastSequence.map { String($0) } ?? "--"
    }

    var selectedMagneticValues: MagneticSensorChipValues {
        guard latestMagneticChips.indices.contains(selectedMagneticChip) else {
            return MagneticSensorChipValues(t: 0, x: 0, y: 0, z: 0)
        }
        return latestMagneticChips[selectedMagneticChip]
    }

    var selectedMagneticMagnitudeText: String {
        let value = selectedMagneticValues
        let magnitude = sqrt(Double(value.x * value.x + value.y * value.y + value.z * value.z))
        return String(format: "%.3f", magnitude)
    }

    init() {
        let defaults = UserDefaults.standard
        hostIP = defaults.string(forKey: Self.hostIPKey) ?? "192.168.1.10"
        hostPort = defaults.string(forKey: Self.hostPortKey) ?? "5555"
        uploadPort = defaults.string(forKey: Self.uploadPortKey) ?? "8000"
        autoUploadExperiments = Self.storedBool(defaults, key: Self.autoUploadExperimentsKey, fallback: true)
        sensorPort = defaults.string(forKey: Self.sensorPortKey) ?? "5556"
        sensorAccessoryProtocol = defaults.string(forKey: Self.sensorAccessoryProtocolKey) ?? "com.example.sensor.pose"
        magneticListenPort = defaults.string(forKey: Self.magneticListenPortKey) ?? "5557"
        computerRegistrationPort = defaults.string(forKey: Self.computerRegistrationPortKey) ?? "5559"
        combinedStreamPort = defaults.string(forKey: Self.combinedStreamPortKey) ?? "5558"
        autoStartMagneticSensor = Self.storedBool(defaults, key: Self.autoStartMagneticSensorKey, fallback: true)
        selectedMagneticChip = min(
            max(defaults.integer(forKey: Self.selectedMagneticChipKey), 0),
            MagneticSensorSample.chipCount - 1
        )
        showMagneticChart = Self.storedBool(defaults, key: Self.showMagneticChartKey, fallback: true)
        receiverPlatform = ReceiverPlatform(rawValue: defaults.string(forKey: Self.receiverPlatformKey) ?? ReceiverPlatform.macOS.rawValue) ?? .macOS
        isVideoStreamingEnabled = Self.storedBool(defaults, key: Self.isVideoStreamingEnabledKey, fallback: false)
        videoPort = defaults.string(forKey: Self.videoPortKey) ?? "5560"
        videoFrameRate = defaults.string(forKey: Self.videoFrameRateKey) ?? "60"
        videoBitrateMbps = defaults.string(forKey: Self.videoBitrateKey) ?? "6.0"
        videoResolution = VideoStreamResolution(rawValue: defaults.string(forKey: Self.videoResolutionKey) ?? VideoStreamResolution.hd720p.rawValue) ?? .hd720p
        isUltraWideVideoStreamingEnabled = Self.storedBool(
            defaults,
            key: Self.isUltraWideVideoStreamingEnabledKey,
            fallback: true
        )
        ultraWideVideoPort = defaults.string(forKey: Self.ultraWideVideoPortKey) ?? "5561"
        showPositionChart = Self.storedBool(defaults, key: Self.showPositionChartKey, fallback: true)
        captureRecords = captureLibraryStore.loadRecords().sorted { $0.createdAt > $1.createdAt }
        videoStatus = isVideoStreamingEnabled ? "Video ready" : "Video off"
        videoStats = VideoStreamStatsViewState()
        videoStats.state = videoStatus
        ultraWideVideoStatus = isUltraWideVideoStreamingEnabled ? "0.5x video ready" : "0.5x video off"
        ultraWideVideoStats = VideoStreamStatsViewState()
        ultraWideVideoStats.state = ultraWideVideoStatus

        configureSender()
        configureMagneticGateway()
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
            sender?.updateUltraWideVideoStreamingConfiguration(makeUltraWideVideoConfiguration())
        }

        startMagneticSensor()
        if !isRecordingVideo {
            magneticGateway?.resetStreamSession()
        }
        sender?.start()
        isSending = true
        if isVideoStreamingEnabled || isUltraWideVideoStreamingEnabled {
            var destinations: [String] = []
            if isVideoStreamingEnabled {
                destinations.append("1x video :\(normalizedPort(videoPort) ?? 5560)")
            }
            if isUltraWideVideoStreamingEnabled {
                destinations.append("0.5x ArUco :\(normalizedPort(ultraWideVideoPort) ?? 5561)")
            }
            sendStatus = "Streaming pose to \(hostIP):\(port); \(destinations.joined(separator: ", "))"
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

    func startMagneticSensor() {
        guard
            let listenPort = normalizedPort(magneticListenPort),
            let registrationPort = normalizedPort(computerRegistrationPort)
        else {
            magneticStatus = "Invalid magnetic or computer registration port"
            return
        }

        if magneticGateway == nil {
            configureMagneticGateway()
        }

        magneticGateway?.start(sensorPort: listenPort, computerPort: registrationPort)
        magneticStatus = "Starting hotspot magnetic listener"
    }

    func stopMagneticSensor() {
        magneticGateway?.stop()
        isMagneticListening = false
        isComputerConnected = false
        magneticStatus = "Magnetic sensor idle"
        computerGatewayStatus = "Computer offline; recording locally"
    }

    func startRecording() {
        guard canStartRecording, activeExperiment == nil else { return }

        if sender == nil {
            configureSender()
        }

        let experiment = ActiveExperimentSession(
            id: UUID(),
            startUnixTime: Date().timeIntervalSince1970,
            startMonotonicTime: ProcessInfo.processInfo.systemUptime
        )
        activeExperiment = experiment
        startMagneticSensor()
        magneticGateway?.resetStreamSession(sessionID: experiment.id)
        sender?.startRecording(
            experimentID: experiment.id,
            startUnixTime: experiment.startUnixTime,
            startMonotonicTime: experiment.startMonotonicTime
        )
        sendExperimentControlEvent(
            experimentID: experiment.id,
            event: "start",
            unixTime: experiment.startUnixTime,
            monotonicTime: experiment.startMonotonicTime
        )
    }

    func stopRecording() {
        guard canStopRecording else { return }

        let stopUnixTime = Date().timeIntervalSince1970
        let stopMonotonicTime = ProcessInfo.processInfo.systemUptime
        sender?.stopRecording(
            stopUnixTime: stopUnixTime,
            stopMonotonicTime: stopMonotonicTime
        )
        if let activeExperiment {
            sendExperimentControlEvent(
                experimentID: activeExperiment.id,
                event: "stop",
                unixTime: stopUnixTime,
                monotonicTime: stopMonotonicTime
            )
        }
        activeExperiment = nil
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

        if isMagneticListening {
            stopMagneticSensor()
        }

        sender?.stopPreview()
    }

    func activatePreview() {
        if sender == nil {
            configureSender()
        }

        sender?.updateVideoStreamingConfiguration(makeVideoConfiguration())
        sender?.updateUltraWideVideoStreamingConfiguration(makeUltraWideVideoConfiguration())
        sender?.startPreview()
        if autoStartMagneticSensor {
            startMagneticSensor()
        }
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
                title: "Capture data already uploaded",
                previousUploadDate: previousUploadDate
            )
        } else {
            upload(record: record, kind: .pose)
        }
    }

    func requestExperimentUpload(for record: CaptureRecord) {
        if let previousUploadDate = record.experimentUploadedAt {
            pendingReuploadPrompt = ReuploadPrompt(
                recordID: record.id,
                kind: .experiment,
                title: "Experiment already uploaded",
                previousUploadDate: previousUploadDate
            )
        } else {
            upload(record: record, kind: .experiment)
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
            var dataDescriptors = [
                UploadDescriptor(fileURL: captureLibraryStore.urlForPoseCSV(record: record), component: "pose_csv")
            ]
            if let magneticURL = captureLibraryStore.urlForMagneticCSV(record: record) {
                dataDescriptors.append(UploadDescriptor(fileURL: magneticURL, component: "magnetic_csv"))
            }
            dataDescriptors.append(
                UploadDescriptor(fileURL: captureLibraryStore.urlForManifest(record: record), component: "manifest")
            )
            descriptors = dataDescriptors
        case .experiment:
            var experimentDescriptors = [
                UploadDescriptor(fileURL: captureLibraryStore.urlForPoseCSV(record: record), component: "pose_csv")
            ]
            if let magneticURL = captureLibraryStore.urlForMagneticCSV(record: record) {
                experimentDescriptors.append(UploadDescriptor(fileURL: magneticURL, component: "magnetic_csv"))
            }
            if let senderTransportURL = captureLibraryStore.urlForSenderTransportCSV(record: record) {
                experimentDescriptors.append(UploadDescriptor(fileURL: senderTransportURL, component: "sender_transport"))
            }
            if let videoURL = captureLibraryStore.videoFileState(for: record).uploadURL {
                experimentDescriptors.append(UploadDescriptor(fileURL: videoURL, component: "video"))
            }
            if let ultraWideVideoURL = captureLibraryStore.urlForUltraWideVideo(record: record) {
                experimentDescriptors.append(
                    UploadDescriptor(fileURL: ultraWideVideoURL, component: "ultrawide_video")
                )
            }
            experimentDescriptors.append(
                UploadDescriptor(fileURL: captureLibraryStore.urlForManifest(record: record), component: "manifest")
            )
            descriptors = experimentDescriptors
        }

        guard let baseURL = URL(string: "http://\(hostIP):\(uploadPort)") else {
            uploadStatus = "Invalid upload server URL"
            return
        }

        uploadingRecordIDs.insert(record.id)
        let kindLabel = uploadKindLabel(kind)
        uploadStatus = "Uploading \(kindLabel) for \(record.displayName)..."
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
                    captureID: kind == .experiment ? record.id.uuidString : record.sessionDirectoryName,
                    serverBaseURL: baseURL,
                    kind: kind,
                    experimentStartUnixTime: kind == .experiment ? record.createdAt.timeIntervalSince1970 : nil,
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
                            let kindLabel = self.uploadKindLabel(kind)
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
                    uploadStatus = "Uploaded \(self.uploadKindLabel(kind)) for \(record.displayName)\(suffix)"
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
            videoConfiguration: makeVideoConfiguration(),
            ultraWideVideoConfiguration: makeUltraWideVideoConfiguration()
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

        newSender?.onUltraWideVideoStateChange = { [weak self] state in
            Task { @MainActor [weak self] in
                self?.ultraWideVideoStatus = state
                self?.ultraWideVideoStats.state = state
            }
        }

        newSender?.onUltraWideVideoStatsChange = { [weak self] stats in
            Task { @MainActor [weak self] in
                self?.ultraWideVideoStats = VideoStreamStatsViewState(from: stats)
                self?.ultraWideVideoStatus = stats.state
            }
        }

        newSender?.onUltraWideRecordingStatusChange = { [weak self] status in
            Task { @MainActor [weak self] in
                if case .saved(let url) = status {
                    self?.lastSavedUltraWideVideoURL = url
                    self?.lastSavedUltraWideVideoName = url.lastPathComponent
                }
            }
        }

        newSender?.onRecordingStatusChange = { [weak self] status in
            Task { @MainActor [weak self] in
                self?.recordingStatus = status.message
                self?.recordingPhase = status
                self?.isRecordingVideo = status.isActive

                if status.isTerminal, let experiment = self?.activeExperiment {
                    let stopUnixTime = Date().timeIntervalSince1970
                    let stopMonotonicTime = ProcessInfo.processInfo.systemUptime
                    self?.sendExperimentControlEvent(
                        experimentID: experiment.id,
                        event: "stop",
                        unixTime: stopUnixTime,
                        monotonicTime: stopMonotonicTime
                    )
                    self?.activeExperiment = nil
                }

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
                    if self.autoUploadExperiments {
                        self.upload(record: record, kind: .experiment)
                    }
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
                if let ultraWideVideoURL = artifact.ultraWideVideoURL {
                    self.lastSavedUltraWideVideoURL = ultraWideVideoURL
                    self.lastSavedUltraWideVideoName = ultraWideVideoURL.lastPathComponent
                }
            }
        }

        wirePoseToMagneticGateway()
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

    private func configureMagneticGateway() {
        let gateway = MagneticSensorHotspotGateway()
        magneticGateway = gateway
        bindMagneticGatewayCallbacks()
        wirePoseToMagneticGateway()
    }

    private func bindMagneticGatewayCallbacks() {
        guard let gateway = magneticGateway else { return }
        let currentSender = sender
        var lastUIPublishTime: TimeInterval = 0

        gateway.onSampleReceived = { [weak currentSender, weak self] sample in
            currentSender?.appendMagneticSample(sample)

            guard sample.receivedMonotonicTime - lastUIPublishTime >= 0.05 else { return }
            lastUIPublishTime = sample.receivedMonotonicTime
            Task { @MainActor [weak self] in
                self?.updateMagneticUI(with: sample)
            }
        }

        gateway.onSensorStatusChanged = { [weak self] status in
            Task { @MainActor [weak self] in
                self?.magneticStatus = status
            }
        }

        gateway.onComputerStatusChanged = { [weak self] status in
            Task { @MainActor [weak self] in
                self?.computerGatewayStatus = status
            }
        }

        gateway.onComputerAvailabilityChanged = { [weak self] isAvailable in
            Task { @MainActor [weak self] in
                self?.isComputerConnected = isAvailable
            }
        }

        gateway.onListeningChanged = { [weak self] isListening in
            Task { @MainActor [weak self] in
                self?.isMagneticListening = isListening
            }
        }

        gateway.onStatsChanged = { [weak self] stats in
            Task { @MainActor [weak self] in
                self?.magneticStats = stats
            }
        }

        gateway.onError = { [weak self] error in
            Task { @MainActor [weak self] in
                self?.latestMagneticSummary = "Magnetic transport: \(error.localizedDescription)"
            }
        }
    }

    private func wirePoseToMagneticGateway() {
        guard let gateway = magneticGateway else { return }
        sender?.onPoseProduced = { [weak gateway] sample in
            gateway?.handlePose(
                PoseMagneticPoseValue(
                    sequence: sample.sequence,
                    senderUnixTime: sample.timestamp,
                    frameMonotonicTime: sample.frameTimestamp,
                    position: sample.position,
                    quaternionXYZW: sample.orientation.vector
                )
            )
        }

        bindMagneticGatewayCallbacks()
    }

    private func applyMagneticConfigurationIfNeeded() {
        guard
            isMagneticListening,
            let listenPort = normalizedPort(magneticListenPort),
            let registrationPort = normalizedPort(computerRegistrationPort)
        else { return }

        magneticGateway?.start(sensorPort: listenPort, computerPort: registrationPort)
    }

    private func updateMagneticUI(with sample: MagneticSensorSample) {
        latestMagneticChips = sample.chips
        let selectedIndex = min(max(selectedMagneticChip, 0), sample.chips.count - 1)
        let selected = sample.chips[selectedIndex]
        latestMagneticSummary = String(
            format: "#%u  S%d  t=%.3f  x=%.3f  y=%.3f  z=%.3f",
            sample.sequence,
            selectedIndex,
            selected.t,
            selected.x,
            selected.y,
            selected.z
        )

        let magnitudes = sample.chips.map { chip in
            sqrt(Double(chip.x * chip.x + chip.y * chip.y + chip.z * chip.z))
        }
        magneticHistory.append(
            MagneticHistorySample(
                timestamp: sample.receivedMonotonicTime,
                sequence: sample.sequence,
                magnitudes: magnitudes
            )
        )

        let cutoff = sample.receivedMonotonicTime - 5
        magneticHistory.removeAll { $0.timestamp < cutoff }
        if magneticHistory.count > maxMagneticHistorySamples {
            magneticHistory.removeFirst(magneticHistory.count - maxMagneticHistorySamples)
        }
    }

    private func applySenderConfigurationIfNeeded() {
        guard let sender else { return }

        if isSending, let posePort = normalizedPort(hostPort) {
            sender.updateDestination(hostIP: hostIP, port: posePort)
        }

        sender.updateVideoStreamingConfiguration(makeVideoConfiguration())
        sender.updateUltraWideVideoStreamingConfiguration(makeUltraWideVideoConfiguration())
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

    private func makeUltraWideVideoConfiguration() -> LowLatencyVideoConfiguration {
        LowLatencyVideoConfiguration(
            isEnabled: isUltraWideVideoStreamingEnabled,
            hostIP: hostIP,
            port: normalizedPort(ultraWideVideoPort) ?? LowLatencyVideoConfiguration.ultraWideDefaults.port,
            resolution: .sd480p,
            frameRate: 10,
            bitrateMbps: 3.0
        )
    }

    private func uploadKindLabel(_ kind: CaptureUploadKind) -> String {
        switch kind {
        case .video:
            return "video"
        case .pose:
            return "capture data"
        case .experiment:
            return "complete experiment"
        }
    }

    private func sendExperimentControlEvent(
        experimentID: UUID,
        event: String,
        unixTime: TimeInterval,
        monotonicTime: TimeInterval
    ) {
        guard
            let port = normalizedPort(uploadPort),
            let baseURL = URL(string: "http://\(hostIP):\(port)")
        else { return }

        Task {
            try? await captureUploadService.sendExperimentEvent(
                experimentID: experimentID,
                event: event,
                eventUnixTime: unixTime,
                eventMonotonicTime: monotonicTime,
                serverBaseURL: baseURL
            )
        }
    }

    private func normalizedPort(_ value: String) -> UInt16? {
        guard let port = UInt16(value), port > 0 else { return nil }
        return port
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

    private static func storedBool(_ defaults: UserDefaults, key: String, fallback: Bool) -> Bool {
        guard defaults.object(forKey: key) != nil else { return fallback }
        return defaults.bool(forKey: key)
    }

    private static let hostIPKey = "ARPoseStreamer.hostIP"
    private static let hostPortKey = "ARPoseStreamer.hostPort"
    private static let uploadPortKey = "ARPoseStreamer.uploadPort"
    private static let autoUploadExperimentsKey = "ARPoseStreamer.autoUploadExperiments"
    private static let sensorPortKey = "ARPoseStreamer.sensorPort"
    private static let sensorAccessoryProtocolKey = "ARPoseStreamer.sensorAccessoryProtocol"
    private static let magneticListenPortKey = "ARPoseStreamer.magnetic.listenPort"
    private static let computerRegistrationPortKey = "ARPoseStreamer.magnetic.computerRegistrationPort"
    private static let combinedStreamPortKey = "ARPoseStreamer.magnetic.combinedStreamPort"
    private static let autoStartMagneticSensorKey = "ARPoseStreamer.magnetic.autoStart"
    private static let selectedMagneticChipKey = "ARPoseStreamer.magnetic.selectedChip"
    private static let showMagneticChartKey = "ARPoseStreamer.magnetic.showChart"
    private static let receiverPlatformKey = "ARPoseStreamer.receiverPlatform"
    private static let isVideoStreamingEnabledKey = "ARPoseStreamer.video.enabled"
    private static let videoPortKey = "ARPoseStreamer.video.port"
    private static let videoFrameRateKey = "ARPoseStreamer.video.frameRate"
    private static let videoBitrateKey = "ARPoseStreamer.video.bitrateMbps"
    private static let videoResolutionKey = "ARPoseStreamer.video.resolution"
    private static let isUltraWideVideoStreamingEnabledKey = "ARPoseStreamer.video.ultrawide.enabled"
    private static let ultraWideVideoPortKey = "ARPoseStreamer.video.ultrawide.port"
    private static let showPositionChartKey = "ARPoseStreamer.showPositionChart"
}
