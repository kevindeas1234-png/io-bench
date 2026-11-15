@echo off
setlocal
rem io_bench launcher: runs the benchmark and keeps the window open.
rem Any arguments are passed through, e.g.  run_bench.bat --size-mib 1024
set "HERE=%~dp0"

set "PYEXE=py"
where py.exe >nul 2>nul
if errorlevel 1 set "PYEXE=python"

"%PYEXE%" "%HERE%bench.py" %*
set "RC=%errorlevel%"

echo.
echo [io_bench] exit code: %RC%
echo [io_bench] results: %HERE%results\
pause
endlocal
