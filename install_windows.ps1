# Install Maseru Smart Traffic as a Windows app:
#   - Python environment at %USERPROFILE%\.venvs\maseru (short path: Windows can't load the AI's DLLs
#     from very long folder paths)
#   - "Maseru Smart Traffic" shortcuts on the Desktop and in the Start menu
# Run from this folder:  powershell -ExecutionPolicy Bypass -File install_windows.ps1
Set-Location $PSScriptRoot
$venv = "$env:USERPROFILE\.venvs\maseru"

if (-not (Test-Path "$venv\Scripts\python.exe")) {
    Write-Host "Creating Python environment (first time only)..."
    python -m venv $venv
}
Write-Host "Installing / checking packages (the first time downloads ~250 MB)..."
& "$venv\Scripts\python.exe" -m pip install --timeout 120 -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
if (-not (Test-Path cameras.json)) { Copy-Item cameras.example.json cameras.json }

$shell = New-Object -ComObject WScript.Shell
$places = @([Environment]::GetFolderPath("Desktop"), "$env:APPDATA\Microsoft\Windows\Start Menu\Programs")
foreach ($dir in $places) {
    $lnk = $shell.CreateShortcut("$dir\Maseru Smart Traffic.lnk")
    $lnk.TargetPath = "$venv\Scripts\pythonw.exe"
    $lnk.Arguments = "`"$PSScriptRoot\desktop\maseru_traffic.py`""
    $lnk.WorkingDirectory = $PSScriptRoot
    $lnk.IconLocation = "$PSScriptRoot\desktop\app.ico"
    $lnk.Description = "Maseru Smart Traffic - live junction monitoring"
    $lnk.Save()
    Write-Host "Shortcut: $dir\Maseru Smart Traffic.lnk"
}
Write-Host "`nDone. Start the app from the 'Maseru Smart Traffic' icon."
Write-Host "Windows may ask to allow network access the first time - allow it so phones on your network can connect."
