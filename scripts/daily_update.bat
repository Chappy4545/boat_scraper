@echo off
cd /d "C:\Users\kcs15\OneDrive\デスクトップ\boat_scraper"

echo [%date% %time%] UPDATE 開始
python main.py update
if errorlevel 1 (
    echo [%date% %time%] UPDATE 失敗
    exit /b 1
)

git add docs/data/
git diff --cached --quiet || git commit -m "auto: update predictions %date%"
git push
echo [%date% %time%] UPDATE 完了
