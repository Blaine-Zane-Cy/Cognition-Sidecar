@echo off
cd /d "%~dp0"

rem Plain launcher: full width layout in the browser, console stays attached.
rem Interpreter comes from data\find_python.bat (capability probe).
rem ASCII-only comments: cmd parses .bat as the OEM codepage (GBK).

set "PY="
set "PYW="
call "%~dp0data\find_python.bat"
if not defined PY (
    pause
    exit /b 1
)

"%PY%" -m streamlit run "%~dp0app.py"
pause
