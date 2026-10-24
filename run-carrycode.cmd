@echo off
setlocal
rem CarryCode launcher - runs from any directory; workspace defaults to cwd.
rem Requires Python 3.10+. Override the interpreter with CARRYCODE_PYTHON.
set "ROOT=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONPATH=%ROOT%"
if not defined CARRYCODE_PYTHON set "CARRYCODE_PYTHON=python"

"%CARRYCODE_PYTHON%" -m carrycode %*
exit /b %ERRORLEVEL%
