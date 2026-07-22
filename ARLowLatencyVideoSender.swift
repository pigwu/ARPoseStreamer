import Foundation
import CoreMedia
import CoreVideo
import Network
import VideoToolbox

struct VideoCameraCalibration: Equatable {
    let fx: Float
    let fy: Float
    let cx: Float
    let cy: Float
    let imageWidth: UInt16
    let imageHeight: UInt16
}

enum VideoStreamResolution: String, CaseIterable, Identifiable {
    case sd480p
    case hd720p
    case fullHD1080p

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .sd480p:
            return "480p"
        case .hd720p:
            return "720p"
        case .fullHD1080p:
            return "1080p"
        }
    }

    var dimensions: (width: Int, height: Int) {
        switch self {
        case .sd480p:
            return (640, 480)
        case .hd720p:
            return (1280, 720)
        case .fullHD1080p:
            return (1920, 1080)
        }
    }
}

struct LowLatencyVideoConfiguration: Equatable {
    var isEnabled: Bool
    var hostIP: String
    var port: UInt16
    var resolution: VideoStreamResolution
    var frameRate: Int
    var bitrateMbps: Double
    var maxDatagramSize: Int = 1200

    var clampedFrameRate: Int {
        min(max(frameRate, 15), 60)
    }

    var clampedBitrateMbps: Double {
        min(max(bitrateMbps, 1.0), 20.0)
    }

    var targetBitrateBitsPerSecond: Int {
        Int((clampedBitrateMbps * 1_000_000.0).rounded())
    }

    var keyFrameInterval: Int {
        max(30, clampedFrameRate / 2)
    }

    static let defaults = LowLatencyVideoConfiguration(
        isEnabled: false,
        hostIP: "192.168.1.10",
        port: 5560,
        resolution: .hd720p,
        frameRate: 60,
        bitrateMbps: 6.0
    )

    static let ultraWideDefaults = LowLatencyVideoConfiguration(
        isEnabled: true,
        hostIP: "192.168.1.10",
        port: 5561,
        resolution: .sd480p,
        frameRate: 10,
        bitrateMbps: 3.0
    )
}

struct LowLatencyVideoStats: Equatable {
    var state = "Video idle"
    var encodedFPS = 0.0
    var sentFPS = 0.0
    var bitrateMbps = 0.0
    var encodedFrames: UInt64 = 0
    var sentFrames: UInt64 = 0
    var droppedFrames: UInt64 = 0
    var keyFrames: UInt64 = 0
    var sentBytes: UInt64 = 0
    var lastCaptureTimestamp: TimeInterval = 0

    static let idle = LowLatencyVideoStats()
}

final class ARLowLatencyVideoSender {
    var onStateChange: ((String) -> Void)?
    var onStatsChange: ((LowLatencyVideoStats) -> Void)?
    var onError: ((Error) -> Void)?

    private let workQueue = DispatchQueue(label: "umi.video.lowlatency", qos: .userInitiated)

    private var configuration: LowLatencyVideoConfiguration
    private var connection: NWConnection?
    private var isConnectionReady = false
    private var isStreamingActive = false
    private var compressionSession: VTCompressionSession?
    private var nextFrameID: UInt32 = 0
    private var forceKeyFrameOnNextFrame = true
    private var lastEncoderDimensions: (width: Int, height: Int)?
    private var stats = LowLatencyVideoStats.idle
    private var statsWindowStartUptime = ProcessInfo.processInfo.systemUptime
    private var windowEncodedFrames: UInt64 = 0
    private var windowSentFrames: UInt64 = 0
    private var windowSentBytes: UInt64 = 0
    private var lastReportedState = LowLatencyVideoStats.idle.state

    init(configuration: LowLatencyVideoConfiguration = .defaults) {
        self.configuration = configuration
        self.stats.state = configuration.isEnabled ? "Video ready" : "Video off"
        self.lastReportedState = self.stats.state
    }

    deinit {
        connection?.cancel()
        if let compressionSession {
            VTCompressionSessionInvalidate(compressionSession)
        }
    }

