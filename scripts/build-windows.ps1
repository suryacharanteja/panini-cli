param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BuildRoot = Join-Path $ProjectRoot 'build\windows'
$StagingRoot = Join-Path $BuildRoot 'release'
$DistRoot = Join-Path $ProjectRoot 'dist'
$Release = Join-Path $DistRoot 'panini-cli.exe'
$SavedPythonPath = $env:PYTHONPATH
Push-Location -LiteralPath $ProjectRoot
try {
    $BuildPython = Join-Path $BuildRoot 'environment\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $BuildPython)) {
        & py -3.12 -m venv (Join-Path $BuildRoot 'environment')
        if ($LASTEXITCODE -ne 0) { throw 'Isolated build environment creation failed.' }
    }
    & $BuildPython -m pip install -r (Join-Path $ProjectRoot 'requirements-build.lock')
    if ($LASTEXITCODE -ne 0) { throw 'Locked build dependencies could not be installed.' }
    & (Join-Path $PSScriptRoot 'build-sandbox.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Native sandbox build failed.' }
    $env:PYTHONPATH = Join-Path $ProjectRoot 'src'
    & $BuildPython (Join-Path $PSScriptRoot 'prepare-beta.py') notices
    if ($LASTEXITCODE -ne 0) { throw 'Redistribution notices could not be collected.' }
    # Keep optional packages installed on a developer's PC out of the CLI bundle.
    # None of these are declared PANINI runtime dependencies.
    $BuildArguments = @('-m', 'PyInstaller', '--noconfirm', '--log-level', 'WARN',
        '--onefile', '--noupx', '--name', 'panini-cli', '--icon', (Join-Path $ProjectRoot 'assets\panini-cli.ico'), '--paths', 'src',
        '--distpath', $StagingRoot, '--workpath', (Join-Path $BuildRoot 'work'), '--specpath', $BuildRoot,
        '--collect-data', 'panini_cli.jev', '--collect-submodules', 'panini_cli',
        '--add-data', ((Join-Path $ProjectRoot 'src\panini_cli\licenses') + ';panini_cli/licenses'),
        '--collect-submodules', 'openai', '--collect-submodules', 'prompt_toolkit',
        '--add-data', ((Join-Path $ProjectRoot 'src\panini_cli\panini-sandbox\runtime') + ';panini_cli/panini-sandbox/runtime'),
        '--hidden-import', 'flatbuffers')
    foreach ($OptionalPackage in @('pytest', 'IPython', 'numpy', 'scipy', 'pandas', 'torch',
                                   'tensorflow', 'keras', 'matplotlib', 'cv2', 'sklearn')) {
        $BuildArguments += @('--exclude-module', $OptionalPackage)
    }
    $BuildArguments += 'packaging_entry.py'
    & $BuildPython @BuildArguments
    if ($LASTEXITCODE -ne 0) { throw "Windows build failed with exit code $LASTEXITCODE" }
    $StagedRelease = Join-Path $StagingRoot 'panini-cli.exe'
    & $StagedRelease --version
    if ($LASTEXITCODE -ne 0) { throw 'Single-file executable validation failed; the previous release was preserved.' }
    & $StagedRelease licenses | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Bundled license notices are unavailable; the previous release was preserved.' }
    New-Item -ItemType Directory -Path $DistRoot -Force | Out-Null
    # Keep old releases recoverable, outside the public distribution directory.
    $ArchiveRoot = Join-Path $ProjectRoot ('build\dist-archive\' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6))
    $Running = @(Get-CimInstance Win32_Process -Filter "Name = 'panini-cli.exe'" | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith($DistRoot + '\', [StringComparison]::OrdinalIgnoreCase)
    })
    $Deferred = @()
    foreach ($Item in Get-ChildItem -LiteralPath $DistRoot -Force) {
        $ItemPath = [IO.Path]::GetFullPath($Item.FullName)
        $ArchivePath = [IO.Path]::GetFullPath((Join-Path $ArchiveRoot $Item.Name))
        if (-not $ItemPath.StartsWith($DistRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
            -not $ArchivePath.StartsWith($ArchiveRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
            ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Unsafe distribution archive target: $ItemPath"
        }
        if ($Running | Where-Object {
            $_.ExecutablePath -eq $ItemPath -or $_.ExecutablePath.StartsWith($ItemPath + '\', [StringComparison]::OrdinalIgnoreCase)
        }) {
            if ($ItemPath -eq $Release) { throw 'The current panini-cli.exe is running. Close it and rebuild; it was preserved.' }
            $Deferred += $ItemPath
            continue
        }
        New-Item -ItemType Directory -Path $ArchiveRoot -Force | Out-Null
        Move-Item -LiteralPath $ItemPath -Destination $ArchivePath
    }
    Copy-Item -LiteralPath $StagedRelease -Destination $Release
    Write-Output "Release: $Release"
    if ($Deferred.Count) {
        Write-Warning ('Running older builds were preserved. Close them before rebuilding to finish archiving: ' + ($Deferred -join ', '))
    }
}
finally { $env:PYTHONPATH = $SavedPythonPath; Pop-Location }
