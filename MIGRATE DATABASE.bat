@echo off
REM Apply pending Polaris DB migrations.
REM Double-clickable — window stays open after the run so you can read output.
REM Forwards any extra args (e.g. --dry-run, --json) to apply.py.

REM cd into the directory this .bat lives in, even if double-clicked from elsewhere.
cd /d "%~dp0"

echo Running: uv run --env-file .env python packages/db-migrations/apply.py %*
echo.

uv run --env-file .env python packages/db-migrations/apply.py %*
set EXITCODE=%ERRORLEVEL%

echo.
echo ----------------------------------------
echo Done. Exit code: %EXITCODE%
echo Press any key to close this window.
pause >nul

exit /b %EXITCODE%