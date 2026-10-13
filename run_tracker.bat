@echo off
rem Start the LLM Visibility Tracker and keep it in the foreground, so the
rem Scheduled Task sees a live process and can restart it if Streamlit dies.
rem Registered to run at boot -- see DEPLOY-WINDOWS.md section 6.

cd /d "C:\Users\Administrator\Desktop\LLM-Visibility-Tracker"
if not exist logs mkdir logs

echo. >> logs\streamlit.log
echo ===== %date% %time%  starting ===== >> logs\streamlit.log

rem The venv's streamlit.exe directly, not "python -m streamlit": the task
rem runs without a shell, so nothing has activated the venv for us.
".venv\Scripts\streamlit.exe" run app.py ^
  --server.address 0.0.0.0 ^
  --server.port 8000 ^
  --server.headless true ^
  >> logs\streamlit.log 2>&1

echo %date% %time%  exited with %errorlevel% >> logs\streamlit.log
exit /b %errorlevel%