    func updateConfiguration(_ newConfiguration: LowLatencyVideoConfiguration) {
        workQueue.async { [weak self] in
            guard let self else { return }

            let previousConfiguration = self.configuration
            self.configuration = newConfiguration

            let endpointChanged =
                previousConfiguration.hostIP != newConfiguration.hostIP ||
                previousConfiguration.port != newConfiguration.port
            let encoderNeedsReset =
                previousConfiguration.resolution != newConfiguration.resolution ||
                previousConfiguration.frameRate != newConfiguration.frameRate ||
                previousConfiguration.bitrateMbps != newConfiguration.bitrateMbps

            if !newConfiguration.isEnabled {
                self.stopStreamingLocked(stateOverride: "Video off")
                return
            }

            if endpointChanged {
                self.disconnectLocked()
                if self.isStreamingActive {
                    self.ensureConnectionLocked()
                }
            }

            if encoderNeedsReset {
                self.resetEncoderLocked()
            }

            if self.isStreamingActive {
                self.ensureConnectionLocked()
                self.reportStateLocked(self.isConnectionReady ? "Video streaming" : "Video connecting")
            } else {
                self.reportStateLocked("Video ready")
            }

            self.reportStatsLocked(force: true)
        }
    }

    func startStreaming() {
        workQueue.async { [weak self] in
            guard let self else { return }
            guard self.configuration.isEnabled else {
                self.reportStateLocked("Video off")
                return
            }

            self.isStreamingActive = true
            self.forceKeyFrameOnNextFrame = true
            self.ensureConnectionLocked()
            self.reportStateLocked(self.isConnectionReady ? "Video streaming" : "Video connecting")
        }
    }

    func stopStreaming() {
        workQueue.async { [weak self] in
            self?.stopStreamingLocked()
        }
    }

    func requestKeyFrame() {
        workQueue.async { [weak self] in
            self?.forceKeyFrameOnNextFrame = true
        }
    }

    func appendFrame(
        pixelBuffer: CVPixelBuffer,
        presentationTimeStamp: CMTime,
        captureTimestamp: TimeInterval,
        cameraCalibration: VideoCameraCalibration
    ) {
        workQueue.async { [weak self] in
            guard let self else { return }
            guard self.isStreamingActive, self.configuration.isEnabled else { return }

            self.ensureConnectionLocked()
            guard self.isConnectionReady else { return }

            do {
                try self.ensureCompressionSessionLocked(for: pixelBuffer)
            } catch {
                self.stats.droppedFrames &+= 1
                self.reportError(error)
                self.reportStatsLocked(force: true)
                return
            }

            guard let compressionSession = self.compressionSession else { return }

            let frameID = self.nextFrameID &+ 1
            self.nextFrameID = frameID

            let metadata = EncodedFrameMetadata(
                frameID: frameID,
                captureTimestamp: captureTimestamp,
                cameraCalibration: cameraCalibration
            )

            let frameProperties: CFDictionary?
            if self.forceKeyFrameOnNextFrame {
                frameProperties = [
                    kVTEncodeFrameOptionKey_ForceKeyFrame as String: kCFBooleanTrue as Any
                ] as CFDictionary
                self.forceKeyFrameOnNextFrame = false
            } else {
                frameProperties = nil
            }

            var infoFlags = VTEncodeInfoFlags()
            let status = VTCompressionSessionEncodeFrame(
                compressionSession,
                imageBuffer: pixelBuffer,
                presentationTimeStamp: presentationTimeStamp,
                duration: .invalid,
                frameProperties: frameProperties,
                sourceFrameRefcon: Unmanaged.passRetained(metadata).toOpaque(),
                infoFlagsOut: &infoFlags
            )

            if status != noErr {
                self.stats.droppedFrames &+= 1
                self.forceKeyFrameOnNextFrame = true
                self.reportError(Self.makeNSError(status: status, message: "Video encode failed"))
                self.reportStatsLocked(force: true)
                return
            }

            if infoFlags.contains(.frameDropped) {
                self.stats.droppedFrames &+= 1
                self.reportStatsLocked(force: true)
            }
        }
    }

    private func stopStreamingLocked(stateOverride: String? = nil) {
        isStreamingActive = false
        forceKeyFrameOnNextFrame = true
        disconnectLocked()
        resetEncoderLocked()
        reportStateLocked(stateOverride ?? (configuration.isEnabled ? "Video idle" : "Video off"))
        stats.encodedFPS = 0
        stats.sentFPS = 0
        stats.bitrateMbps = 0
        statsWindowStartUptime = ProcessInfo.processInfo.systemUptime
        windowEncodedFrames = 0
        windowSentFrames = 0
        windowSentBytes = 0
        reportStatsLocked(force: true)
    }

