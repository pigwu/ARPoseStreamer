param(
    [string]$BindHost = "0.0.0.0",
    [int]$VideoPort = 5560,
    [int]$PosePort = 5555,
    [int]$CombinedPort = 5558,
    [int]$UploadPort = 8000,
    [string]$PhoneIP = "172.20.10.1",
    [string]$ArucoConfig = "config\umi_gripper_aruco.json"
)

Set-Location $PSScriptRoot

$arguments = @(
    "experiment_replay_ui.py",
    "--bind", $BindHost,
    "--video-port", $VideoPort,
    "--pose-port", $PosePort,
    "--combined-port", $CombinedPort,
    "--upload-port", $UploadPort,
    "--phone-ip", $PhoneIP,
    "--aruco-config", $ArucoConfig
)

if (Get-Command py -ErrorAction SilentlyContinue) {
    py @arguments
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    python @arguments
} else {
    Write-Error "Python launcher not found. Install Python 3 first."
}
