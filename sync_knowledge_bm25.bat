@echo off
setlocal

set "PYTHON_EXE=C:\Users\jamee\.pyenv\pyenv-win\versions\3.12.7\python.exe"
if not exist "%PYTHON_EXE%" (
    echo Python was not found at:
    echo %PYTHON_EXE%
    echo Edit PYTHON_EXE in this batch file to match the interpreter running Open WebUI.
    pause
    exit /b 1
)

set /p "KNOWLEDGE_ID=Enter the Open WebUI Knowledge Base ID to sync: "
if not defined KNOWLEDGE_ID (
    echo A Knowledge Base ID is required.
    pause
    exit /b 1
)

"%PYTHON_EXE%" "%~dp0sync_knowledge_bm25.py" "%KNOWLEDGE_ID%"
if errorlevel 1 (
    echo.
    echo Synchronization failed. Review the error above.
)
pause
