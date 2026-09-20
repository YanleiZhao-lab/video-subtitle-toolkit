$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$errors = [System.Collections.Generic.List[string]]::new()
$textExtensions = @('.py', '.md', '.json', '.yml', '.yaml', '.ps1', '.bat', '.toml', '.spec')
$excluded = @('\.git\', '\.venv\', '\build\', '\dist\', '\release\', '\tests\')
$files = Get-ChildItem -LiteralPath $projectRoot -File -Recurse | Where-Object {
    $path = $_.FullName
    ($textExtensions -contains $_.Extension) -and -not ($excluded | Where-Object { $path -like "*$_*" })
}
foreach ($file in $files) {
    if ($file.FullName -eq $PSCommandPath) { continue }
    $content = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue
    if ($content -match '(?i)[A-Z]:\\Users\\[^\\]+|[A-Z]:\\Python\d+|\.chatgpt|api[_-]?key\s*[:=]\s*["''][^"'']+') {
        $errors.Add("Local path or credential pattern: $($file.FullName)")
    }
}
Get-ChildItem -LiteralPath $projectRoot -File -Recurse | Where-Object {
    $_.FullName -notlike '*\.git\*' -and $_.Length -gt 25MB
} | ForEach-Object { $errors.Add("File exceeds 25 MiB: $($_.FullName)") }
if ($errors.Count) {
    $errors | ForEach-Object { Write-Error $_ }
    exit 1
}
Write-Host 'Repository privacy and large-file audit passed.'
