import Foundation
import ARKit
import simd

struct CameraPoseSample {
    let timestamp: TimeInterval
    let position: SIMD3<Float>
    let orientation: simd_quatf

    static let zero = CameraPoseSample(
        timestamp: 0,
        position: .zero,
        orientation: simd_quatf(angle: 0, axis: SIMD3<Float>(1, 0, 0))
    )

    var formattedString: String {
        String(
            format: "%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f",
            locale: Locale(identifier: "en_US_POSIX"),
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
}

final class ARPositionTracker: NSObject, ARSessionDelegate {
    let session = ARSession()

    private(set) var currentPosition: SIMD3<Float> = .zero
    private(set) var currentOrientation = CameraPoseSample.zero.orientation
    private(set) var latestSample = CameraPoseSample.zero
    private(set) var latestFormattedSample = CameraPoseSample.zero.formattedString

    var onPositionUpdate: ((SIMD3<Float>) -> Void)?
    var onPoseUpdate: ((CameraPoseSample) -> Void)?
    var onFormattedSampleReady: ((String) -> Void)?

    private let zUpAlignment = simd_quatf(angle: .pi / 2, axis: SIMD3<Float>(1, 0, 0))
    private var originTransform: simd_float4x4?
    private var packetTimer: Timer?

    override init() {
        super.init()
        session.delegate = self
        startPacketTimer()
        startSession()
    }

    deinit {
        packetTimer?.invalidate()
        session.pause()
    }

    func resetOrigin() {
        restartSession(resetTracking: true)
        originTransform = nil
        publish(sample: .zero)
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        let cameraTransform = frame.camera.transform
        if originTransform == nil {
            originTransform = cameraTransform
        }

        // Keep ARKit's gravity-aligned world axes; only shift the origin position.
        var relativeTransform = cameraTransform
        if let originTransform {
            relativeTransform.columns.3.x -= originTransform.columns.3.x
            relativeTransform.columns.3.y -= originTransform.columns.3.y
            relativeTransform.columns.3.z -= originTransform.columns.3.z
        }

        let sample = makePoseSample(from: relativeTransform, timestamp: frame.timestamp)

        DispatchQueue.main.async { [sample, weak self] in
            self?.publish(sample: sample)
        }
    }

    private func startSession() {
        restartSession(resetTracking: false)
    }

    private func restartSession(resetTracking: Bool) {
        guard ARWorldTrackingConfiguration.isSupported else { return }

        let configuration = ARWorldTrackingConfiguration()
        configuration.planeDetection = []
        configuration.worldAlignment = .gravity
        configuration.isAutoFocusEnabled = true

        let options: ARSession.RunOptions = resetTracking
            ? [.resetTracking, .removeExistingAnchors]
            : []

        session.run(configuration, options: options)
    }

    private func startPacketTimer() {
        DispatchQueue.main.async { [weak self] in
            guard let self, self.packetTimer == nil else { return }

            let timer = Timer(timeInterval: 1.0 / 30.0, repeats: true) { [weak self] _ in
                guard let self else { return }
                self.onFormattedSampleReady?(self.latestFormattedSample)
            }

            timer.tolerance = 1.0 / 120.0
            RunLoop.main.add(timer, forMode: .common)
            self.packetTimer = timer
        }
    }

    private func publish(sample: CameraPoseSample) {
        currentPosition = sample.position
        currentOrientation = sample.orientation
        latestSample = sample
        latestFormattedSample = sample.formattedString

        onPositionUpdate?(sample.position)
        onPoseUpdate?(sample)
    }

    private func makePoseSample(from transform: simd_float4x4, timestamp: TimeInterval) -> CameraPoseSample {
        let positionYUp = SIMD3(
            transform.columns.3.x,
            transform.columns.3.y,
            transform.columns.3.z
        )
        let orientationYUp = simd_quatf(simd_float3x3(transform))

        return CameraPoseSample(
            timestamp: timestamp,
            position: convertPositionToZUp(positionYUp),
            orientation: convertQuaternionToZUp(orientationYUp)
        )
    }

    private func convertPositionToZUp(_ position: SIMD3<Float>) -> SIMD3<Float> {
        SIMD3(position.x, -position.z, position.y)
    }

    private func convertQuaternionToZUp(_ quaternion: simd_quatf) -> simd_quatf {
        let alignmentMatrix = simd_float3x3(zUpAlignment)
        let rotationYUp = simd_float3x3(quaternion)
        let converted = simd_mul(alignmentMatrix, simd_mul(rotationYUp, simd_transpose(alignmentMatrix)))
        return normalizedQuaternion(simd_quatf(converted))
    }

    private func normalizedQuaternion(_ quaternion: simd_quatf) -> simd_quatf {
        let vector = quaternion.vector
        let norm = simd_length(vector)
        guard norm.isFinite, norm > 1e-6 else {
            return simd_quatf(angle: 0, axis: SIMD3<Float>(1, 0, 0))
        }

        return simd_quatf(vector: vector / norm)
    }
}
