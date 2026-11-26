@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Transfer control without CALL to avoid expanding forwarded arguments twice.
"%~dp0scripts\windows_bootstrap.bat" desktop --local-data %*
