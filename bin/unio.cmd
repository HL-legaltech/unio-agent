@echo off
rem unio on Windows, the twin of bin\unio: Unio's command on this computer.
rem install.ps1 -Runner puts this folder on the user's Path; it runs from the
rem clone it sits in.
rem
rem unio help prints the lines of bin\unio that start with "#:", the one
rem table of commands. A command in that table needs its branch here too.
rem
rem This file stays ASCII, with CRLF line ends: cmd reads it in the console's
rem code page, and in a file with bare LFs it can miss a label. The help
rem text, in UTF-8, is printed under code page 65001 and the console's own
rem is put back after, since a chcp outlives this script.
setlocal EnableExtensions DisableDelayedExpansion
for %%r in ("%~dp0..") do set "unio_root=%%~fr"
set "unio_cmd=%~1"
if not defined unio_cmd goto :help
if /i "%unio_cmd%"=="runner" goto :runner
if /i "%unio_cmd%"=="help" goto :help
if /i "%unio_cmd%"=="-h" goto :help
if /i "%unio_cmd%"=="--help" goto :help
if /i "%unio_cmd%"=="--version" goto :version
setlocal EnableDelayedExpansion
>&2 echo Comando desconhecido: !unio_cmd:~0,40!. Rode unio help.
exit /b 2

:runner
if not exist "%unio_root%\runner\unio-runner" goto :no_runner
set "unio_python="
where py >nul 2>nul && set "unio_python=py -3"
if not defined unio_python where python >nul 2>nul && set "unio_python=python"
if not defined unio_python goto :no_python
rem Everything after the first word, quoted as it was typed.
set "unio_args=%*"
set "unio_args=%unio_args:*runner=%"
%unio_python% -X utf8 "%unio_root%\runner\unio-runner"%unio_args%
exit /b %errorlevel%

:help
set "unio_cp="
for /f "tokens=2 delims=:." %%c in ('chcp') do set "unio_cp=%%c"
chcp 65001 >nul 2>nul
for /f "usebackq delims=" %%l in (`findstr /b /c:"#:" "%unio_root%\bin\unio"`) do (
  set "unio_line=%%l"
  setlocal EnableDelayedExpansion
  echo(!unio_line:~3!
  endlocal
)
if defined unio_cp chcp %unio_cp% >nul 2>nul
exit /b 0

:version
set "unio_version="
if exist "%unio_root%\runner\VERSION" for /f "usebackq delims=" %%v in ("%unio_root%\runner\VERSION") do if not defined unio_version set "unio_version=%%v"
if not defined unio_version goto :no_version
echo(%unio_version%
exit /b 0

:no_runner
setlocal EnableDelayedExpansion
>&2 echo Falta runner\unio-runner em !unio_root!. Atualize o clone e rode este comando de novo.
exit /b 1

:no_python
>&2 echo Falta o Python 3.9 ou mais novo: instale com winget install Python.Python.3.12, abra outro terminal e rode este comando de novo.
exit /b 1

:no_version
setlocal EnableDelayedExpansion
>&2 echo Falta runner\VERSION em !unio_root!.
exit /b 1
