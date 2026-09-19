:: TRACE SWE-bench fixture bridge (Windows).
::
:: The Django corpus lives in the external artifact cache (never inside the
:: OneDrive-synced repo), but the TRACE runner resolves a task's fixture as a
:: path *inside* the repo root. This bridge exposes the cached clone at
:: fixtures/swebench_repo via a directory junction, so no multi-hundred-MB
:: object store is ever copied into the repository tree.
::
:: Run once:  cmd /c scripts\\link_swebench_fixture.cmd
:: Remove:    fsutil reparsepoint delete fixtures\\swebench_repo   (or rmdir)

@echo off
setlocal

set "REPO_ROOT=%~dp0.."
set "TARGET=%LOCALAPPDATA%\TraceSWECache\repos\django__django"
set "LINK=%REPO_ROOT%\fixtures\swebench_repo"

if not exist "%TARGET%\.git" (
  echo ERROR: cached django clone not found at "%TARGET%"
  exit /b 1
)

if exist "%LINK%" (
  echo Already present: "%LINK%"
  exit /b 0
)

mklink /J "%LINK%" "%TARGET%"
if errorlevel 1 (
  echo ERROR: could not create junction
  exit /b 1
)

echo Linked:
echo   %LINK%
echo   -^> %TARGET%
exit /b 0
