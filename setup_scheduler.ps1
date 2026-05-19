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

# 毎晩22:30 - 的中判定 → JSONエクスポート → GitHubへPush
$action5 = New-ScheduledTaskAction -Execute $python -Argument "main.py judge" -WorkingDirectory $workDir
$trigger5 = New-ScheduledTaskTrigger -Daily -At "22:30"
Register-ScheduledTask -TaskName "BoatRacer_Judge" -Action $action5 -Trigger $trigger5 -Settings $settings -Force
Write-Host "✓ BoatRacer_Judge 登録完了 (毎晩22:30)"

# 毎晩22:45 - 判定後のJSONをPush（夜間更新）
$action6 = New-ScheduledTaskAction -Execute $ps -Argument "-ExecutionPolicy Bypass -File push_daily.ps1" -WorkingDirectory $workDir
$trigger6 = New-ScheduledTaskTrigger -Daily -At "22:45"
Register-ScheduledTask -TaskName "BoatRacer_PushNight" -Action $action6 -Trigger $trigger6 -Settings $settings -Force
Write-Host "✓ BoatRacer_PushNight 登録完了 (毎晩22:45)"

# 毎週日曜9:00 - モデル再学習（週次PDCA）
$action7 = New-ScheduledTaskAction -Execute $python -Argument "main.py train" -WorkingDirectory $workDir
$triggerWeekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "09:00"
$settingsTrain = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) -StartWhenAvailable
Register-ScheduledTask -TaskName "BoatRacer_Train" -Action $action7 -Trigger $triggerWeekly -Settings $settingsTrain -Force
Write-Host "✓ BoatRacer_Train 登録完了 (毎週日曜9:00)"

Write-Host ""
Write-Host "登録済みタスク確認:"
Get-ScheduledTask | Where-Object TaskName -like "BoatRacer_*" | Select-Object TaskName, State
