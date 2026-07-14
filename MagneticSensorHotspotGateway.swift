import Foundation
import Network

struct MagneticGatewayStats: Equatable, Sendable {
    var receivedPackets: UInt64 = 0
    var invalidPackets: UInt64 = 0
    var droppedPackets: UInt64 = 0
    var duplicatePackets: UInt64 = 0
    var outOfOrderPackets: UInt64 = 0
    var pendingOverflows: UInt64 = 0
    var combinedPacketsSent: UInt64 = 0
    var receiveRateHz: Double = 0
    var lastSequence: UInt32?
    var boardEndpoint = ""
    var computerEndpoint = ""

    var lossPercent: Double {
        let expected = receivedPackets + droppedPackets
        guard expected > 0 else { return 0 }
        return Double(droppedPackets) * 100 / Double(expected)
    }
}

/// Receives ASKN datagrams from a board connected to the iPhone Personal
/// Hotspot, keeps local collection independent from computer availability, and
/// forwards APM1 packets after a computer registers with PC_HELLO.
final class MagneticSensorHotspotGateway {
    var onSampleReceived: ((MagneticSensorSample) -> Void)?
    var onSensorStatusChanged: ((String) -> Void)?
    var onComputerStatusChanged: ((String) -> Void)?
    var onComputerAvailabilityChanged: ((Bool) -> Void)?
    var onListeningChanged: ((Bool) -> Void)?
    var onStatsChanged: ((MagneticGatewayStats) -> Void)?
    var onError: ((Error) -> Void)?

    private let queue = DispatchQueue(label: "umi.pose.magnetic.hotspot", qos: .userInitiated)
    private let maximumPendingSamples = 64
    private let sensorOfflineTimeout: TimeInterval = 2.0
    private let computerOfflineTimeout: TimeInterval = 5.0
    private let poseSilenceFlushInterval: TimeInterval = 0.05

    private var sensorListener: NWListener?
    private var computerListener: NWListener?
    private var sensorConnections: [UUID: NWConnection] = [:]
    private var computerConnections: [UUID: NWConnection] = [:]
    private var acknowledgedSensorConnections: Set<UUID> = []
    private var combinedConnection: NWConnection?
    private var combinedConnectionReady = false
    private var timer: DispatchSourceTimer?

    private var sensorListenPort: UInt16 = 5557
    private var computerRegistrationPort: UInt16 = 5559
    private var registeredComputerHost: NWEndpoint.Host?
    private var registeredCombinedPort: UInt16?
    private var registeredVideoPort: UInt16?
    private var lastSensorMessageTime: TimeInterval?
    private var lastComputerHeartbeatTime: TimeInterval?
    private var lastPoseArrivalTime: TimeInterval?
    private var pendingMagneticSamples: [MagneticSensorSample] = []
    private var receiveTimes: [TimeInterval] = []
    private var lastStatsPublishTime: TimeInterval = 0
    private var lastMCUTimeUs: UInt64?
    private var stats = MagneticGatewayStats()
    private var sessionID = UUID()
    private var combinedPacketSequence: UInt32 = 0
    private var isRunning = false

    func start(sensorPort: UInt16 = 5557, computerPort: UInt16 = 5559) {
        queue.async { [weak self] in
            self?.startLocked(sensorPort: sensorPort, computerPort: computerPort)
        }
    }

    func stop() {
        queue.async { [weak self] in
            self?.stopLocked()
        }
    }

    func resetStreamSession(sessionID: UUID = UUID()) {
        queue.async { [weak self] in
            guard let self else { return }
            self.sessionID = sessionID
            self.combinedPacketSequence = 0
            self.pendingMagneticSamples.removeAll(keepingCapacity: true)
        }
    }

    func handlePose(_ pose: PoseMagneticPoseValue) {
        queue.async { [weak self] in
            self?.handlePoseLocked(pose)
        }
    }

