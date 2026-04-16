param(
    [string]$HostIP = "0.0.0.0",
    [int]$Port = 8000
)

if (Get-Command py -ErrorAction SilentlyContinue) {
    py capture_upload_server.py --host $HostIP --port $Port
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    python capture_upload_server.py --host $HostIP --port $Port
} else {
    Write-Error "Python launcher not found. Install Python 3 first."
}
