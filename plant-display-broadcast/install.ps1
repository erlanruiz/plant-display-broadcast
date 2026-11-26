<#
  Plant Display all-in-one installer
  Run in PowerShell AS ADMINISTRATOR:
      Set-ExecutionPolicy -Scope Process Bypass -Force ; .\install.ps1

  Installs, verifying each step (never silently half-completes):
    1. Python 3.12.10  (direct python.org download — not winget)
    2. pip  (ensurepip / get-pip fallback)
    3. Python dependencies from requirements.txt
    4. ffmpeg  (direct gyan.dev download onto PATH) — for Chromecast
    5. Playwright browser (Chromium) — for URL mode
    6. Windows Firewall rule for TCP 8090
#>

$ErrorActionPreference = "Stop"

# Resolve the folder this script lives in — robust to being dot-sourced,
# pasted, or launched in ways where $MyInvocation.MyCommand.Path is null.
$Root = $null
if ($PSScriptRoot) { $Root = $PSScriptRoot }
elseif ($MyInvocation.MyCommand.Path) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Root -or -not (Test-Path (Join-Path $Root "requirements.txt"))) {
  if (Test-Path (Join-Path (Get-Location) "requirements.txt")) { $Root = (Get-Location).Path }
}
if (-not $Root) {
  Write-Host "[FAILED] Could not locate the Plant Display folder. In PowerShell, 'cd' into the plantdisplay folder (the one containing requirements.txt) and run: .\install.ps1" -ForegroundColor Red
  Read-Host "Press Enter to exit"; exit 1
}
Set-Location $Root

function Say ($m)  { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Ok  ($m)  { Write-Host "  [OK] $m"   -ForegroundColor Green }
function Warn($m)  { Write-Host "  [!] $m"    -ForegroundColor Yellow }
function Die ($m)  { Write-Host "`n[FAILED] $m" -ForegroundColor Red; Read-Host "Press Enter to exit"; exit 1 }

# --- admin check ---
$isAdmin = ([Security.Principal.WindowsPrincipal] `
  [Security.Principal.WindowsIdentity]::GetCurrent()
  ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) { Die "Right-click PowerShell and 'Run as administrator', then run this again." }

# ------------------------------------------------------------------ Python
Say "Step 1/6  Python 3.12"
function Get-Py {
  foreach ($c in @("py -3","python","python3")) {
    try {
      $v = & cmd /c "$c --version" 2>&1
      if ($v -match "Python 3") { return $c }
    } catch {}
  }
  return $null
}
$py = Get-Py
if ($py) {
  Ok "Python already present ($py — $(& cmd /c "$py --version" 2>&1))"
} else {
  Warn "Python not found — downloading the official 3.12.10 installer"
  $exe = "$env:TEMP\python-3.12.10-amd64.exe"
  Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $exe
  Ok "Downloaded installer"
  Warn "Installing silently (all users, added to PATH, launcher included)…"
  Start-Process $exe -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1" -Wait
  # refresh this session's PATH so we can use it immediately
  $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
              [Environment]::GetEnvironmentVariable("Path","User")
  Start-Sleep 2
  $py = Get-Py
  if (-not $py) { Die "Python installed but not detected. Close this window, open a NEW admin PowerShell, and run install.ps1 again." }
  Ok "Python installed ($(& cmd /c "$py --version" 2>&1))"
}

# ------------------------------------------------------------------ pip
Say "Step 2/6  pip"
& cmd /c "$py -m pip --version" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  Warn "pip missing — bootstrapping with ensurepip"
  & cmd /c "$py -m ensurepip --upgrade" 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Warn "ensurepip unavailable — using get-pip.py"
    $gp = "$env:TEMP\get-pip.py"
    Invoke-WebRequest "https://bootstrap.pypa.io/get-pip.py" -OutFile $gp
    & cmd /c "$py `"$gp`"" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Die "Could not install pip." }
  }
}
& cmd /c "$py -m pip install --upgrade pip" 2>&1 | Out-Null
Ok "pip ready"

