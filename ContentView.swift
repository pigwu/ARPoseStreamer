import SwiftUI

struct ContentView: View {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var viewModel = PositionViewModel()
    @State private var isShowingSettings = false
    @State private var isShowingHistory = false
    @State private var isSidebarPresented = false

    var body: some View {
        ZStack(alignment: .leading) {
            ARCameraPreviewView(session: viewModel.previewSession)
                .ignoresSafeArea()

            LinearGradient(
                colors: [
                    .black.opacity(0.42),
                    .clear,
                    .black.opacity(0.62)
                ],
                startPoint: .top,
                endPoint: .bottom
            )
            .ignoresSafeArea()
            .allowsHitTesting(false)

            VStack(spacing: 0) {
                TopBar(
                    title: "ARPoseStreamer",
                    onMenuTapped: {
                        withAnimation(.easeInOut(duration: 0.22)) {
                            isSidebarPresented.toggle()
                        }
                    }
                )

                Spacer()

                BottomDashboard(viewModel: viewModel)
            }

            if isSidebarPresented {
                Color.black.opacity(0.35)
                    .ignoresSafeArea()
                    .onTapGesture {
                        withAnimation(.easeInOut(duration: 0.22)) {
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
        .statusBarHidden(true)
        .persistentSystemOverlays(.hidden)
        .sheet(isPresented: $isShowingSettings) {
            AppSettingsView(viewModel: viewModel)
        }
        .sheet(isPresented: $isShowingHistory) {
            NavigationStack {
                CaptureHistoryView(viewModel: viewModel)
            }
        }
        .task {
            viewModel.activatePreview()
        }
        .onChange(of: scenePhase) { _, newPhase in
            switch newPhase {
            case .active:
                viewModel.activatePreview()
            case .inactive:
                viewModel.deactivatePreviewIfPossible()
            case .background:
                if viewModel.canStopRecording {
                    viewModel.stopRecording()
                }
                viewModel.deactivatePreviewIfPossible()
            @unknown default:
                break
            }
        }
        .onDisappear {
            viewModel.shutdown()
        }
    }
}

private struct TopBar: View {
    let title: String
    let onMenuTapped: () -> Void

    var body: some View {
        HStack {
            Button(action: onMenuTapped) {
                Image(systemName: "line.3.horizontal")
                    .font(.system(size: 19, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 44, height: 44)
                    .background(.ultraThinMaterial, in: Circle())
            }

            Spacer()

            Text(title)
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.white.opacity(0.9))
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(.ultraThinMaterial, in: Capsule())

            Spacer()

            Color.clear
                .frame(width: 44, height: 44)
        }
        .padding(.horizontal, 16)
        .padding(.top, 10)
    }
}

private struct BottomDashboard: View {
    @ObservedObject var viewModel: PositionViewModel

    var body: some View {
        VStack(spacing: 10) {
            StatusStrip(viewModel: viewModel)

            HStack(spacing: 8) {
                AxisCard(axis: "X", value: viewModel.formattedValue(for: viewModel.position.x))
                AxisCard(axis: "Y", value: viewModel.formattedValue(for: viewModel.position.y))
                AxisCard(axis: "Z", value: viewModel.formattedValue(for: viewModel.position.z))
            }

            if viewModel.hasVideoStreamingEnabled {
                VideoMetricsRow(viewModel: viewModel)
            }

            if viewModel.isMagneticListening && viewModel.magneticStats.receivedPackets > 0 {
                MagneticMetricsRow(viewModel: viewModel)

                HStack {
                    Text(viewModel.latestMagneticSummary)
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.white.opacity(0.72))
                        .lineLimit(1)
                        .minimumScaleFactor(0.68)

                    Spacer()
                }

                if viewModel.showMagneticChart {
                    MagneticMagnitudeChart(
                        samples: viewModel.magneticHistory,
                        chipIndex: viewModel.selectedMagneticChip
                    )
                    .frame(height: 76)
                }
            }

            if viewModel.showPositionChart {
                Trajectory3DView(samples: viewModel.positionHistory)
                    .frame(height: 120)
            }

            HStack {
                Text(viewModel.latestPacketSummary)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.white.opacity(0.72))
                    .lineLimit(1)
                    .minimumScaleFactor(0.72)

                Spacer()
            }

            RecordButton(viewModel: viewModel)
        }
        .padding(12)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .padding(.horizontal, 12)
        .padding(.bottom, 10)
    }
}

private struct StatusStrip: View {
    @ObservedObject var viewModel: PositionViewModel

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                StatusChip(text: viewModel.recordingStatus, isActive: viewModel.recordingPhase.isActive)

                StatusChip(text: viewModel.trackingStatus, isActive: viewModel.trackingStatus == "Tracking normal")

                if viewModel.isSending {
                    StatusChip(text: "Streaming", isActive: true)
                }

                if viewModel.isSensorStreaming {
                    StatusChip(text: "Sensor", isActive: true)
                }

                if viewModel.isMagneticListening {
                    StatusChip(text: "Magnetic", isActive: viewModel.magneticStats.receiveRateHz > 0)
                }

                if viewModel.isComputerConnected {
                    StatusChip(text: "PC linked", isActive: true)
                }

                if viewModel.hasVideoStreamingEnabled {
                    StatusChip(text: viewModel.videoStatus, isActive: viewModel.videoStatus == "Video streaming")
                }

                if viewModel.uploadDetails.isActive {
                    StatusChip(text: "Upload \(viewModel.uploadDetails.completedFiles)/\(viewModel.uploadDetails.totalFiles)", isActive: true)
                } else if viewModel.uploadStatus != "Upload idle" {
                    StatusChip(text: viewModel.uploadStatus.hasPrefix("Upload failed") ? "Upload failed" : "Upload done", isActive: false)
                }
            }
        }
    }
}

