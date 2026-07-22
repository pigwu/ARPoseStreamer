param(
    [string]$Port = "COM9",
    [int]$Baud = 115200
)

$ScriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$Mapper = Join-Path $ScriptDirectory "anyskin_serial_mapper.py"
$MapperArguments = @($Mapper, "--port", $Port, "--baud", $Baud)

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py @MapperArguments
    exit $LASTEXITCODE
}
if (Get-Command python -ErrorAction SilentlyContinue) {
    & python @MapperArguments
    exit $LASTEXITCODE
}

Write-Error "Python launcher not found. Install Python 3 first."
exit 1
