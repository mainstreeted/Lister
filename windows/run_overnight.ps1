# Lister — overnight applier launcher for Windows Task Scheduler.
#
# Register it to run nightly (~4am, while Ed is asleep) with:
#
#   schtasks /Create /TN "Lister Applier" /SC DAILY /ST 04:00 ^
#     /TR "powershell -ExecutionPolicy Bypass -File C:\path\to\Lister\windows\run_overnight.ps1"
#
# Run it once by hand first to confirm it works.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Find a Python interpreter.
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Error "No Python found on PATH. Install Python 3.11+ for Windows."
    exit 1
}

Write-Host "Lister applier: starting $(Get-Date -Format o)"
& $py.Source "applier.py" @args
$code = $LASTEXITCODE
Write-Host "Lister applier: exit code $code"
exit $code
