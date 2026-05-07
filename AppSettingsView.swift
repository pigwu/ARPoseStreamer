import SwiftUI

struct AppSettingsView: View {
    @ObservedObject var viewModel: PositionViewModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section("Receiver") {
                    TextField("Host IP", text: $viewModel.hostIP)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.decimalPad)

                    TextField("Port", text: $viewModel.hostPort)
                        .keyboardType(.numberPad)

                    TextField("Upload Port", text: $viewModel.uploadPort)
                        .keyboardType(.numberPad)

                    TextField("Sensor UDP Port", text: $viewModel.sensorPort)
                        .keyboardType(.numberPad)

                    Picker("Receiver OS", selection: $viewModel.receiverPlatform) {
                        ForEach(ReceiverPlatform.allCases) { platform in
                            Text(platform.displayName).tag(platform)
                        }
                    }
                }

                Section("Wired Sensor") {
                    TextField("Accessory Protocol", text: $viewModel.sensorAccessoryProtocol)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()

                    LabeledContent("Mirror Target") {
                        Text(viewModel.sensorTargetSummary)
                            .multilineTextAlignment(.trailing)
                    }

                    Text("Expected serial lines: seq,t,x,y,z,qx,qy,qz,qw or t,x,y,z,qx,qy,qz,qw. Quaternion order is xyzw.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Section("Display") {
                    Toggle("Show 3D Trajectory", isOn: $viewModel.showPositionChart)
                }

                Section("Receiver Help") {
                    LabeledContent("Target") {
                        Text(viewModel.targetSummary)
                            .multilineTextAlignment(.trailing)
                    }

                    Text(viewModel.receiverPlatform.receiverCommand)
                        .font(.footnote.monospaced())
                        .textSelection(.enabled)

                    Text(viewModel.receiverPlatform.uploadServerCommand)
                        .font(.footnote.monospaced())
                        .textSelection(.enabled)

                    Text(viewModel.receiverPlatform.ipHint)
                        .font(.footnote)
                        .foregroundStyle(.secondary)

                    Text("Recommended upload method: HTTP over local network. Bluetooth is not used for Mac/Windows upload in this app.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Section("Video Export") {
                    Text(viewModel.videoAccessHint)
                        .font(.footnote)
                        .foregroundStyle(.secondary)

                    Text("Saved files appear in the app Documents directory.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Section("Capture Library") {
                    NavigationLink("Manage Past Records") {
                        CaptureHistoryView(viewModel: viewModel)
                    }
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") {
                        dismiss()
                    }
                }
            }
        }
    }
}
