@echo off
REM ---------------------------------------------------------------------
REM  Wabbajack Wizard - launch from the mapped share
REM
REM  Prefers the interpreter kept inside this folder (.python\), then the
REM  environment made by setup_env.bat, then a lib-win folder, then
REM  whatever Python is on PATH.
REM
REM  With no arguments this opens the desktop window (no console). Any
REM  argument runs the console side instead, and passes straight through:
REM      run.bat --wizard        the five-stage console wizard
REM      run.bat --run           a full unattended run (a scheduled task)
REM      run.bat --gallery       summarise the published gallery
REM      run.bat --gallery --sync-reports   refresh the report cache
REM      run.bat --check         just report what has updates
REM      run.bat --dry-run       plan everything, change nothing
REM      run.bat --rediscover    re-match local manifests against the feeds
REM      run.bat --only <list>   do the archive work for one list only
REM      run.bat --selftest      check upstream formats still read as expected
REM
REM  Exit codes: 0 clean, 1 finished with problems, 2 could not start,
REM              3 another run holds the lock, 4 unhandled error.
REM
REM  It waits for a key at the end when someone is at the console to press
REM  one, so a double-clicked shortcut does not vanish with the last lines.
REM  A scheduled task has nobody there: set WJ_NO_PAUSE=1 for it, or run
REM  .python\python.exe wj_autoupdate.py --run directly.
REM ---------------------------------------------------------------------

REM Nothing set here outlives the script, and nothing the calling session
REM had set - a PY of its own, say - chooses the interpreter.
setlocal
set "PY="
set "PYW="
set "WHERE="
set "RC=0"

REM pushd copes with UNC paths (\\server\share\...) that plain cd cannot.
pushd "%~dp0"

if exist ".python\python.exe" goto useproject
if exist ".venv-win\Scripts\python.exe" goto usevenv
if exist "lib-win" goto uselib
goto usesystem

:useproject
REM An interpreter unpacked inside the project, with xxhash installed into it
REM by setup_env.bat. Nothing on the machine, and nothing that breaks when
REM the folder moves: every path here is relative to this file.
set PY=.python\python.exe
set PYW=.python\pythonw.exe
set WHERE=.python (inside this folder)
goto launch

:usevenv
set PY=.venv-win\Scripts\python.exe
set PYW=.venv-win\Scripts\pythonw.exe
set WHERE=.venv-win
goto launch

:uselib
REM No venv, but a --target install is present. Put it on the import path.
set "PYTHONPATH=%CD%\lib-win;%PYTHONPATH%"
goto usesystem

:usesystem
where py >nul 2>nul && set PY=py -3
if defined PY set PYW=pyw -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PYW where pythonw >nul 2>nul && set PYW=pythonw
if not defined PY goto nopython
if not defined WHERE set WHERE=system Python
goto launch

:launch
if not "%~1"=="" goto console

REM The window. pythonw has no console, so everything the tool says goes to
REM the window's log pane and to runtime\wj_autoupdate.log; a failure before the
REM window exists is shown in a dialog. Falls back to a console Python if
REM there is no pythonw, which only means a console window sits behind it.
REM No popd: launched from a UNC path, pushd made a temporary drive, and the
REM window runs from it - popping it here pulled the folder out from under
REM the window it had just started.
if not defined PYW set PYW=%PY%
start "" %PYW% wj_gui.py
exit /b 0

:console
echo Using: %WHERE%
echo.
%PY% wj_autoupdate.py %*
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="0" echo [OK]       Finished cleanly.
if "%RC%"=="1" echo [PARTIAL]  Finished, but something failed. See runtime\wj_autoupdate.log.
if "%RC%"=="2" echo [STOPPED]  Could not start. See runtime\wj_autoupdate.log.
if "%RC%"=="3" echo [SKIPPED]  Another run holds the lock; nothing was done.
if "%RC%"=="4" echo [CRASH]    Unhandled error. See runtime\wj_autoupdate.log.
goto end

:nopython
echo.
echo Python was not found on PATH.
echo Either unpack a python-build-standalone release into .python\ here
echo (see README.md), or install Python from https://www.python.org/downloads/
echo with "Add python.exe to PATH" ticked; then run setup_env.bat once.
echo.
set RC=2

:end
popd
REM Wait only for someone who can answer: never when WJ_NO_PAUSE is set, and
REM never when stdin is not a console - timeout refuses to run without one,
REM which is how a scheduler's session, or input redirected, is told apart.
REM A pause that always ran kept a scheduled run.bat from ever exiting, and
REM its exit code from ever reaching the scheduler.
if defined WJ_NO_PAUSE goto done
timeout /t 0 >nul 2>nul || goto done
echo.
pause
:done
exit /b %RC%
