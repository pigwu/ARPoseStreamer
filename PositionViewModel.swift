import Foundation
import Combine
import simd

@MainActor
final class PositionViewModel: ObservableObject {
    @Published var hostIP = "192.168.1.10"
    @Published private(set) var position: SIMD3<Float> = .zero
    @Published private(set) var sendStatus = "Idle"
    @Published private(set) var latestPacketSummary = "No packets yet"
    @Published private(set) var isSending = false

    private var sender: ARPoseUDPSender?

    init() {
        configureSender()
    }

    func startSending() {
        if sender == nil {
            configureSender()
        } else {
            sender?.updateDestination(hostIP: hostIP)
        }

        sender?.start()
        isSending = true
        sendStatus = "Streaming to \(hostIP):5555"
    }

    func stopSending() {
        sender?.stop()
        isSending = false
        sendStatus = "Stopped"
    }

    func resetOrigin() {
        sender?.resetOrigin()
    }

    func formattedValue(for value: Float) -> String {
        String(format: "%.3f m", value)
    }

    private func configureSender() {
        let newSender = ARPoseUDPSender(hostIP: hostIP)
        sender = newSender

        newSender?.onSampleSent = { [weak self] sample in
            Task { @MainActor [weak self] in
                self?.position = sample.position
                self?.latestPacketSummary = String(
                    format: "#%u  t=%.3f  x=%.3f  y=%.3f  z=%.3f",
                    sample.sequence,
                    sample.timestamp,
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
    }
}
