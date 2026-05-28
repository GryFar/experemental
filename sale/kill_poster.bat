@echo off
setlocal
set PID=
for /f "usebackq delims=" %%p in ("%~dp0pid.txt") do set PID=%%p
if "%PID%"=="" (
  echo pid.txt not found or empty in %~dp0
  exit /b 1
)
echo Killing PID %PID% ...
taskkill /PID %PID% /F
endlocal
