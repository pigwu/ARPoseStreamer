import Foundation
import ARKit
import Network
import CoreMedia
import CoreVideo
import simd

private struct UltraWideFrameData {
    let pixelBuffer: CVPixelBuffer
    let timestamp: TimeInterval
    let calibration: VideoCameraCalibration
}

private final class ExperimentRecordingContext {
    let videoRecorder = ARSessionVideoRecorder()
    let ultraWideVideoRecorder = ARSessionVideoRecorder(
        fileNamePrefix: "ARPoseStreamer-UltraWide",
        expectedFrameRate: 10
    )
    let poseSessionRecorder = PoseDataSessionRecorder()
    private let magneticRecordingQueue = DispatchQueue(
        label: "umi.pose.recording.magnetic.\(UUID().uuidString)",
        qos: .utility
    )
    var isDiscarded = false

    func appendMagneticSample(_ sample: MagneticSensorSample) {
        magneticRecordingQueue.async { [recorder = poseSessionRecorder] in
            recorder.append(magneticSample: sample)
        }
    }

    /// Finalization runs off the AR queue and waits for every magnetic sample
    /// accepted before stop/failure to reach disk before closing the CSV files.
    func finishSessionAfterMagneticWrites() {
        magneticRecordingQueue.sync { [recorder = poseSessionRecorder] in
            recorder.finishSession()
        }
    }

    func discardSessionAfterMagneticWrites() {
        magneticRecordingQueue.sync { [recorder = poseSessionRecorder] in
            recorder.discardSession()
        }
    }
}

final class ARPoseUDPSender: NSObject, ARSessionDelegate {
    enum CoordinateSystem {
        case arkitYUp
        case zUpRightHanded
    }

    enum PayloadEncoding {
        case binaryFloat32
        case csvUTF8
    }

    struct PoseSample {
        let sequence: UInt32
        let timestamp: TimeInterval
        let frameTimestamp: TimeInterval
        let position: SIMD3<Float>
        let orientation: simd_quatf

        var csvString: String {
            String(
                format: "%u,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f",
                locale: Locale(identifier: "en_US_POSIX"),
                sequence,
                timestamp,
                position.x,
                position.y,
                position.z,
                orientation.vector.x,
                orientation.vector.y,
                orientation.vector.z,
                orientation.vector.w
            )
        }

        var binaryData: Data {
            let vectorScalars: [UInt32] = [
                position.x.bitPattern.littleEndian,
                position.y.bitPattern.littleEndian,
                position.z.bitPattern.littleEndian,
                orientation.vector.x.bitPattern.littleEndian,
                orientation.vector.y.bitPattern.littleEndian,
                orientation.vector.z.bitPattern.littleEndian,
                orientation.vector.w.bitPattern.littleEndian
            ]

            var data = Data()
            var sequenceLE = sequence.littleEndian
            var timestampLE = timestamp.bitPattern.littleEndian

            withUnsafeBytes(of: &sequenceLE) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: &timestampLE) { data.append(contentsOf: $0) }
            vectorScalars.withUnsafeBytes { data.append(contentsOf: $0) }

