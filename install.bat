@echo off
rem lvmcp setup: Python environment, self-test (opens LabVIEW if needed), Claude Code registration.
rem Needs Python 3.10+ from python.org and LabVIEW 2014 or newer.
cd /d "%~dp0"
echo == 1/3 Python environment
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv 2>nul || python -m venv .venv
)
if not exist .venv\Scripts\python.exe (
  echo Python was not found. Install Python 3.10+ from https://www.python.org ^(tick "Add python.exe to PATH"^)
  echo and run install.bat again.
  pause
  exit /b 1
)
.venv\Scripts\python -m pip install --disable-pip-version-check -q -r requirements.txt || (
  echo pip install failed ^(no internet?^)
  pause
  exit /b 1
)
echo.
echo == 2/3 Self-test with LabVIEW ^(opens labview\lvmcp_server.vi if it is not running; first run about 1-2 min^)
.venv\Scripts\python -m lvmcp.selftest || (
  echo.
  echo The self-test failed: see above. Fix it and run install.bat again.
  pause
  exit /b 1
)
echo.
echo == 3/3 AI client
where claude >nul 2>nul && (
  rem replace an earlier registration (e.g. of an older lvmcp folder)
  call claude mcp remove -s user labview >nul 2>nul
  call claude mcp add -s user labview -- "%~dp0.venv\Scripts\python.exe" "%~dp0lvmcp\mcp_server.py" && (
    echo Registered with Claude Code as MCP server "labview" ^(all folders^).
  ) || (
    echo Could not register with Claude Code: see docs\INSTALL.md to do it by hand.
  )
) || (
  echo Claude Code not found. To use lvmcp from an AI client, register this command as an MCP server:
  echo   "%~dp0.venv\Scripts\python.exe" "%~dp0lvmcp\mcp_server.py"
  echo ^(details: docs\INSTALL.md^)
)
echo.
echo Done. Start your AI client and ask it to build a VI, e.g. "make a LabVIEW VI that adds two numbers".
pause
