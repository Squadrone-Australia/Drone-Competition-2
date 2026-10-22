# One-step setup and launcher for the COMP2 drone racing simulator on Windows.
#
#   irm https://raw.githubusercontent.com/Squadrone-Australia/Drone-Competition-2/main/scripts/install.ps1 | iex
#
# Gets the code (git clone/pull, or a zip download when git is missing),
# installs uv, creates venv\ with Python 3.13 or 3.14, installs the app and the
# squadrone client into it, then starts the simulator. It also writes the
# "Start COMP2" launcher (plus a desktop shortcut), which runs this script
# again: every start pulls updates, reinstalls only when the code changed, and
# still starts offline as long as the existing install works.
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
    # Where the uv installer puts uv; a launcher window may predate the PATH change.
    $env:Path = (Join-Path $HOME '.local\bin') + ';' + $env:Path

    $Repo = 'https://github.com/Squadrone-Australia/Drone-Competition-2'
    $Api = 'https://api.github.com/repos/Squadrone-Australia/Drone-Competition-2'
    $Port = 8765
    $Steps = 6

    function Step($n, $msg) { Write-Host "[$n/$Steps] $msg" -ForegroundColor Cyan }
    function Note($msg) { Write-Host "      $msg" }
    function Warn($msg) { Write-Host "      Warning: $msg" -ForegroundColor Yellow }
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

    # True when the simulator answers on the port, false when the port is free.
    # The page title tells it apart from another program, which is an error.
    function Test-SimRunning {
        $page = $null
        try {
            $page = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$Port/"
        } catch [System.Net.WebException] {
            # Refused or timed out means free; an HTTP error answer means taken.
            if (-not $_.Exception.Response) { return $false }
        } catch {
            return $false
        }
        if ($page -and $page.Content -match 'Squadrone') { return $true }
        Fail "port $Port is used by another program. Close it and run this again."
    }

    # A venv the simulator can start from as it is.
    function Test-Venv($py) {
        if (-not (Test-Path $py)) { return $false }
        Quiet { & $py -c 'import sys; sys.exit(not (3, 13) <= sys.version_info[:2] < (3, 15))' }
        if ($LASTEXITCODE -ne 0) { return $false }
        Quiet { & $py -c 'import comp2, squadrone, cv2, av' }
        $LASTEXITCODE -eq 0
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
    # Updating would fail on the running app's locked files, so just show it.
    if (Test-SimRunning) {
        Note 'The simulator is already running - opening it in your browser.'
        Start-Process "http://localhost:$Port/"
        return
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
    $Venv = Join-Path $Dir 'venv'
    $Py = Join-Path $Venv 'Scripts\python.exe'
    # The code version the venv was last installed from (written in step 5).
    $Stamp = Join-Path $Venv '.comp2-installed'
    # With a working install, a failed update is a warning, not the end.
    $working = Test-Venv $Py

    # --- 2. Code -------------------------------------------------------------
    Step 2 "Getting the latest code into $Dir"
    $git = Get-Command git -ErrorAction SilentlyContinue
    $version = $null
    if ($git -and (Test-Path (Join-Path $Dir '.git'))) {
        Quiet { git -C "$Dir" pull --ff-only }
        if ($LASTEXITCODE -ne 0) { Warn 'could not update (offline, or local changes). Using the code you have.' }
        $version = (git -C "$Dir" rev-parse HEAD).Trim()
    } elseif ($git -and -not (Test-Path $Dir)) {
        git clone "$Repo.git" "$Dir"
        Check 'git clone'
        $version = (git -C "$Dir" rev-parse HEAD).Trim()
    } elseif ((Test-Path $Dir) -and -not (Test-Repo $Dir) -and (Get-ChildItem $Dir -Force | Select-Object -First 1)) {
        Fail "$Dir exists but is not the COMP2 repository. Set COMP2_DIR to another folder."
    } else {
        if (-not $git) { Note 'git not found - using a zip download instead.' }
        # Without git, .comp2-version holds the commit the files came from, so
        # the archive is only downloaded when main has moved on.
        $marker = Join-Path $Dir '.comp2-version'
        $have = $null
        if ((Test-Repo $Dir) -and (Test-Path $marker)) { $have = (Get-Content $marker -Raw).Trim() }
        $latest = $null
        try {
            $latest = (Invoke-RestMethod -TimeoutSec 15 -Uri "$Api/commits/main" -Headers @{ Accept = 'application/vnd.github.sha' }).Trim()
        } catch {}
        if ($latest -and $latest -eq $have) {
            $version = $have
        } elseif (-not $latest -and (Test-Repo $Dir)) {
            Warn 'could not check for updates (offline?). Using the code you have.'
            $version = $have
        } else {
            $ref = if ($latest) { $latest } else { 'refs/heads/main' }
            $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("comp2-" + [guid]::NewGuid())
            New-Item -ItemType Directory -Path $tmp | Out-Null
            try {
                $zip = Join-Path $tmp 'code.zip'
                Invoke-WebRequest -UseBasicParsing -Uri "$Repo/archive/$ref.zip" -OutFile $zip
                Expand-Archive -Path $zip -DestinationPath $tmp
                New-Item -ItemType Directory -Force -Path $Dir | Out-Null
                # Copy over the top: keeps venv\ and the contestant's own files.
                $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
                Copy-Item -Path (Join-Path $src.FullName '*') -Destination $Dir -Recurse -Force
                if ($latest) { Set-Content -Path $marker -Value $latest -Encoding ascii }
                $version = $latest
            } catch {
                if (-not ((Test-Repo $Dir) -and $working)) { throw }
                Warn "could not download the update ($($_.Exception.Message)). Using the code you have."
                $version = $have
            } finally {
                Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
            }
        }
    }
    if (-not (Test-Repo $Dir)) { Fail "no COMP2 code found in $Dir" }

    $installed = $null
    if (Test-Path $Stamp) { $installed = (Get-Content $Stamp -Raw).Trim() }
    if ($working -and $version -and $version -eq $installed) {
        # Same code as the last install: no uv or network needed.
        Note 'Already up to date.'
    } else {
        try {
            # --- 3. uv -------------------------------------------------------
            Step 3 'Checking for uv (Python installer)'
            if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
                Note 'Installing uv from https://astral.sh/uv ...'
                # A child shell, so the installer cannot touch this session's state.
                powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
                Check 'uv install'
                if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
                    Fail 'uv was installed but cannot be found. Open a new PowerShell window and run this again.'
                }
            }
            uv --version

            # --- 4. venv -----------------------------------------------------
            Step 4 'Preparing the Python environment (venv)'
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

            # --- 5. Packages -------------------------------------------------
            Step 5 'Installing the simulator and the squadrone client'
            # Old name of the client package; the current one may share its files.
            Quiet { uv pip uninstall --python "$Py" squadrone-client }
            # Editable: code changes take effect as they are pulled; this step
            # only reruns to pick up changed dependencies.
            uv pip install --python "$Py" -e "$Dir" -e (Join-Path $Dir 'client')
            Check 'installing packages'
            & $Py -c 'import comp2, squadrone, cv2, av'
            Check 'import check'
            if ($version) { Set-Content -Path $Stamp -Value $version -Encoding ascii }
        } catch {
            # Offline with an install that still works: start that one.
            if (-not (Test-Venv $Py)) { throw }
            Warn "could not finish updating ($($_.Exception.Message)). Starting the version you have."
        }
    }

    # --- 6. Launch -----------------------------------------------------------
    # %~dp0 keeps the file ASCII: cmd.exe would misread a non-ASCII user name.
    $launcher = Join-Path $Dir 'Start COMP2.cmd'
    $cmd = "@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0scripts\install.ps1`"`r`nif errorlevel 1 pause`r`n"
    [System.IO.File]::WriteAllText($launcher, $cmd, [System.Text.Encoding]::ASCII)
    $shortcut = $null
    $desktop = [Environment]::GetFolderPath('Desktop')
    if ($desktop -and (Test-Path $desktop)) {
        try {
            $shortcut = Join-Path $desktop 'Start COMP2.lnk'
            $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
            $lnk.TargetPath = $launcher
            $lnk.WorkingDirectory = $Dir
            $lnk.Description = 'Update and start the COMP2 drone racing simulator'
            $lnk.Save()
        } catch {
            $shortcut = $null
        }
    }

    Write-Host ''
    Write-Host 'COMP2 is ready.' -ForegroundColor Green
    Write-Host "  Select this interpreter in your IDE:  $Py"
    if ($shortcut) {
        Write-Host '  Next time, double-click "Start COMP2" on your desktop'
        Write-Host "  (or $launcher). It updates COMP2, then starts it."
    } else {
        Write-Host "  Next time, double-click $launcher"
        Write-Host '  It updates COMP2, then starts it.'
    }
    Write-Host ''
    if ($env:COMP2_NO_LAUNCH -eq '1') { return }
    Step 6 'Starting the simulator (Ctrl+C to stop)'
    Push-Location $Dir
    try { & $Py -m comp2 } finally { Pop-Location }
}

Install-Comp2
