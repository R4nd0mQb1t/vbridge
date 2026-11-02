@echo off
cd /d "%~dp0"
python -m bridge --config config.yaml
pause
