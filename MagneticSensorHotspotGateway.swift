import Foundation
import Network

enum RemoteRecordingAction: String {
    case start = "START"
    case stop = "STOP"
    case status = "STATUS"
}

struct RemoteRecordingCommand {
    let requestID: String
    let action: RemoteRecordingAction
    fileprivate let connectionID: UUID
}

struct MagneticBoardStats: Equatable, Sendable {
    var receivedPackets: UInt64 = 0
    var invalidPackets: UInt64 = 0
    var droppedPackets: UInt64 = 0
    var duplicatePackets: UInt64 = 0
    var outOfOrderPackets: UInt64 = 0
    var receiveRateHz: Double = 0
    var lastSequence: UInt32?
    var endpoint = ""

    var lossPercent: Double {
        let expected = receivedPackets + droppedPackets
        guard expected > 0 else { return 0 }
        return Double(droppedPackets) * 100 / Double(expected)
    }
}

struct MagneticGatewayStats: Equatable, Sendable {
    var rightBoard = MagneticBoardStats()
    var leftBoard = MagneticBoardStats()
    var pendingOverflows: UInt64 = 0
    var combinedPacketsSent: UInt64 = 0
    var computerEndpoint = ""

    subscript(side: MagneticBoardSide) -> MagneticBoardStats {
        get {
            switch side {
            case .right:
                return rightBoard
            case .left:
                return leftBoard
            }
        }
        set {
            switch side {
            case .right:
                rightBoard = newValue
            case .left:
                leftBoard = newValue
            }
        }
    }

    var receivedPackets: UInt64 { rightBoard.receivedPackets + leftBoard.receivedPackets }
    var invalidPackets: UInt64 { rightBoard.invalidPackets + leftBoard.invalidPackets }
    var droppedPackets: UInt64 { rightBoard.droppedPackets + leftBoard.droppedPackets }
    var duplicatePackets: UInt64 { rightBoard.duplicatePackets + leftBoard.duplicatePackets }
    var outOfOrderPackets: UInt64 { rightBoard.outOfOrderPackets + leftBoard.outOfOrderPackets }
    var receiveRateHz: Double { rightBoard.receiveRateHz + leftBoard.receiveRateHz }
    var lastSequence: UInt32? { rightBoard.lastSequence }
    var boardEndpoint: String {
        [
            rightBoard.endpoint.isEmpty ? nil : "Right: \(rightBoard.endpoint)",
            leftBoard.endpoint.isEmpty ? nil : "Left: \(leftBoard.endpoint)"
        ]
        .compactMap { $0 }
        .joined(separator: "; ")
    }

    var lossPercent: Double {
        let expected = receivedPackets + droppedPackets
        guard expected > 0 else { return 0 }
        return Double(droppedPackets) * 100 / Double(expected)
    }
}

private struct MagneticSensorConnection {
    let connection: NWConnection
    let side: MagneticBoardSide
    let listenPort: UInt16
}

/// Receives ASKN datagrams from a board connected to the iPhone Personal
/// Hotspot, keeps local collection independent from computer availability, and
/// forwards APM2 packets after a computer registers with PC_HELLO.
final class MagneticSensorHotspotGateway {
    var onSampleReceived: ((MagneticSensorSample) -> Void)?
    var onSensorStatusChanged: ((String) -> Void)?
    var onBoardStatusChanged: ((MagneticBoardSide, String) -> Void)?
    var onComputerStatusChanged: ((String) -> Void)?
    var onComputerAvailabilityChanged: ((Bool) -> Void)?
    var onListeningChanged: ((Bool) -> Void)?
    var onStatsChanged: ((MagneticGatewayStats) -> Void)?
    var onRemoteRecordingCommand: ((RemoteRecordingCommand) -> Void)?
    var onError: ((Error) -> Void)?

    private let queue = DispatchQueue(label: "umi.pose.magnetic.hotspot", qos: .userInitiated)
    private let maximumPendingSamples = 64
    private let sensorOfflineTimeout: TimeInterval = 2.0
    private let computerOfflineTimeout: TimeInterval = 5.0
    private let poseSilenceFlushInterval: TimeInterval = 0.05

    private var sensorListeners: [MagneticBoardSide: NWListener] = [:]
    private var computerListener: NWListener?
    private var sensorConnections: [UUID: MagneticSensorConnection] = [:]
    private var computerConnections: [UUID: NWConnection] = [:]
    private var acknowledgedSensorConnections: Set<UUID> = []
    private var combinedConnection: NWConnection?
    private var combinedConnectionReady = false
    private var timer: DispatchSourceTimer?

