# Start Maseru Smart Traffic on http://localhost:8000
# The virtual environment lives at a short path: Windows can't load OpenCV/PyTorch DLLs
# from very long folder paths ("The filename or extension is too long").
Set-Location $PSScriptRoot
$venv = "$env:USERPROFILE\.venvs\maseru"
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    python -m venv $venv
    & "$venv\Scripts\python.exe" -m pip install -r requirements.txt
}
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
if (-not (Test-Path cameras.json)) { Copy-Item cameras.example.json cameras.json }
& "$venv\Scripts\python.exe" -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
