import SwiftUI

struct CaptureHistoryView: View {
    @ObservedObject var viewModel: PositionViewModel

    var body: some View {
        List {
            if viewModel.captureRecords.isEmpty {
                ContentUnavailableView(
                    "No Captures Yet",
                    systemImage: "tray",
                    description: Text("Start a streaming or recording session to create reusable capture history.")
                )
            } else {
                ForEach(viewModel.captureRecords) { record in
                    CaptureRecordCard(
                        record: record,
                        onRename: { newName in
                            viewModel.renameCapture(record, to: newName)
                        },
                        onUploadVideo: {
                            viewModel.requestVideoUpload(for: record)
                        },
                        onUploadPose: {
                            viewModel.requestPoseUpload(for: record)
                        }
                    )
                    .listRowInsets(EdgeInsets(top: 10, leading: 16, bottom: 10, trailing: 16))
                    .listRowSeparator(.hidden)
                }
            }
        }
        .listStyle(.plain)
        .navigationTitle("Past Records")
        .sheet(item: $viewModel.activeShareRequest, onDismiss: {
            viewModel.activeShareRequest = nil
        }) { request in
            ActivityShareSheet(activityItems: request.itemURLs.map { $0 as Any }) { completed in
                viewModel.markShareCompleted(for: request, completed: completed)
                viewModel.activeShareRequest = nil
            }
        }
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
    let onRename: (String) -> Void
    let onUploadVideo: () -> Void
    let onUploadPose: () -> Void

    @State private var draftName: String

    init(
        record: CaptureRecord,
        onRename: @escaping (String) -> Void,
        onUploadVideo: @escaping () -> Void,
        onUploadPose: @escaping () -> Void
    ) {
        self.record = record
        self.onRename = onRename
        self.onUploadVideo = onUploadVideo
        self.onUploadPose = onUploadPose
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

            VStack(alignment: .leading, spacing: 6) {
                Text(uploadStatusText(for: .video))
                    .font(.footnote)
                    .foregroundStyle(.secondary)

                Text(uploadStatusText(for: .pose))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 12) {
                Button("Upload Video") {
                    onUploadVideo()
                }
                .buttonStyle(.borderedProminent)
                .disabled(record.videoFileName == nil)

                Button("Upload Pose") {
                    onUploadPose()
                }
                .buttonStyle(.bordered)
            }
        }
        .padding(16)
        .background(Color(.secondarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
    }

    private func uploadStatusText(for kind: CaptureUploadKind) -> String {
        switch kind {
        case .video:
            if let date = record.videoUploadedAt {
                return "Video uploaded before: \(date.formatted(date: .abbreviated, time: .shortened))"
            }
            return record.videoFileName == nil ? "Video: no video recorded in this capture" : "Video: not uploaded yet"
        case .pose:
            if let date = record.poseUploadedAt {
                return "Pose uploaded before: \(date.formatted(date: .abbreviated, time: .shortened))"
            }
            return "Pose: not uploaded yet"
        }
    }
}
