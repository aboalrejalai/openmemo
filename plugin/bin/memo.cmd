@echo off
rem Memo launcher for Windows (cmd and PowerShell). The vault is MEMO_VAULT, else
rem OPENMEMO_HOME, else the checkout this file lives in (plugin\bin\..\..).
setlocal
set "PYTHONUTF8=1"
if defined MEMO_VAULT (
  set "VAULT=%MEMO_VAULT%"
) else if defined OPENMEMO_HOME (
  set "VAULT=%OPENMEMO_HOME%"
) else (
  for %%I in ("%~dp0..\..") do set "VAULT=%%~fI"
)
set "MEMO_VAULT=%VAULT%"
set "CLI=%VAULT%\scripts\memo_capture\cli.py"
if not exist "%CLI%" (
  if "%~1"=="hook" echo {}
  exit /b 0
)
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 "%CLI%" %*
  exit /b %ERRORLEVEL%
)
python "%CLI%" %*
exit /b %ERRORLEVEL%
