# 毎日の予測後にdocs/data/*.jsonをGitHubへ自動Push
# Task Schedulerから 08:45 頃に呼ばれる
# 使い方: powershell -ExecutionPolicy Bypass -File push_daily.ps1

$workDir = "c:\Users\kcs15\OneDrive\デスクトップ\boat_scraper"
Set-Location $workDir

$today = (Get-Date).ToString("yyyy-MM-dd")

# 変更があるか確認
$status = git status --porcelain docs/data/ 2>&1
if (-not $status) {
    Write-Host "[$today] docs/data/ に変更なし — Push スキップ"
    exit 0
}

git add docs/data/
git commit -m "data: $today の予測・買い目を更新"
git push origin master

if ($LASTEXITCODE -eq 0) {
    Write-Host "[$today] GitHub Push 完了"
} else {
    Write-Host "[$today] Push 失敗 (exit $LASTEXITCODE)"
    exit 1
}
