import Foundation
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

                    Toggle("Auto-upload Completed Experiments", isOn: $viewModel.autoUploadExperiments)

                    TextField("Legacy Sensor Mirror Port", text: $viewModel.sensorPort)
                        .keyboardType(.numberPad)

                    Picker("Receiver OS", selection: $viewModel.receiverPlatform) {
                        ForEach(ReceiverPlatform.allCases) { platform in
                            Text(platform.displayName).tag(platform)
                        }
                    }
                }

                Section("Low-Latency Video") {
                    Toggle("Enable Video Stream", isOn: $viewModel.isVideoStreamingEnabled)

                    TextField("Video Port", text: $viewModel.videoPort)
                        .keyboardType(.numberPad)

                    Picker("Resolution", selection: $viewModel.videoResolution) {
                        ForEach(VideoStreamResolution.allCases) { resolution in
                            Text(resolution.displayName).tag(resolution)
                        }
                    }

                    TextField("FPS", text: $viewModel.videoFrameRate)
                        .keyboardType(.numberPad)

                    TextField("Bitrate (Mbps)", text: $viewModel.videoBitrateMbps)
                        .keyboardType(.decimalPad)

                    Toggle(
                        "Enable 0.5x ArUco Stream",
                        isOn: $viewModel.isUltraWideVideoStreamingEnabled
                    )

                    TextField("0.5x ArUco Video Port", text: $viewModel.ultraWideVideoPort)
                        .keyboardType(.numberPad)

                    LabeledContent("0.5x Status") {
                        Text(viewModel.ultraWideVideoStatus)
                            .multilineTextAlignment(.trailing)
                    }

                    Text("The video path uses H.264 over raw UDP with small packets. For a lower-latency test, try 480p at 2–3 Mbps.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)

                    Text("The original 1x ARKit stream remains unchanged. On supported iPhone Pro models, a research-only ARKit private frame supplies a separate 0.5x stream at about 10 FPS for ArUco processing (default UDP 5561).")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Section("Phone Hotspot Magnetic Sensor") {
                    Toggle("Start Listener Automatically", isOn: $viewModel.autoStartMagneticSensor)

                    TextField("Right Board -> Phone UDP Port", text: $viewModel.magneticListenPort)
                        .keyboardType(.numberPad)

                    TextField("Left Board -> Phone UDP Port", text: $viewModel.leftMagneticListenPort)
                        .keyboardType(.numberPad)

                    TextField("Computer Registration Port", text: $viewModel.computerRegistrationPort)
                        .keyboardType(.numberPad)

                    TextField("Phone -> Computer APM2 Port", text: $viewModel.combinedStreamPort)
                        .keyboardType(.numberPad)

                    Picker("Displayed Chip", selection: $viewModel.selectedMagneticChip) {
                        ForEach(0..<MagneticSensorSample.chipCount, id: \.self) { index in
                            Text("S\(index)").tag(index)
                        }
                    }

                    Toggle("Show Magnetic Chart", isOn: $viewModel.showMagneticChart)

                    Button(viewModel.isMagneticListening ? "Stop Magnetic Listener" : "Start Magnetic Listener") {
                        if viewModel.isMagneticListening {
                            viewModel.stopMagneticSensor()
                        } else {
                            viewModel.startMagneticSensor()
                        }
                    }

                    LabeledContent("Right Board") {
                        Text(viewModel.rightMagneticStatus)
                            .multilineTextAlignment(.trailing)
                    }

                    LabeledContent("Left Board") {
                        Text(viewModel.leftMagneticStatus)
                            .multilineTextAlignment(.trailing)
                    }

                    LabeledContent("Sensor Summary") {
                        Text(viewModel.magneticStatus)
                            .multilineTextAlignment(.trailing)
                    }

                    LabeledContent("Computer") {
                        Text(viewModel.computerGatewayStatus)
                            .multilineTextAlignment(.trailing)
                    }

                    Text("Turn on Personal Hotspot manually. For ESP32-class boards, enable Maximum Compatibility and use a simple ASCII iPhone name and password. The board should send ASKN UDP to its DHCP gateway on the configured port; do not hard-code the phone IP.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)

                    Text("Both magnetic boards are optional during experiments. The right board sends ASKN to UDP 5557 and the left board to UDP 5562. Pose/video recording and computer output continue normally while either board is offline.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Section("Magnetic Diagnostics") {
                    ForEach(MagneticBoardSide.allCases, id: \.self) { side in
                        let stats = viewModel.magneticStats[side]
                        LabeledContent("\(side.displayName) Rate / Loss") {
                            Text("\(viewModel.magneticReceiveRateText(for: side)) Hz / \(viewModel.magneticLossText(for: side))")
                        }
                        LabeledContent("\(side.displayName) Packets / Missing") {
                            Text("\(stats.receivedPackets) / \(stats.droppedPackets)")
                        }
                        LabeledContent("\(side.displayName) Sequence / Invalid") {
                            Text("\(viewModel.magneticSequenceText(for: side)) / \(stats.invalidPackets)")
                        }
                        if !stats.endpoint.isEmpty {
                            LabeledContent("\(side.displayName) Endpoint") {
                                Text(stats.endpoint)
                                    .multilineTextAlignment(.trailing)
                            }
                        }
                    }
                    LabeledContent("Forwarded") {
                        Text("\(viewModel.magneticStats.combinedPacketsSent)")
                    }
                    if !viewModel.magneticStats.computerEndpoint.isEmpty {
                        LabeledContent("Computer") {
                            Text(viewModel.magneticStats.computerEndpoint)
                                .multilineTextAlignment(.trailing)
                        }
                    }

                    ForEach(MagneticBoardSide.allCases, id: \.self) { side in
                        let chips = side == .right
                            ? viewModel.latestMagneticChips
                            : viewModel.latestLeftMagneticChips
                        ForEach(0..<chips.count, id: \.self) { index in
                            let chip = chips[index]
                            LabeledContent("\(side.displayName) S\(index)") {
                                Text(String(format: "t %.3f  x %.3f  y %.3f  z %.3f", chip.t, chip.x, chip.y, chip.z))
                                    .font(.footnote.monospacedDigit())
                                    .multilineTextAlignment(.trailing)
                            }
                        }
                    }
                }

                Section("Legacy Wired Pose Sensor") {
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

                Section("Legacy Wired Sensor Diagnostics") {
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

                    LabeledContent("Video") {
                        Text(viewModel.videoTargetSummary)
                            .multilineTextAlignment(.trailing)
                    }

                    Text(viewModel.receiverPlatform.receiverCommand)
                        .font(.footnote.monospaced())
                        .textSelection(.enabled)

                    Text(viewModel.combinedReceiverCommand)
                        .font(.footnote.monospaced())
                        .textSelection(.enabled)

                    Text(viewModel.videoReceiverCommand)
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