private struct VideoMetricsRow: View {
    @ObservedObject var viewModel: PositionViewModel

    var body: some View {
        HStack(spacing: 8) {
            CompactMetricCard(label: "Enc FPS", value: viewModel.videoEncodedFPSText)
            CompactMetricCard(label: "Send FPS", value: viewModel.videoSentFPSText)
            CompactMetricCard(label: "Mbps", value: viewModel.videoBitrateText)
            CompactMetricCard(label: "Drops", value: viewModel.videoDroppedFramesText)
        }
    }
}

private struct MagneticMetricsRow: View {
    @ObservedObject var viewModel: PositionViewModel

    var body: some View {
        HStack(spacing: 8) {
            CompactMetricCard(label: "Mag Hz", value: viewModel.magneticReceiveRateText)
            CompactMetricCard(label: "Loss", value: viewModel.magneticLossText)
            CompactMetricCard(label: "Seq", value: viewModel.magneticSequenceText)
            CompactMetricCard(label: "S\(viewModel.selectedMagneticChip) |B|", value: viewModel.selectedMagneticMagnitudeText)
        }
    }
}

private struct RecordButton: View {
    @ObservedObject var viewModel: PositionViewModel

    private var title: String {
        if viewModel.recordingPhase.isSaving {
            return "Saving"
        }

        if viewModel.canStopRecording {
            return "Stop & Save Experiment"
        }

        return "Start Experiment"
    }

    private var systemImage: String {
        if viewModel.canStopRecording {
            return "stop.fill"
        }

        return "record.circle"
    }

    private var isDisabled: Bool {
        viewModel.isSavingRecording || (!viewModel.canStartRecording && !viewModel.canStopRecording)
    }

