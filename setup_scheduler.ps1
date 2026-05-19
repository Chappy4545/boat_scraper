# ボートレース予測 - 自動スケジューラー設定
# 管理者権限のPowerShellで実行: Right-click PowerShell -> "Run as Administrator"
# cd "c:\Users\kcs15\OneDrive\デスクトップ\boat_scraper"
# .\setup_scheduler.ps1

$python = "C:\Users\kcs15\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$workDir = "c:\Users\kcs15\OneDrive\デスクトップ\boat_scraper"

# 毎朝8:00 - 出走表・オッズ収集（1R発走前に完了）
$action1 = New-ScheduledTaskAction -Execute $python -Argument "main.py collect" -WorkingDirectory $workDir
$trigger1 = New-ScheduledTaskTrigger -Daily -At "08:00"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 1) -StartWhenAvailable
Register-ScheduledTask -TaskName "BoatRacer_Collect" -Action $action1 -Trigger $trigger1 -Settings $settings -Force
Write-Host "✓ BoatRacer_Collect 登録完了 (毎朝8:00)"

# 毎朝8:30 - 予測・買い目生成（1R発走前に間に合う）
$action2 = New-ScheduledTaskAction -Execute $python -Argument "main.py predict" -WorkingDirectory $workDir
$trigger2 = New-ScheduledTaskTrigger -Daily -At "08:30"
Register-ScheduledTask -TaskName "BoatRacer_Predict" -Action $action2 -Trigger $trigger2 -Settings $settings -Force
Write-Host "✓ BoatRacer_Predict 登録完了 (毎朝8:30)"

# 毎朝8:45 - GitHub Push（予測JSONをpush）
$ps = "powershell.exe"
$action3 = New-ScheduledTaskAction -Execute $ps -Argument "-ExecutionPolicy Bypass -File push_daily.ps1" -WorkingDirectory $workDir
$trigger3 = New-ScheduledTaskTrigger -Daily -At "08:45"
Register-ScheduledTask -TaskName "BoatRacer_Push" -Action $action3 -Trigger $trigger3 -Settings $settings -Force
Write-Host "✓ BoatRacer_Push 登録完了 (毎朝8:45)"

# 毎晩22:00 - 当日結果収集（的中判定のため）
$action4 = New-ScheduledTaskAction -Execute $python -Argument "main.py collect" -WorkingDirectory $workDir
$trigger4 = New-ScheduledTaskTrigger -Daily -At "22:00"
Register-ScheduledTask -TaskName "BoatRacer_CollectResult" -Action $action4 -Trigger $trigger4 -Settings $settings -Force
Write-Host "✓ BoatRacer_CollectResult 登録完了 (毎晩22:00)"

Write-Host ""
Write-Host "登録済みタスク確認:"
Get-ScheduledTask | Where-Object TaskName -like "BoatRacer_*" | Select-Object TaskName, State
