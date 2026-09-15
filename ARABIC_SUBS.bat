@echo off
rem  Arabic Edition launcher.
rem
rem  Drag a video file or a folder onto this file, or double-click it to pick a
rem  file with the Windows dialog. Either way the TUI opens with Arabic as the
rem  language and every configured provider as the engine. Nothing is downloaded
rem  without you choosing the subtitle: auto_selection stays whatever config.yaml
rem  says, and this launcher passes no flag that could change it.
rem
rem  Keep this file ASCII-only. cmd.exe reads a batch file's own bytes in the
rem  console code page, so a non-ASCII literal here -- a comment included -- is
rem  decoded as garbage and can break the lines around it. Paths with Arabic
rem  characters are unaffected: they arrive as arguments, not as file content.

set "PYTHON=C:\Python314\python.exe"
set "APP=%~dp0download_subs.py"

rem  Explorer leaves its own directory current when a file is dropped, so anchor
rem  the working directory to the launcher instead.
cd /d "%~dp0"

if "%~1"=="" goto :pick

rem  Branches use goto rather than an if block so that a dropped path
rem  containing parentheses cannot confuse the block parser.
"%PYTHON%" "%APP%" "%~1" --lang ar --backend all-providers
goto :finish

:pick
rem  No argument: choose a file and launch it from the same PowerShell process.
rem  Reading the selected path back into a batch variable instead would push it
rem  through the console code page and corrupt an Arabic file name.
powershell -NoProfile -STA -Command "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.OpenFileDialog; $d.Title = 'Select a video file'; $d.Filter = 'Video files|*.mkv;*.mp4;*.avi;*.mov;*.m4v;*.ts;*.webm|All files|*.*'; if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { & '%PYTHON%' '%APP%' $d.FileName '--lang' 'ar' '--backend' 'all-providers'; exit $LASTEXITCODE } else { Write-Host 'No file selected.'; exit 1 }"

:finish
rem  Hold the window open on failure so a double-click cannot swallow the error.
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo.
    echo The launcher exited with an error. Press any key to close.
    pause
)
exit /b %RESULT%
