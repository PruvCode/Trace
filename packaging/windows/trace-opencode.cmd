@echo off
setlocal
if exist "%~dp0..\git\cmd\git.exe" set "PATH=%~dp0..\git\cmd;%PATH%"
"%~dp0..\opencode\opencode.exe" %*
exit /b %errorlevel%
