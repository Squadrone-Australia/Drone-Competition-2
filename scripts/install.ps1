# One-step setup for the COMP2 drone racing simulator on Windows.
#
#   irm https://raw.githubusercontent.com/Squadrone-Australia/Drone-Competition-2/main/scripts/install.ps1 | iex
#
# Gets the code (git clone/pull, or a zip download when git is missing),
# installs uv, creates venv\ with Python 3.13 or 3.14, installs the app and the
# squadrone client into it, then starts the simulator. Run it again to update.
#
# Environment variables:
#   COMP2_DIR        install folder (default: .\Drone-Competition-2, or the
#                    repository the script is run from)
#   COMP2_NO_LAUNCH  set to 1 to stop after installing (used by CI)
#
# Everything lives in one function: under `irm | iex` an `exit` would close
# the contestant's PowerShell window, so failures `throw` instead.

function Install-Comp2 {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is very slow with the progress bar on PS 5.1
    $env:PYTHONUTF8 = '1'

    $Repo = 'https://github.com/Squadrone-Australia/Drone-Competition-2'
    $Port = 8765
    $Steps = 6

    function Step($n, $msg) { Write-Host "[$n/$Steps] $msg" -ForegroundColor Cyan }
    function Note($msg) { Write-Host "      $msg" }
    function Fail($msg) { throw "COMP2 setup failed: $msg" }
    # Native commands don't throw on failure; check the exit code after each one.
    function Check($what) { if ($LASTEXITCODE -ne 0) { Fail "$what (exit code $LASTEXITCODE)" } }
    # Runs a native command whose failure is expected and handled by the caller.
    # PS 5.1 turns redirected stderr into a terminating error under 'Stop'.
    function Quiet([scriptblock]$cmd) {
        $ErrorActionPreference = 'Continue'
        & $cmd 2>&1 | Out-Null
    }

    function Test-Repo($dir) {
        (Test-Path (Join-Path $dir 'comp2\__main__.py')) -and (Test-Path (Join-Path $dir 'client\pyproject.toml'))
    }

    function Test-PortOpen($port) {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $async = $client.BeginConnect('127.0.0.1', $port, $null, $null)
            $async.AsyncWaitHandle.WaitOne(500) -and $client.Connected
        } catch { $false } finally { $client.Close() }
    }

    # --- 1. Platform ---------------------------------------------------------
    Step 1 'Checking this computer'
    $arch = $env:PROCESSOR_ARCHITECTURE
    try { $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() } catch {}
    # The Python versions CI tests (.github/workflows/ci.yml); a newer release
    # may not have numpy/opencv/av wheels yet.
    $PythonRequest = '>=3.13,<3.15'
    # opencv-python ships no Windows ARM64 wheel; an x64 Python runs under emulation.
    if ($arch -match 'ARM64') {
        $PythonRequest = 'cpython-3.14-windows-x86_64-none'
        Note 'Windows on ARM: using x64 Python (runs under emulation) because OpenCV has no ARM64 build.'
    }
    if (Test-PortOpen $Port) {
        Fail "port $Port is in use - the simulator is probably already running. Close it (Ctrl+C in its window) and run this again."
    }

    # Where to install: COMP2_DIR, else the repository this script or the
    # current folder belongs to, else a new folder here.
    if ($env:COMP2_DIR) {
        $Dir = $env:COMP2_DIR
    } elseif ($PSScriptRoot -and (Test-Repo (Split-Path $PSScriptRoot -Parent))) {
        $Dir = Split-Path $PSScriptRoot -Parent
    } elseif (Test-Repo (Get-Location).Path) {
        $Dir = (Get-Location).Path
    } else {
        $Dir = Join-Path (Get-Location).Path 'Drone-Competition-2'
    }
    $Dir = [System.IO.Path]::GetFullPath($Dir)

    # --- 2. Code -------------------------------------------------------------
    Step 2 "Getting the code into $Dir"
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git -and (Test-Path (Join-Path $Dir '.git'))) {
        git -C "$Dir" pull --ff-only
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'git pull failed (local changes?). Continuing with the code you have.'
        }
    } elseif ($git -and -not (Test-Path $Dir)) {
        git clone "$Repo.git" "$Dir"
        Check 'git clone'
    } elseif ((Test-Path $Dir) -and -not (Test-Repo $Dir) -and (Get-ChildItem $Dir -Force | Select-Object -First 1)) {
        Fail "$Dir exists but is not the COMP2 repository. Set COMP2_DIR to another folder."
    } else {
        if (-not $git) { Note 'git not found - downloading a zip instead (run this script again to update).' }
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("comp2-" + [guid]::NewGuid())
        New-Item -ItemType Directory -Path $tmp | Out-Null
        try {
            $zip = Join-Path $tmp 'main.zip'
            Invoke-WebRequest -UseBasicParsing -Uri "$Repo/archive/refs/heads/main.zip" -OutFile $zip
            Expand-Archive -Path $zip -DestinationPath $tmp
            New-Item -ItemType Directory -Force -Path $Dir | Out-Null
            # Copy over the top: keeps venv\ and the contestant's own files.
            Copy-Item -Path (Join-Path $tmp 'Drone-Competition-2-main\*') -Destination $Dir -Recurse -Force
        } finally {
            Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        }
    }
    if (-not (Test-Repo $Dir)) { Fail "no COMP2 code found in $Dir" }

    # --- 3. uv ---------------------------------------------------------------
    Step 3 'Checking for uv (Python installer)'
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Note 'Installing uv from https://astral.sh/uv ...'
        # A child shell, so the installer cannot touch this session's state.
        powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
        Check 'uv install'
        $env:Path = (Join-Path $HOME '.local\bin') + ';' + $env:Path
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            Fail 'uv was installed but cannot be found. Open a new PowerShell window and run this again.'
        }
    }
    uv --version

    # --- 4. venv -------------------------------------------------------------
    Step 4 'Preparing the Python environment (venv)'
    $Venv = Join-Path $Dir 'venv'
    $Py = Join-Path $Venv 'Scripts\python.exe'
    $ok = $false
    if (Test-Path $Py) {
        Quiet { & $Py -c 'import sys; sys.exit(not (3, 13) <= sys.version_info[:2] < (3, 15))' }
        $ok = ($LASTEXITCODE -eq 0)
        if (-not $ok) { Note 'Existing venv is broken or not Python 3.13/3.14 - recreating it.' }
    }
    if (-not $ok) {
        # --seed puts pip in the venv so the README's manual update commands keep working.
        uv venv "$Venv" --python "$PythonRequest" --seed --clear
        Check 'creating the venv'
    }
    & $Py --version

    # --- 5. Packages ---------------------------------------------------------
    Step 5 'Installing the simulator and the squadrone client'
    # Old name of the client package; the current one may share its files.
    Quiet { uv pip uninstall --python "$Py" squadrone-client }
    # Editable: a later git pull takes effect without reinstalling.
    uv pip install --python "$Py" -e "$Dir" -e (Join-Path $Dir 'client')
    Check 'installing packages'
    & $Py -c 'import comp2, squadrone, cv2, av'
    Check 'import check'

    # --- 6. Launch -----------------------------------------------------------
    Write-Host ''
    Write-Host 'COMP2 is installed.' -ForegroundColor Green
    Write-Host "  Select this interpreter in your IDE:  $Py"
    Write-Host "  Start the simulator later with:       & '$Py' -m comp2"
    Write-Host ''
    if ($env:COMP2_NO_LAUNCH -eq '1') { return }
    Step 6 'Starting the simulator (Ctrl+C to stop)'
    Push-Location $Dir
    try { & $Py -m comp2 } finally { Pop-Location }
}

Install-Comp2