    private var rightSensorListenPort: UInt16 = 5557
    private var leftSensorListenPort: UInt16 = 5562
    private var computerRegistrationPort: UInt16 = 5559
    private var registeredComputerHost: NWEndpoint.Host?
    private var registeredCombinedPort: UInt16?
    private var registeredVideoPort: UInt16?
    private var lastSensorMessageTimes: [MagneticBoardSide: TimeInterval] = [:]
    private var lastComputerHeartbeatTime: TimeInterval?
    private var lastPoseArrivalTime: TimeInterval?
    private var pendingMagneticSamples: [MagneticSensorSample] = []
    private var receiveTimes: [MagneticBoardSide: [TimeInterval]] = [:]
    private var lastStatsPublishTime: TimeInterval = 0
    private var lastMCUTimesUs: [MagneticBoardSide: UInt64] = [:]
    private var stats = MagneticGatewayStats()
    private var sessionID = UUID()
    private var combinedPacketSequence: UInt32 = 0
    private var isRunning = false

    func start(
        rightSensorPort: UInt16 = 5557,
        leftSensorPort: UInt16 = 5562,
        computerPort: UInt16 = 5559
    ) {
        queue.async { [weak self] in
            self?.startLocked(
                rightSensorPort: rightSensorPort,
                leftSensorPort: leftSensorPort,
                computerPort: computerPort
            )
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

    func acknowledgeRemoteRecordingCommand(
        _ command: RemoteRecordingCommand,
        accepted: Bool,
        state: String
    ) {
        queue.async { [weak self] in
            self?.sendRemoteRecordingAcknowledgementLocked(
                command,
                accepted: accepted,
                state: state
            )
        }
    }

    private func startLocked(
        rightSensorPort: UInt16,
        leftSensorPort: UInt16,
        computerPort: UInt16
    ) {
        if
            isRunning,
            rightSensorListenPort == rightSensorPort,
            leftSensorListenPort == leftSensorPort,
            computerRegistrationPort == computerPort
        {
            return
        }

        stopLocked(publishStoppedState: false)
        guard Set([rightSensorPort, leftSensorPort, computerPort]).count == 3 else {
            let error = MagneticGatewayError.duplicatePorts
            onError?(error)
            onSensorStatusChanged?(error.localizedDescription)
            onListeningChanged?(false)
            return
        }
        rightSensorListenPort = rightSensorPort
        leftSensorListenPort = leftSensorPort
        computerRegistrationPort = computerPort
        stats = MagneticGatewayStats()
        lastStatsPublishTime = 0
        sessionID = UUID()
        combinedPacketSequence = 0

        do {
            for (side, listenPort) in [
                (MagneticBoardSide.right, rightSensorPort),
                (MagneticBoardSide.left, leftSensorPort)
            ] {
                let sensorParameters = NWParameters.udp
                sensorParameters.allowLocalEndpointReuse = true
                sensorParameters.includePeerToPeer = true
                let sensorNWPort = try Self.port(listenPort)
                let sensorListener = try NWListener(using: sensorParameters, on: sensorNWPort)
                sensorListeners[side] = sensorListener

                sensorListener.stateUpdateHandler = { [weak self, weak sensorListener] state in
                    guard
                        let self,
                        let sensorListener,
                        self.sensorListeners[side] === sensorListener
                    else { return }
                    self.handleSensorListenerState(state, side: side, port: listenPort)
                }
                sensorListener.newConnectionHandler = { [weak self, weak sensorListener] connection in
                    guard
                        let self,
                        let sensorListener,
                        self.sensorListeners[side] === sensorListener
                    else {
                        connection.cancel()
                        return
                    }
                    self.acceptSensorConnection(connection, side: side, listenPort: listenPort)
                }
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
            sensorListeners.values.forEach { $0.start(queue: queue) }
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

        sensorListeners.values.forEach { $0.cancel() }
        computerListener?.cancel()
        sensorListeners.removeAll()
        computerListener = nil

        sensorConnections.values.forEach { $0.connection.cancel() }
        computerConnections.values.forEach { $0.cancel() }
        sensorConnections.removeAll()
        computerConnections.removeAll()
        acknowledgedSensorConnections.removeAll()

        disconnectCombinedLocked()
        pendingMagneticSamples.removeAll(keepingCapacity: false)
        receiveTimes.removeAll(keepingCapacity: false)
        lastSensorMessageTimes.removeAll(keepingCapacity: false)
        lastMCUTimesUs.removeAll(keepingCapacity: false)
        lastComputerHeartbeatTime = nil
        lastPoseArrivalTime = nil
        registeredComputerHost = nil
        registeredCombinedPort = nil
        registeredVideoPort = nil

        if publishStoppedState {
            onSensorStatusChanged?("Magnetic sensor listener stopped")
            onBoardStatusChanged?(.right, "Right board idle")
            onBoardStatusChanged?(.left, "Left board idle")
            onComputerStatusChanged?("Computer offline")
            onComputerAvailabilityChanged?(false)
            onListeningChanged?(false)
        }
    }

    private func handleSensorListenerState(
        _ state: NWListener.State,
        side: MagneticBoardSide,
        port: UInt16
    ) {
        switch state {
        case .ready:
            let message = "Waiting on hotspot UDP \(port)"
            onBoardStatusChanged?(side, message)
            onSensorStatusChanged?("Waiting for right and left magnetic boards")
        case .failed(let error):
            onError?(error)
            sensorListeners[side]?.cancel()
            sensorListeners.removeValue(forKey: side)
            onBoardStatusChanged?(side, "Listener failed: \(error.localizedDescription)")
            onSensorStatusChanged?("\(side.displayName) magnetic listener failed")
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

    private func acceptSensorConnection(
        _ connection: NWConnection,
        side: MagneticBoardSide,
        listenPort: UInt16
    ) {
        let id = UUID()
        sensorConnections[id] = MagneticSensorConnection(
            connection: connection,
            side: side,
            listenPort: listenPort
        )
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
        guard let sensorConnection = sensorConnections[connectionID] else { return }
        let side = sensorConnection.side
        let wallTime = Date().timeIntervalSince1970
        let monotonicTime = ProcessInfo.processInfo.systemUptime

        do {
            let sample = try ASKNPacketDecoder.decode(
                data,
                receivedWallTime: wallTime,
                receivedMonotonicTime: monotonicTime,
                boardSide: side
            )

            let endpoint = Self.endpointDescription(connection.endpoint)
            var boardStats = stats[side]
            let shouldPublishConnectedStatus =
                lastSensorMessageTimes[side] == nil || boardStats.endpoint != endpoint
            lastSensorMessageTimes[side] = monotonicTime
            boardStats.endpoint = endpoint
            stats[side] = boardStats
            updateSequenceStats(with: sample)
            updateReceiveRate(at: monotonicTime, side: side)
            appendPending(sample)

            if acknowledgedSensorConnections.insert(connectionID).inserted {
                let ack = Data("APP_ACK,1,\(sensorConnection.listenPort)\n".utf8)
                connection.send(content: ack, completion: .contentProcessed { _ in })
            }

            onSampleReceived?(sample)
            if shouldPublishConnectedStatus {
                let message = "Receiving from \(endpoint)"
                onBoardStatusChanged?(side, message)
                onSensorStatusChanged?("Magnetic boards active")
            }
            publishStatsIfNeeded(at: monotonicTime)
        } catch {
            var boardStats = stats[side]
            boardStats.invalidPackets &+= 1
            stats[side] = boardStats
            onError?(error)
            publishStatsIfNeeded(at: monotonicTime, force: true)
        }
    }

    private func updateSequenceStats(with sample: MagneticSensorSample) {
        let side = sample.boardSide
        var boardStats = stats[side]
        boardStats.receivedPackets &+= 1
        guard
            let previous = boardStats.lastSequence,
            let previousMCUTimeUs = lastMCUTimesUs[side]
        else {
            boardStats.lastSequence = sample.sequence
            lastMCUTimesUs[side] = sample.mcuTimeUs
            stats[side] = boardStats
            return
        }
        if sample.mcuTimeUs < previousMCUTimeUs {
            boardStats.lastSequence = sample.sequence
            lastMCUTimesUs[side] = sample.mcuTimeUs
            stats[side] = boardStats
            return
        }

        let delta = sample.sequence &- previous
        if delta == 0 {
            boardStats.duplicatePackets &+= 1
        } else if delta < UInt32.max / 2 {
            if delta > 1 {
                boardStats.droppedPackets &+= UInt64(delta - 1)
            }
            boardStats.lastSequence = sample.sequence
            lastMCUTimesUs[side] = sample.mcuTimeUs
        } else {
            boardStats.outOfOrderPackets &+= 1
        }
        stats[side] = boardStats
    }

    private func updateReceiveRate(at time: TimeInterval, side: MagneticBoardSide) {
        var times = receiveTimes[side] ?? []
        times.append(time)
        let cutoff = time - 5
        if let firstValid = times.firstIndex(where: { $0 >= cutoff }), firstValid > 0 {
            times.removeFirst(firstValid)
        }

        var boardStats = stats[side]
        if let first = times.first, let last = times.last, last > first {
            boardStats.receiveRateHz = Double(times.count - 1) / (last - first)
        }
        receiveTimes[side] = times
        stats[side] = boardStats
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
        guard sensorConnections[id]?.connection === connection else { return }
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
                self.processComputerMessage(data, from: connection, connectionID: id)
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

    private func processComputerMessage(
        _ data: Data,
        from connection: NWConnection,
        connectionID: UUID
    ) {
        guard
            let text = String(data: data, encoding: .utf8),
            case .hostPort(let host, _) = connection.endpoint
        else { return }

        if let command = Self.parseRemoteRecordingCommand(text, connectionID: connectionID) {
            lastComputerHeartbeatTime = ProcessInfo.processInfo.systemUptime
            if let onRemoteRecordingCommand {
                onRemoteRecordingCommand(command)
            } else {
                sendRemoteRecordingAcknowledgementLocked(
                    command,
                    accepted: false,
                    state: "busy"
                )
            }
            return
        }

        guard let registration = Self.parseComputerHello(text) else { return }
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

    private func sendRemoteRecordingAcknowledgementLocked(
        _ command: RemoteRecordingCommand,
        accepted: Bool,
        state: String
    ) {
        guard let connection = computerConnections[command.connectionID] else { return }
        let allowedStates = ["idle", "recording", "saving", "busy"]
        let normalizedState = allowedStates.contains(state) ? state : "busy"
        let result = accepted ? "OK" : "REJECTED"
        let payload = Data(
            "PC_RECORD_ACK,1,\(command.requestID),\(command.action.rawValue),\(result),\(normalizedState)\n".utf8
        )
        connection.send(content: payload, completion: .contentProcessed { [weak self] error in
            if let error {
                self?.onError?(error)
            }
        })
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
            let end = min(index + APM2PacketEncoder.maximumMagneticSampleCount, samples.count)
            sendCombinedPacket(pose: poseForNextPacket, samples: Array(samples[index..<end]))
            poseForNextPacket = nil
            index = end
        }
    }

    private func sendCombinedPacket(pose: PoseMagneticPoseValue?, samples: [MagneticSensorSample]) {
        guard combinedConnectionReady, let combinedConnection else { return }
        combinedPacketSequence &+= 1

        do {
            let payload = try APM2PacketEncoder.encode(
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

        for side in MagneticBoardSide.allCases {
            if
                let lastMessageTime = lastSensorMessageTimes[side],
                now - lastMessageTime > sensorOfflineTimeout
            {
                onBoardStatusChanged?(
                    side,
                    "Waiting on hotspot UDP \(listenPort(for: side))"
                )
                lastSensorMessageTimes.removeValue(forKey: side)
                receiveTimes[side] = []
                var boardStats = stats[side]
                boardStats.receiveRateHz = 0
                boardStats.endpoint = ""
                stats[side] = boardStats
            }
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

    private func listenPort(for side: MagneticBoardSide) -> UInt16 {
        switch side {
        case .right:
            return rightSensorListenPort
        case .left:
            return leftSensorListenPort
        }
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

    private static func parseRemoteRecordingCommand(
        _ text: String,
        connectionID: UUID
    ) -> RemoteRecordingCommand? {
        let fields = text.trimmingCharacters(in: .whitespacesAndNewlines).split(
            separator: ",",
            omittingEmptySubsequences: false
        )
        guard
            fields.count == 4,
            fields[0].uppercased() == "PC_RECORD",
            fields[1] == "1",
            isSafeRemoteRecordingField(String(fields[2])),
            let action = RemoteRecordingAction(rawValue: fields[3].uppercased())
        else { return nil }
        return RemoteRecordingCommand(
            requestID: String(fields[2]),
            action: action,
            connectionID: connectionID
        )
    }

    private static func isSafeRemoteRecordingField(_ value: String) -> Bool {
        guard !value.isEmpty, value.count <= 128 else { return false }
        return value.unicodeScalars.allSatisfy { scalar in
            guard scalar.value < 128 else { return false }
            return CharacterSet.alphanumerics.contains(scalar) || "-_.".unicodeScalars.contains(scalar)
        }
    }
}

enum MagneticGatewayError: LocalizedError {
    case invalidPort(UInt16)
    case duplicatePorts

    var errorDescription: String? {
        switch self {
        case .invalidPort(let port):
            return "Invalid UDP port \(port)."
        case .duplicatePorts:
            return "Right board, left board, and computer registration ports must be different."
        }
    }
}
