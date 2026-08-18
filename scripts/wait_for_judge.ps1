<#
判定が走っている間だけ待つ。更新側から呼ぶ。

取りこぼした判定は「PC が起きた瞬間」に走るため、朝 8:00 の更新と同じ秒に
始まることがある（2026-08-18 実測: JUDGE start / UPDATE start がどちらも
8:00:06）。両者は同じ SQLite に書き、どちらも git fetch/rebase/push するので、
重なれば database is locked か index.lock でどちらかが落ちる。
判定は5分で終わり、当日の収集より先に済ませるのが正しい順序なので、更新が待つ。

ロックファイルを使わないのは、途中で落ちたときに取り残しが残り、翌日以降
ずっと待たされるのを避けるため。実際に動いているプロセスだけを見る。

戻り値: 0 = 空いた / 1 = 待ち時間切れ（呼び出し側は警告して続行してよい。
        5分の処理が30分終わらないなら、それはもう別の壊れ方）
#>
param([int]$MaxWaitMin = 30)

$deadline = (Get-Date).AddMinutes($MaxWaitMin)
while ($true) {
    $busy = @(
        Get-CimInstance Win32_Process -Filter "Name='cmd.exe' OR Name='python.exe'" |
            Where-Object {
                $_.ProcessId -ne $PID -and (
                    $_.CommandLine -like '*daily_judge*' -or
                    $_.CommandLine -like '*main.py judge*'
                )
            }
    )
    if ($busy.Count -eq 0) { Write-Output "judge: clear"; exit 0 }
    if ((Get-Date) -gt $deadline) { Write-Output "judge: wait timed out"; exit 1 }
    Write-Output ("judge: running ({0} proc) - waiting" -f $busy.Count)
    Start-Sleep -Seconds 20
}
