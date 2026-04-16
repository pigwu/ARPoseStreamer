import SwiftUI

struct ContentView: View {
    @StateObject private var viewModel = PositionViewModel()

    var body: some View {
        VStack(spacing: 22) {
            VStack(alignment: .leading, spacing: 10) {
                Text("Target Host IP")
                    .font(.headline)

                TextField("192.168.1.10", text: $viewModel.hostIP)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.decimalPad)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 12)
                    .background(Color(.secondarySystemBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))

                Text(viewModel.sendStatus)
                    .font(.footnote)
                    .foregroundStyle(.secondary)

                Text(viewModel.recordingStatus)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            VStack(spacing: 20) {
                AxisValueView(axis: "X", value: viewModel.formattedValue(for: viewModel.position.x))
                AxisValueView(axis: "Y", value: viewModel.formattedValue(for: viewModel.position.y))
                AxisValueView(axis: "Z", value: viewModel.formattedValue(for: viewModel.position.z))
            }
            .frame(maxWidth: .infinity)

            Spacer()

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

                Text("Recorded videos are stored in the app's Documents folder and can be exported through Files or Finder file sharing.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)

                Text("The app will request camera and local network access for AR tracking and UDP pose streaming.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
        }
        .padding(24)
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
                .font(.system(size: 38, weight: .bold, design: .rounded))
                .monospacedDigit()
        }
        .frame(maxWidth: .infinity)
    }
}

#Preview {
    ContentView()
}
