@echo off
rem Check that lvmcp can talk to lvmcp_server.vi running in LabVIEW
cd /d "%~dp0"
.venv\Scripts\python -m lvmcp.selftest %*
pause