    private func ensureConnectionLocked() {
        guard connection == nil else { return }
        guard let port = NWEndpoint.Port(rawValue: configuration.port) else {
            reportError(Self.makeNSError(status: -1, message: "Invalid video port \(configuration.port)"))
            return
        }

        let parameters = NWParameters.udp
        parameters.allowLocalEndpointReuse = true
        parameters.includePeerToPeer = true

        let connection = NWConnection(
            host: NWEndpoint.Host(configuration.hostIP),
            port: port,
            using: parameters
        )

        connection.stateUpdateHandler = { [weak self] state in
            guard let self else { return }

            self.workQueue.async {
                switch state {
                case .ready:
                    self.isConnectionReady = true
                    self.reportStateLocked(self.isStreamingActive ? "Video streaming" : "Video ready")
                case .waiting(let error):
                    self.isConnectionReady = false
                    self.reportStateLocked("Video waiting")
                    self.reportError(error)
                case .failed(let error):
                    self.isConnectionReady = false
                    self.connection?.cancel()
                    self.connection = nil
                    self.reportStateLocked("Video reconnecting")
                    self.reportError(error)
                case .cancelled:
                    self.isConnectionReady = false
                default:
                    break
                }
            }
        }

        self.connection = connection
        connection.start(queue: workQueue)
    }

    private func disconnectLocked() {
        isConnectionReady = false
        connection?.cancel()
        connection = nil
    }

    private func ensureCompressionSessionLocked(for pixelBuffer: CVPixelBuffer) throws {
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)

        if let lastEncoderDimensions, lastEncoderDimensions.width == width, lastEncoderDimensions.height == height, compressionSession != nil {
            return
        }

        resetEncoderLocked()

        var session: VTCompressionSession?
        let createStatus = VTCompressionSessionCreate(
            allocator: kCFAllocatorDefault,
            width: Int32(width),
            height: Int32(height),
            codecType: kCMVideoCodecType_H264,
            encoderSpecification: nil,
            imageBufferAttributes: nil,
            compressedDataAllocator: nil,
            outputCallback: Self.compressionOutputCallback,
            refcon: Unmanaged.passUnretained(self).toOpaque(),
            compressionSessionOut: &session
        )

        guard createStatus == noErr, let session else {
            throw Self.makeNSError(status: createStatus, message: "Could not create H.264 encoder")
        }

        try Self.setCompressionProperty(
            session,
            key: kVTCompressionPropertyKey_RealTime,
            value: kCFBooleanTrue
        )
        try Self.setCompressionProperty(
            session,
            key: kVTCompressionPropertyKey_AllowFrameReordering,
            value: kCFBooleanFalse
        )
        try Self.setCompressionProperty(
            session,
            key: kVTCompressionPropertyKey_ProfileLevel,
            value: kVTProfileLevel_H264_Baseline_AutoLevel
        )
        try Self.setCompressionProperty(
            session,
            key: kVTCompressionPropertyKey_H264EntropyMode,
            value: kVTH264EntropyMode_CAVLC
        )
        try Self.setCompressionProperty(
            session,
            key: kVTCompressionPropertyKey_ExpectedFrameRate,
            value: NSNumber(value: configuration.clampedFrameRate)
        )
        try Self.setCompressionProperty(
            session,
            key: kVTCompressionPropertyKey_AverageBitRate,
            value: NSNumber(value: configuration.targetBitrateBitsPerSecond)
        )
        try Self.setCompressionProperty(
            session,
            key: kVTCompressionPropertyKey_MaxKeyFrameInterval,
            value: NSNumber(value: configuration.keyFrameInterval)
        )
        try Self.setCompressionProperty(
            session,
            key: kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration,
            value: NSNumber(value: 1.0)
        )
        try Self.setCompressionProperty(
            session,
            key: kVTCompressionPropertyKey_DataRateLimits,
            value: [
                NSNumber(value: configuration.targetBitrateBitsPerSecond / 8),
                NSNumber(value: 1)
            ] as CFArray
        )

        let prepareStatus = VTCompressionSessionPrepareToEncodeFrames(session)
        guard prepareStatus == noErr else {
            VTCompressionSessionInvalidate(session)
            throw Self.makeNSError(status: prepareStatus, message: "Could not prepare H.264 encoder")
        }

