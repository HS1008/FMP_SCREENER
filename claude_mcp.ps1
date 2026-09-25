$ErrorActionPreference = "Stop"

$Repo = "C:\Users\dipka\Documents\FMP_SCREENER"
$Python = "$Repo\.venv\Scripts\python.exe"
$SecretFile = "$env:APPDATA\FMP_Dashboard\dburl.secure.xml"

# The PostgreSQL tunnel is managed independently by Windows Task Scheduler.
$listener = Get-NetTCPConnection `
    -LocalAddress 127.0.0.1 `
    -LocalPort 5433 `
    -State Listen `
    -ErrorAction SilentlyContinue

if (-not $listener) {
    throw "FMP PostgreSQL tunnel is unavailable on 127.0.0.1:5433."
}

if (-not (Test-Path $SecretFile)) {
    throw "Secure database configuration is missing."
}

$secure = Import-Clixml $SecretFile
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)

try {
    $env:DATABASE_READONLY_URL =
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
}

$env:PYTHONPATH = $Repo

Set-Location $Repo

& $Python -m ai_gateway --stdio
