@echo off
rem unio on Windows, the twin of bin\unio: Unio's command on this computer.
rem install.ps1 puts this folder on the user's Path; it runs from the clone
rem it sits in. Every command but runner and resume is bin\unio itself, run
rem by Git for Windows' own sh.exe, so both systems print the same words.
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
if /i "%unio_cmd%"=="connect" goto :posix
if /i "%unio_cmd%"=="status" goto :posix
if /i "%unio_cmd%"=="doctor" goto :posix
if /i "%unio_cmd%"=="update" goto :posix
if /i "%unio_cmd%"=="disconnect" goto :posix
if /i "%unio_cmd%"=="resume" goto :resume
if /i "%unio_cmd%"=="runner" goto :runner
if /i "%unio_cmd%"=="help" if not "%~2"=="" if /i not "%~2"=="runner" goto :posix
if /i "%unio_cmd%"=="help" goto :help
if /i "%unio_cmd%"=="-h" goto :help
if /i "%unio_cmd%"=="--help" goto :help
if /i "%unio_cmd%"=="--version" goto :version
setlocal EnableDelayedExpansion
>&2 echo Comando desconhecido: !unio_cmd:~0,40!. Rode unio help.
exit /b 2

:posix
rem Git for Windows' sh.exe is not on the Path. git names its own install
rem with --exec-path (<root>/mingw64/libexec/git-core), whether the git on
rem the Path is Git's cmd\git.exe, its bin\git.exe or a shim such as
rem Scoop's; a git that cannot answer falls back to the folder beside each
rem git.exe that where finds. The script's path goes to sh with forward
rem slashes, which bin\unio reads as its own.
set "unio_sh="
set "unio_exec="
for /f "delims=" %%e in ('git --exec-path 2^>nul') do if not defined unio_exec set "unio_exec=%%e"
if defined unio_exec for %%r in ("%unio_exec:/=\%\..\..\..") do (
  if exist "%%~fr\bin\sh.exe" set "unio_sh=%%~fr\bin\sh.exe"
  if not defined unio_sh if exist "%%~fr\usr\bin\sh.exe" set "unio_sh=%%~fr\usr\bin\sh.exe"
)
if not defined unio_sh for /f "delims=" %%g in ('where git 2^>nul') do if not defined unio_sh if exist "%%~dpg..\bin\sh.exe" set "unio_sh=%%~dpg..\bin\sh.exe"
if not defined unio_sh goto :no_git_bash
set "unio_script=%unio_root:\=/%/bin/unio"
"%unio_sh%" "%unio_script%" %*
exit /b %errorlevel%

:runner
rem Everything after the first word, quoted as it was typed. A bare unio
rem runner shows the runner's help, as unio help runner does.
set "unio_args=%*"
rem An argument holding "&", "|" or "^" would break cmd's parsing of
rem these lines; workspace ids and flags never hold one.
set "unio_args=%unio_args:*runner=%"
if not defined unio_args set "unio_args= --help"

:start_runner
if not exist "%unio_root%\runner\unio-runner" goto :no_runner
rem Only a run that exits 0 proves an interpreter: the python.exe Windows
rem ships is a Microsoft Store alias that opens the Store. call, so that a
rem batch shim such as pyenv-win's python.bat comes back here.
set "unio_python="
call py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" <nul >nul 2>nul && set "unio_python=py -3"
if not defined unio_python call python -c "import sys; sys.exit(sys.version_info < (3, 9))" <nul >nul 2>nul && set "unio_python=python"
if not defined unio_python goto :no_python
%unio_python% -X utf8 "%unio_root%\runner\unio-runner"%unio_args%
exit /b %errorlevel%

:resume
rem The session list lives in %LOCALAPPDATA%, which Python reads natively,
rem and claude must start in this console, so Python runs here and not
rem under Git's sh. Its help is bin\unio's, which sh prints.
for %%a in (%*) do if /i "%%~a"=="-h" goto :posix
for %%a in (%*) do if /i "%%~a"=="--help" goto :posix
rem The arguments go on in their own slots, never through a variable:
rem expanding one would let a quoted "&" or "|" end the line.
set "unio_python="
call py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" <nul >nul 2>nul && set "unio_python=py -3"
if not defined unio_python call python -c "import sys; sys.exit(sys.version_info < (3, 9))" <nul >nul 2>nul && set "unio_python=python"
if not defined unio_python goto :no_python
%unio_python% -X utf8 -I "%unio_root%\plugins\unio\scripts\sessions.py" resume %2 %3 %4 %5 %6 %7 %8 %9
exit /b %errorlevel%

:help
if /i "%~2"=="runner" (
  set "unio_args= --help"
  goto :start_runner
)
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

:no_git_bash
>&2 echo Falta o Git for Windows, que traz o sh em que este comando roda: instale com winget install --id Git.Git -e, abra outro terminal e rode este comando de novo.
exit /b 1

:no_python
>&2 echo Falta o Python 3.9 ou mais novo: instale com winget install Python.Python.3.12, abra outro terminal e rode este comando de novo.
exit /b 1

:no_version
setlocal EnableDelayedExpansion
>&2 echo Falta runner\VERSION em !unio_root!.
exit /b 1