    var body: some View {
        Button {
            if viewModel.canStopRecording {
                viewModel.stopRecording()
            } else {
                viewModel.startRecording()
            }
        } label: {
            Label(title, systemImage: systemImage)
                .font(.headline.weight(.semibold))
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity)
                .frame(height: 54)
                .background(buttonBackground, in: Capsule())
        }
        .buttonStyle(.plain)
        .disabled(isDisabled)
        .opacity(isDisabled ? 0.65 : 1)
    }

    private var buttonBackground: Color {
        if viewModel.canStopRecording {
            return .red
        }

        return .blue
    }
}

private struct SidebarDrawer: View {
    @ObservedObject var viewModel: PositionViewModel
    let onOpenHistory: () -> Void
    let onOpenSettings: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Menu")
                .font(.headline)
                .foregroundStyle(.white)

            Button(viewModel.isSending ? "Stop Legacy/Video Stream" : "Start Legacy/Video Stream") {
                if viewModel.isSending {
                    viewModel.stopSending()
                } else {
                    viewModel.startSending()
                }
            }
            .buttonStyle(.borderedProminent)

            Button(viewModel.isMagneticListening ? "Stop Magnetic Sensor" : "Start Magnetic Sensor") {
                if viewModel.isMagneticListening {
                    viewModel.stopMagneticSensor()
                } else {
                    viewModel.startMagneticSensor()
                }
            }
            .buttonStyle(.borderedProminent)

            Button("Reset Origin") {
                viewModel.resetOrigin()
            }
            .buttonStyle(.bordered)

            Divider()
                .overlay(.white.opacity(0.15))

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

            if let lastSavedUltraWideVideoURL = viewModel.lastSavedUltraWideVideoURL {
                ShareLink(item: lastSavedUltraWideVideoURL) {
                    Label("Share Last 0.5x Video", systemImage: "square.and.arrow.up")
                }
                .buttonStyle(.bordered)
            }

            Spacer()

            Text(viewModel.computerGatewayStatus)
                .font(.footnote)
                .foregroundStyle(.white.opacity(0.72))

            Text(viewModel.videoAccessHint)
                .font(.footnote)
                .foregroundStyle(.white.opacity(0.72))
        }
        .padding(20)
        .frame(width: 300)
        .frame(maxHeight: .infinity, alignment: .topLeading)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
        .padding(.leading, 14)
        .padding(.top, 68)
        .padding(.bottom, 14)
    }
}

private struct StatusChip: View {
    let text: String
    var isActive = false

    var body: some View {
        Text(text)
            .font(.caption.weight(.medium))
            .foregroundStyle(.white.opacity(0.9))
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .lineLimit(1)
            .minimumScaleFactor(0.72)
            .background((isActive ? Color.green.opacity(0.28) : Color.white.opacity(0.12)), in: Capsule())
    }
}

private struct AxisCard: View {
    let axis: String
    let value: String

