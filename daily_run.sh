#!/usr/bin/env bash
# 毎日の自動実行スクリプト
# タスクスケジューラから呼び出す: bash daily_run.sh
set -e
cd "$(dirname "$0")"
LOGFILE="logs/daily_$(date '+%Y%m%d').log"

echo "=== 日次実行開始 $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOGFILE"

echo "--- データ収集 ---" | tee -a "$LOGFILE"
python main.py collect 2>&1 | tee -a "$LOGFILE"

echo "--- 予測・買い目生成 ---" | tee -a "$LOGFILE"
python main.py predict 2>&1 | tee -a "$LOGFILE"

echo "=== 完了 $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOGFILE"
