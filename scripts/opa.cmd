@echo off
rem Run the OPA CLI through Docker (see opa_docker.py). Set OPA_BIN to this file to run the policy tests.
if exist "%~dp0..\.venv\Scripts\python.exe" (
  "%~dp0..\.venv\Scripts\python.exe" "%~dp0opa_docker.py" %*
) else (
  python "%~dp0opa_docker.py" %*
)