    private func startLocked(sensorPort: UInt16, computerPort: UInt16) {
        if isRunning, sensorListenPort == sensorPort, computerRegistrationPort == computerPort {
            return
        }

        stopLocked(publishStoppedState: false)
        sensorListenPort = sensorPort
        computerRegistrationPort = computerPort
        stats = MagneticGatewayStats()
        lastStatsPublishTime = 0
        sessionID = UUID()
        combinedPacketSequence = 0

        do {
            let sensorParameters = NWParameters.udp
            sensorParameters.allowLocalEndpointReuse = true
            sensorParameters.includePeerToPeer = true
            let sensorNWPort = try Self.port(sensorPort)
            let sensorListener = try NWListener(using: sensorParameters, on: sensorNWPort)
            self.sensorListener = sensorListener

            sensorListener.stateUpdateHandler = { [weak self, weak sensorListener] state in
                guard let self, let sensorListener, self.sensorListener === sensorListener else { return }
                self.handleSensorListenerState(state)
            }
            sensorListener.newConnectionHandler = { [weak self, weak sensorListener] connection in
                guard let self, let sensorListener, self.sensorListener === sensorListener else {
                    connection.cancel()
                    return
                }
                self.acceptSensorConnection(connection)
            }

            let computerParameters = NWParameters.udp
            computerParameters.allowLocalEndpointReuse = true
            computerParameters.includePeerToPeer = true
            let computerNWPort = try Self.port(computerPort)
            let computerListener = try NWListener(using: computerParameters, on: computerNWPort)
            self.computerListener = computerListener

            computerListener.stateUpdateHandler = { [weak self, weak computerListener] state in
                guard let self, let computerListener, self.computerListener === computerListener else { return }
                self.handleComputerListenerState(state)
            }
            computerListener.newConnectionHandler = { [weak self, weak computerListener] connection in
                guard let self, let computerListener, self.computerListener === computerListener else {
                    connection.cancel()
                    return
                }
                self.acceptComputerConnection(connection)
            }

            isRunning = true
            sensorListener.start(queue: queue)
            computerListener.start(queue: queue)
            startTimerLocked()
            onListeningChanged?(true)
        } catch {
            stopLocked(publishStoppedState: false)
            onError?(error)
            onSensorStatusChanged?("Could not listen for magnetic sensor: \(error.localizedDescription)")
            onListeningChanged?(false)
        }
    }

    private func stopLocked(publishStoppedState: Bool = true) {
        isRunning = false
        timer?.setEventHandler {}
        timer?.cancel()
        timer = nil

        sensorListener?.cancel()
        computerListener?.cancel()
        sensorListener = nil
        computerListener = nil

        sensorConnections.values.forEach { $0.cancel() }
        computerConnections.values.forEach { $0.cancel() }
        sensorConnections.removeAll()
        computerConnections.removeAll()
        acknowledgedSensorConnections.removeAll()

        disconnectCombinedLocked()
        pendingMagneticSamples.removeAll(keepingCapacity: false)
        receiveTimes.removeAll(keepingCapacity: false)
        lastSensorMessageTime = nil
        lastComputerHeartbeatTime = nil
        lastPoseArrivalTime = nil
        registeredComputerHost = nil
        registeredCombinedPort = nil
        registeredVideoPort = nil

        if publishStoppedState {
            onSensorStatusChanged?("Magnetic sensor listener stopped")
            onComputerStatusChanged?("Computer offline")
            onComputerAvailabilityChanged?(false)
            onListeningChanged?(false)
        }
    }

    private func handleSensorListenerState(_ state: NWListener.State) {
        switch state {
        case .ready:
            onSensorStatusChanged?("Waiting for sensor on hotspot UDP \(sensorListenPort)")
        case .failed(let error):
            onError?(error)
            stopLocked(publishStoppedState: false)
            onSensorStatusChanged?("Magnetic listener failed: \(error.localizedDescription)")
            onListeningChanged?(false)
            onComputerAvailabilityChanged?(false)
        case .cancelled:
            break
        default:
            break
        }
    }

    private func handleComputerListenerState(_ state: NWListener.State) {
        switch state {
        case .ready:
            onComputerStatusChanged?("Waiting for computer on UDP \(computerRegistrationPort); recording locally")
        case .failed(let error):
            onError?(error)
            stopLocked(publishStoppedState: false)
            onComputerStatusChanged?("Computer discovery failed: \(error.localizedDescription)")
            onListeningChanged?(false)
            onComputerAvailabilityChanged?(false)
        case .cancelled:
            break
        default:
            break
        }
    }

    private func acceptSensorConnection(_ connection: NWConnection) {
        let id = UUID()
        sensorConnections[id] = connection
        connection.stateUpdateHandler = { [weak self, weak connection] state in
            guard let self, let connection else { return }
            if case .failed(let error) = state {
                self.onError?(error)
                self.removeSensorConnection(id, connection: connection)
            } else if case .cancelled = state {
                self.removeSensorConnection(id, connection: connection)
            }
        }
        connection.start(queue: queue)
        receiveSensorMessage(on: connection, id: id)
    }

