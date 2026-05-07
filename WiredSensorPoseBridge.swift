import Foundation
import ExternalAccessory
import Network
import simd

struct SensorPoseSample {
    let sequence: UInt32
    let sensorTimestamp: TimeInterval?
    let receivedTimestamp: TimeInterval
    let position: SIMD3<Float>
    let orientation: simd_quatf

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
        var timestampLE = receivedTimestamp.bitPattern.littleEndian

        withUnsafeBytes(of: &sequenceLE) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &timestampLE) { data.append(contentsOf: $0) }
        vectorScalars.withUnsafeBytes { data.append(contentsOf: $0) }

        return data
    }
}

final class SensorPoseLineParser {
    private var nextSequence: UInt32 = 0

    func parse(line: String, receivedTimestamp: TimeInterval = Date().timeIntervalSince1970) -> SensorPoseSample? {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !trimmed.hasPrefix("#") else { return nil }

        var tokens = trimmed
            .split { character in
                character == "," || character == ";" || character == " " || character == "\t"
            }
            .map(String.init)

        if let first = tokens.first, Double(first) == nil {
            tokens.removeFirst()
        }

        let values = tokens.compactMap(Double.init)
        guard values.count == tokens.count else { return nil }

        switch values.count {
        case 9:
            return makeSample(
                sequence: sequence(from: values[0]),
                sensorTimestamp: values[1],
                receivedTimestamp: receivedTimestamp,
                fields: Array(values[2...8])
            )
        case 8:
            return makeSample(
                sequence: autoSequence(),
                sensorTimestamp: values[0],
                receivedTimestamp: receivedTimestamp,
                fields: Array(values[1...7])
            )
        case 7:
            return makeSample(
                sequence: autoSequence(),
                sensorTimestamp: nil,
                receivedTimestamp: receivedTimestamp,
                fields: values
            )
        default:
            return nil
        }
    }

    private func makeSample(
        sequence: UInt32,
        sensorTimestamp: TimeInterval?,
        receivedTimestamp: TimeInterval,
        fields: [Double]
    ) -> SensorPoseSample? {
        guard fields.count == 7 else { return nil }

        let position = SIMD3<Float>(Float(fields[0]), Float(fields[1]), Float(fields[2]))
        let rawQuaternion = SIMD4<Float>(Float(fields[3]), Float(fields[4]), Float(fields[5]), Float(fields[6]))
        let norm = simd_length(rawQuaternion)
        guard norm > 1e-6 else { return nil }

        return SensorPoseSample(
            sequence: sequence,
            sensorTimestamp: sensorTimestamp,
            receivedTimestamp: receivedTimestamp,
            position: position,
            orientation: simd_quatf(vector: rawQuaternion / norm)
        )
    }

    private func autoSequence() -> UInt32 {
        nextSequence &+= 1
        return nextSequence
    }

    private func sequence(from value: Double) -> UInt32 {
        UInt32(min(max(value, 0), Double(UInt32.max)))
    }
}

enum WiredSensorError: LocalizedError {
    case missingProtocolString
    case noMatchingAccessory(String)
    case sessionOpenFailed(String)

    var errorDescription: String? {
        switch self {
        case .missingProtocolString:
            return "External accessory protocol is empty."
        case .noMatchingAccessory(let protocolString):
            return "No connected accessory exposes protocol \(protocolString)."
        case .sessionOpenFailed(let protocolString):
            return "Could not open an ExternalAccessory session for \(protocolString)."
        }
    }
}

final class WiredSensorSerialReceiver: NSObject, StreamDelegate {
    var onSampleReceived: ((SensorPoseSample) -> Void)?
    var onStatusChanged: ((String) -> Void)?
    var onError: ((Error) -> Void)?

    private let parser = SensorPoseLineParser()
    private var accessoryProtocol = ""
    private var accessorySession: EASession?
    private var inputStream: InputStream?
    private var outputStream: OutputStream?
    private var readBuffer = Data()
    private var isRunning = false
    private var lastFlowStatusTime: TimeInterval = 0

    func start(protocolString: String) {
        let trimmedProtocol = protocolString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedProtocol.isEmpty else {
            onError?(WiredSensorError.missingProtocolString)
            return
        }

        accessoryProtocol = trimmedProtocol
        isRunning = true

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(accessoryListChanged),
            name: .EAAccessoryDidConnect,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(accessoryListChanged),
            name: .EAAccessoryDidDisconnect,
            object: nil
        )
        EAAccessoryManager.shared().registerForLocalNotifications()