# ------------------------------------------------------------------ deps
Say "Step 3/6  Python dependencies"
if (-not (Test-Path "$Root\requirements.txt")) { Die "requirements.txt not found beside this script." }
& cmd /c "$py -m pip install -r `"$Root\requirements.txt`""
if ($LASTEXITCODE -ne 0) { Die "Dependency install failed — see errors above." }
# verify the imports that actually matter
$check = "import flask,PIL,mss,pychromecast,samsungtvws,paho.mqtt.client; print('deps ok')"
$r = & cmd /c "$py -c `"$check`"" 2>&1
if ($r -notmatch "deps ok") { Die "Dependencies installed but a key import failed: $r" }
Ok "All Python dependencies verified"

# ------------------------------------------------------------------ ffmpeg
Say "Step 4/6  ffmpeg (for Chromecast)"
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
  Ok "ffmpeg already on PATH"
} else {
  try {
    Warn "Downloading ffmpeg (gyan.dev, ~90 MB)…"
    $zip = "$env:TEMP\ffmpeg.zip"
    Invoke-WebRequest "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip
    $dest = "C:\ffmpeg"
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    Expand-Archive $zip -DestinationPath $dest -Force
    $bin = (Get-ChildItem $dest -Recurse -Filter ffmpeg.exe | Select-Object -First 1).DirectoryName
    if (-not $bin) { throw "ffmpeg.exe not found after extract" }
    $machinePath = [Environment]::GetEnvironmentVariable("Path","Machine")
    if ($machinePath -notlike "*$bin*") {
      [Environment]::SetEnvironmentVariable("Path", "$machinePath;$bin", "Machine")
    }
    $env:Path += ";$bin"
    Ok "ffmpeg installed to $bin"
  } catch {
    Warn "ffmpeg install failed ($_). Chromecast will be disabled until installed; Samsung/Hisense TVs still work. You can retry later with: winget install Gyan.FFmpeg"
  }
}

# -------------------------------------------------------- playwright browser
Say "Step 5/6  Playwright browser (for URL mode)"
try {
  Warn "Downloading Chromium for the virtual browser (~150 MB, one time)…"
  & cmd /c "$py -m playwright install chromium" 2>&1 | Out-Null
  if ($LASTEXITCODE -eq 0) { Ok "Playwright Chromium installed" }
  else { Warn "playwright install returned $LASTEXITCODE — URL mode will fall back to the Microsoft Edge already on this PC." }
} catch {
  Warn "Could not install Playwright's browser ($_). URL mode will fall back to Microsoft Edge."
}

# ------------------------------------------------------------------ firewall
Say "Step 6/6  Firewall (TCP 8090)"
if (Get-NetFirewallRule -DisplayName "Plant Display 8090" -ErrorAction SilentlyContinue) {
  Ok "Firewall rule already exists"
} else {
  New-NetFirewallRule -DisplayName "Plant Display 8090" -Direction Inbound `
    -Action Allow -Protocol TCP -LocalPort 8090 -Profile Any | Out-Null
  Ok "Opened inbound TCP 8090"
}

# ------------------------------------------------------------------ done
Say "Done"
Write-Host "  Plant Display is installed." -ForegroundColor Green
Write-Host "  Next:" -ForegroundColor Gray
Write-Host "    1. Open config.json to see or change the two passwords (random at first start)." -ForegroundColor Gray
Write-Host "    2. Double-click run.bat (or: $py app.py)." -ForegroundColor Gray
Write-Host "    3. On a TV browser open the URL the console prints." -ForegroundColor Gray
$launch = Read-Host "`nStart Plant Display now? (Y/N)"
if ($launch -match '^[Yy]') { Start-Process cmd -ArgumentList "/k","$py `"$Root\app.py`"" }