    private func receiveSensorMessage(on connection: NWConnection, id: UUID) {
        connection.receiveMessage { [weak self, weak connection] data, _, _, error in
            guard let self, let connection else { return }

            if let data, !data.isEmpty {
                self.processSensorDatagram(data, from: connection, connectionID: id)
            }

            if let error {
                self.onError?(error)
                self.removeSensorConnection(id, connection: connection)
                return
            }

            guard self.isRunning, self.sensorConnections[id] != nil else { return }
            self.receiveSensorMessage(on: connection, id: id)
        }
    }

    private func processSensorDatagram(_ data: Data, from connection: NWConnection, connectionID: UUID) {
        let wallTime = Date().timeIntervalSince1970
        let monotonicTime = ProcessInfo.processInfo.systemUptime

        do {
            let sample = try ASKNPacketDecoder.decode(
                data,
                receivedWallTime: wallTime,
                receivedMonotonicTime: monotonicTime
            )

            let endpoint = Self.endpointDescription(connection.endpoint)
            let shouldPublishConnectedStatus = lastSensorMessageTime == nil || stats.boardEndpoint != endpoint
            lastSensorMessageTime = monotonicTime
            stats.boardEndpoint = endpoint
            updateSequenceStats(with: sample)
            updateReceiveRate(at: monotonicTime)
            appendPending(sample)

            if acknowledgedSensorConnections.insert(connectionID).inserted {
                let ack = Data("APP_ACK,1,\(sensorListenPort)\n".utf8)
                connection.send(content: ack, completion: .contentProcessed { _ in })
            }

            onSampleReceived?(sample)
            if shouldPublishConnectedStatus {
                onSensorStatusChanged?("Magnetic sensor receiving from \(endpoint)")
            }
            publishStatsIfNeeded(at: monotonicTime)
        } catch {
            stats.invalidPackets &+= 1
            onError?(error)
            publishStatsIfNeeded(at: monotonicTime, force: true)
        }
    }

    private func updateSequenceStats(with sample: MagneticSensorSample) {
        stats.receivedPackets &+= 1
        guard let previous = stats.lastSequence, let previousMCUTimeUs = lastMCUTimeUs else {
            stats.lastSequence = sample.sequence
            lastMCUTimeUs = sample.mcuTimeUs
            return
        }
        if sample.mcuTimeUs < previousMCUTimeUs {
            stats.lastSequence = sample.sequence
            lastMCUTimeUs = sample.mcuTimeUs
            return
        }

        let delta = sample.sequence &- previous
        if delta == 0 {
            stats.duplicatePackets &+= 1
        } else if delta < UInt32.max / 2 {
            if delta > 1 {
                stats.droppedPackets &+= UInt64(delta - 1)
            }
            stats.lastSequence = sample.sequence
            lastMCUTimeUs = sample.mcuTimeUs
        } else {
            stats.outOfOrderPackets &+= 1
        }
    }

    private func updateReceiveRate(at time: TimeInterval) {
        receiveTimes.append(time)
        let cutoff = time - 5
        if let firstValid = receiveTimes.firstIndex(where: { $0 >= cutoff }), firstValid > 0 {
            receiveTimes.removeFirst(firstValid)
        }

        if let first = receiveTimes.first, let last = receiveTimes.last, last > first {
            stats.receiveRateHz = Double(receiveTimes.count - 1) / (last - first)
        }
    }

    private func appendPending(_ sample: MagneticSensorSample) {
        pendingMagneticSamples.append(sample)
        if pendingMagneticSamples.count > maximumPendingSamples {
            let overflow = pendingMagneticSamples.count - maximumPendingSamples
            pendingMagneticSamples.removeFirst(overflow)
            stats.pendingOverflows &+= UInt64(overflow)
        }
    }

    private func removeSensorConnection(_ id: UUID, connection: NWConnection) {
        guard sensorConnections[id] === connection else { return }
        sensorConnections.removeValue(forKey: id)
        acknowledgedSensorConnections.remove(id)
        connection.cancel()
    }

    private func acceptComputerConnection(_ connection: NWConnection) {
        let id = UUID()
        computerConnections[id] = connection
        connection.stateUpdateHandler = { [weak self, weak connection] state in
            guard let self, let connection else { return }
            if case .failed(let error) = state {
                self.onError?(error)
                self.removeComputerConnection(id, connection: connection)
            } else if case .cancelled = state {
                self.removeComputerConnection(id, connection: connection)
            }
        }
        connection.start(queue: queue)
        receiveComputerMessage(on: connection, id: id)
    }

