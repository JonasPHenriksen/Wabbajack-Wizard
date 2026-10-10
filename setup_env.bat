@echo off
REM ---------------------------------------------------------------------
REM  Get the Python side of these scripts ready.
REM
REM  Two arrangements, decided by what is in this folder:
REM
REM    .python\      an interpreter unpacked inside the project (a
REM                  python-build-standalone release, tkinter included).
REM                  requirements.txt is installed straight into it, no
REM                  virtual environment is made, and nothing is written
REM                  outside this folder - so the folder can be moved or
REM                  copied and everything still runs. This is the
REM                  arrangement for a machine with no Python installed.
REM
REM    otherwise     a machine-wide Python builds .venv-win\ here, and
REM                  requirements.txt goes into that. A venv records where
REM                  its Python is, so it is rebuilt by running this again
REM                  whenever Python moves or is upgraded.
REM
REM  Run once. Safe to run again. Exits 0 when everything is in place, 1
REM  when something could not be done.
REM ---------------------------------------------------------------------

setlocal
set "RC=1"
pushd "%~dp0"
echo Folder: "%CD%"
echo.

if exist ".python\python.exe" goto inproject

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
goto install

REM --- The in-project interpreter needs no environment around it ---------
:inproject
set VPY=.python\python.exe
echo Interpreter: .python\python.exe, kept inside this folder.
"%VPY%" -c "import sys; print('  ' + sys.version.split()[0])"
echo.

:install
echo Installing packages ...
"%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
"%VPY%" -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 goto pipfail

REM --- Prove it works ---------------------------------------------------
echo.
echo Verifying ...
"%VPY%" -c "import xxhash, tkinter, sys; print('  python  ' + sys.version.split()[0]); print('  xxhash  ' + xxhash.VERSION); print('  tk      ' + str(tkinter.TkVersion))"
if errorlevel 1 goto pipfail

echo.
echo Done. run.bat will use %VPY% automatically.
set "RC=0"
goto end

REM ---------------------------------------------------------------------
:nopython
echo Python was not found on PATH.
echo Either unpack a python-build-standalone release into .python\ here
echo (see README.md - nothing gets installed on the machine), or install
echo Python from https://www.python.org/downloads/ with "Add python.exe to
echo PATH" ticked, then run this again.
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
REM Wait only for someone at a console (see run.bat).
if defined WJ_NO_PAUSE goto done
timeout /t 0 >nul 2>nul || goto done
echo.
pause
:done
exit /b %RC%
