@echo off
cd /d "%~dp0"

rem Narrow layout + server, in a normal console window.
rem Same interpreter resolution as the gadget launcher: PATH may expose a
rem Python that has no streamlit. ASCII-only comments (OEM codepage).

set "PY="
set "PYW="
call "%~dp0data\find_python.bat"
if not defined PY (
    pause
    exit /b 1
)

rem Helper opens the browser once the server answers on /_stcore/health.
start "" /b "%PY%" "%~dp0data\open_narrow.py"

"%PY%" -m streamlit run "%~dp0app.py"
pause
