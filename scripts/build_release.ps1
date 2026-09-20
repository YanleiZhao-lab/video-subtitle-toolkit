param([string]$Version = '1.0.0')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$releaseRoot = Join-Path $projectRoot 'release'
$stage = Join-Path $releaseRoot "video-subtitle-toolkit-$Version-windows-x64"
$buildRoot = Join-Path $projectRoot 'build'
$distRoot = Join-Path $projectRoot 'dist'
foreach ($target in @($releaseRoot, $buildRoot, $distRoot)) {
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}
& python -m PyInstaller --noconfirm --clean "$projectRoot\video-subtitle-toolkit.spec"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item -Path "$projectRoot\dist\VideoSubtitleToolkit\*" -Destination $stage -Recurse
Copy-Item -LiteralPath "$projectRoot\README.md","$projectRoot\README.zh-CN.md","$projectRoot\LICENSE","$projectRoot\THIRD_PARTY_NOTICES.md" -Destination $stage
$zip = Join-Path $releaseRoot "video-subtitle-toolkit-$Version-windows-x64.zip"
Compress-Archive -Path "$stage\*" -DestinationPath $zip -CompressionLevel Optimal
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLower()
Set-Content -LiteralPath "$zip.sha256" -Value "$hash  $(Split-Path -Leaf $zip)" -Encoding ascii
Write-Host "Release: $zip"
Write-Host "SHA256: $hash"