            return data
        }
    }

    let session = ARSession()

    var onSampleUpdated: ((PoseSample) -> Void)?
    var onPoseProduced: ((PoseSample) -> Void)?
    var onSampleSent: ((PoseSample) -> Void)?
    var onError: ((Error) -> Void)?
    var onRecordingStatusChange: ((VideoRecordingStatus) -> Void)?
    var onCaptureSessionSaved: ((PoseCaptureArtifact) -> Void)?
    var onTrackingStatusChange: ((String) -> Void)?
    var onVideoStateChange: ((String) -> Void)?
    var onVideoStatsChange: ((LowLatencyVideoStats) -> Void)?
    var onUltraWideRecordingStatusChange: ((VideoRecordingStatus) -> Void)?
    var onUltraWideVideoStateChange: ((String) -> Void)?
    var onUltraWideVideoStatsChange: ((LowLatencyVideoStats) -> Void)?

    private let coordinateSystem: CoordinateSystem
    private let payloadEncoding: PayloadEncoding
    private let minimumSendInterval = 1.0 / 60.0
    private let arQueue = DispatchQueue(label: "umi.pose.ar", qos: .userInitiated)
    private let networkQueue = DispatchQueue(label: "umi.pose.udp", qos: .userInitiated)
    private let recordingFinalizationQueue = DispatchQueue(
        label: "umi.pose.recording.finalization",
        qos: .utility
    )
    private let zUpAlignment = simd_quatf(angle: .pi / 2, axis: SIMD3<Float>(1, 0, 0))
    private let videoSender: ARLowLatencyVideoSender
    private let ultraWideVideoSender: ARLowLatencyVideoSender

    private var host: NWEndpoint.Host
    private var port: NWEndpoint.Port
    private var videoConfiguration: LowLatencyVideoConfiguration
    private var ultraWideVideoConfiguration: LowLatencyVideoConfiguration
    private var connection: NWConnection?
    private var originTransform: simd_float4x4?
    private var lastSentTimestamp: TimeInterval = -.infinity
    private var lastUltraWideFrameTimestamp: TimeInterval = -.infinity
    private var sequenceNumber: UInt32 = 0
    private var shouldResetOriginOnNextFrame = true
    private var isConnectionReady = false
    private var isSessionRunning = false
    private var isStreamingEnabled = false
    private var isRecordingEnabled = false
    private var activeRecordingContext: ExperimentRecordingContext?

    init?(
        hostIP: String,
        port: UInt16 = 5555,
        coordinateSystem: CoordinateSystem = .zUpRightHanded,
        payloadEncoding: PayloadEncoding = .binaryFloat32,
        videoConfiguration: LowLatencyVideoConfiguration = .defaults,
        ultraWideVideoConfiguration: LowLatencyVideoConfiguration = .ultraWideDefaults
    ) {
        guard let udpPort = NWEndpoint.Port(rawValue: port) else {
            return nil
        }

        self.host = NWEndpoint.Host(hostIP)
        self.port = udpPort
        self.coordinateSystem = coordinateSystem
        self.payloadEncoding = payloadEncoding
        self.videoConfiguration = videoConfiguration
        self.ultraWideVideoConfiguration = ultraWideVideoConfiguration
        self.videoSender = ARLowLatencyVideoSender(configuration: videoConfiguration)
        self.ultraWideVideoSender = ARLowLatencyVideoSender(configuration: ultraWideVideoConfiguration)

        super.init()

        session.delegate = self
        videoSender.onStateChange = { [weak self] state in
            self?.onVideoStateChange?(state)
        }
        videoSender.onStatsChange = { [weak self] stats in
            self?.arQueue.async {
                guard let context = self?.activeRecordingContext else { return }
                context.poseSessionRecorder.appendSenderTransport(
                    stats: stats,
                    sampleUnixTime: Date().timeIntervalSince1970
                )
            }
            self?.onVideoStatsChange?(stats)
        }
        videoSender.onError = { [weak self] error in
            self?.onError?(error)
        }
        ultraWideVideoSender.onStateChange = { [weak self] state in
            self?.onUltraWideVideoStateChange?(state)
        }
        ultraWideVideoSender.onStatsChange = { [weak self] stats in
            self?.onUltraWideVideoStatsChange?(stats)
        }
        ultraWideVideoSender.onError = { [weak self] error in
            self?.onError?(error)
        }
    }

    deinit {
        connection?.cancel()
        connection = nil
        session.pause()
        session.delegate = nil
    }

    func start() {
        networkQueue.async { [weak self] in
            self?.connectUDP()
        }

        arQueue.async { [weak self] in
            guard let self else { return }
            self.isStreamingEnabled = true
            self.videoSender.startStreaming()
            self.ultraWideVideoSender.startStreaming()
            self.startSessionIfNeeded()
        }
    }

    func startPreview() {
        arQueue.async { [weak self] in
            self?.startSessionIfNeeded()
        }
    }

    func stop() {
        networkQueue.async { [weak self] in
            self?.disconnectUDP()
        }

        arQueue.async { [weak self] in
            guard let self else { return }
            self.isStreamingEnabled = false
            self.videoSender.stopStreaming()
            self.ultraWideVideoSender.stopStreaming()
            if !self.isRecordingEnabled {
                self.pauseSessionIfNeeded()
            }
        }
    }

    func stopPreview() {
        arQueue.async { [weak self] in
            guard let self else { return }
            if !self.isStreamingEnabled && !self.isRecordingEnabled {
                self.pauseSessionIfNeeded()
            }
        }
    }

    func updateDestination(hostIP: String, port: UInt16 = 5555) {
        guard let udpPort = NWEndpoint.Port(rawValue: port) else { return }

        networkQueue.async { [weak self] in
            guard let self else { return }
            self.host = NWEndpoint.Host(hostIP)
            self.port = udpPort
            self.connection?.cancel()
            self.connection = nil
            self.isConnectionReady = false
            self.connectUDP()
        }
    }

    func updateVideoStreamingConfiguration(_ configuration: LowLatencyVideoConfiguration) {
        arQueue.async { [weak self] in
            guard let self else { return }

            let needsSessionRestart =
                self.videoConfiguration.resolution != configuration.resolution ||
                self.videoConfiguration.frameRate != configuration.frameRate

            self.videoConfiguration = configuration
            self.videoSender.updateConfiguration(configuration)

            if needsSessionRestart, self.isSessionRunning, !self.isRecordingEnabled {
                self.pauseSessionIfNeeded()
                self.startSessionIfNeeded()
            }
        }
    }

    func updateUltraWideVideoStreamingConfiguration(_ configuration: LowLatencyVideoConfiguration) {
        arQueue.async { [weak self] in
            guard let self else { return }
            self.ultraWideVideoConfiguration = configuration
            self.ultraWideVideoSender.updateConfiguration(configuration)
        }
    }

    func resetOrigin() {
        arQueue.async { [weak self] in
            self?.originTransform = nil
            self?.shouldResetOriginOnNextFrame = true
        }
    }

    func appendMagneticSample(_ sample: MagneticSensorSample) {
        arQueue.async { [weak self] in
            guard let context = self?.activeRecordingContext else { return }
            context.appendMagneticSample(sample)
        }
    }

    func startRecording(
        experimentID: UUID,
        startUnixTime: TimeInterval,
        startMonotonicTime: TimeInterval
    ) {
        arQueue.async { [weak self] in
            guard let self else { return }
            guard self.activeRecordingContext == nil else { return }

            let context = ExperimentRecordingContext()
            self.configureRecordingCallbacks(for: context)
            self.activeRecordingContext = context
            self.isRecordingEnabled = true
            context.poseSessionRecorder.startSession(
                experimentID: experimentID,
                startUnixTime: startUnixTime,
                startMonotonicTime: startMonotonicTime
            )
            context.videoRecorder.startRecording()
            context.ultraWideVideoRecorder.startRecording()
            self.startSessionIfNeeded()
            self.failRecordingIfFirstFrameDoesNotArrive(for: context)
        }
    }

    func stopRecording(stopUnixTime: TimeInterval, stopMonotonicTime: TimeInterval) {
        arQueue.async { [weak self] in
            guard let self, let context = self.activeRecordingContext else { return }

            self.activeRecordingContext = nil
            self.isRecordingEnabled = false
            context.poseSessionRecorder.markExperimentStopped(
                unixTime: stopUnixTime,
                monotonicTime: stopMonotonicTime
            )
            let recorders = DispatchGroup()
            var primaryFinalStatus: VideoRecordingStatus?
            var ultraWideFinalStatus: VideoRecordingStatus?

            recorders.enter()
            context.videoRecorder.stopRecording { [weak self] status in
                guard let self else {
                    recorders.leave()
                    return
                }
                self.arQueue.async {
                    primaryFinalStatus = status
                    if case .saved(let url) = status {
                        context.poseSessionRecorder.attachVideo(url: url)
                    }
                    recorders.leave()
                }
            }

            recorders.enter()
            context.ultraWideVideoRecorder.stopRecording { [weak self] status in
                guard let self else {
                    recorders.leave()
                    return
                }
                self.arQueue.async {
                    ultraWideFinalStatus = status
                    if case .saved(let url) = status {
                        context.poseSessionRecorder.attachUltraWideVideo(url: url)
                    }
                    recorders.leave()
                }
            }

            recorders.notify(queue: self.arQueue) { [weak self] in
                guard let self else { return }
                if let primaryFinalStatus, case .failed(let message) = primaryFinalStatus {
                    context.poseSessionRecorder.addWarning("1x recording failed: \(message)")
                }
                if let ultraWideFinalStatus, case .failed(let message) = ultraWideFinalStatus {
                    context.poseSessionRecorder.addWarning("0.5x recording failed: \(message)")
                }
                if !self.isStreamingEnabled, self.activeRecordingContext == nil {
                    self.pauseSessionIfNeeded()
                }
                let finalPrimaryStatus = primaryFinalStatus
                self.recordingFinalizationQueue.async { [weak self] in
                    context.finishSessionAfterMagneticWrites()
                    self?.arQueue.async { [weak self] in
                        guard
                            let self,
                            self.activeRecordingContext == nil,
                            let finalPrimaryStatus,
                            case .saved(let url) = finalPrimaryStatus
                        else { return }

                        DispatchQueue.main.async { [weak self] in
                            self?.onRecordingStatusChange?(.saved(url))
                        }
                    }
                }
            }
        }
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard case .normal = frame.camera.trackingState else { return }
        let cameraTransform = frame.camera.transform
        let cameraIntrinsics = frame.camera.intrinsics
        let imageResolution = frame.camera.imageResolution
        let frameTimestamp = frame.timestamp
        let capturedImage = frame.capturedImage
        let videoCalibration = VideoCameraCalibration(
            fx: cameraIntrinsics.columns.0.x,
            fy: cameraIntrinsics.columns.1.y,
            cx: cameraIntrinsics.columns.2.x,
            cy: cameraIntrinsics.columns.2.y,
            imageWidth: UInt16(clamping: Int(imageResolution.width.rounded())),
            imageHeight: UInt16(clamping: Int(imageResolution.height.rounded()))
        )
        let ultraWideFrame: UltraWideFrameData?
        if
            let ultraWideImage = frame.capturedUltraWideImage,
            let ultraWideCamera = frame.ultraWideCamera
        {
            let ultraWideIntrinsics = ultraWideCamera.intrinsics
            let ultraWideWidth = CVPixelBufferGetWidth(ultraWideImage)
            let ultraWideHeight = CVPixelBufferGetHeight(ultraWideImage)
            let calibrationResolution = ultraWideCamera.imageResolution
            let scaleX = Float(ultraWideWidth) / max(Float(calibrationResolution.width), 1)
            let scaleY = Float(ultraWideHeight) / max(Float(calibrationResolution.height), 1)
            ultraWideFrame = UltraWideFrameData(
                pixelBuffer: ultraWideImage,
                timestamp: frame.ultraWideImageTimestamp ?? frameTimestamp,
                calibration: VideoCameraCalibration(
                    fx: ultraWideIntrinsics.columns.0.x * scaleX,
                    fy: ultraWideIntrinsics.columns.1.y * scaleY,
                    cx: ultraWideIntrinsics.columns.2.x * scaleX,
                    cy: ultraWideIntrinsics.columns.2.y * scaleY,
                    imageWidth: UInt16(clamping: ultraWideWidth),
                    imageHeight: UInt16(clamping: ultraWideHeight)
                )
            )
        } else {
            ultraWideFrame = nil
        }

        arQueue.async { [weak self] in
            self?.processFrame(
                transform: cameraTransform,
                pixelBuffer: capturedImage,
                frameTimestamp: frameTimestamp,
                videoCalibration: videoCalibration,
                ultraWideFrame: ultraWideFrame
            )
        }
    }

    private func startSessionIfNeeded() {
        guard !isSessionRunning else { return }
        guard ARWorldTrackingConfiguration.isSupported else {
            activeRecordingContext?.videoRecorder.failPreparing(
                "AR world tracking is not supported on this device"
            )
            return
        }

        let configuration = ARWorldTrackingConfiguration()
        configuration.worldAlignment = .gravity
        configuration.planeDetection = []
        configuration.isAutoFocusEnabled = true
        if let videoFormat = preferredVideoFormat() {
            configuration.videoFormat = videoFormat
        }

        session.run(configuration, options: [.resetTracking, .removeExistingAnchors])
        isSessionRunning = true
        reportTrackingStatus("AR session starting")
    }

    private func failRecordingIfFirstFrameDoesNotArrive(
        for context: ExperimentRecordingContext
    ) {
        arQueue.asyncAfter(deadline: .now() + 5.0) { [weak self, weak context] in
            guard
                let self,
                let context,
                self.activeRecordingContext === context
            else { return }

            context.videoRecorder.failPreparing("Waiting for AR tracking")
        }
    }

    private func pauseSessionIfNeeded() {
        guard isSessionRunning else { return }
        session.pause()
        isSessionRunning = false
    }

    private func preferredVideoFormat() -> ARConfiguration.VideoFormat? {
        let formats = ARWorldTrackingConfiguration.supportedVideoFormats
        guard !formats.isEmpty else { return nil }

        let targetFPS = videoConfiguration.clampedFrameRate
        let targetResolution = videoConfiguration.resolution.dimensions
        let targetPixels = targetResolution.width * targetResolution.height

        return formats.min { lhs, rhs in
            let lhsScore = Self.videoFormatScore(lhs, targetFPS: targetFPS, targetPixels: targetPixels)
            let rhsScore = Self.videoFormatScore(rhs, targetFPS: targetFPS, targetPixels: targetPixels)
            return lhsScore < rhsScore
        }
    }

    private func connectUDP(port: NWEndpoint.Port? = nil) {
        guard connection == nil else { return }

        let parameters = NWParameters.udp
        parameters.allowLocalEndpointReuse = true
        parameters.includePeerToPeer = true

        let connection = NWConnection(
            host: host,
            port: port ?? self.port,
            using: parameters
        )

        connection.stateUpdateHandler = { [weak self] state in
            guard let self else { return }

            switch state {
            case .ready:
                self.isConnectionReady = true
            case .failed(let error):
                self.isConnectionReady = false
                self.onError?(error)
            case .cancelled:
                self.isConnectionReady = false
            default:
                break
            }
        }

        self.connection = connection
        connection.start(queue: networkQueue)
    }

    private func disconnectUDP() {
        isConnectionReady = false
        connection?.cancel()
        connection = nil
    }

    func session(_ session: ARSession, cameraDidChangeTrackingState camera: ARCamera) {
        reportTrackingStatus(Self.trackingDescription(for: camera.trackingState))
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        arQueue.async { [weak self] in
            guard let self else { return }
            self.isSessionRunning = false
            self.videoSender.stopStreaming()
            self.ultraWideVideoSender.stopStreaming()
            self.activeRecordingContext?.videoRecorder.cancelRecording(
                reason: "AR session failed: \(error.localizedDescription)"
            )
            self.activeRecordingContext?.ultraWideVideoRecorder.cancelRecording(
                reason: "AR session failed: \(error.localizedDescription)"
            )
            self.reportTrackingStatus("AR failed: \(error.localizedDescription)")
            self.recoverStreamingAfterSessionFailure()
        }
    }

    func sessionWasInterrupted(_ session: ARSession) {
        arQueue.async { [weak self] in
            guard let self else { return }
            self.isSessionRunning = false
            self.videoSender.stopStreaming()
            self.ultraWideVideoSender.stopStreaming()
            self.activeRecordingContext?.videoRecorder.cancelRecording(
                reason: "AR session interrupted"
            )
            self.activeRecordingContext?.ultraWideVideoRecorder.cancelRecording(
                reason: "AR session interrupted"
            )
            self.reportTrackingStatus("AR interrupted")
        }
    }

    func sessionInterruptionEnded(_ session: ARSession) {
        arQueue.async { [weak self] in
            guard let self else { return }
            self.isSessionRunning = false
            if self.isStreamingEnabled || self.isRecordingEnabled {
                if self.isStreamingEnabled {
                    self.videoSender.startStreaming()
                    self.ultraWideVideoSender.startStreaming()
                }
                self.startSessionIfNeeded()
            } else {
                self.reportTrackingStatus("AR interruption ended")
            }
        }
    }

    private func recoverStreamingAfterSessionFailure() {
        arQueue.asyncAfter(deadline: .now() + 0.75) { [weak self] in
            guard let self, self.isStreamingEnabled, !self.isSessionRunning else { return }
            self.videoSender.startStreaming()
            self.ultraWideVideoSender.startStreaming()
            self.startSessionIfNeeded()
        }
    }

    private func processFrame(
        transform: simd_float4x4,
        pixelBuffer: CVPixelBuffer,
        frameTimestamp: TimeInterval,
        videoCalibration: VideoCameraCalibration,
        ultraWideFrame: UltraWideFrameData?
    ) {
        if shouldResetOriginOnNextFrame || originTransform == nil {
            originTransform = transform
            shouldResetOriginOnNextFrame = false
        }

        let relativeTransform = originTransform.map {
            simd_mul(simd_inverse($0), transform)
        } ?? transform

        let sample = makePoseSample(from: relativeTransform, frameTimestamp: frameTimestamp)
        let presentationTime = CMTime(seconds: frameTimestamp, preferredTimescale: 600)

        onPoseProduced?(sample)

        DispatchQueue.main.async { [sample, weak self] in
            self?.onSampleUpdated?(sample)
        }

        let recordingContext = activeRecordingContext
        if let recordingContext {
            recordingContext.poseSessionRecorder.append(
                sample: PoseSampleRecord(
                    sequence: sample.sequence,
                    senderTimestamp: sample.timestamp,
                    frameTimestamp: frameTimestamp,
                    position: sample.position,
                    orientation: sample.orientation
                )
            )
        }

        if let recordingContext {
            recordingContext.poseSessionRecorder.markVideoStarted(frameTimestamp: frameTimestamp)
            recordingContext.videoRecorder.appendFrame(
                pixelBuffer: pixelBuffer,
                at: presentationTime
            )
        }

        if
            let ultraWideFrame,
            ultraWideFrame.timestamp > lastUltraWideFrameTimestamp + 0.000_001
        {
            lastUltraWideFrameTimestamp = ultraWideFrame.timestamp
            let ultraWidePresentationTime = CMTime(
                seconds: ultraWideFrame.timestamp,
                preferredTimescale: 600
            )

            if let recordingContext {
                recordingContext.poseSessionRecorder.markUltraWideVideoStarted(
                    frameTimestamp: ultraWideFrame.timestamp
                )
                recordingContext.ultraWideVideoRecorder.appendFrame(
                    pixelBuffer: ultraWideFrame.pixelBuffer,
                    at: ultraWidePresentationTime
                )
            }

            if isStreamingEnabled && ultraWideVideoConfiguration.isEnabled {
                let ultraWideCaptureUnixTime = sample.timestamp + ultraWideFrame.timestamp - frameTimestamp
                ultraWideVideoSender.appendFrame(
                    pixelBuffer: ultraWideFrame.pixelBuffer,
                    presentationTimeStamp: ultraWidePresentationTime,
                    captureTimestamp: ultraWideCaptureUnixTime,
                    cameraCalibration: ultraWideFrame.calibration
                )
            }
        }

        if isStreamingEnabled && videoConfiguration.isEnabled {
            videoSender.appendFrame(
                pixelBuffer: pixelBuffer,
                presentationTimeStamp: presentationTime,
                captureTimestamp: sample.timestamp,
                cameraCalibration: videoCalibration
            )
        }

        guard isStreamingEnabled else { return }
        guard frameTimestamp - lastSentTimestamp >= minimumSendInterval * 0.95 else { return }

        let payload = encode(sample)
        lastSentTimestamp = frameTimestamp

        networkQueue.async { [weak self] in
            guard let self, self.isConnectionReady, let connection = self.connection else { return }

            connection.send(
                content: payload,
                contentContext: .defaultMessage,
                isComplete: true,
                completion: .contentProcessed { [weak self] error in
                    if let error {
                        self?.onError?(error)
                        return
                    }

                    self?.onSampleSent?(sample)
                }
            )
        }
    }

    func discardRecording() {
        arQueue.async { [weak self] in
            guard let self, let context = self.activeRecordingContext else { return }

            context.isDiscarded = true
            self.activeRecordingContext = nil
            self.isRecordingEnabled = false
            context.videoRecorder.discardRecording()
            context.ultraWideVideoRecorder.discardRecording()
            self.recordingFinalizationQueue.async {
                context.discardSessionAfterMagneticWrites()
            }

            if !self.isStreamingEnabled {
                self.pauseSessionIfNeeded()
            }
        }
    }

    private func configureRecordingCallbacks(for context: ExperimentRecordingContext) {
        context.videoRecorder.onStatusChange = { [weak self, weak context] status in
            self?.arQueue.async { [weak self, weak context] in
                guard let self, let context else { return }
                if context.isDiscarded {
                    guard case .discarded = status else { return }
                }
                let isCurrentContext = self.activeRecordingContext === context

                if case .failed(let message) = status, isCurrentContext {
                    self.activeRecordingContext = nil
                    self.isRecordingEnabled = false
                    context.ultraWideVideoRecorder.cancelRecording(
                        reason: "Primary 1x recording failed"
                    )
                    context.poseSessionRecorder.markExperimentStopped(
                        unixTime: Date().timeIntervalSince1970,
                        monotonicTime: ProcessInfo.processInfo.systemUptime
                    )
                    context.poseSessionRecorder.addWarning("1x recording failed: \(message)")
                    self.recordingFinalizationQueue.async {
                        context.finishSessionAfterMagneticWrites()
                    }
                    if !self.isStreamingEnabled {
                        self.pauseSessionIfNeeded()
                    }
                }

                guard isCurrentContext || self.activeRecordingContext == nil else { return }
                if case .saved = status {
                    return
                }
                DispatchQueue.main.async { [weak self] in
                    self?.onRecordingStatusChange?(status)
                }
            }
        }

        context.ultraWideVideoRecorder.onStatusChange = { [weak self, weak context] status in
            self?.arQueue.async { [weak self, weak context] in
                guard let self, let context else { return }
                if context.isDiscarded {
                    guard case .discarded = status else { return }
                }
                guard
                    self.activeRecordingContext === context ||
                    self.activeRecordingContext == nil
                else { return }

                DispatchQueue.main.async { [weak self] in
                    self?.onUltraWideRecordingStatusChange?(status)
                }
            }
        }

        context.poseSessionRecorder.onSessionSaved = { [weak self] artifact in
            DispatchQueue.main.async { [weak self] in
                self?.onCaptureSessionSaved?(artifact)
            }
        }
    }

    private func makePoseSample(from transform: simd_float4x4, frameTimestamp: TimeInterval) -> PoseSample {
        let positionYUp = SIMD3(
            transform.columns.3.x,
            transform.columns.3.y,
            transform.columns.3.z
        )
        let rotationMatrix = simd_float3x3(
            SIMD3(transform.columns.0.x, transform.columns.0.y, transform.columns.0.z),
            SIMD3(transform.columns.1.x, transform.columns.1.y, transform.columns.1.z),
            SIMD3(transform.columns.2.x, transform.columns.2.y, transform.columns.2.z)
        )
        let orientationYUp = simd_quatf(rotationMatrix)

        let convertedPosition: SIMD3<Float>
        let convertedOrientation: simd_quatf

        switch coordinateSystem {
        case .arkitYUp:
            convertedPosition = positionYUp
            convertedOrientation = Self.normalizedQuaternion(orientationYUp)
        case .zUpRightHanded:
            convertedPosition = SIMD3(positionYUp.x, -positionYUp.z, positionYUp.y)
            convertedOrientation = Self.convertRotationToZUp(rotationMatrix, alignment: zUpAlignment)
        }

        sequenceNumber &+= 1

        return PoseSample(
            sequence: sequenceNumber,
            timestamp: Date().timeIntervalSince1970,
            frameTimestamp: frameTimestamp,
            position: convertedPosition,
            orientation: convertedOrientation
        )
    }

    private func encode(_ sample: PoseSample) -> Data {
        switch payloadEncoding {
        case .binaryFloat32:
            return sample.binaryData
        case .csvUTF8:
            return Data(sample.csvString.utf8)
        }
    }

    private func reportTrackingStatus(_ message: String) {
        DispatchQueue.main.async { [weak self] in
            self?.onTrackingStatusChange?(message)
        }
    }

    private static func videoFormatScore(
        _ format: ARConfiguration.VideoFormat,
        targetFPS: Int,
        targetPixels: Int
    ) -> Int {
        let fpsDelta = abs(format.framesPerSecond - targetFPS)
        let pixels = Int(format.imageResolution.width) * Int(format.imageResolution.height)
        let pixelDelta = abs(pixels - targetPixels)
        return fpsDelta * 10_000_000 + pixelDelta
    }

    private static func trackingDescription(for state: ARCamera.TrackingState) -> String {
        switch state {
        case .normal:
            return "Tracking normal"
        case .notAvailable:
            return "Tracking unavailable"
        case .limited(let reason):
            switch reason {
            case .initializing:
                return "Tracking limited: initializing"
            case .excessiveMotion:
                return "Tracking limited: excessive motion"
            case .insufficientFeatures:
                return "Tracking limited: insufficient features"
            case .relocalizing:
                return "Tracking limited: relocalizing"
            @unknown default:
                return "Tracking limited"
            }
        }
    }

    private static func normalizedQuaternion(_ quaternion: simd_quatf) -> simd_quatf {
        let vector = quaternion.vector
        let norm = simd_length(vector)
        guard norm.isFinite, norm > 1e-6 else {
            return simd_quatf(angle: 0, axis: SIMD3<Float>(1, 0, 0))
        }

        return simd_quatf(vector: vector / norm)
    }

    private static func convertRotationToZUp(_ rotationYUp: simd_float3x3, alignment: simd_quatf) -> simd_quatf {
        let alignmentMatrix = simd_float3x3(alignment)
        let converted = simd_mul(alignmentMatrix, simd_mul(rotationYUp, simd_transpose(alignmentMatrix)))
        return normalizedQuaternion(simd_quatf(converted))
    }
}
