import Foundation
import ExternalAccessory
import Network
import simd

struct SensorPoseSample {
    let sequence: UInt32
    let source: String
    let protocolVersion: Int
    let sensorTimestamp: TimeInterval?
    let receivedTimestamp: TimeInterval
    let position: SIMD3<Float>
    let orientation: simd_quatf
    let checksumValid: Bool?
    let rawLine: String?

    var binaryData: Data {
        binaryV2Data
    }

    var binaryV1Data: Data {
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

    var binaryV2Data: Data {
        var data = Data()
        data.append(contentsOf: [0x41, 0x50, 0x53, 0x32])
        Self.append(UInt16(2), to: &data)

        var flags: UInt16 = 0
        if sensorTimestamp != nil { flags |= 1 << 0 }
        if checksumValid == true { flags |= 1 << 1 }
        if checksumValid == false { flags |= 1 << 2 }
        Self.append(flags, to: &data)
        Self.append(sequence, to: &data)

        Self.append(sensorTimestamp ?? .nan, to: &data)
        Self.append(receivedTimestamp, to: &data)

        let vectorScalars: [UInt32] = [
            position.x.bitPattern.littleEndian,
            position.y.bitPattern.littleEndian,
            position.z.bitPattern.littleEndian,
            orientation.vector.x.bitPattern.littleEndian,
            orientation.vector.y.bitPattern.littleEndian,
            orientation.vector.z.bitPattern.littleEndian,
            orientation.vector.w.bitPattern.littleEndian
        ]
        vectorScalars.withUnsafeBytes { data.append(contentsOf: $0) }

        let checksum = Self.fnv1a(data)
        Self.append(checksum, to: &data)
        return data
    }

    private static func append(_ value: UInt16, to data: inout Data) {
        var littleEndian = value.littleEndian
        withUnsafeBytes(of: &littleEndian) { data.append(contentsOf: $0) }
    }

    private static func append(_ value: UInt32, to data: inout Data) {
        var littleEndian = value.littleEndian
        withUnsafeBytes(of: &littleEndian) { data.append(contentsOf: $0) }
    }

    private static func append(_ value: Double, to data: inout Data) {
        var littleEndian = value.bitPattern.littleEndian
        withUnsafeBytes(of: &littleEndian) { data.append(contentsOf: $0) }
    }

    private static func fnv1a(_ data: Data) -> UInt32 {
        var hash: UInt32 = 2166136261
        for byte in data {
            hash ^= UInt32(byte)
            hash &*= 16777619
        }
        return hash
    }
}

struct WiredSensorAccessoryInfo: Identifiable, Hashable {
    let name: String
    let manufacturer: String
    let modelNumber: String
    let serialNumber: String
    let firmwareRevision: String
    let hardwareRevision: String
    let protocolStrings: [String]

    var id: String {
        [name, manufacturer, modelNumber, serialNumber].joined(separator: "|")
    }

    var subtitle: String {
        [manufacturer, modelNumber, serialNumber]
            .filter { !$0.isEmpty }
            .joined(separator: "  ")
    }
}

struct WiredSensorRuntimeStats {
    var bytesRead: Int = 0
    var linesRead: Int = 0
    var parsedSamples: Int = 0
    var parseFailures: Int = 0
    var lastRawLine: String = ""
    var lastParseFailure: String = ""
    var connectedAccessoryName: String = ""
}

enum WiredSensorAccessoryScanner {
    static func currentAccessories() -> [WiredSensorAccessoryInfo] {
        EAAccessoryManager.shared().connectedAccessories.map { accessory in
            WiredSensorAccessoryInfo(
                name: safe(accessory.name),
                manufacturer: safe(accessory.manufacturer),
                modelNumber: safe(accessory.modelNumber),
                serialNumber: safe(accessory.serialNumber),
                firmwareRevision: safe(accessory.firmwareRevision),
                hardwareRevision: safe(accessory.hardwareRevision),
                protocolStrings: safe(accessory.protocolStrings)
            )
        }
    }

    private static func safe(_ value: String?) -> String {
        value ?? ""
    }

    private static func safe(_ value: [String]?) -> [String] {
        value ?? []
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

        if let first = tokens.first?.uppercased(), first == "AP2" || first == "ARPOSE2" {
            return parseV2(tokens: tokens, rawLine: trimmed, receivedTimestamp: receivedTimestamp)
        }

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
        fields: [Double],
        source: String = "legacy",
        protocolVersion: Int = 1,
        checksumValid: Bool? = nil,
        rawLine: String? = nil
    ) -> SensorPoseSample? {
        guard fields.count == 7 else { return nil }

        let position = SIMD3<Float>(Float(fields[0]), Float(fields[1]), Float(fields[2]))
        let rawQuaternion = SIMD4<Float>(Float(fields[3]), Float(fields[4]), Float(fields[5]), Float(fields[6]))
        let norm = simd_length(rawQuaternion)
        guard norm > 1e-6 else { return nil }

        return SensorPoseSample(
            sequence: sequence,
            source: source,
            protocolVersion: protocolVersion,
            sensorTimestamp: sensorTimestamp,
            receivedTimestamp: receivedTimestamp,
            position: position,
            orientation: simd_quatf(vector: rawQuaternion / norm),
            checksumValid: checksumValid,
            rawLine: rawLine
        )
    }

