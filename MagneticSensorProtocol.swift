import Foundation

/// One magnetic chip's values in the order used by the ASKN wire protocol.
struct ChipValues: Equatable, Sendable {
    let t: Float
    let x: Float
    let y: Float
    let z: Float

    var valuesInProtocolOrder: [Float] {
        [t, x, y, z]
    }
}

/// A domain-specific name for call sites where the short `ChipValues` name is
/// not sufficiently descriptive.
typealias MagneticSensorChipValues = ChipValues

enum MagneticBoardSide: UInt32, CaseIterable, Equatable, Hashable, Sendable {
    case right = 0
    case left = 1

    var displayName: String {
        switch self {
        case .right:
            return "Right"
        case .left:
            return "Left"
        }
    }

    var fileStem: String {
        displayName.lowercased()
    }

    var defaultListenPort: UInt16 {
        switch self {
        case .right:
            return 5557
        case .left:
            return 5562
        }
    }
}

enum MagneticSensorComponent: String, Equatable, Sendable {
    case t
    case x
    case y
    case z
}

enum MagneticSensorSampleValidationError: Error, Equatable, LocalizedError {
    case invalidChipCount(expected: Int, actual: Int)
    case nonFiniteValue(chipIndex: Int, component: MagneticSensorComponent)

    var errorDescription: String? {
        switch self {
        case let .invalidChipCount(expected, actual):
            return "Magnetic sample must contain \(expected) chips; received \(actual)."
        case let .nonFiniteValue(chipIndex, component):
            return "Magnetic sample chip \(chipIndex) has a non-finite \(component.rawValue) value."
        }
    }
}

/// A single five-chip measurement received from the sensor board.
///
/// `receivedWallTime` is Unix time and is useful across devices. The monotonic
/// timestamp is used to align this sample with AR frames on the phone without
/// being affected by wall-clock adjustments.
struct MagneticSensorSample: Equatable, Sendable {
    static let chipCount = 5
    static let valuesPerChip = 4

    let sequence: UInt32
    let mcuTimeUs: UInt64
    let receivedWallTime: TimeInterval
    let receivedMonotonicTime: TimeInterval
    let chips: [ChipValues]
    let boardSide: MagneticBoardSide

    init(
        sequence: UInt32,
        mcuTimeUs: UInt64,
        receivedWallTime: TimeInterval,
        receivedMonotonicTime: TimeInterval,
        chips: [ChipValues],
        boardSide: MagneticBoardSide = .right
    ) throws {
        try Self.validate(chips: chips)
        self.sequence = sequence
        self.mcuTimeUs = mcuTimeUs
        self.receivedWallTime = receivedWallTime
        self.receivedMonotonicTime = receivedMonotonicTime
        self.chips = chips
        self.boardSide = boardSide
    }

    fileprivate init(
        sequence: UInt32,
        mcuTimeUs: UInt64,
        receivedWallTime: TimeInterval,
        receivedMonotonicTime: TimeInterval,
        validatedChips chips: [ChipValues],
        boardSide: MagneticBoardSide
    ) {
        self.sequence = sequence
        self.mcuTimeUs = mcuTimeUs
        self.receivedWallTime = receivedWallTime
        self.receivedMonotonicTime = receivedMonotonicTime
        self.chips = chips
        self.boardSide = boardSide
    }

    subscript(chipIndex: Int) -> ChipValues {
        chips[chipIndex]
    }

    var valuesInProtocolOrder: [Float] {
        chips.flatMap(\.valuesInProtocolOrder)
    }

    private static func validate(chips: [ChipValues]) throws {
        guard chips.count == chipCount else {
            throw MagneticSensorSampleValidationError.invalidChipCount(
                expected: chipCount,
                actual: chips.count
            )
        }

        for (chipIndex, chip) in chips.enumerated() {
            let values: [(MagneticSensorComponent, Float)] = [
                (.t, chip.t),
                (.x, chip.x),
                (.y, chip.y),
                (.z, chip.z)
            ]
            for (component, value) in values where !value.isFinite {
                throw MagneticSensorSampleValidationError.nonFiniteValue(
                    chipIndex: chipIndex,
                    component: component
                )
            }
        }
    }
}

