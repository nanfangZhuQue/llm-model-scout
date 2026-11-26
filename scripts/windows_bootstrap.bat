@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHONUTF8=1"
set "SCOUT_EXIT=1"
pushd "%~dp0.."
if errorlevel 1 goto root_failed
set "SCOUT_PYTHON=%CD%\.venv\Scripts\python.exe"
if exist "%SCOUT_PYTHON%" goto check_python
if exist ".venv" goto invalid_venv

py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>&1
if errorlevel 1 goto try_python
echo [Model Scout] Creating the local Python environment...
py -3 -m venv ".venv"
if errorlevel 1 goto failed
goto check_python

:try_python
python -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>&1
if errorlevel 1 goto missing_python
echo [Model Scout] Creating the local Python environment...
python -m venv ".venv"
if errorlevel 1 goto failed

:check_python
"%SCOUT_PYTHON%" -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>&1
if errorlevel 1 goto invalid_venv
"%SCOUT_PYTHON%" -c "import llm_model_scout.cli, httpx, socksio, typer, fastapi, uvicorn" >nul 2>&1
if not errorlevel 1 goto launch
echo [Model Scout] Installing missing application dependencies...
"%SCOUT_PYTHON%" -m pip install -e ".[agent]" --timeout 30 --retries 1
if errorlevel 1 goto failed
"%SCOUT_PYTHON%" -c "import llm_model_scout.cli, httpx, socksio, typer, fastapi, uvicorn" >nul 2>&1
if errorlevel 1 goto failed

:launch
"%SCOUT_PYTHON%" -m llm_model_scout %*
set "SCOUT_EXIT=%ERRORLEVEL%"
goto finish

:missing_python
echo [Model Scout] Python 3.11 or newer is required.
echo Install Python from python.org and enable the launcher or Add Python to PATH.
goto failed

:invalid_venv
echo [Model Scout] The existing .venv is incomplete or requires Python 3.11 or newer.
echo Repair the environment manually. No existing files were removed.
goto failed

:failed
set "SCOUT_EXIT=1"

:finish
popd
goto result

:root_failed
echo [Model Scout] Cannot access the project directory.

:result
if "%SCOUT_EXIT%"=="0" goto done
echo [Model Scout] Startup stopped. Review the message above.
rem Set MODEL_SCOUT_NO_PAUSE=1 to return immediately on noninteractive errors.
if "%MODEL_SCOUT_NO_PAUSE%"=="1" goto done
pause

:done
endlocal & exit /b %SCOUT_EXIT%
