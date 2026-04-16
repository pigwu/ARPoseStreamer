import SwiftUI
import Charts

struct ContentView: View {
    @StateObject private var viewModel = PositionViewModel()
    @State private var isShowingSettings = false
    @State private var isShowingHistory = false
    @State private var isSidebarPresented = false

    var body: some View {
        NavigationStack {
            ZStack(alignment: .leading) {
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

                    if viewModel.showPositionChart {
                        PositionChartView(samples: viewModel.positionHistory)
                            .frame(height: 220)
                    }

                    Text(viewModel.latestPacketSummary)
                        .font(.footnote.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(14)
                        .background(Color(.secondarySystemBackground))
                        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))

                    Text("Latest capture: \(viewModel.lastCaptureSessionName)")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)

                    Text("Use the side menu for streaming, recording, history, and settings.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .padding(24)
                .navigationTitle("ARPoseStreamer")
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) {
                        Button {
                            withAnimation(.easeInOut(duration: 0.2)) {
                                isSidebarPresented.toggle()
                            }
                        } label: {
                            Image(systemName: "line.3.horizontal")
                        }
                    }
                }

                if isSidebarPresented {
                    Color.black.opacity(0.25)
                        .ignoresSafeArea()
                        .onTapGesture {
                            withAnimation(.easeInOut(duration: 0.2)) {
                                isSidebarPresented = false
                            }
                        }

                    SidebarDrawer(
                        viewModel: viewModel,
                        onOpenHistory: {
                            isShowingHistory = true
                            isSidebarPresented = false
                        },
                        onOpenSettings: {
                            isShowingSettings = true
                            isSidebarPresented = false
                        }
                    )
                    .transition(.move(edge: .leading))
                }
            }
            .sheet(isPresented: $isShowingSettings) {
                AppSettingsView(viewModel: viewModel)
            }
            .sheet(isPresented: $isShowingHistory) {
                NavigationStack {
                    CaptureHistoryView(viewModel: viewModel)
                }
            }
        }
        .onDisappear {
            viewModel.shutdown()
        }
    }
}

private struct SidebarDrawer: View {
    @ObservedObject var viewModel: PositionViewModel
    let onOpenHistory: () -> Void
    let onOpenSettings: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Menu")
                .font(.title2.bold())

            Button(viewModel.isSending ? "Stop Streaming" : "Start Streaming") {
                if viewModel.isSending {
                    viewModel.stopSending()
                } else {
                    viewModel.startSending()
                }
            }
            .buttonStyle(.borderedProminent)

            Button(viewModel.isRecordingVideo ? "Stop & Save Video" : "Start Video Recording") {
                if viewModel.isRecordingVideo {
                    viewModel.stopRecording()
                } else {
                    viewModel.startRecording()
                }
            }
            .buttonStyle(.borderedProminent)

            Button("Reset Origin") {
                viewModel.resetOrigin()
            }
            .buttonStyle(.bordered)

            Divider()

            Button("Past Records") {
                onOpenHistory()
            }
            .buttonStyle(.bordered)

            Button("Settings") {
                onOpenSettings()
            }
            .buttonStyle(.bordered)

            if let lastSavedVideoURL = viewModel.lastSavedVideoURL {
                ShareLink(item: lastSavedVideoURL) {
                    Label("Share Last Video", systemImage: "square.and.arrow.up")
                }
                .buttonStyle(.bordered)
            }

            Spacer()

            Text(viewModel.videoAccessHint)
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
        .padding(20)
        .frame(width: 300, maxHeight: .infinity, alignment: .topLeading)
        .background(Color(.systemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .padding(.leading, 12)
        .padding(.vertical, 12)
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
