param([switch]$PackagedUat)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TestPython = Join-Path $ProjectRoot 'build\windows\environment\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $TestPython)) {
    throw 'Create the isolated environment with scripts\build-windows.ps1 first.'
}
Push-Location -LiteralPath $ProjectRoot
try {
    function Get-SourceSnapshot {
        $Files = @(Get-ChildItem (Join-Path $ProjectRoot 'src\panini_cli') -Recurse -File -Filter '*.py')
        $Files += @(Get-ChildItem (Join-Path $ProjectRoot 'tests') -File -Filter '*.py')
        $Files += @(Get-ChildItem (Join-Path $ProjectRoot 'src\panini_cli\panini-sandbox\runtime') -Recurse -File -Filter 'build.json')
        @($Files | Sort-Object FullName | ForEach-Object {
            @{ path = $_.FullName.Substring($ProjectRoot.Length + 1); sha256 = (Get-FileHash -LiteralPath $_.FullName).Hash }
        }) | ConvertTo-Json -Compress
    }
    $Before = Get-SourceSnapshot
    New-Item -ItemType Directory -Path 'build\coverage' -Force | Out-Null
    Set-Content 'build\coverage\source-before.json' $Before -Encoding utf8
    $Release = Join-Path $ProjectRoot 'dist\panini-cli.exe'
    if (Test-Path -LiteralPath $Release) {
        & $Release sandbox install
        if ($LASTEXITCODE -ne 0) { throw 'Current native runtime installation failed.' }
    }
    & $TestPython -m coverage erase
    if ($LASTEXITCODE -ne 0) { throw 'Coverage reset failed.' }
    & $TestPython -m coverage run -m pytest -q --junitxml=build/coverage/pytest.xml
    $TestExit = $LASTEXITCODE
    if ($PackagedUat) {
        & $TestPython -m coverage run scripts/smoke-sandbox.py
        if ($LASTEXITCODE -ne 0) { $TestExit = $LASTEXITCODE }
        & $TestPython -m coverage run scripts/uat-web-sandbox.py
        if ($LASTEXITCODE -ne 0) { $TestExit = $LASTEXITCODE }
    }
    & $TestPython -m coverage combine --append --keep
    if ($LASTEXITCODE -ne 0) { throw 'Subprocess coverage collection failed.' }
    & $TestPython -m coverage json --keep-combined -o build/coverage/coverage.json
    if ($LASTEXITCODE -ne 0) { throw 'Coverage JSON generation failed.' }
    & $TestPython -m coverage html --keep-combined -d build/coverage/html
    if ($LASTEXITCODE -ne 0) { throw 'Coverage HTML generation failed.' }
    & $TestPython -m coverage report --keep-combined
    $After = Get-SourceSnapshot
    Set-Content 'build\coverage\source-after.json' $After -Encoding utf8
    if ($Before -ne $After) { throw 'Source or runtime changed during verification; do not treat this coverage run as closure.' }
    if ($TestExit -ne 0) { throw "Tests failed with exit code $TestExit; coverage reports were retained." }
}
finally { Pop-Location }
