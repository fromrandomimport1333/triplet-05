@echo off
rem scenarios\run B3   (same arguments as scenarios/run.py)
set "PY=%~dp0..\brain\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0run.py" %*
exit /b %ERRORLEVEL%
