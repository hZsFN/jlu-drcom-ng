@echo off
rem Build drcomd.exe and the test binaries with the MSVC toolchain.
rem   native\build.bat            build drcomd.exe + tests
rem   native\build.bat test       build and run the offline test suite
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VSROOT="
if exist "%ProgramFiles%\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" set "VSROOT=%ProgramFiles%\Microsoft Visual Studio\18\Community"
if not defined VSROOT if exist "%ProgramFiles%\Microsoft Visual Studio\18\Enterprise\VC\Auxiliary\Build\vcvars64.bat" set "VSROOT=%ProgramFiles%\Microsoft Visual Studio\18\Enterprise"
if not defined VSROOT if exist "%ProgramFiles%\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat" set "VSROOT=%ProgramFiles%\Microsoft Visual Studio\18\BuildTools"
if not defined VSROOT if exist "%ProgramFiles%\Microsoft Visual Studio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat" set "VSROOT=%ProgramFiles%\Microsoft Visual Studio\2022\Enterprise"
if not defined VSROOT if exist "%ProgramFiles%\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat" set "VSROOT=%ProgramFiles%\Microsoft Visual Studio\2022\Professional"
if not defined VSROOT if exist "%ProgramFiles%\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" set "VSROOT=%ProgramFiles%\Microsoft Visual Studio\2022\Community"
if not defined VSROOT if exist "%ProgramFiles%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" set "VSROOT=%ProgramFiles%\Microsoft Visual Studio\2022\BuildTools"
if not defined VSROOT if exist "%ProgramFiles(x86)%\Microsoft Visual Studio\2019\Enterprise\VC\Auxiliary\Build\vcvars64.bat" set "VSROOT=%ProgramFiles(x86)%\Microsoft Visual Studio\2019\Enterprise"
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

set "DRCOMD_VER=2.2.2"
for /f "tokens=2 delims== " %%A in ('findstr /B "__version__" ..\drcom\__init__.py') do set "DRCOMD_VER=%%~A"

set "CFLAGS=/nologo /Os /GL /Gy /MT /W3 /utf-8 /D_CRT_SECURE_NO_WARNINGS /DDRCOMD_VERSION=\"!DRCOMD_VER!\""
set "LDFLAGS=/link /OPT:REF /OPT:ICF /LTCG"
set "SRC=md4.c md5.c sha1.c protocol.c engine.c config_load.c tray.c main.c"

echo [build] %VSROOT% (version !DRCOMD_VER!)
echo [build] compiling resources
rc /nologo /fo drcomd.res drcomd.rc
if errorlevel 1 exit /b 1

echo [build] compiling drcomd.exe
cl %CFLAGS% %SRC% drcomd.res /Fe:drcomd.exe %LDFLAGS%
if errorlevel 1 exit /b 1

echo [build] compiling tests
set "TCFLAGS=/nologo /O2 /MT /W3 /utf-8 /D_CRT_SECURE_NO_WARNINGS"
cl %TCFLAGS% md4.c md5.c sha1.c protocol.c engine.c tests\test_hash.c     /Fe:tests\test_hash.exe     || exit /b 1
cl %TCFLAGS% md4.c md5.c sha1.c protocol.c engine.c tests\test_protocol.c /Fe:tests\test_protocol.exe || exit /b 1
cl %TCFLAGS% md4.c md5.c sha1.c protocol.c engine.c tests\test_engine.c   /Fe:tests\test_engine.exe   || exit /b 1

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
