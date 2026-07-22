import ARKit
import CoreVideo
import ObjectiveC

/// Research-only access to the secondary ultra-wide frame ARKit keeps on
/// supported Pro iPhones. These ivars are private API and can change between
/// iOS releases, so every lookup is optional and the primary ARKit path must
/// continue to work when they are unavailable.
extension ARFrame {
    var capturedUltraWideImage: CVPixelBuffer? {
        guard let pointer = privateIvarPointer(named: "_capturedUltraWideImage") else {
            return nil
        }
        return pointer.load(as: CVPixelBuffer?.self)
    }

    var ultraWideImageTimestamp: TimeInterval? {
        guard let pointer = privateIvarPointer(named: "_ultraWideImageTimestamp") else {
            return nil
        }
        return pointer.load(as: TimeInterval?.self)
    }

    var ultraWideCamera: ARCamera? {
        guard let pointer = privateIvarPointer(named: "_ultraWideCamera") else {
            return nil
        }
        return pointer.load(as: ARCamera?.self)
    }

    private func privateIvarPointer(named name: String) -> UnsafeRawPointer? {
        guard let ivar = class_getInstanceVariable(ARFrame.self, name) else {
            return nil
        }

        let objectPointer = UnsafeRawPointer(Unmanaged.passUnretained(self).toOpaque())
        return objectPointer.advanced(by: ivar_getOffset(ivar))
    }
}
