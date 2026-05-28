# Release v1.0.0 - Robot Arm Validation Support

## 🎯 New Features

### CSV File Loading for Offline Validation
- **Load iPhone CSV**: Import ARKit pose data from recorded sessions
- **Load Robot Arm CSV**: Import ground truth pose data from robot arm
- **Clear All Data**: Reset and start fresh with new data
- **File Status Display**: Shows loaded file names and sample counts

### Upload Progress Visualization
- **Real-time Progress**: Display upload progress (10%, 20%, ... 100%)
- **File Information**: Shows file name, type, size during upload
- **Save Path Display**: Clear indication of where files are saved
- **Upload Statistics**: Track total number of uploaded files

### Improved User Experience
- **Clear Instructions**: Startup message with step-by-step setup guide
- **Upload Folder Path**: Prominently displayed on server startup
- **Better Button Labels**: "Start Live Validation" vs offline CSV loading

## 🔧 Technical Improvements

### Data Integrity
- iPhone CSV recording uses direct file writes (no UDP packet loss)
- HTTP upload uses TCP protocol with automatic retransmission
- Complete file integrity verification

### Validation Workflow
- Offline pose comparison between iPhone ARKit and robot arm
- Automatic time synchronization and spatial calibration
- Position and orientation error metrics
- Calibration quality assessment

## 📱 Use Cases

This release enables:
1. **Robot Arm Validation**: Compare iPhone ARKit tracking against robot arm ground truth
2. **Offline Analysis**: Load and analyze previously recorded sessions
3. **Calibration**: Automatic sensor-to-ARKit spatial transformation
4. **Quality Assessment**: Quantitative error metrics for tracking accuracy

## 🚀 Getting Started

### For Offline Validation:
```bash
# Start the validator
python pose_tracking_validator.py

# Click "Load iPhone CSV" and "Load Robot Arm CSV"
# View real-time comparison and error metrics
```

### For HTTP Upload:
```bash
# Start the upload server
python capture_upload_server.py

# On iPhone: Past Records → Select recording → Upload
# Watch real-time progress on server
```

## 📝 CSV Format Requirements

Robot arm CSV should match this format:
```csv
sequence,sender_time,x,y,z,qx,qy,qz,qw
0,1715443200.123,0.5,0.3,0.8,0.0,0.0,0.0,1.0
```

- `x,y,z`: Position in meters
- `qx,qy,qz,qw`: Orientation quaternion
- `sender_time`: Timestamp in seconds

## 🔗 Files Changed

- `pose_tracking_validator.py`: Added CSV loading UI and functions
- `capture_upload_server.py`: Added progress visualization
- `test_upload.py`: New test script for upload functionality

## 🙏 Acknowledgments

This release was developed to support robotics research requiring accurate pose tracking validation against ground truth data.
