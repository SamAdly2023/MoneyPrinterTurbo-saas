@echo off
title Vidzy - running locally
cd /d "%~dp0"

REM ---------------------------------------------------------------------------
REM Runs the full app on this PC at http://localhost:8080
REM
REM MPT_RENDER_MODE is deliberately NOT set here. In the cloud it is set to
REM "cloudrun_job" so the web service only dispatches work; unset, the engine
REM renders in-process - which is the whole point of running locally. Videos
REM are made by this machine's CPU and cost nothing.
REM
REM Note: this connects to the SAME live Firestore as vidzy.web.app. Jobs you
REM queue here are the real queue, and finished videos really publish to your
REM connected YouTube/TikTok accounts.
REM ---------------------------------------------------------------------------

set "GOOGLE_CLOUD_PROJECT=vvvvv-504116"

REM winget installs ffmpeg into a versioned folder and only updates PATH for
REM new sessions. Find it ourselves so this works right after installing, and
REM keep working when ffmpeg is upgraded to a different version number.
for /d %%D in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin") do set "PATH=%%D;%PATH%"

if not exist ".venv-local\Scripts\python.exe" (
  echo.
  echo   The local environment is missing ^(.venv-local^).
  echo   Re-run the setup, or ask Claude to rebuild it.
  echo.
  pause
  exit /b 1
)

if not exist "%APPDATA%\gcloud\application_default_credentials.json" (
  echo.
  echo   ============================================================
  echo    One-time setup: sign in to Google so the app can reach
  echo    the database. Your browser is opening now.
  echo.
  echo    Sign in as samadly728@gmail.com and click Allow.
  echo   ============================================================
  echo.
  call gcloud auth application-default login
  if not exist "%APPDATA%\gcloud\application_default_credentials.json" (
    echo.
    echo   Sign-in did not complete. Close this window and try again.
    echo.
    pause
    exit /b 1
  )
  echo.
  echo   Signed in. Starting Vidzy...
  echo.
)

echo.
echo   Starting Vidzy on http://localhost:8080
echo   Your browser opens automatically in a few seconds.
echo.
echo   Keep this black window OPEN while you use the app.
echo   Closing it stops the app.
echo.

start "" cmd /c "timeout /t 12 /nobreak >nul && start """" http://localhost:8080"

".venv-local\Scripts\python.exe" main.py

echo.
echo   Vidzy has stopped.
pause
