@echo off
cd /d "C:\Users\kcs15\OneDrive\デスクトップ\boat_scraper"

echo [%date% %time%] JUDGE 開始
python main.py collect
python main.py judge
if errorlevel 1 (
    echo [%date% %time%] JUDGE 失敗
    exit /b 1
)

git add docs/data/
git diff --cached --quiet || git commit -m "auto: judge results %date%"
git push
echo [%date% %time%] JUDGE 完了
