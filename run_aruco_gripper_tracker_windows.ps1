$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python aruco_gripper_tracker.py `
  --config config\umi_gripper_aruco.json `
  --bind 0.0.0.0 `
  --video-port 5560 `
  --output-host 127.0.0.1 `
  --output-port 5570 `
  --csv-log uploads\aruco_gripper_tracking.csv
