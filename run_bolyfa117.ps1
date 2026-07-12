# BOLYFA 117 Data Logger Launcher (PowerShell)
# =============================================

Write-Host "BOLYFA 117 USB Digital Multimeter Data Logger" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# Check Python
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "[ERROR] Python not found. Install from https://python.org" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# Check pyserial
python -c "import serial" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[INFO] Installing pyserial..." -ForegroundColor Yellow
    pip install pyserial
}

Write-Host ""
Write-Host "Choose mode:" -ForegroundColor White
Write-Host "  1. Live console output"
Write-Host "  2. CSV data logger"
Write-Host "  3. Web dashboard"
Write-Host "  4. List available COM ports"
Write-Host ""
$choice = Read-Host "Enter choice (1-4)"

if ($choice -eq "4") {
    python bolyfa117_logger.py --list
    Read-Host "Press Enter to exit"
    exit 0
}

Write-Host ""
$port = Read-Host "Enter COM port (e.g., COM3, COM4, or just 3, 4)"

# Auto-fix bare numbers
if ($port -match '^\d+$') {
    $port = "COM$port"
    Write-Host "[INFO] Auto-corrected to $port" -ForegroundColor Green
}

switch ($choice) {
    "1" { python bolyfa117_logger.py --mode live --port $port }
    "2" { python bolyfa117_logger.py --mode csv --port $port }
    "3" { python bolyfa117_logger.py --mode dashboard --port $port }
    default { Write-Host "Invalid choice." -ForegroundColor Red }
}

Read-Host "Press Enter to exit"
