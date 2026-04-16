import SwiftUI
import ARKit
import SceneKit

struct ARCameraPreviewView: UIViewRepresentable {
    let session: ARSession?

    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView(frame: .zero)
        view.automaticallyUpdatesLighting = false
        view.scene = SCNScene()
        view.backgroundColor = .black
        view.contentMode = .scaleAspectFill
        view.rendersContinuously = true
        return view
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {
        guard let session else { return }
        if uiView.session !== session {
            uiView.session = session
        }
    }
}
