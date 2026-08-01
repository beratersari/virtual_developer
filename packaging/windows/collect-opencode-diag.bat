@echo off
REM Collect OpenCode / oh-my-opencode diagnostics when the TUI is a black screen.
REM Run from any cmd.exe (no admin needed). Creates a folder of text files you can zip + send.

setlocal EnableDelayedExpansion
set "OUT=%USERPROFILE%\vd-opencode-diag-%DATE:~-4%%DATE:~4,2%%DATE:~7,2%-%RANDOM%"
set "OUT=%OUT:/=-%"
set "OUT=%OUT:\=-%"
set "OUT=%USERPROFILE%\vd-opencode-diag"
if exist "%OUT%" rmdir /s /q "%OUT%" 2>nul
mkdir "%OUT%" 2>nul

echo Writing diagnostics to:
echo   %OUT%
echo.

> "%OUT%\00-meta.txt" (
  echo generated=%DATE% %TIME%
  echo USERPROFILE=%USERPROFILE%
  echo COMPUTERNAME=%COMPUTERNAME%
  echo PROCESSOR_ARCHITECTURE=%PROCESSOR_ARCHITECTURE%
)

echo [1/8] where / version ...
where opencode > "%OUT%\01-where-opencode.txt" 2>&1
where glab >> "%OUT%\01-where-opencode.txt" 2>&1
opencode --version > "%OUT%\02-opencode-version.txt" 2>&1
opencode --help > "%OUT%\02-opencode-help.txt" 2>&1

echo [2/8] configs ...
if exist "%USERPROFILE%\.config\opencode\opencode.json" (
  copy /Y "%USERPROFILE%\.config\opencode\opencode.json" "%OUT%\03-config-opencode.json" >nul
) else (
  echo MISSING > "%OUT%\03-config-opencode.json"
)
if exist "%USERPROFILE%\.opencode\opencode.json" (
  copy /Y "%USERPROFILE%\.opencode\opencode.json" "%OUT%\03-home-opencode.json" >nul
)
if exist "%USERPROFILE%\.config\opencode\oh-my-opencode.json" (
  copy /Y "%USERPROFILE%\.config\opencode\oh-my-opencode.json" "%OUT%\03-oh-my-opencode.json" >nul
)
if exist "%USERPROFILE%\.config\opencode\package.json" (
  copy /Y "%USERPROFILE%\.config\opencode\package.json" "%OUT%\03-config-package.json" >nul
)

echo [3/8] tree presence / sizes ...
> "%OUT%\04-paths.txt" (
  echo === .opencode ===
  if exist "%USERPROFILE%\.opencode" (dir /s /-c "%USERPROFILE%\.opencode" | findstr /i "File(s)") else echo MISSING
  echo === .config\opencode ===
  if exist "%USERPROFILE%\.config\opencode" (dir /s /-c "%USERPROFILE%\.config\opencode" | findstr /i "File(s)") else echo MISSING
  echo === .cache\opencode ===
  if exist "%USERPROFILE%\.cache\opencode" (dir /s /-c "%USERPROFILE%\.cache\opencode" | findstr /i "File(s)") else echo MISSING
  echo === plugin files ===
  if exist "%USERPROFILE%\.opencode\node_modules\oh-my-opencode\dist\index.js" (echo HOME plugin OK) else echo HOME plugin MISSING dist\index.js
  if exist "%USERPROFILE%\.opencode\node_modules\oh-my-opencode\dist\agents" (echo HOME agents OK) else echo HOME agents MISSING
  if exist "%USERPROFILE%\.cache\opencode\node_modules\oh-my-opencode\dist\index.js" (echo CACHE plugin OK) else echo CACHE plugin MISSING
  if exist "%USERPROFILE%\.cache\opencode\packages\oh-my-opencode\dist\index.js" (echo CACHE packages OK) else echo CACHE packages MISSING
  if exist "%USERPROFILE%\.config\opencode\node_modules\oh-my-opencode\dist\index.js" (echo CONFIG plugin OK) else echo CONFIG plugin MISSING
)

echo [4/8] file counts ...
> "%OUT%\05-counts.txt" (
  for %%P in (
    "%USERPROFILE%\.opencode\node_modules\oh-my-opencode"
    "%USERPROFILE%\.cache\opencode\node_modules\oh-my-opencode"
    "%USERPROFILE%\.cache\opencode\packages\oh-my-opencode"
    "%USERPROFILE%\.config\opencode\node_modules\oh-my-opencode"
  ) do (
    echo --- %%~P ---
    if exist %%~P (
      dir /s /b "%%~P" 2>nul | find /c /v ""
      dir /s /b "%%~P\*.md" 2>nul | find /c /v ""
    ) else (
      echo MISSING
    )
  )
)

echo [5/8] OpenCode logs ...
set "LOGDIR=%USERPROFILE%\.local\share\opencode\log"
if exist "%LOGDIR%" (
  mkdir "%OUT%\logs" 2>nul
  xcopy /Y /Q "%LOGDIR%\*.*" "%OUT%\logs\" >nul 2>&1
  dir /b /o-d "%LOGDIR%" > "%OUT%\06-log-list.txt" 2>&1
) else (
  echo NO_LOG_DIR %LOGDIR% > "%OUT%\06-log-list.txt"
)

echo [6/8] non-TUI debug commands ^(may take up to 60s each^) ...
REM These often print errors even when the TUI is black.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Continue';" ^
  "$oc=(Get-Command opencode -ErrorAction SilentlyContinue).Source;" ^
  "if (-not $oc) { $oc=Join-Path $env:USERPROFILE '.opencode\bin\opencode.exe' };" ^
  "if (-not (Test-Path -LiteralPath $oc)) { 'opencode.exe not found' | Set-Content '%OUT%\07-debug-config.txt'; exit 0 };" ^
  "function Run-Cap($args,$out,$sec=45) {" ^
  "  $p=Start-Process -FilePath $oc -ArgumentList $args -NoNewWindow -PassThru -RedirectStandardOutput $out -RedirectStandardError ($out+'.err');" ^
  "  if (-not $p.WaitForExit($sec*1000)) { try{$p.Kill()}catch{}; 'TIMEOUT after '+$sec+'s' | Add-Content $out }" ^
  "};" ^
  "Run-Cap @('--print-logs','--log-level','DEBUG','debug','config') '%OUT%\07-debug-config.txt' 60;" ^
  "Run-Cap @('--print-logs','--log-level','DEBUG','models') '%OUT%\08-models.txt' 45;" ^
  "Run-Cap @('run','--help') '%OUT%\09-run-help.txt' 20;"

echo [7/8] env PATH snippet ...
echo %PATH% > "%OUT%\10-path.txt"

echo [8/8] packing zip ...
set "ZIP=%USERPROFILE%\vd-opencode-diag.zip"
if exist "%ZIP%" del /f /q "%ZIP%"
powershell -NoProfile -Command "Compress-Archive -Path '%OUT%\*' -DestinationPath '%ZIP%' -Force"

echo.
echo ========================================
echo  Diagnostics ready
echo ========================================
echo Folder : %OUT%
echo Zip    : %ZIP%
echo.
echo Share the ZIP ^(or paste the latest file under logs\ + 07-debug-config.txt^).
echo.
echo Optional quick test ^(safe mode - disables plugins^):
echo   1. Rename %%USERPROFILE%%\.config\opencode\opencode.json to opencode.json.bak
echo   2. Create a file with only:  {"$schema":"https://opencode.ai/config.json","plugin":[]}
echo   3. Run: opencode
echo   If TUI works then, the hang is the plugin load path.
echo.
pause
exit /b 0
