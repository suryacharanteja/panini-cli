param([ValidateSet('x64', 'arm64')][string]$Architecture = 'x64')

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SourceRoot = Join-Path $ProjectRoot 'src\panini_cli\panini-sandbox'
$Target = if ($Architecture -eq 'arm64') { 'aarch64-pc-windows-msvc' } else { 'x86_64-pc-windows-msvc' }
$Cargo = Join-Path $env:USERPROFILE '.cargo\bin\cargo.exe'
if (-not (Test-Path -LiteralPath $Cargo)) {
    $Cargo = (Get-Command cargo -ErrorAction Stop).Source
}
$VsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$VsRoot = & $VsWhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $VsRoot) { throw 'Install Visual Studio C++ build tools and the Windows SDK before building PANINI Sandbox.' }
Import-Module (Join-Path $VsRoot 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
Enter-VsDevShell -VsInstallPath $VsRoot -SkipAutomaticLocation -DevCmdArguments "-arch=$Architecture -host_arch=x64" | Out-Null
$SavedTargetDir = $env:CARGO_TARGET_DIR
$SavedSourceId = $env:PANINI_SANDBOX_SOURCE_ID
$TargetRoot = Join-Path $ProjectRoot 'build\panini-sandbox'
$RuntimeRoot = Join-Path $SourceRoot "runtime\win-$Architecture"
function Get-SourceHashes {
    $Hashes = [ordered]@{}
    Get-ChildItem -LiteralPath $SourceRoot -File -Recurse |
        Where-Object { -not $_.FullName.StartsWith((Join-Path $SourceRoot 'runtime') + '\', [StringComparison]::OrdinalIgnoreCase) } |
        Sort-Object FullName | ForEach-Object {
            $RelativePath = $_.FullName.Substring($SourceRoot.Length + 1).Replace('\', '/')
            $Hashes[$RelativePath] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    return $Hashes
}
Push-Location -LiteralPath (Join-Path $SourceRoot 'src')
try {
    $env:CARGO_TARGET_DIR = $TargetRoot
    $SourceHashes = Get-SourceHashes
    $SourceJson = $SourceHashes | ConvertTo-Json -Compress
    $HashAlgorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $env:PANINI_SANDBOX_SOURCE_ID = ([BitConverter]::ToString($HashAlgorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($SourceJson)))).Replace('-', '').ToLowerInvariant().Substring(0, 24)
    }
    finally { $HashAlgorithm.Dispose() }
    # The original source pins Rust 1.93. Preserve its CFG, ASLR, CET and static CRT flags.
    & $Cargo build --release --locked --target $Target -p wxc -p mxc_ffi
    if ($LASTEXITCODE -ne 0) { throw "Native sandbox build failed with exit code $LASTEXITCODE" }
    if ((Get-SourceHashes | ConvertTo-Json -Compress) -ne $SourceJson) { throw 'Sandbox source changed during compilation. Build again before installing.' }
    New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
    foreach ($FileName in @('wxc-exec.exe', 'mxc_ffi.dll')) {
        Copy-Item -LiteralPath (Join-Path $TargetRoot "$Target\release\$FileName") -Destination $RuntimeRoot -Force
    }
    Copy-Item -LiteralPath (Join-Path $SourceRoot 'LICENSE.md') -Destination $RuntimeRoot -Force
    $Files = [ordered]@{}
    foreach ($FileName in @('wxc-exec.exe', 'mxc_ffi.dll', 'LICENSE.md')) {
        $Files[$FileName] = (Get-FileHash -LiteralPath (Join-Path $RuntimeRoot $FileName) -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $Manifest = [ordered]@{
        format = 1
        engine = 'PANINI Sandbox (Microsoft MXC 1.0.0 source)'
        target = $Target
        rust = (& $Cargo --version)
        sourceId = $env:PANINI_SANDBOX_SOURCE_ID
        sources = $SourceHashes
        files = $Files
    }
    $Manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $RuntimeRoot 'build.json') -Encoding utf8
    Write-Output "Built PANINI Sandbox from $SourceRoot into $RuntimeRoot"
}
finally {
    $env:CARGO_TARGET_DIR = $SavedTargetDir
    $env:PANINI_SANDBOX_SOURCE_ID = $SavedSourceId
    Pop-Location
}
