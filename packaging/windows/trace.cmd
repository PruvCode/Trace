@echo off
setlocal
if exist "%~dp0..\git\cmd\git.exe" set "PATH=%~dp0..\git\cmd;%PATH%"
set "PATH=%~dp0..\opencode;%PATH%"
"%~dp0..\python\python.exe" -I -m trace_memory.cli %*
exit /b %errorlevel%