    private func receiveComputerMessage(on connection: NWConnection, id: UUID) {
        connection.receiveMessage { [weak self, weak connection] data, _, _, error in
            guard let self, let connection else { return }

            if let data, !data.isEmpty {
                self.processComputerRegistration(data, from: connection)
            }

            if let error {
                self.onError?(error)
                self.removeComputerConnection(id, connection: connection)
                return
            }

            guard self.isRunning, self.computerConnections[id] != nil else { return }
            self.receiveComputerMessage(on: connection, id: id)
        }
    }

    private func processComputerRegistration(_ data: Data, from connection: NWConnection) {
        guard
            let text = String(data: data, encoding: .utf8),
            let registration = Self.parseComputerHello(text),
            case .hostPort(let host, _) = connection.endpoint
        else { return }

        lastComputerHeartbeatTime = ProcessInfo.processInfo.systemUptime
        registeredVideoPort = registration.videoPort
        let targetChanged = registeredComputerHost.map { String(describing: $0) } != String(describing: host)
            || registeredCombinedPort != registration.combinedPort

        if targetChanged || combinedConnection == nil {
            registeredComputerHost = host
            registeredCombinedPort = registration.combinedPort
            connectCombinedLocked(host: host, port: registration.combinedPort)
            onComputerStatusChanged?("Computer registered at \(host):\(registration.combinedPort)")
        }

        stats.computerEndpoint = "\(host):\(registration.combinedPort)"
        publishStatsIfNeeded(at: ProcessInfo.processInfo.systemUptime, force: true)
    }

    private func removeComputerConnection(_ id: UUID, connection: NWConnection) {
        guard computerConnections[id] === connection else { return }
        computerConnections.removeValue(forKey: id)
        connection.cancel()
    }

    private func connectCombinedLocked(host: NWEndpoint.Host, port: UInt16) {
        disconnectCombinedLocked(clearRegistration: false)

        guard let nwPort = NWEndpoint.Port(rawValue: port) else { return }
        let parameters = NWParameters.udp
        parameters.allowLocalEndpointReuse = true
        parameters.includePeerToPeer = true
        let connection = NWConnection(host: host, port: nwPort, using: parameters)
        combinedConnection = connection
        connection.stateUpdateHandler = { [weak self, weak connection] state in
            guard let self, let connection, self.combinedConnection === connection else { return }
            switch state {
            case .ready:
                self.combinedConnectionReady = true
                self.onComputerStatusChanged?("Computer online at \(host):\(port)")
                self.onComputerAvailabilityChanged?(true)
            case .failed(let error):
                self.combinedConnectionReady = false
                self.combinedConnection = nil
                connection.cancel()
                self.onError?(error)
                self.onComputerStatusChanged?("Computer stream failed: \(error.localizedDescription)")
                self.onComputerAvailabilityChanged?(false)
            case .cancelled:
                self.combinedConnectionReady = false
                self.combinedConnection = nil
                self.onComputerAvailabilityChanged?(false)
            default:
                break
            }
        }
        connection.start(queue: queue)
    }

    private func disconnectCombinedLocked(clearRegistration: Bool = true) {
        combinedConnectionReady = false
        combinedConnection?.stateUpdateHandler = nil
        combinedConnection?.cancel()
        combinedConnection = nil
        if clearRegistration {
            registeredComputerHost = nil
            registeredCombinedPort = nil
            registeredVideoPort = nil
            stats.computerEndpoint = ""
        }
    }

    private func handlePoseLocked(_ pose: PoseMagneticPoseValue) {
        let now = ProcessInfo.processInfo.systemUptime
        lastPoseArrivalTime = now

        let splitIndex = pendingMagneticSamples.firstIndex {
            $0.receivedMonotonicTime > pose.frameMonotonicTime + 0.002
        } ?? pendingMagneticSamples.endIndex
        let eligible = Array(pendingMagneticSamples[..<splitIndex])
        pendingMagneticSamples.removeSubrange(..<splitIndex)

        guard isComputerAvailable(at: now) else { return }
        sendInChunks(pose: pose, samples: eligible)
    }

