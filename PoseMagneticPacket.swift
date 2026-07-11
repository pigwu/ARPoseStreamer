import Foundation

/// AR pose data in the representation needed by the combined APM1 protocol.
/// This deliberately has no ARKit dependency. An `ARPoseUDPSender.PoseSample`
/// can be adapted by passing `sample.position` and
/// `sample.orientation.vector` to this initializer.
struct PoseMagneticPoseValue: Equatable, Sendable {
    let sequence: UInt32
    let senderUnixTime: TimeInterval
    let frameMonotonicTime: TimeInterval
    let position: SIMD3<Float>
    let quaternionXYZW: SIMD4<Float>

    init(
        sequence: UInt32,
        senderUnixTime: TimeInterval,
        frameMonotonicTime: TimeInterval,
        position: SIMD3<Float>,
        quaternionXYZW: SIMD4<Float>
    ) {
        self.sequence = sequence
        self.senderUnixTime = senderUnixTime
        self.frameMonotonicTime = frameMonotonicTime
        self.position = position
        self.quaternionXYZW = quaternionXYZW
    }
}

enum APM1PacketEncodingError: Error, Equatable, LocalizedError {
    case tooManyMagneticSamples(maximum: Int, actual: Int)

    var errorDescription: String? {
        switch self {
        case let .tooManyMagneticSamples(maximum, actual):
            return "APM1 supports at most \(maximum) magnetic samples per packet; received \(actual)."
        }
    }
}

/// Encodes one optional pose and up to ten magnetic samples into an APM1 UDP
/// payload. Every numeric value, including the trailing CRC32, is little-endian.
enum APM1PacketEncoder {
    static let version: UInt16 = 1
    static let posePresentFlag: UInt16 = 1 << 0
    static let maximumMagneticSampleCount = 10
    static let fixedBytesBeforeMagneticSamples = 88
    static let bytesPerMagneticSample = 108
    static let crcByteCount = 4

    static func encode(
        packetSequence: UInt32,
        sessionID: UUID,
        phoneSendUnixTime: TimeInterval,
        pose: PoseMagneticPoseValue?,
        magneticSamples: [MagneticSensorSample]
    ) throws -> Data {
        guard magneticSamples.count <= maximumMagneticSampleCount else {
            throw APM1PacketEncodingError.tooManyMagneticSamples(
                maximum: maximumMagneticSampleCount,
                actual: magneticSamples.count
            )
        }

        let expectedSize = fixedBytesBeforeMagneticSamples
            + bytesPerMagneticSample * magneticSamples.count
            + crcByteCount
        var data = Data()
        data.reserveCapacity(expectedSize)

        // The APM1 magic is raw ASCII, not a byte-swapped integer.
        data.append(contentsOf: [0x41, 0x50, 0x4D, 0x31])
        append(version, to: &data)
        append(pose == nil ? 0 : posePresentFlag, to: &data)
        append(packetSequence, to: &data)
        append(sessionID, to: &data)
        append(phoneSendUnixTime, to: &data)

        if let pose {
            append(pose.sequence, to: &data)
            append(pose.senderUnixTime, to: &data)
            append(pose.frameMonotonicTime, to: &data)
            append(pose.position.x, to: &data)
            append(pose.position.y, to: &data)
            append(pose.position.z, to: &data)
            append(pose.quaternionXYZW.x, to: &data)
            append(pose.quaternionXYZW.y, to: &data)
            append(pose.quaternionXYZW.z, to: &data)
            append(pose.quaternionXYZW.w, to: &data)
        } else {
            appendZeroPoseBlock(to: &data)
        }

        append(UInt16(magneticSamples.count), to: &data)
        append(UInt16(0), to: &data) // reserved

        for sample in magneticSamples {
            append(sample.sequence, to: &data)
            append(sample.mcuTimeUs, to: &data)
            append(sample.receivedWallTime, to: &data)
            append(sample.receivedMonotonicTime, to: &data)
            for chip in sample.chips {
                append(chip.t, to: &data)
                append(chip.x, to: &data)
                append(chip.y, to: &data)
                append(chip.z, to: &data)
            }
        }

        append(CRC32.checksum(data), to: &data)
        assert(data.count == expectedSize)
        return data
    }

    private static func appendZeroPoseBlock(to data: inout Data) {
        append(UInt32(0), to: &data)
        append(Double(0), to: &data)
        append(Double(0), to: &data)
        for _ in 0..<7 {
            append(Float(0), to: &data)
        }
    }

    private static func append(_ value: UInt16, to data: inout Data) {
        data.append(UInt8(truncatingIfNeeded: value))
        data.append(UInt8(truncatingIfNeeded: value >> 8))
    }

    private static func append(_ value: UInt32, to data: inout Data) {
        data.append(UInt8(truncatingIfNeeded: value))
        data.append(UInt8(truncatingIfNeeded: value >> 8))
        data.append(UInt8(truncatingIfNeeded: value >> 16))
        data.append(UInt8(truncatingIfNeeded: value >> 24))
    }

    private static func append(_ value: UInt64, to data: inout Data) {
        data.append(UInt8(truncatingIfNeeded: value))
        data.append(UInt8(truncatingIfNeeded: value >> 8))
        data.append(UInt8(truncatingIfNeeded: value >> 16))
        data.append(UInt8(truncatingIfNeeded: value >> 24))
        data.append(UInt8(truncatingIfNeeded: value >> 32))
        data.append(UInt8(truncatingIfNeeded: value >> 40))
        data.append(UInt8(truncatingIfNeeded: value >> 48))
        data.append(UInt8(truncatingIfNeeded: value >> 56))
    }

    private static func append(_ value: Float, to data: inout Data) {
        append(value.bitPattern, to: &data)
    }

    private static func append(_ value: Double, to data: inout Data) {
        append(value.bitPattern, to: &data)
    }

    private static func append(_ value: UUID, to data: inout Data) {
        let bytes = value.uuid
        data.append(contentsOf: [
            bytes.0, bytes.1, bytes.2, bytes.3,
            bytes.4, bytes.5, bytes.6, bytes.7,
            bytes.8, bytes.9, bytes.10, bytes.11,
            bytes.12, bytes.13, bytes.14, bytes.15
        ])
    }
}

/// Standard CRC-32/ISO-HDLC (also known as CRC-32/IEEE), with reflected
/// polynomial 0xEDB88320, initial value 0xFFFFFFFF, and final XOR 0xFFFFFFFF.
enum CRC32 {
    static func checksum(_ data: Data) -> UInt32 {
        var crc: UInt32 = 0xFFFF_FFFF
        for byte in data {
            crc ^= UInt32(byte)
            for _ in 0..<8 {
                if crc & 1 == 1 {
                    crc = (crc >> 1) ^ 0xEDB8_8320
                } else {
                    crc >>= 1
                }
            }
        }
        return crc ^ 0xFFFF_FFFF
    }
}
