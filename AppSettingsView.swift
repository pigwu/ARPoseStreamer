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

                    Button("Refresh Accessories") {
                        viewModel.refreshConnectedAccessories()
                    }

                    LabeledContent("Mirror Target") {
                        Text(viewModel.sensorTargetSummary)
                            .multilineTextAlignment(.trailing)
                    }

                    if viewModel.connectedAccessories.isEmpty {
                        Text("No ExternalAccessory devices are currently visible to iOS.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(viewModel.connectedAccessories) { accessory in
                            VStack(alignment: .leading, spacing: 6) {
                                Text(accessory.name)
                                    .font(.headline)
                                if !accessory.subtitle.isEmpty {
                                    Text(accessory.subtitle)
                                        .font(.footnote)
                                        .foregroundStyle(.secondary)
                                }
                                Text(accessory.protocolStrings.joined(separator: "\n"))
                                    .font(.footnote.monospaced())
                                    .textSelection(.enabled)
                            }
                        }
                    }

                    Text("Expected serial lines: AP2,2,source,seq,t,x,y,z,qx,qy,qz,qw,checksum or legacy seq,t,x,y,z,qx,qy,qz,qw. Quaternion order is xyzw.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Section("Sensor Diagnostics") {
                    LabeledContent("Bytes") {
                        Text("\(viewModel.wiredSensorStats.bytesRead)")
                    }
                    LabeledContent("Lines") {
                        Text("\(viewModel.wiredSensorStats.linesRead)")
                    }
                    LabeledContent("Samples") {
                        Text("\(viewModel.wiredSensorStats.parsedSamples)")
                    }
                    LabeledContent("Parse Failures") {
                        Text("\(viewModel.wiredSensorStats.parseFailures)")
                    }
                    if !viewModel.wiredSensorStats.connectedAccessoryName.isEmpty {
                        LabeledContent("Connected") {
                            Text(viewModel.wiredSensorStats.connectedAccessoryName)
                                .multilineTextAlignment(.trailing)
                        }
                    }
                    if !viewModel.wiredSensorStats.lastRawLine.isEmpty {
                        Text(viewModel.wiredSensorStats.lastRawLine)
                            .font(.footnote.monospaced())
                            .lineLimit(4)
                            .textSelection(.enabled)
                    }
                    if !viewModel.wiredSensorStats.lastParseFailure.isEmpty {
                        Text("Last parse failure: \(viewModel.wiredSensorStats.lastParseFailure)")
                            .font(.footnote.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(4)
                            .textSelection(.enabled)
                    }
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
            .onAppear {
                viewModel.refreshConnectedAccessories()
            }
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