        openMatchingAccessory()
    }

    func stop() {
        isRunning = false
        NotificationCenter.default.removeObserver(self)
        closeSession()
        onStatusChanged?("Sensor idle")
    }

    func stream(_ aStream: Stream, handle eventCode: Stream.Event) {
        switch eventCode {
        case .hasBytesAvailable:
            readAvailableBytes()
        case .errorOccurred:
            if let error = aStream.streamError {
                onError?(error)
            }
        case .endEncountered:
            closeSession()
            if isRunning {
                onStatusChanged?("Sensor disconnected")
                openMatchingAccessory()
            }
        default:
            break
        }
    }

    @objc private func accessoryListChanged() {
        guard isRunning else { return }
        openMatchingAccessory()
    }

    private func openMatchingAccessory() {
        closeSession()

        guard let accessory = EAAccessoryManager.shared().connectedAccessories.first(where: {
            $0.protocolStrings.contains(accessoryProtocol)
        }) else {
            onStatusChanged?("Waiting for wired sensor")
            onError?(WiredSensorError.noMatchingAccessory(accessoryProtocol))
            return
        }

        guard let session = EASession(accessory: accessory, forProtocol: accessoryProtocol) else {
            onError?(WiredSensorError.sessionOpenFailed(accessoryProtocol))
            return
        }

        accessorySession = session
        inputStream = session.inputStream
        outputStream = session.outputStream

        inputStream?.delegate = self
        outputStream?.delegate = self
        inputStream?.schedule(in: .main, forMode: .common)
        outputStream?.schedule(in: .main, forMode: .common)
        inputStream?.open()
        outputStream?.open()

        onStatusChanged?("Sensor connected: \(accessory.name)")
    }

    private func closeSession() {
        inputStream?.delegate = nil
        outputStream?.delegate = nil
        inputStream?.remove(from: .main, forMode: .common)
        outputStream?.remove(from: .main, forMode: .common)
        inputStream?.close()
        outputStream?.close()
        inputStream = nil
        outputStream = nil
        accessorySession = nil
        readBuffer.removeAll(keepingCapacity: true)
    }

    private func readAvailableBytes() {
        guard let inputStream else { return }

        var chunk = [UInt8](repeating: 0, count: 1024)
        while inputStream.hasBytesAvailable {
            let count = inputStream.read(&chunk, maxLength: chunk.count)
            if count > 0 {
                readBuffer.append(contentsOf: chunk[0..<count])
                drainCompleteLines()
            } else if count < 0, let error = inputStream.streamError {
                onError?(error)
                break
            } else {
                break
            }
        }

        if readBuffer.count > 8192 {
            readBuffer.removeAll(keepingCapacity: true)
            onStatusChanged?("Sensor line too long")
        }
    }

    private func drainCompleteLines() {
        while let newlineIndex = readBuffer.firstIndex(of: 10) {
            let lineData = readBuffer.prefix(upTo: newlineIndex)
            readBuffer.removeSubrange(readBuffer.startIndex...newlineIndex)

            guard let line = String(data: lineData, encoding: .utf8) else { continue }
            guard let sample = parser.parse(line: line) else { continue }

            onSampleReceived?(sample)

            let now = Date().timeIntervalSince1970
            if now - lastFlowStatusTime > 1.0 {
                lastFlowStatusTime = now
                onStatusChanged?("Sensor receiving")
            }
        }
    }
}

final class SensorPoseUDPSender {
    var onStatusChanged: ((String) -> Void)?
    var onSampleSent: ((SensorPoseSample) -> Void)?
    var onError: ((Error) -> Void)?

    private let networkQueue = DispatchQueue(label: "umi.pose.sensor.udp", qos: .userInitiated)
    private var host: NWEndpoint.Host
    private var port: NWEndpoint.Port
    private var connection: NWConnection?
    private var isConnectionReady = false

    init?(hostIP: String, port: UInt16 = 5556) {
        guard let udpPort = NWEndpoint.Port(rawValue: port) else { return nil }
        host = NWEndpoint.Host(hostIP)
        self.port = udpPort
    }

    func start() {
        networkQueue.async { [weak self] in
            self?.connectUDP()
        }
    }

    func stop() {
        networkQueue.async { [weak self] in
            self?.disconnectUDP()
        }
    }

    func updateDestination(hostIP: String, port: UInt16 = 5556) {
        guard let udpPort = NWEndpoint.Port(rawValue: port) else { return }

        networkQueue.async { [weak self] in
            guard let self else { return }
            self.host = NWEndpoint.Host(hostIP)
            self.port = udpPort
            self.disconnectUDP()
            self.connectUDP()
        }
    }

    func send(_ sample: SensorPoseSample) {
        let payload = sample.binaryData

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

    private func connectUDP() {
        guard connection == nil else { return }

        let parameters = NWParameters.udp
        parameters.allowLocalEndpointReuse = true
        parameters.includePeerToPeer = true

        let connection = NWConnection(host: host, port: port, using: parameters)
        connection.stateUpdateHandler = { [weak self] state in
            guard let self else { return }

            switch state {
            case .ready:
                self.isConnectionReady = true
                self.onStatusChanged?("Sensor UDP ready")
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
}

final class WiredSensorPoseBridge {
    var onSampleReceived: ((SensorPoseSample) -> Void)?
    var onSampleForwarded: ((SensorPoseSample) -> Void)?
    var onStatusChanged: ((String) -> Void)?
    var onError: ((Error) -> Void)?

    private let receiver = WiredSensorSerialReceiver()
    private let sender: SensorPoseUDPSender

    init?(hostIP: String, port: UInt16 = 5556) {
        guard let sender = SensorPoseUDPSender(hostIP: hostIP, port: port) else { return nil }
        self.sender = sender

        receiver.onSampleReceived = { [weak self] sample in
            self?.onSampleReceived?(sample)
            self?.sender.send(sample)
        }
        receiver.onStatusChanged = { [weak self] status in
            self?.onStatusChanged?(status)
        }
        receiver.onError = { [weak self] error in
            self?.onError?(error)
        }
        sender.onStatusChanged = { [weak self] status in
            self?.onStatusChanged?(status)
        }
        sender.onSampleSent = { [weak self] sample in
            self?.onSampleForwarded?(sample)
        }
        sender.onError = { [weak self] error in
            self?.onError?(error)
        }
    }

    func start(accessoryProtocol: String) {
        sender.start()
        receiver.start(protocolString: accessoryProtocol)
    }

    func stop() {
        receiver.stop()
        sender.stop()
    }

    func updateDestination(hostIP: String, port: UInt16 = 5556) {
        sender.updateDestination(hostIP: hostIP, port: port)
    }
}