        compressionSession = session
        lastEncoderDimensions = (width, height)
        forceKeyFrameOnNextFrame = true
    }

    private func resetEncoderLocked() {
        if let compressionSession {
            VTCompressionSessionInvalidate(compressionSession)
        }

        compressionSession = nil
        lastEncoderDimensions = nil
    }

    private func handleEncodedSampleBuffer(
        status: OSStatus,
        sampleBuffer: CMSampleBuffer?,
        metadata: EncodedFrameMetadata?
    ) {
        workQueue.async { [weak self] in
            guard let self else { return }

            guard self.isStreamingActive, self.configuration.isEnabled else { return }
            guard status == noErr else {
                self.stats.droppedFrames &+= 1
                self.forceKeyFrameOnNextFrame = true
                self.reportError(Self.makeNSError(status: status, message: "Video encoder callback failed"))
                self.reportStatsLocked(force: true)
                return
            }

            guard let sampleBuffer, CMSampleBufferDataIsReady(sampleBuffer), let metadata else {
                self.stats.droppedFrames &+= 1
                self.reportStatsLocked(force: true)
                return
            }

            do {
                let isKeyFrame = Self.isKeyFrame(sampleBuffer)
                let parameterSets = isKeyFrame ? try Self.extractParameterSets(from: sampleBuffer) : []
                let nalUnits = try Self.extractNALUnits(from: sampleBuffer)
                let allNALUnits = parameterSets + nalUnits

                guard !allNALUnits.isEmpty else {
                    self.stats.droppedFrames &+= 1
                    self.reportStatsLocked(force: true)
                    return
                }

                let bytesSent = try self.sendFrameLocked(
                    frameID: metadata.frameID,
                    captureTimestamp: metadata.captureTimestamp,
                    cameraCalibration: metadata.cameraCalibration,
                    nalUnits: allNALUnits,
                    parameterSetCount: parameterSets.count,
                    isKeyFrame: isKeyFrame
                )

                self.stats.encodedFrames &+= 1
                self.stats.sentFrames &+= 1
                self.stats.sentBytes &+= UInt64(bytesSent)
                self.stats.lastCaptureTimestamp = metadata.captureTimestamp
                if isKeyFrame {
                    self.stats.keyFrames &+= 1
                }

                self.windowEncodedFrames &+= 1
                self.windowSentFrames &+= 1
                self.windowSentBytes &+= UInt64(bytesSent)
                self.reportStatsLocked(force: false)
            } catch {
                self.stats.droppedFrames &+= 1
                self.forceKeyFrameOnNextFrame = true
                self.reportError(error)
                self.reportStatsLocked(force: true)
            }
        }
    }

    private func sendFrameLocked(
        frameID: UInt32,
        captureTimestamp: TimeInterval,
        cameraCalibration: VideoCameraCalibration,
        nalUnits: [Data],
        parameterSetCount: Int,
        isKeyFrame: Bool
    ) throws -> Int {
        guard let connection, isConnectionReady else {
            throw Self.makeNSError(status: -1, message: "Video connection is not ready")
        }

        let maxPayloadSize = max(256, configuration.maxDatagramSize - VideoPacketHeader.size)
        var bytesSent = 0

        for (naluIndex, nalUnit) in nalUnits.enumerated() {
            let fragmentCount = max(1, Int(ceil(Double(max(nalUnit.count, 1)) / Double(maxPayloadSize))))
            let containsParameterSet = naluIndex < parameterSetCount

            for fragmentIndex in 0..<fragmentCount {
                let start = fragmentIndex * maxPayloadSize
                let end = min(start + maxPayloadSize, nalUnit.count)
                let payload = nalUnit.subdata(in: start..<end)
                let header = VideoPacketHeader(
                    flags: VideoPacketFlags(
                        isKeyFrame: isKeyFrame,
                        containsParameterSets: containsParameterSet
                    ),
                    frameID: frameID,
                    captureTimestamp: captureTimestamp,
                    cameraCalibration: cameraCalibration,
                    naluIndex: UInt16(naluIndex),
                    naluCount: UInt16(nalUnits.count),
                    fragmentIndex: UInt16(fragmentIndex),
                    fragmentCount: UInt16(fragmentCount)
                )

                var packet = Data(capacity: VideoPacketHeader.size + payload.count)
                packet.append(header.encodedData())
                packet.append(payload)
                bytesSent += packet.count

                connection.send(
                    content: packet,
                    contentContext: .defaultMessage,
                    isComplete: true,
                    completion: .contentProcessed { [weak self] error in
                        guard let self, let error else { return }
                        self.workQueue.async {
                            self.isConnectionReady = false
                            self.connection?.cancel()
                            self.connection = nil
                            self.forceKeyFrameOnNextFrame = true
                            self.reportStateLocked("Video reconnecting")
                            self.reportError(error)
                        }
                    }
                )
            }
        }

        return bytesSent
    }

    private func reportStateLocked(_ state: String) {
        stats.state = state
        guard state != lastReportedState else { return }
        lastReportedState = state

        DispatchQueue.main.async { [weak self] in
            self?.onStateChange?(state)
        }
    }

    private func reportStatsLocked(force: Bool) {
        let now = ProcessInfo.processInfo.systemUptime
        let elapsed = now - statsWindowStartUptime

        guard force || elapsed >= 0.25 else { return }

        if elapsed > 0 {
            stats.encodedFPS = Double(windowEncodedFrames) / elapsed
            stats.sentFPS = Double(windowSentFrames) / elapsed
            stats.bitrateMbps = (Double(windowSentBytes) * 8.0 / elapsed) / 1_000_000.0
        } else {
            stats.encodedFPS = 0
            stats.sentFPS = 0
            stats.bitrateMbps = 0
        }

        let snapshot = stats
        DispatchQueue.main.async { [weak self] in
            self?.onStatsChange?(snapshot)
        }

        statsWindowStartUptime = now
        windowEncodedFrames = 0
        windowSentFrames = 0
        windowSentBytes = 0
    }

    private func reportError(_ error: Error) {
        DispatchQueue.main.async { [weak self] in
            self?.onError?(error)
        }
    }

    private static func setCompressionProperty(
        _ session: VTCompressionSession,
        key: CFString,
        value: CFTypeRef
    ) throws {
        let status = VTSessionSetProperty(session, key: key, value: value)
        guard status == noErr else {
            throw makeNSError(status: status, message: "Could not configure H.264 encoder")
        }
    }

    private static func extractNALUnits(from sampleBuffer: CMSampleBuffer) throws -> [Data] {
        guard let dataBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else {
            throw makeNSError(status: -1, message: "Missing encoded video data buffer")
        }

        var totalLength = 0
        var dataPointer: UnsafeMutablePointer<Int8>?
        let status = CMBlockBufferGetDataPointer(
            dataBuffer,
            atOffset: 0,
            lengthAtOffsetOut: nil,
            totalLengthOut: &totalLength,
            dataPointerOut: &dataPointer
        )

        guard status == kCMBlockBufferNoErr, let dataPointer else {
            throw makeNSError(status: status, message: "Could not access encoded video bytes")
        }

        var nalUnits: [Data] = []
        var offset = 0

        while offset + 4 <= totalLength {
            var nalUnitLengthBE: UInt32 = 0
            memcpy(&nalUnitLengthBE, dataPointer + offset, 4)
            let nalUnitLength = Int(CFSwapInt32BigToHost(nalUnitLengthBE))
            offset += 4

            guard nalUnitLength > 0, offset + nalUnitLength <= totalLength else {
                throw makeNSError(status: -1, message: "Malformed H.264 sample buffer")
            }

            nalUnits.append(Data(bytes: dataPointer + offset, count: nalUnitLength))
            offset += nalUnitLength
        }

        return nalUnits
    }

    private static func extractParameterSets(from sampleBuffer: CMSampleBuffer) throws -> [Data] {
        guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer) else {
            throw makeNSError(status: -1, message: "Missing H.264 format description")
        }

        var parameterSetCount = 0
        var headerLength: Int32 = 0
        let countStatus = CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
            formatDescription,
            parameterSetIndex: 0,
            parameterSetPointerOut: nil,
            parameterSetSizeOut: nil,
            parameterSetCountOut: &parameterSetCount,
            nalUnitHeaderLengthOut: &headerLength
        )

        guard countStatus == noErr, parameterSetCount > 0, headerLength > 0 else {
            throw makeNSError(status: countStatus, message: "Could not inspect H.264 parameter sets")
        }

        var parameterSets: [Data] = []
        parameterSets.reserveCapacity(parameterSetCount)

        for parameterSetIndex in 0..<parameterSetCount {
            var parameterSetPointer: UnsafePointer<UInt8>?
            var parameterSetSize = 0
            let status = CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                formatDescription,
                parameterSetIndex: parameterSetIndex,
                parameterSetPointerOut: &parameterSetPointer,
                parameterSetSizeOut: &parameterSetSize,
                parameterSetCountOut: nil,
                nalUnitHeaderLengthOut: nil
            )

            guard status == noErr, let parameterSetPointer, parameterSetSize > 0 else {
                throw makeNSError(status: status, message: "Could not read H.264 parameter set")
            }

            parameterSets.append(Data(bytes: parameterSetPointer, count: parameterSetSize))
        }

        return parameterSets
    }

    private static func isKeyFrame(_ sampleBuffer: CMSampleBuffer) -> Bool {
        guard
            let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false)
                as? [[CFString: Any]],
            let firstAttachment = attachments.first
        else {
            return false
        }

        let notSync = firstAttachment[kCMSampleAttachmentKey_NotSync] as? Bool ?? false
        return !notSync
    }

    private static func makeNSError(status: OSStatus, message: String) -> NSError {
        NSError(
            domain: "ARLowLatencyVideoSender",
            code: Int(status),
            userInfo: [NSLocalizedDescriptionKey: message]
        )
    }

    private static let compressionOutputCallback: VTCompressionOutputCallback = {
        outputCallbackRefCon,
        sourceFrameRefCon,
        status,
        _,
        sampleBuffer
    in
        guard let outputCallbackRefCon else { return }
        let sender = Unmanaged<ARLowLatencyVideoSender>.fromOpaque(outputCallbackRefCon).takeUnretainedValue()
        let metadata = sourceFrameRefCon.map { Unmanaged<EncodedFrameMetadata>.fromOpaque($0).takeRetainedValue() }
        sender.handleEncodedSampleBuffer(
            status: status,
            sampleBuffer: sampleBuffer,
            metadata: metadata
        )
    }
}

