@echo off
setlocal
rem Reproduce every experiment in order. Takes a few minutes; depth 1 alone
rem spends ~19 s per pass waiting on the device.
rem
rem   reproduce.bat            (uses D:\io_bench\work)
rem   set WORK=C:\path && reproduce.bat
set "HERE=%~dp0"
set "PYEXE=py"
where py.exe >nul 2>nul
if errorlevel 1 set "PYEXE=python"
if "%WORK%"=="" set "WORK=%HERE%work"

echo ==========================================================
echo   io_bench - full reproduction
echo   workdir: %WORK%
echo ==========================================================
echo.

echo ### 1/6 cached paths (shows how much of a "disk" benchmark is RAM)
"%PYEXE%" "%HERE%bench.py" --workdir "%WORK%" --size-mib 256 --methods buffered,mmap

echo.
echo ### 2/6 device paths, depth sweep 1..64
"%PYEXE%" "%HERE%bench.py" --workdir "%WORK%" --size-mib 256 --methods direct,iocp --depths 1,8,64

echo.
echo ### 3/6 in-place vs copy, cache bypassed
"%PYEXE%" "%HERE%inplace.py" --workdir "%WORK%" --size-mib 256 --runs 3 --direct

echo.
echo ### 4/6 compression on compressible data
"%PYEXE%" "%HERE%compress.py" --workdir "%WORK%" --data text --size-mib 256 --runs 3 --direct

echo.
echo ### 5/6 compression on incompressible data (negative control)
"%PYEXE%" "%HERE%compress.py" --workdir "%WORK%" --data random --size-mib 256 --runs 3 --direct

echo.
echo ### 6/6 policy: probe, decide, write
"%PYEXE%" "%HERE%choose.py" --src "%WORK%\input_text.bin" --dst "%WORK%\chosen_text.out" --direct
"%PYEXE%" "%HERE%choose.py" --src "%WORK%\input_random.bin" --dst "%WORK%\chosen_random.out" --direct

echo.
echo ==========================================================
echo   done. every run is archived under results\
echo ==========================================================
pause
endlocal