    private func sendInChunks(pose: PoseMagneticPoseValue?, samples: [MagneticSensorSample]) {
        if samples.isEmpty {
            sendCombinedPacket(pose: pose, samples: [])
            return
        }

        var index = 0
        var poseForNextPacket = pose
        while index < samples.count {
            let end = min(index + APM1PacketEncoder.maximumMagneticSampleCount, samples.count)
            sendCombinedPacket(pose: poseForNextPacket, samples: Array(samples[index..<end]))
            poseForNextPacket = nil
            index = end
        }
    }

    private func sendCombinedPacket(pose: PoseMagneticPoseValue?, samples: [MagneticSensorSample]) {
        guard combinedConnectionReady, let combinedConnection else { return }
        combinedPacketSequence &+= 1

        do {
            let payload = try APM1PacketEncoder.encode(
                packetSequence: combinedPacketSequence,
                sessionID: sessionID,
                phoneSendUnixTime: Date().timeIntervalSince1970,
                pose: pose,
                magneticSamples: samples
            )

            combinedConnection.send(
                content: payload,
                contentContext: .defaultMessage,
                isComplete: true,
                completion: .contentProcessed { [weak self] error in
                    guard let self else { return }
                    if let error {
                        self.onError?(error)
                        return
                    }
                    self.stats.combinedPacketsSent &+= 1
                    self.publishStatsIfNeeded(at: ProcessInfo.processInfo.systemUptime)
                }
            )
        } catch {
            onError?(error)
        }
    }

    private func startTimerLocked() {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 0.25, repeating: 0.05, leeway: .milliseconds(10))
        timer.setEventHandler { [weak self] in
            self?.timerFiredLocked()
        }
        self.timer = timer
        timer.resume()
    }

    private func timerFiredLocked() {
        let now = ProcessInfo.processInfo.systemUptime

        if let lastSensorMessageTime, now - lastSensorMessageTime > sensorOfflineTimeout {
            onSensorStatusChanged?("Waiting for magnetic sensor on phone hotspot")
            self.lastSensorMessageTime = nil
            receiveTimes.removeAll(keepingCapacity: true)
            stats.receiveRateHz = 0
        }

        if let lastComputerHeartbeatTime, now - lastComputerHeartbeatTime > computerOfflineTimeout {
            disconnectCombinedLocked()
            self.lastComputerHeartbeatTime = nil
            onComputerStatusChanged?("Computer offline; recording locally")
            onComputerAvailabilityChanged?(false)
        }

        let poseIsSilent = lastPoseArrivalTime.map { now - $0 > poseSilenceFlushInterval } ?? true
        if poseIsSilent, !pendingMagneticSamples.isEmpty {
            let samples = pendingMagneticSamples
            pendingMagneticSamples.removeAll(keepingCapacity: true)
            if isComputerAvailable(at: now) {
                sendInChunks(pose: nil, samples: samples)
            }
        }

        publishStatsIfNeeded(at: now)
    }

    private func isComputerAvailable(at time: TimeInterval) -> Bool {
        guard
            combinedConnectionReady,
            let lastComputerHeartbeatTime,
            time - lastComputerHeartbeatTime <= computerOfflineTimeout
        else { return false }
        return true
    }

    private func publishStatsIfNeeded(at time: TimeInterval, force: Bool = false) {
        guard force || time - lastStatsPublishTime >= 0.25 else { return }
        lastStatsPublishTime = time
        onStatsChanged?(stats)
    }

    private static func port(_ value: UInt16) throws -> NWEndpoint.Port {
        guard value > 0, let port = NWEndpoint.Port(rawValue: value) else {
            throw MagneticGatewayError.invalidPort(value)
        }
        return port
    }

    private static func endpointDescription(_ endpoint: NWEndpoint) -> String {
        switch endpoint {
        case .hostPort(let host, let port):
            return "\(host):\(port)"
        default:
            return String(describing: endpoint)
        }
    }

    private static func parseComputerHello(_ text: String) -> (combinedPort: UInt16, videoPort: UInt16)? {
        let fields = text.trimmingCharacters(in: .whitespacesAndNewlines).split(separator: ",")
        guard
            fields.count == 4,
            fields[0].uppercased() == "PC_HELLO",
            fields[1] == "1",
            let combinedPort = UInt16(fields[2]),
            let videoPort = UInt16(fields[3])
        else { return nil }
        return (combinedPort, videoPort)
    }
}

enum MagneticGatewayError: LocalizedError {
    case invalidPort(UInt16)

    var errorDescription: String? {
        switch self {
        case .invalidPort(let port):
            return "Invalid UDP port \(port)."
        }
    }
}
