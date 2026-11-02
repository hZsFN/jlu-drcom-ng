@echo off
rem ---------------------------------------------------------------------------
rem Build drcomd.exe and the test binaries with the official MSVC environment.
rem
rem Why this exists: calling vcvars64.bat from a POSIX shell mangles the quoted
rem Windows path (the shell hands cmd a literal \"C:\... ), and cmd then fails
rem with "文件名、目录名或卷标语法不正确".  Keeping the whole thing inside a .bat
rem avoids the quoting problem entirely.
rem
rem   native\build.bat            build drcomd.exe + tests
rem   native\build.bat test       build and run the offline test suite
rem ---------------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VSROOT="
if exist "%ProgramFiles%\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" set "VSROOT=%ProgramFiles%\Microsoft Visual Studio\18\Community"
if not defined VSROOT if exist "%ProgramFiles%\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" set "VSROOT=%ProgramFiles%\Microsoft Visual Studio\2022\Community"
if not defined VSROOT if exist "%ProgramFiles%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" set "VSROOT=%ProgramFiles%\Microsoft Visual Studio\2022\BuildTools"
if not defined VSROOT if exist "%ProgramFiles(x86)%\Microsoft Visual Studio\2019\Community\VC\Auxiliary\Build\vcvars64.bat" set "VSROOT=%ProgramFiles(x86)%\Microsoft Visual Studio\2019\Community"
if not defined VSROOT (
    echo [build] cannot find vcvars64.bat - install Visual Studio Build Tools
    exit /b 2
)

call "%VSROOT%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 (
    echo [build] vcvars64.bat failed
    exit /b 2
)

set "CFLAGS=/nologo /O2 /MT /W3 /utf-8 /D_CRT_SECURE_NO_WARNINGS"
set "SRC=md4.c md5.c sha1.c protocol.c engine.c config_load.c tray.c main.c"

echo [build] %VSROOT%
rem The tray icon is loaded from resources, so the .rc has to be compiled and
rem then linked in.  Skipping it would give an installed daemon the generic
rem Windows icon - nobody would know which tray entry was theirs.
echo [build] compiling resources
rc /nologo /fo drcomd.res drcomd.rc
if errorlevel 1 exit /b 1

echo [build] compiling drcomd.exe
cl %CFLAGS% %SRC% drcomd.res /Fe:drcomd.exe
if errorlevel 1 exit /b 1

echo [build] compiling tests
cl %CFLAGS% md4.c md5.c sha1.c protocol.c engine.c tests\test_hash.c     /Fe:tests\test_hash.exe     || exit /b 1
cl %CFLAGS% md4.c md5.c sha1.c protocol.c engine.c tests\test_protocol.c /Fe:tests\test_protocol.exe || exit /b 1
cl %CFLAGS% md4.c md5.c sha1.c protocol.c engine.c tests\test_engine.c   /Fe:tests\test_engine.exe   || exit /b 1

if /i not "%~1"=="test" (
    echo [build] done - run "build.bat test" to execute the suite
    exit /b 0
)

echo.
echo [build] running offline tests
tests\test_hash.exe
if errorlevel 1 exit /b 1
tests\test_protocol.exe
if errorlevel 1 exit /b 1
echo [build] engine test needs its mock server:
echo         python tests\mock_server.py --port 51440 --duration 20
echo         tests\test_engine.exe 51440 51441 5
exit /b 0
