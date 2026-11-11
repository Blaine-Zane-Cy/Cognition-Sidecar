@echo off
rem ---------------------------------------------------------------------------
rem Resolve a Python interpreter that REALLY has streamlit + pywebview.
rem
rem Why probe by capability instead of trusting "where pythonw.exe":
rem   this machine also carries a Python 3.12 at C:\Users\<user>\pythonw.exe
rem   (the py launcher default, prefix = the user home directory). It sits in
rem   PATH ahead of the project interpreter and has neither streamlit nor
rem   pywebview, so a name lookup starts a broken Python and the launcher dies
rem   with "ModuleNotFoundError: No module named 'webview'".
rem
rem Output (absolute paths, valid in the caller because CALL shares the env):
rem   PY  = python.exe   console build, runs streamlit
rem   PYW = pythonw.exe  windowless build, runs the gadget
rem Returns errorlevel 1 when no interpreter has both packages.
rem Comments stay ASCII on purpose: cmd parses .bat as the OEM codepage (GBK).
rem ---------------------------------------------------------------------------
set "PY="
set "PYW="

rem 1) whatever PATH exposes (probe filters out the broken one)
for /f "delims=" %%p in ('where pythonw.exe 2^>nul') do call :probe "%%p"
for /f "delims=" %%p in ('where pyw.exe 2^>nul') do call :probe "%%p"

rem 2) a virtualenv next to this project, if one ever gets created
call :probe "%~dp0..\.venv\Scripts\pythonw.exe"
call :probe "%~dp0..\venv\Scripts\pythonw.exe"

rem 3) absolute fallbacks, for when PATH is broken or empty
if exist "%USERPROFILE%\.workbuddy\binaries\python\versions" (
    for /d %%v in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do call :probe "%%~v\pythonw.exe"
)
call :probe "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
call :probe "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
call :probe "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
call :probe "C:\Python313\pythonw.exe"
call :probe "C:\Python312\pythonw.exe"

if defined PYW exit /b 0

echo.
echo   找不到装好依赖的 Python（需要 streamlit 和 pywebview）。
echo.
echo   先装一次：
echo       python -m pip install -r requirements.txt
echo.
echo   机器上有多个 Python 时，请指定装好包的那一个：
echo       "C:\path\to\python.exe" -m pip install -r requirements.txt
echo.
exit /b 1

:probe
rem %1 = candidate pythonw.exe. Accept it only if both packages import.
if defined PYW goto :eof
if not exist "%~1" goto :eof
"%~1" -c "import streamlit,webview" >nul 2>nul
if errorlevel 1 goto :eof
set "PYW=%~1"
set "PY=%~dp1python.exe"
if not exist "%PY%" set "PY=%~1"
goto :eof
