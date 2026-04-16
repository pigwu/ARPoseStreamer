import SwiftUI
import Charts

struct ContentView: View {
    @StateObject private var viewModel = PositionViewModel()
    @State private var isShowingSettings = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 22) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(viewModel.targetSummary)
                        .font(.headline)

                    Text(viewModel.sendStatus)
                        .font(.footnote)
                        .foregroundStyle(.secondary)

                    Text(viewModel.recordingStatus)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                VStack(spacing: 20) {
                    AxisValueView(axis: "X", value: viewModel.formattedValue(for: viewModel.position.x))
                    AxisValueView(axis: "Y", value: viewModel.formattedValue(for: viewModel.position.y))
                    AxisValueView(axis: "Z", value: viewModel.formattedValue(for: viewModel.position.z))
                }
                .frame(maxWidth: .infinity)

                PositionChartView(samples: viewModel.positionHistory)
                    .frame(height: 220)

                VStack(spacing: 12) {
                    Text(viewModel.latestPacketSummary)
                        .font(.footnote.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(14)
                        .background(Color(.secondarySystemBackground))
                        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))

                    Button(viewModel.isSending ? "Stop Streaming" : "Start Streaming") {
                        if viewModel.isSending {
                            viewModel.stopSending()
                        } else {
                            viewModel.startSending()
                        }
                    }
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .background(viewModel.isSending ? Color.red : Color.accentColor)
                    .foregroundStyle(.white)
                    .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))

                    Button("Reset Origin") {
                        viewModel.resetOrigin()
                    }
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .background(Color(.secondarySystemBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))

                    Button(viewModel.isRecordingVideo ? "Stop & Save Video" : "Start Video Recording") {
                        if viewModel.isRecordingVideo {
                            viewModel.stopRecording()
                        } else {
                            viewModel.startRecording()
                        }
                    }
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .background(viewModel.isRecordingVideo ? Color.orange : Color.green)
                    .foregroundStyle(.white)
                    .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))

                    if let lastSavedVideoURL = viewModel.lastSavedVideoURL {
                        ShareLink(item: lastSavedVideoURL) {
                            Label("Share Last Video", systemImage: "square.and.arrow.up")
                                .font(.headline)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 16)
                        }
                        .background(Color.blue)
                        .foregroundStyle(.white)
                        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                    }

                    Text("Last saved video: \(viewModel.lastSavedVideoName)")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)

                    Text(viewModel.videoAccessHint)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)

                    Text("The app requests camera access for AR tracking and local network access for UDP pose streaming.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
            }
            .padding(24)
            .navigationTitle("ARPoseStreamer")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        isShowingSettings = true
                    } label: {
                        Label("Settings", systemImage: "gearshape")
                    }
                }
            }
            .sheet(isPresented: $isShowingSettings) {
                AppSettingsView(viewModel: viewModel)
            }
        }
        .onDisappear {
            viewModel.shutdown()
        }
    }
}

private struct AxisValueView: View {
    let axis: String
    let value: String

    var body: some View {
        VStack(spacing: 8) {
            Text(axis)
                .font(.title.bold())
                .foregroundStyle(.secondary)

            Text(value)
                .font(.system(size: 34, weight: .bold, design: .rounded))
                .monospacedDigit()
        }
        .frame(maxWidth: .infinity)
    }
}

private struct PositionChartView: View {
    let samples: [PositionHistorySample]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Recent Position")
                .font(.headline)

            Chart {
                ForEach(samples) { sample in
                    LineMark(
                        x: .value("Sequence", sample.sequence),
                        y: .value("Meters", sample.x)
                    )
                    .foregroundStyle(.red)
                    .lineStyle(StrokeStyle(lineWidth: 2))
                    .interpolationMethod(.catmullRom)

                    LineMark(
                        x: .value("Sequence", sample.sequence),
                        y: .value("Meters", sample.y)
                    )
                    .foregroundStyle(.green)
                    .lineStyle(StrokeStyle(lineWidth: 2))
                    .interpolationMethod(.catmullRom)

                    LineMark(
                        x: .value("Sequence", sample.sequence),
                        y: .value("Meters", sample.z)
                    )
                    .foregroundStyle(.blue)
                    .lineStyle(StrokeStyle(lineWidth: 2))
                    .interpolationMethod(.catmullRom)
                }
            }
            .chartLegend(.hidden)
            .chartYAxisLabel("m")
            .frame(maxWidth: .infinity)

            HStack(spacing: 14) {
                LegendPill(color: .red, label: "X")
                LegendPill(color: .green, label: "Y")
                LegendPill(color: .blue, label: "Z")
            }
        }
        .padding(16)
        .background(Color(.secondarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
    }
}

private struct LegendPill: View {
    let color: Color
    let label: String

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(color)
                .frame(width: 10, height: 10)

            Text(label)
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
    }
}

#Preview {
    ContentView()
}
