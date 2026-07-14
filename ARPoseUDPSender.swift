import Foundation
import ARKit
import Network
import CoreMedia
import simd

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

    private let coordinateSystem: CoordinateSystem
    private let payloadEncoding: PayloadEncoding
    private let minimumSendInterval = 1.0 / 60.0
    private let arQueue = DispatchQueue(label: "umi.pose.ar", qos: .userInitiated)
    private let networkQueue = DispatchQueue(label: "umi.pose.udp", qos: .userInitiated)
    private let zUpAlignment = simd_quatf(angle: .pi / 2, axis: SIMD3<Float>(1, 0, 0))
    private let videoRecorder = ARSessionVideoRecorder()
    private let poseSessionRecorder = PoseDataSessionRecorder()
    private let videoSender: ARLowLatencyVideoSender

    private var host: NWEndpoint.Host
    private var port: NWEndpoint.Port
    private var videoConfiguration: LowLatencyVideoConfiguration
    private var connection: NWConnection?
    private var originTransform: simd_float4x4?
    private var lastSentTimestamp: TimeInterval = -.infinity
    private var sequenceNumber: UInt32 = 0
    private var shouldResetOriginOnNextFrame = true
    private var isConnectionReady = false
    private var isSessionRunning = false
    private var isStreamingEnabled = false
    private var isRecordingEnabled = false
    private var hasPoseCaptureSession = false
    private var recordingAttemptID = UUID()

    init?(
        hostIP: String,
        port: UInt16 = 5555,
        coordinateSystem: CoordinateSystem = .zUpRightHanded,
        payloadEncoding: PayloadEncoding = .binaryFloat32,
        videoConfiguration: LowLatencyVideoConfiguration = .defaults
    ) {
        guard let udpPort = NWEndpoint.Port(rawValue: port) else {
            return nil
        }

        self.host = NWEndpoint.Host(hostIP)
        self.port = udpPort
        self.coordinateSystem = coordinateSystem
        self.payloadEncoding = payloadEncoding
        self.videoConfiguration = videoConfiguration
        self.videoSender = ARLowLatencyVideoSender(configuration: videoConfiguration)

        super.init()

        session.delegate = self
        videoSender.onStateChange = { [weak self] state in
            self?.onVideoStateChange?(state)
        }
        videoSender.onStatsChange = { [weak self] stats in
            self?.arQueue.async {
                guard let self, self.isRecordingEnabled else { return }
                self.poseSessionRecorder.appendSenderTransport(
                    stats: stats,
                    sampleUnixTime: Date().timeIntervalSince1970
                )
            }
            self?.onVideoStatsChange?(stats)
        }
        videoSender.onError = { [weak self] error in
            self?.onError?(error)
        }
        videoRecorder.onStatusChange = { [weak self] status in
            self?.arQueue.async {
                guard let self else { return }

                if case .saved(let url) = status {
                    self.poseSessionRecorder.attachVideo(url: url)
                }

                if status.isTerminal {
                    self.isRecordingEnabled = false
                    self.finishPoseSessionIfNeeded()
                    if !self.isStreamingEnabled {
                        self.pauseSessionIfNeeded()
                    }
                }
            }

            DispatchQueue.main.async {
                self?.onRecordingStatusChange?(status)
            }
        }
        poseSessionRecorder.onSessionSaved = { [weak self] artifact in
            DispatchQueue.main.async {
                self?.onCaptureSessionSaved?(artifact)
            }
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

    func resetOrigin() {
        arQueue.async { [weak self] in
            self?.originTransform = nil
            self?.shouldResetOriginOnNextFrame = true
        }
    }

    func appendMagneticSample(_ sample: MagneticSensorSample) {
        arQueue.async { [weak self] in
            guard let self, self.isRecordingEnabled else { return }
            self.ensurePoseSession()
            self.poseSessionRecorder.append(magneticSample: sample)
        }
    }

    func startRecording(
        experimentID: UUID,
        startUnixTime: TimeInterval,
        startMonotonicTime: TimeInterval
    ) {
        arQueue.async { [weak self] in
            guard let self else { return }
            guard !self.isRecordingEnabled else { return }

            self.isRecordingEnabled = true
            self.recordingAttemptID = UUID()
            let attemptID = self.recordingAttemptID

            if self.hasPoseCaptureSession {
                self.finishPoseSessionIfNeeded()
            }

            self.poseSessionRecorder.startSession(
                experimentID: experimentID,
                startUnixTime: startUnixTime,
                startMonotonicTime: startMonotonicTime
            )
            self.ensurePoseSession()
            self.videoRecorder.startRecording()
            self.startSessionIfNeeded()
            self.failRecordingIfFirstFrameDoesNotArrive(attemptID: attemptID)
        }
    }

    func stopRecording(stopUnixTime: TimeInterval, stopMonotonicTime: TimeInterval) {
        arQueue.async { [weak self] in
            guard let self else { return }
            self.isRecordingEnabled = false
            self.recordingAttemptID = UUID()
            self.poseSessionRecorder.markExperimentStopped(
                unixTime: stopUnixTime,
                monotonicTime: stopMonotonicTime
            )
            self.videoRecorder.stopRecording { [weak self] _ in
                guard let self else { return }

                self.arQueue.async {
                    if !self.isStreamingEnabled {
                        self.pauseSessionIfNeeded()
                    }
                }
            }
        }
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard case .normal = frame.camera.trackingState else { return }
        let cameraTransform = frame.camera.transform
        let frameTimestamp = frame.timestamp
        let capturedImage = frame.capturedImage

        arQueue.async { [weak self] in
            self?.processFrame(
                transform: cameraTransform,
                pixelBuffer: capturedImage,
                frameTimestamp: frameTimestamp
            )
        }
    }

    private func startSessionIfNeeded() {
        guard !isSessionRunning else { return }
        guard ARWorldTrackingConfiguration.isSupported else {
            videoRecorder.failPreparing("AR world tracking is not supported on this device")
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

    private func failRecordingIfFirstFrameDoesNotArrive(attemptID: UUID) {
        arQueue.asyncAfter(deadline: .now() + 5.0) { [weak self] in
            guard let self else { return }
            guard self.isRecordingEnabled, self.recordingAttemptID == attemptID else { return }

            self.videoRecorder.failPreparing("Waiting for AR tracking")
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
            self.videoRecorder.cancelRecording(reason: "AR session failed: \(error.localizedDescription)")
            self.reportTrackingStatus("AR failed: \(error.localizedDescription)")
        }
    }

    func sessionWasInterrupted(_ session: ARSession) {
        arQueue.async { [weak self] in
            guard let self else { return }
            self.isSessionRunning = false
            self.videoSender.stopStreaming()
            self.videoRecorder.cancelRecording(reason: "AR session interrupted")
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
                }
                self.startSessionIfNeeded()
            } else {
                self.reportTrackingStatus("AR interruption ended")
            }
        }
    }

    private func processFrame(transform: simd_float4x4, pixelBuffer: CVPixelBuffer, frameTimestamp: TimeInterval) {
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

        if isRecordingEnabled {
            ensurePoseSession()
            poseSessionRecorder.append(
                sample: PoseSampleRecord(
                    sequence: sample.sequence,
                    senderTimestamp: sample.timestamp,
                    frameTimestamp: frameTimestamp,
                    position: sample.position,
                    orientation: sample.orientation
                )
            )
        }

        if isRecordingEnabled {
            poseSessionRecorder.markVideoStarted(frameTimestamp: frameTimestamp)
            videoRecorder.appendFrame(pixelBuffer: pixelBuffer, at: presentationTime)
        }

        if isStreamingEnabled && videoConfiguration.isEnabled {
            videoSender.appendFrame(
                pixelBuffer: pixelBuffer,
                presentationTimeStamp: presentationTime,
                captureTimestamp: sample.timestamp
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

    private func ensurePoseSession() {
        guard !hasPoseCaptureSession else { return }
        poseSessionRecorder.startSessionIfNeeded()
        hasPoseCaptureSession = true
    }

    private func finishPoseSessionIfNeeded() {
        guard hasPoseCaptureSession else { return }
        poseSessionRecorder.finishSession()
        hasPoseCaptureSession = false
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
