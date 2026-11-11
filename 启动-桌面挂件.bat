@echo off
cd /d "%~dp0"

rem Launch the desktop gadget with a windowless Python (pythonw.exe).
rem The interpreter is resolved by data\find_python.bat, which probes for
rem streamlit + pywebview instead of trusting PATH -- a second Python 3.12 in
rem C:\Users\<user>\ shadows the project one and has no dependencies.
rem ASCII-only comments: cmd parses .bat as the OEM codepage (GBK), and UTF-8
rem Chinese in comments corrupts the following set/if lines.

set "PY="
set "PYW="
call "%~dp0data\find_python.bat"
if not defined PYW (
    pause
    exit /b 1
)

rem Absolute script path: the callback above may leave a different cwd in play.
start "" "%PYW%" "%~dp0desktop_app.py"
exit /b 0