    private func parseV2(tokens: [String], rawLine: String, receivedTimestamp: TimeInterval) -> SensorPoseSample? {
        guard tokens.count == 12 || tokens.count == 13 else { return nil }
        let hasVersion = tokens.count == 13
        let version = hasVersion ? Int(tokens[1]) ?? 2 : 2
        let sourceIndex = hasVersion ? 2 : 1
        let sequenceIndex = hasVersion ? 3 : 2
        let sensorTimeIndex = hasVersion ? 4 : 3
        let fieldsStart = hasVersion ? 5 : 4

        let payloadTokens = Array(tokens.dropLast())
        let expectedChecksum = parseChecksum(tokens.last ?? "")
        let actualChecksum = Self.fnv1a(payloadTokens.joined(separator: ","))
        let checksumValid = expectedChecksum.map { $0 == actualChecksum }
        if checksumValid == false { return nil }

        guard let sequenceValue = Double(tokens[sequenceIndex]), let sensorTime = Double(tokens[sensorTimeIndex]) else {
            return nil
        }

        let fieldTokens = tokens[fieldsStart..<(fieldsStart + 7)]
        let fields = fieldTokens.compactMap(Double.init)
        guard fields.count == 7 else { return nil }

        return makeSample(
            sequence: sequence(from: sequenceValue),
            sensorTimestamp: sensorTime,
            receivedTimestamp: receivedTimestamp,
            fields: fields,
            source: tokens[sourceIndex],
            protocolVersion: version,
            checksumValid: checksumValid,
            rawLine: rawLine
        )
    }

    private func autoSequence() -> UInt32 {
        nextSequence &+= 1
        return nextSequence
    }

    private func sequence(from value: Double) -> UInt32 {
        UInt32(min(max(value, 0), Double(UInt32.max)))
    }

    private func parseChecksum(_ value: String) -> UInt32? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if trimmed.hasPrefix("0x") {
            return UInt32(trimmed.dropFirst(2), radix: 16)
        }
        return UInt32(trimmed)
    }

    private static func fnv1a(_ payload: String) -> UInt32 {
        var hash: UInt32 = 2166136261
        for byte in payload.utf8 {
            hash ^= UInt32(byte)
            hash &*= 16777619
        }
        return hash
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
    var onStatsChanged: ((WiredSensorRuntimeStats) -> Void)?
    var onParseFailure: ((String) -> Void)?

    private let parser = SensorPoseLineParser()
    private var accessoryProtocol = ""
    private var accessorySession: EASession?
    private var inputStream: InputStream?
    private var outputStream: OutputStream?
    private var readBuffer = Data()
    private var isRunning = false
    private var lastFlowStatusTime: TimeInterval = 0
    private var stats = WiredSensorRuntimeStats()

    func start(protocolString: String) {
        let trimmedProtocol = protocolString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedProtocol.isEmpty else {
            onError?(WiredSensorError.missingProtocolString)
            return
        }

        accessoryProtocol = trimmedProtocol
        isRunning = true
        stats = WiredSensorRuntimeStats()
        publishStats()

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

        stats.connectedAccessoryName = accessory.name
        publishStats()
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
                stats.bytesRead += count
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
            let trimmedLine = line.trimmingCharacters(in: .whitespacesAndNewlines)
            stats.linesRead += 1
            stats.lastRawLine = trimmedLine

            guard let sample = parser.parse(line: line) else {
                stats.parseFailures += 1
                stats.lastParseFailure = trimmedLine
                onParseFailure?(trimmedLine)
                publishStats()
                continue
            }

            stats.parsedSamples += 1
            onSampleReceived?(sample)
            publishStats()

            let now = Date().timeIntervalSince1970
            if now - lastFlowStatusTime > 1.0 {
                lastFlowStatusTime = now
                onStatusChanged?("Sensor receiving")
            }
        }
    }

    private func publishStats() {
        onStatsChanged?(stats)
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
    var onStatsChanged: ((WiredSensorRuntimeStats) -> Void)?
    var onParseFailure: ((String) -> Void)?

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
        receiver.onStatsChanged = { [weak self] stats in
            self?.onStatsChanged?(stats)
        }
        receiver.onParseFailure = { [weak self] line in
            self?.onParseFailure?(line)
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
