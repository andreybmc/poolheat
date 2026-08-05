@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

rem ============================================================================
rem  Whatsminer :8889 capture on OFFICE PC (WMT at 192.168.1.200 via VPN)
rem
rem  1) Install Wireshark (includes dumpcap + Npcap) if missing:
rem       https://www.wireshark.org/download.html
rem  2) Run this .bat AS ADMINISTRATOR
rem  3) In WhatsMinerTool: only 192.168.1.10 → Remote Ctrl → Suspend
rem  4) Press any key here to stop capture
rem  5) Send the .pcapng to the poolheat Mac:
rem       tools/whatsminer-proxy/pcap/
rem
rem  Optional env overrides before run:
rem    set MINER=192.168.1.10
rem    set OUTDIR=%USERPROFILE%\Desktop
rem ============================================================================

if not defined MINER set "MINER=192.168.1.10"
if not defined OUTDIR set "OUTDIR=%USERPROFILE%\Desktop"
if not exist "%OUTDIR%" mkdir "%OUTDIR%" 2>nul

for /f "tokens=1-3 delims=/. " %%a in ("%date%") do set "D=%%c%%b%%a"
for /f "tokens=1-3 delims=:.," %%a in ("%time: =0%") do set "T=%%a%%b%%c"
set "STAMP=%D%_%T%"
set "STAMP=%STAMP: =0%"
set "OUT=%OUTDIR%\wmt8889_%STAMP%.pcapng"
set "FILTER=host %MINER% and tcp port 8889"

echo.
echo ============================================================
echo  WMT :8889 capture  miner=%MINER%
echo  output: %OUT%
echo  filter: %FILTER%
echo ============================================================
echo.

rem --- locate dumpcap ---
set "DUMPCAP="
if exist "%ProgramFiles%\Wireshark\dumpcap.exe" set "DUMPCAP=%ProgramFiles%\Wireshark\dumpcap.exe"
if exist "%ProgramFiles(x86)%\Wireshark\dumpcap.exe" set "DUMPCAP=%ProgramFiles(x86)%\Wireshark\dumpcap.exe"
if exist "%LocalAppData%\Programs\Wireshark\dumpcap.exe" set "DUMPCAP=%LocalAppData%\Programs\Wireshark\dumpcap.exe"
where dumpcap >nul 2>&1 && for /f "delims=" %%i in ('where dumpcap') do if not defined DUMPCAP set "DUMPCAP=%%i"

if not defined DUMPCAP (
  echo [ERROR] dumpcap.exe not found.
  echo Install Wireshark with Npcap, then re-run as Administrator.
  echo https://www.wireshark.org/download.html
  echo.
  pause
  exit /b 1
)

echo Using: %DUMPCAP%
echo.

rem --- list interfaces ---
echo Available interfaces:
"%DUMPCAP%" -D
echo.
echo Pick the adapter that has the VPN / office LAN
echo (often "Ethernet", "Wi-Fi", or something with SSTP/VPN in the name).
echo.
set /p IFACE=Interface number (e.g. 1): 
if not defined IFACE (
  echo No interface selected.
  pause
  exit /b 1
)

echo.
echo ------------------------------------------------------------
echo  CAPTURE RUNNING on interface %IFACE%
echo.
echo  CRITICAL: miner must CHANGE state during capture.
echo  If already Suspend, WRITE is NOT sent (we saw empty capture).
echo.
echo  WhatsMinerTool sequence:
echo    1. Select ONLY %MINER%  (not bulk scan)
echo    2. Connect / refresh
echo    3. Remote Ctrl -^> Mining Control -^> RESUME
echo    4. Wait until hashrate/power rises ~10-20 s
echo    5. Remote Ctrl -^> Mining Control -^> SUSPEND
echo    6. Wait 3 s
echo    7. Come back here - press any key to STOP
echo.
echo  Expect in pcap: WRITE128 (or 80) after AUTH64.
echo ------------------------------------------------------------
echo.

rem dumpcap: -i iface -f capture filter -w file -q quiet
start "WMT-8889-capture" /min "%DUMPCAP%" -i %IFACE% -f "%FILTER%" -w "%OUT%" -q
if errorlevel 1 (
  echo [ERROR] dumpcap failed to start. Run as Administrator? Npcap installed?
  pause
  exit /b 1
)

rem find dumpcap PID for this output file is hard; stop all our dumpcap at end
echo Capture started. Output will be:
echo   %OUT%
echo.
pause

echo Stopping dumpcap...
taskkill /IM dumpcap.exe /F >nul 2>&1
timeout /t 1 /nobreak >nul

if not exist "%OUT%" (
  echo [WARN] File not found yet: %OUT%
  echo Check Desktop for wmt8889_*.pcapng
) else (
  for %%A in ("%OUT%") do echo Saved: %%~fA  size=%%~zA bytes
)

echo.
echo Next:
echo   1) Copy the pcapng to the Mac repo:
echo        Documents\poolheat\tools\whatsminer-proxy\pcap\
echo   2) Or send the file to the agent / chat
echo   3) On Mac:
echo        python3 analyze_8889_stream.py pcap\wmt8889_....pcapng --out-dir pcap\stream-out-office
echo.
echo Done.
pause
endlocal
exit /b 0
