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
    var onSampleSent: ((PoseSample) -> Void)?
    var onError: ((Error) -> Void)?
    var onRecordingStatusChange: ((VideoRecordingStatus) -> Void)?
    var onCaptureSessionSaved: ((PoseCaptureArtifact) -> Void)?

    private let coordinateSystem: CoordinateSystem
    private let payloadEncoding: PayloadEncoding
    private let minimumSendInterval = 1.0 / 60.0
    private let arQueue = DispatchQueue(label: "umi.pose.ar", qos: .userInitiated)
    private let networkQueue = DispatchQueue(label: "umi.pose.udp", qos: .userInitiated)
    private let zUpAlignment = simd_quatf(angle: .pi / 2, axis: SIMD3<Float>(1, 0, 0))
    private let videoRecorder = ARSessionVideoRecorder()
    private let poseSessionRecorder = PoseDataSessionRecorder()

    private var host: NWEndpoint.Host
    private var port: NWEndpoint.Port
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

    init?(
        hostIP: String,
        port: UInt16 = 5555,
        coordinateSystem: CoordinateSystem = .zUpRightHanded,
        payloadEncoding: PayloadEncoding = .binaryFloat32
    ) {
        guard let udpPort = NWEndpoint.Port(rawValue: port) else {
            return nil
        }

        self.host = NWEndpoint.Host(hostIP)
        self.port = udpPort
        self.coordinateSystem = coordinateSystem
        self.payloadEncoding = payloadEncoding

        super.init()

        session.delegate = self
        videoRecorder.onStatusChange = { [weak self] status in
            self?.arQueue.async {
                if case .saved(let url) = status {
                    self?.poseSessionRecorder.attachVideo(url: url)
                }

                if status.isTerminal {
                    self?.isRecordingEnabled = false
                    self?.finishPoseSessionIfNeeded()
                    if self?.isStreamingEnabled == false {
                        self?.pauseSessionIfNeeded()
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
        isStreamingEnabled = true

        networkQueue.async { [weak self] in
            self?.connectUDP()
        }

        arQueue.async { [weak self] in
            self?.startSessionIfNeeded()
        }
    }

    func startPreview() {
        arQueue.async { [weak self] in
            self?.startSessionIfNeeded()
        }
    }

    func stop() {
        isStreamingEnabled = false

        networkQueue.async { [weak self] in
            self?.disconnectUDP()
        }

        arQueue.async { [weak self] in
            guard let self else { return }
            if !self.isRecordingEnabled {
                self.finishPoseSessionIfNeeded()
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

    func resetOrigin() {
        arQueue.async { [weak self] in
            self?.originTransform = nil
            self?.shouldResetOriginOnNextFrame = true
        }
    }

    func startRecording() {
        isRecordingEnabled = true

        arQueue.async { [weak self] in
            guard let self else { return }
            self.startSessionIfNeeded()

            if self.hasPoseCaptureSession {
                self.finishPoseSessionIfNeeded()
            }

            self.ensurePoseSession()
            self.videoRecorder.startRecording()
        }
    }

    func stopRecording() {
        isRecordingEnabled = false

        arQueue.async { [weak self] in
            guard let self else { return }
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
        guard ARWorldTrackingConfiguration.isSupported else { return }

        let configuration = ARWorldTrackingConfiguration()
        configuration.worldAlignment = .gravity
        configuration.planeDetection = []
        configuration.isAutoFocusEnabled = true
        configuration.videoFormat = preferredVideoFormat()

        session.run(configuration, options: [.resetTracking, .removeExistingAnchors])
        isSessionRunning = true
    }

    private func pauseSessionIfNeeded() {
        guard isSessionRunning else { return }
        session.pause()
        isSessionRunning = false
    }

    private func preferredVideoFormat() -> ARConfiguration.VideoFormat {
        let formats = ARWorldTrackingConfiguration.supportedVideoFormats

        let exact60 = formats
            .filter { $0.framesPerSecond == 60 }
            .sorted {
                let lhsPixels = $0.imageResolution.width * $0.imageResolution.height
                let rhsPixels = $1.imageResolution.width * $1.imageResolution.height
                return lhsPixels < rhsPixels
            }

        if let best60 = exact60.first {
            return best60
        }

        let bestAvailableFPS = formats.map(\.framesPerSecond).max() ?? 60
        let fallback = formats
            .filter { $0.framesPerSecond == bestAvailableFPS }
            .sorted {
                let lhsPixels = $0.imageResolution.width * $0.imageResolution.height
                let rhsPixels = $1.imageResolution.width * $1.imageResolution.height
                return lhsPixels < rhsPixels
            }

        return fallback.first ?? formats[0]
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

    private func processFrame(transform: simd_float4x4, pixelBuffer: CVPixelBuffer, frameTimestamp: TimeInterval) {
        if shouldResetOriginOnNextFrame || originTransform == nil {
            originTransform = transform
            shouldResetOriginOnNextFrame = false
        }

        let relativeTransform = originTransform.map {
            simd_mul(simd_inverse($0), transform)
        } ?? transform

        let sample = makePoseSample(from: relativeTransform)
        let presentationTime = CMTime(seconds: frameTimestamp, preferredTimescale: 600)

        DispatchQueue.main.async { [sample, weak self] in
            self?.onSampleUpdated?(sample)
        }

        if isStreamingEnabled || isRecordingEnabled {
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

    private func makePoseSample(from transform: simd_float4x4) -> PoseSample {
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
            convertedOrientation = simd_quatf(vector: simd_normalize(orientationYUp.vector))
        case .zUpRightHanded:
            convertedPosition = SIMD3(positionYUp.x, -positionYUp.z, positionYUp.y)
            let aligned = zUpAlignment * orientationYUp
            convertedOrientation = simd_quatf(vector: simd_normalize(aligned.vector))
        }

        sequenceNumber &+= 1

        return PoseSample(
            sequence: sequenceNumber,
            timestamp: Date().timeIntervalSince1970,
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
}
