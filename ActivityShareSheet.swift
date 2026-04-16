import SwiftUI
import UIKit

struct ActivityShareSheet: UIViewControllerRepresentable {
    let activityItems: [Any]
    let completion: (Bool) -> Void

    func makeUIViewController(context: Context) -> UIActivityViewController {
        let controller = UIActivityViewController(activityItems: activityItems, applicationActivities: nil)
        controller.completionWithItemsHandler = { _, completed, _, _ in
            completion(completed)
        }
        return controller
    }

    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) {}
}
