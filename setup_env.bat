@echo off
REM ---------------------------------------------------------------------
REM  Create the isolated Python environment for these scripts.
REM
REM  Makes .venv-win\ in this folder and installs requirements.txt into it.
REM  Run once. Run it again any time you upgrade Python, or if run.bat
REM  starts complaining that the environment is broken.
REM
REM  The name has -win on it deliberately: a virtual environment is tied to
REM  the interpreter that built it. Rebuild it here if Python changes.
REM ---------------------------------------------------------------------

pushd "%~dp0"
echo Folder: %CD%
echo.

REM --- Find a base interpreter to build the environment from ------------
set BASEPY=
where py >nul 2>nul && set BASEPY=py -3
if not defined BASEPY where python >nul 2>nul && set BASEPY=python
if not defined BASEPY goto nopython

echo Base interpreter:
%BASEPY% -c "import sys; print('  ' + sys.version.split()[0] + '  ' + sys.executable)"
echo.

REM --- Build it ---------------------------------------------------------
if exist ".venv-win\Scripts\python.exe" (
    echo .venv-win already exists. Updating packages in place.
) else (
    echo Creating .venv-win ...
    %BASEPY% -m venv .venv-win
    if errorlevel 1 goto venvfail
)

set VPY=.venv-win\Scripts\python.exe
if not exist "%VPY%" goto venvfail

echo.
echo Installing packages ...
"%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
"%VPY%" -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 goto pipfail

REM --- Prove it works ---------------------------------------------------
echo.
echo Verifying ...
"%VPY%" -c "import xxhash, sys; print('  python  ' + sys.version.split()[0]); print('  xxhash  ' + xxhash.VERSION)"
if errorlevel 1 goto pipfail

echo.
echo Done. run.bat will now use .venv-win automatically.
goto end

REM ---------------------------------------------------------------------
:nopython
echo Python was not found on PATH.
echo Install it from https://www.python.org/downloads/ and tick
echo "Add python.exe to PATH" during setup, then run this again.
goto end

:venvfail
echo.
echo Could not create the virtual environment here.
echo.
echo Creating a venv on a mapped network drive occasionally fails because
echo the interpreter has to be copied onto the share. If that is what
echo happened, use the no-venv fallback instead, which needs no copying:
echo.
echo     %BASEPY% -m pip install --target lib-win -r requirements.txt
echo.
echo run.bat picks up a lib-win folder automatically if it exists.
goto end

:pipfail
echo.
echo Package installation failed. Check that this machine can reach
echo pypi.org, then run this again.
goto end

:end
popd
echo.
pause
