param(
    [string]$BindHost = "0.0.0.0",
    [int]$VideoPort = 5560,
    [int]$PosePort = 5555
)

if (Get-Command py -ErrorAction SilentlyContinue) {
    py udp_video_debug_ui.py --bind $BindHost --video-port $VideoPort --pose-port $PosePort
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    python udp_video_debug_ui.py --bind $BindHost --video-port $VideoPort --pose-port $PosePort
} else {
    Write-Error "Python launcher not found. Install Python 3 first."
}
