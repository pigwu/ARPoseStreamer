param(
    [string]$BindHost = "0.0.0.0",
    [int]$Port = 5558,
    [string]$PhoneIP = "",
    [int]$VideoPort = 5560,
    [string]$OutputDirectory = ""
)

$ScriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$Receiver = Join-Path $ScriptDirectory "pose_magnetic_receiver.py"
$ReceiverArguments = @(
    $Receiver,
    "--host", $BindHost,
    "--port", $Port,
    "--video-port", $VideoPort
)

if ($PhoneIP) {
    $ReceiverArguments += @("--phone-ip", $PhoneIP)
}
if ($OutputDirectory) {
    $ReceiverArguments += @("--output-dir", $OutputDirectory)
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py @ReceiverArguments
    exit $LASTEXITCODE
}
if (Get-Command python -ErrorAction SilentlyContinue) {
    & python @ReceiverArguments
    exit $LASTEXITCODE
}

Write-Error "Python launcher not found. Install Python 3 first."
exit 1
