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
                        isUploading: viewModel.isUploading(record),
                        uploadDetails: viewModel.uploadDetails,
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
    let onUploadVideo: () -> Void
    let onUploadPose: () -> Void

    @State private var draftName: String

    init(
        record: CaptureRecord,
        isUploading: Bool,
        uploadDetails: UploadStatusViewState,
        onRename: @escaping (String) -> Void,
        onUploadVideo: @escaping () -> Void,
        onUploadPose: @escaping () -> Void
    ) {
        self.record = record
        self.isUploading = isUploading
        self.uploadDetails = uploadDetails
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
                .disabled(!videoFileState.canUpload || isUploading)

                Button("Upload Pose") {
                    onUploadPose()
                }
                .buttonStyle(.bordered)
                .disabled(isUploading)
            }

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

    private func uploadStatusText(for kind: CaptureUploadKind) -> String {
        switch kind {
        case .video:
            if let date = record.videoUploadedAt {
                return "Video uploaded before: \(date.formatted(date: .abbreviated, time: .shortened))"
            }
            switch videoFileState {
            case .notRecorded:
                return "Video: no video recorded in this capture"
            case .missing:
                return "Video: file missing"
            case .empty:
                return "Video: file is empty"
            case .available(let fileSize):
                return "Video: not uploaded yet (\(Self.formatBytes(fileSize)))"
            }
        case .pose:
            if let date = record.poseUploadedAt {
                return "Pose uploaded before: \(date.formatted(date: .abbreviated, time: .shortened))"
            }
            return "Pose: not uploaded yet"
        }
    }

    private var videoFileState: VideoFileState {
        guard let videoURL = CaptureLibraryStore().urlForVideo(record: record) else {
            return .notRecorded
        }

        guard FileManager.default.fileExists(atPath: videoURL.path) else {
            return .missing
        }

        guard
            let values = try? videoURL.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey]),
            values.isRegularFile == true
        else {
            return .missing
        }

        guard let fileSize = values.fileSize, fileSize > 0 else {
            return .empty
        }

        return .available(fileSize)
    }

    private static func formatBytes(_ byteCount: Int) -> String {
        let formatter = ByteCountFormatter()
        formatter.allowedUnits = [.useKB, .useMB, .useGB]
        formatter.countStyle = .file
        return formatter.string(fromByteCount: Int64(byteCount))
    }
}

private enum VideoFileState {
    case notRecorded
    case missing
    case empty
    case available(Int)

    var canUpload: Bool {
        if case .available = self {
            return true
        }

        return false
    }
}
