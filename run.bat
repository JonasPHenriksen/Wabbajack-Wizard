@echo off
REM ---------------------------------------------------------------------
REM  Wabbajack modlist update - launch from the mapped share
REM
REM  Prefers the isolated environment made by setup_env.bat, then a
REM  lib-win folder, then whatever Python is on PATH.
REM
REM  With no arguments this opens the interactive manager: it walks four
REM  stages - updates, incomplete modlists, prepare one for install, disk
REM  space - showing what it can see at each and offering skip / all / some.
REM  Every stage is independent; skipping one does not affect the others.
REM
REM  Arguments are for scripted or scheduled use and pass straight through:
REM      run.bat --check         just report what has updates
REM      run.bat --dry-run       plan everything, change nothing
REM      run.bat --rediscover    re-match local manifests against the feeds
REM      run.bat --only <list>   do the archive work for one list only
REM
REM  Exit codes: 0 clean, 1 finished with problems, 2 could not start,
REM              3 another run holds the lock, 4 unhandled error.
REM ---------------------------------------------------------------------

REM pushd copes with UNC paths (\\server\share\...) that plain cd cannot.
pushd "%~dp0"

if exist ".venv-win\Scripts\python.exe" goto usevenv
if exist "lib-win" goto uselib
goto usesystem

:usevenv
set PY=.venv-win\Scripts\python.exe
set WHERE=.venv-win
goto launch

:uselib
REM No venv, but a --target install is present. Put it on the import path.
set PYTHONPATH=%CD%\lib-win;%PYTHONPATH%
goto usesystem

:usesystem
if defined PY goto launch
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY goto nopython
if not defined WHERE set WHERE=system Python
goto launch

:launch
echo Using: %WHERE%
echo.
%PY% wj_autoupdate.py %*
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="0" echo [OK]       Finished cleanly.
if "%RC%"=="1" echo [PARTIAL]  Finished, but something failed. See wj_autoupdate.log.
if "%RC%"=="2" echo [STOPPED]  Could not start. See wj_autoupdate.log.
if "%RC%"=="3" echo [SKIPPED]  Another run holds the lock; nothing was done.
if "%RC%"=="4" echo [CRASH]    Unhandled error. See wj_autoupdate.log.
goto end

:nopython
echo.
echo Python was not found on PATH.
echo Install it from https://www.python.org/downloads/ and tick
echo "Add python.exe to PATH", then run setup_env.bat once.
echo.

:end
popd
echo.
pause
exit /b %RC%