private final class EncodedFrameMetadata {
    let frameID: UInt32
    let captureTimestamp: TimeInterval
    let cameraCalibration: VideoCameraCalibration

    init(
        frameID: UInt32,
        captureTimestamp: TimeInterval,
        cameraCalibration: VideoCameraCalibration
    ) {
        self.frameID = frameID
        self.captureTimestamp = captureTimestamp
        self.cameraCalibration = cameraCalibration
    }
}

private struct VideoPacketFlags {
    let isKeyFrame: Bool
    let containsParameterSets: Bool

    var rawValue: UInt8 {
        var value: UInt8 = 0
        if isKeyFrame {
            value |= 1 << 0
        }
        if containsParameterSets {
            value |= 1 << 1
        }
        return value
    }
}

private struct VideoPacketHeader {
    static let magic = "APV2"
    static let version: UInt8 = 2
    static let size = 48

    let flags: VideoPacketFlags
    let frameID: UInt32
    let captureTimestamp: TimeInterval
    let cameraCalibration: VideoCameraCalibration
    let naluIndex: UInt16
    let naluCount: UInt16
    let fragmentIndex: UInt16
    let fragmentCount: UInt16

    func encodedData() -> Data {
        var data = Data(capacity: Self.size)
        data.append(contentsOf: Self.magic.utf8)
        data.append(contentsOf: [Self.version, flags.rawValue])

        var reserved: UInt16 = 0
        reserved = reserved.littleEndian
        var frameIDLE = frameID.littleEndian
        var captureTimestampLE = captureTimestamp.bitPattern.littleEndian
        var naluIndexLE = naluIndex.littleEndian
        var naluCountLE = naluCount.littleEndian
        var fragmentIndexLE = fragmentIndex.littleEndian
        var fragmentCountLE = fragmentCount.littleEndian
        var fxLE = cameraCalibration.fx.bitPattern.littleEndian
        var fyLE = cameraCalibration.fy.bitPattern.littleEndian
        var cxLE = cameraCalibration.cx.bitPattern.littleEndian
        var cyLE = cameraCalibration.cy.bitPattern.littleEndian
        var imageWidthLE = cameraCalibration.imageWidth.littleEndian
        var imageHeightLE = cameraCalibration.imageHeight.littleEndian

        withUnsafeBytes(of: &reserved) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &frameIDLE) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &captureTimestampLE) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &naluIndexLE) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &naluCountLE) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &fragmentIndexLE) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &fragmentCountLE) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &fxLE) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &fyLE) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &cxLE) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &cyLE) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &imageWidthLE) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: &imageHeightLE) { data.append(contentsOf: $0) }

        return data
    }
}
