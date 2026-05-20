$workDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $workDir

$today = (Get-Date).ToString("yyyy-MM-dd")

$status = git status --porcelain docs/data/ 2>&1
if (-not $status) {
    Write-Host "[$today] no changes - skipping push"
    exit 0
}

git add docs/data/
git commit -m "data: update $today"
git push origin master

if ($LASTEXITCODE -eq 0) {
    Write-Host "[$today] push OK"
} else {
    Write-Host "[$today] push failed"
    exit 1
}
