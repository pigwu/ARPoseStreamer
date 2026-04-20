param(
    [string]$HostIP = "0.0.0.0",
    [int]$Port = 5555,
    [string]$Encoding = "binary"
)

if (Get-Command py -ErrorAction SilentlyContinue) {
    py udp_pose_receiver.py --host $HostIP --port $Port --encoding $Encoding
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    python udp_pose_receiver.py --host $HostIP --port $Port --encoding $Encoding
} else {
    Write-Error "Python launcher not found. Install Python 3 first."
}
