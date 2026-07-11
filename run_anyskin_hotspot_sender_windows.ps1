param(
    [string]$PhoneIP = "172.20.10.1",
    [int]$Port = 5557,
    [double]$Rate = 100
)

$ScriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$Sender = Join-Path $ScriptDirectory "anyskin_hotspot_sender.py"
$SenderArguments = @($Sender, "--host", $PhoneIP, "--port", $Port, "--rate", $Rate)

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py @SenderArguments
    exit $LASTEXITCODE
}
if (Get-Command python -ErrorAction SilentlyContinue) {
    & python @SenderArguments
    exit $LASTEXITCODE
}

Write-Error "Python launcher not found. Install Python 3 first."
exit 1