    var body: some View {
        VStack(spacing: 6) {
            Text(axis)
                .font(.caption.weight(.bold))
                .foregroundStyle(.white.opacity(0.6))

            Text(value)
                .font(.title2.weight(.bold))
                .foregroundStyle(.white)
                .minimumScaleFactor(0.7)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 10)
        .background(Color.white.opacity(0.1), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private struct CompactMetricCard: View {
    let label: String
    let value: String

    var body: some View {
        VStack(spacing: 4) {
            Text(label)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.white.opacity(0.62))

            Text(value)
                .font(.subheadline.monospacedDigit().weight(.semibold))
                .foregroundStyle(.white)
                .lineLimit(1)
                .minimumScaleFactor(0.72)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 10)
        .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private struct MagneticMagnitudeChart: View {
    let samples: [MagneticHistorySample]
    let chipIndex: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("5 Second Magnetic Magnitude - S\(chipIndex)")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.white.opacity(0.72))

            Canvas { context, size in
                let points = samples.compactMap { sample -> (TimeInterval, Double)? in
                    guard sample.magnitudes.indices.contains(chipIndex) else { return nil }
                    return (sample.timestamp, sample.magnitudes[chipIndex])
                }
                guard points.count > 1, let firstTime = points.first?.0, let lastTime = points.last?.0 else { return }

                let values = points.map { $0.1 }
                let low = values.min() ?? 0
                let high = values.max() ?? 1
                let valueRange = max(high - low, 1e-6)
                let timeRange = max(lastTime - firstTime, 1e-6)

                var path = Path()
                for (index, point) in points.enumerated() {
                    let x = CGFloat((point.0 - firstTime) / timeRange) * size.width
                    let normalized = (point.1 - low) / valueRange
                    let y = size.height - CGFloat(normalized) * size.height
                    if index == 0 {
                        path.move(to: CGPoint(x: x, y: y))
                    } else {
                        path.addLine(to: CGPoint(x: x, y: y))
                    }
                }

                context.stroke(path, with: .color(.mint), lineWidth: 2)
            }
        }
        .padding(8)
        .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private struct Trajectory3DView: View {
    let samples: [PositionHistorySample]

    var recentSamples: [PositionHistorySample] {
        let cutoff = Date().timeIntervalSince1970 - 5.0
        return samples.filter { $0.timestamp >= cutoff }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("5 Second Trajectory")
                .font(.headline)
                .foregroundStyle(.white)

            GeometryReader { geometry in
                Canvas { context, size in
                    let center = CGPoint(x: size.width * 0.5, y: size.height * 0.58)
                    let axisLength = min(size.width, size.height) * 0.26

                    func project(_ sample: PositionHistorySample, scale: CGFloat) -> CGPoint {
                        let x = CGFloat(sample.x)
                        let y = CGFloat(sample.y)
                        let z = CGFloat(sample.z)

                        let px = (x - y * 0.55) * scale
                        let py = (-z + y * 0.35) * scale
                        return CGPoint(x: center.x + px, y: center.y + py)
                    }

                    func drawAxis(to point: CGPoint, color: Color, label: String) {
                        var path = Path()
                        path.move(to: center)
                        path.addLine(to: point)
                        context.stroke(path, with: .color(color.opacity(0.5)), lineWidth: 2)
                        context.draw(
                            Text(label)
                                .font(.caption.bold())
                                .foregroundColor(.white.opacity(0.7)),
                            at: CGPoint(x: point.x + 12, y: point.y + 4),
                            anchor: .center
                        )
                    }

                    drawAxis(to: CGPoint(x: center.x + axisLength, y: center.y), color: .red, label: "X")
                    drawAxis(to: CGPoint(x: center.x - axisLength * 0.52, y: center.y + axisLength * 0.34), color: .green, label: "Y")
                    drawAxis(to: CGPoint(x: center.x, y: center.y - axisLength), color: .blue, label: "Z")

                    guard recentSamples.count > 1 else { return }

                    let maxMagnitude = max(
                        recentSamples.map { max(abs($0.x), max(abs($0.y), abs($0.z))) }.max() ?? 0.05,
                        0.05
                    )
                    let scale = axisLength / CGFloat(maxMagnitude)

                    for index in 1..<recentSamples.count {
                        let previous = recentSamples[index - 1]
                        let current = recentSamples[index]
                        let ageRatio = CGFloat(index) / CGFloat(max(recentSamples.count - 1, 1))
                        let hue = 0.58 - 0.38 * ageRatio
                        let color = Color(hue: hue, saturation: 0.92, brightness: 1.0)

                        var path = Path()
                        path.move(to: project(previous, scale: scale))
                        path.addLine(to: project(current, scale: scale))
                        context.stroke(path, with: .color(color.opacity(0.85)), lineWidth: 4)
                    }

                    if let latest = recentSamples.last {
                        let latestPoint = project(latest, scale: scale)
                        let markerRect = CGRect(x: latestPoint.x - 5, y: latestPoint.y - 5, width: 10, height: 10)
                        context.fill(Path(ellipseIn: markerRect), with: .color(.white))
                    }
                }
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.09), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

#Preview {
    ContentView()
}