enum ASKNDecodeError: Error, Equatable, LocalizedError {
    case invalidLength(expected: Int, actual: Int)
    case invalidMagic(expected: UInt32, actual: UInt32)
    case nonFiniteValue(chipIndex: Int, component: MagneticSensorComponent)

    var errorDescription: String? {
        switch self {
        case let .invalidLength(expected, actual):
            return "ASKN v1 packet must be exactly \(expected) bytes; received \(actual)."
        case let .invalidMagic(expected, actual):
            return String(
                format: "Invalid ASKN magic 0x%08X; expected 0x%08X.",
                actual,
                expected
            )
        case let .nonFiniteValue(chipIndex, component):
            return "ASKN chip \(chipIndex) contains a non-finite \(component.rawValue) value."
        }
    }
}

/// Strict decoder for the board's 96-byte ASKN v1 datagram (`<IIQ20f`).
enum ASKNPacketDecoder {
    static let packetLength = 96
    static let magic: UInt32 = 0x4153_4B4E

    static func decode(
        _ data: Data,
        receivedWallTime: TimeInterval,
        receivedMonotonicTime: TimeInterval,
        boardSide: MagneticBoardSide = .right
    ) throws -> MagneticSensorSample {
        guard data.count == packetLength else {
            throw ASKNDecodeError.invalidLength(expected: packetLength, actual: data.count)
        }

        var reader = LittleEndianByteReader(data)
        let decodedMagic = reader.readUInt32()
        guard decodedMagic == magic else {
            throw ASKNDecodeError.invalidMagic(expected: magic, actual: decodedMagic)
        }

        let sequence = reader.readUInt32()
        let mcuTimeUs = reader.readUInt64()
        var chips: [ChipValues] = []
        chips.reserveCapacity(MagneticSensorSample.chipCount)

        for chipIndex in 0..<MagneticSensorSample.chipCount {
            let chip = ChipValues(
                t: reader.readFloat32(),
                x: reader.readFloat32(),
                y: reader.readFloat32(),
                z: reader.readFloat32()
            )

            let components: [(MagneticSensorComponent, Float)] = [
                (.t, chip.t),
                (.x, chip.x),
                (.y, chip.y),
                (.z, chip.z)
            ]
            if let (component, _) = components.first(where: { !$0.1.isFinite }) {
                throw ASKNDecodeError.nonFiniteValue(
                    chipIndex: chipIndex,
                    component: component
                )
            }

            chips.append(chip)
        }

        return MagneticSensorSample(
            sequence: sequence,
            mcuTimeUs: mcuTimeUs,
            receivedWallTime: receivedWallTime,
            receivedMonotonicTime: receivedMonotonicTime,
            validatedChips: chips,
            boardSide: boardSide
        )
    }
}

private struct LittleEndianByteReader {
    private let bytes: [UInt8]
    private var offset = 0

    init(_ data: Data) {
        bytes = Array(data)
    }

    mutating func readUInt32() -> UInt32 {
        defer { offset += 4 }
        return UInt32(bytes[offset])
            | (UInt32(bytes[offset + 1]) << 8)
            | (UInt32(bytes[offset + 2]) << 16)
            | (UInt32(bytes[offset + 3]) << 24)
    }

    mutating func readUInt64() -> UInt64 {
        defer { offset += 8 }
        var value: UInt64 = 0
        for byteIndex in 0..<8 {
            value |= UInt64(bytes[offset + byteIndex]) << (byteIndex * 8)
        }
        return value
    }

    mutating func readFloat32() -> Float {
        Float(bitPattern: readUInt32())
    }
}
