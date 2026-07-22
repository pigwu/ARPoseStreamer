import SwiftUI

struct CaptureHistoryView: View {
    @ObservedObject var viewModel: PositionViewModel

    var body: some View {
        List {
            if viewModel.captureRecords.isEmpty {
                ContentUnavailableView(
                    "No Captures Yet",
                    systemImage: "tray",
                    description: Text("Start an experiment to save synchronized pose, sensor, video, and transport data.")
                )
            } else {
                ForEach(viewModel.captureRecords) { record in
                    CaptureRecordCard(
                        record: record,
                        isUploading: viewModel.isUploading(record),
                        uploadDetails: viewModel.uploadDetails,
                        onRename: { newName in
                            viewModel.renameCapture(record, to: newName)
                        },
                        onUploadExperiment: {
                            viewModel.requestExperimentUpload(for: record)
                        }
                    )
                    .listRowInsets(EdgeInsets(top: 10, leading: 16, bottom: 10, trailing: 16))
                    .listRowSeparator(.hidden)
                }
            }
        }
        .listStyle(.plain)
        .navigationTitle("Past Records")
        .alert(item: $viewModel.pendingReuploadPrompt) { prompt in
            Alert(
                title: Text(prompt.title),
                message: Text("This item was previously uploaded on \(prompt.previousUploadDate.formatted(date: .abbreviated, time: .shortened)). You can upload it again."),
                primaryButton: .default(Text("Upload Again")) {
                    viewModel.confirmReupload(prompt)
                },
                secondaryButton: .cancel {
                    viewModel.cancelReuploadPrompt()
                }
            )
        }
    }
}

private struct CaptureRecordCard: View {
    let record: CaptureRecord
    let isUploading: Bool
    let uploadDetails: UploadStatusViewState
    let onRename: (String) -> Void
    let onUploadExperiment: () -> Void

    @State private var draftName: String

    init(
        record: CaptureRecord,
        isUploading: Bool,
        uploadDetails: UploadStatusViewState,
        onRename: @escaping (String) -> Void,
        onUploadExperiment: @escaping () -> Void
    ) {
        self.record = record
        self.isUploading = isUploading
        self.uploadDetails = uploadDetails
        self.onRename = onRename
        self.onUploadExperiment = onUploadExperiment
        _draftName = State(initialValue: record.displayName)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            TextField("Capture name", text: $draftName)
                .font(.headline)
                .textFieldStyle(.roundedBorder)

            HStack(spacing: 12) {
                Button("Save Name") {
                    onRename(draftName)
                }
                .buttonStyle(.borderedProminent)

                Text(record.createdAt.formatted(date: .abbreviated, time: .shortened))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            Text(experimentUploadStatusText)
                .font(.footnote)
                .foregroundStyle(.secondary)

            Button("Upload Complete Experiment") {
                onUploadExperiment()
            }
            .buttonStyle(.borderedProminent)
            .disabled(isUploading)

            if isUploading {
                VStack(alignment: .leading, spacing: 6) {
                    Text(uploadDetails.progressText.isEmpty ? "Uploading..." : uploadDetails.progressText)
                        .font(.footnote.monospacedDigit())
                        .foregroundStyle(.secondary)

                    if let latestSavedPath = uploadDetails.latestSavedPath {
                        Text("Host path: \(latestSavedPath)")
                            .font(.footnote.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }
            }
        }
        .padding(16)
        .background(Color(.secondarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .onChange(of: record.displayName) { _, newValue in
            draftName = newValue
        }
    }

    private var experimentUploadStatusText: String {
        if let date = record.experimentUploadedAt {
            return "Complete experiment uploaded: \(date.formatted(date: .abbreviated, time: .shortened))"
        }
        let modalities = [
            "pose",
            record.magneticCSVFileName == nil ? nil : "sensor",
            videoFileState.canUpload ? "video" : nil,
            record.ultraWideVideoFileName == nil ? nil : "0.5x video",
            record.senderTransportCSVFileName == nil ? nil : "transport"
        ].compactMap { $0 }
        return "Ready to upload: \(modalities.joined(separator: " + "))"
    }

    private var videoFileState: CaptureVideoFileState {
        CaptureLibraryStore().videoFileState(for: record)
    }
}
