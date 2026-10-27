"""What differs between Linux, macOS and Windows: where the state lives, file
locks, a process's name, parent, start time and environment, and starting a
process that outlives the hook.

A start time is an opaque string, compared only for equality: the clock
ticks of /proc/<pid>/stat on Linux (as the hand-made hook wrote them, so a
session it watches keeps its watcher), `ps -o lstart=` on macOS, and the
creation FILETIME on Windows.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

WINDOWS = os.name == "nt"
LINUX = sys.platform.startswith("linux")

if WINDOWS:
    import msvcrt
else:
    import fcntl

# What Claude Code runs a hook command through, and the launcher itself; the
# walk up from the hook passes these and stops at anything else. py is the
# Windows launcher, which runs python.exe as its child; Git for Windows'
# bin\bash.exe runs usr\bin\bash.exe as its child in turn.
SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash", "env", "timeout", "cmd", "py"}
CHILD_MARK = b"CLAUDE_CODE_CHILD_SESSION="


def state_dir():
    """~/.local/state/unio/sessions (or under XDG_STATE_HOME), the hand-made
    hook's own folder; %LOCALAPPDATA%\\unio\\sessions on Windows, where an
    XDG_STATE_HOME naming a drive wins as it does elsewhere."""
    xdg = os.environ.get("XDG_STATE_HOME") or ""
    if WINDOWS:
        if re.match(r"^[A-Za-z]:[\\/]", xdg):
            return Path(xdg) / "unio" / "sessions"
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "unio" / "sessions"
    return Path(xdg or Path.home() / ".local" / "state") / "unio" / "sessions"


def try_lock(f):
    """An exclusive lock on open file `f`, at once or not at all: flock on
    POSIX, its first byte on Windows (opened to append, since Windows refuses
    to truncate a file another process locks)."""
    try:
        if WINDOWS:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def unlock(f):
    """Windows frees a closed file's lock only when it gets to it."""
    if WINDOWS:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


def replace(src, dst):
    """os.replace, retried for a moment on Windows, where it fails while
    another process has `dst` open."""
    for attempt in range(10):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if not WINDOWS or attempt == 9:
                raise
            import time
            time.sleep(0.05)


def base_name(raw):
    name = re.split(r"[\\/]", raw.strip())[-1].lstrip("-").lower()
    return name[:-4] if name.endswith(".exe") else name


# Linux --------------------------------------------------------------------

def _linux_process(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    end = raw.rfind(")")
    fields = raw[end + 2:].split()
    if len(fields) < 20 or fields[0] in ("Z", "X"):
        return None
    return base_name(raw[raw.find("(") + 1:end]), int(fields[1]), fields[19]


def _linux_environ(pid):
    try:
        return Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return None


# macOS (and any other POSIX system with ps) --------------------------------

def _ps_process(pid):
    """ps names a process by its executable's path, and lstart has a fixed
    five words in the C locale."""
    try:
        out = subprocess.run(["ps", "-o", "ppid=,stat=,lstart=,comm=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=3, stdin=subprocess.DEVNULL,
                             env=dict(os.environ, LC_ALL="C")).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    parts = out.strip().split(None, 7)
    if len(parts) < 8 or not parts[0].isdigit() or parts[1].startswith("Z"):
        return None
    return base_name(parts[7]), int(parts[0]), " ".join(parts[2:7])


def parse_procargs(raw):
    """The environment in a KERN_PROCARGS2 buffer: argc, the executable's
    path, NUL padding, argc arguments, then the environment up to an empty
    string."""
    if len(raw) < 4:
        return []
    argc = int.from_bytes(raw[:4], sys.byteorder)
    rest = raw[4:]
    end = rest.find(b"\0")
    if end < 0:
        return []
    items = rest[end:].lstrip(b"\0").split(b"\0")
    env = []
    for item in items[argc:]:
        if not item:
            break
        env.append(item)
    return env


def _macos_environ(pid):
    """Another process's environment, readable for the user's own processes."""
    import ctypes
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    argmax = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(argmax))
    if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:
        return None  # CTL_KERN, KERN_ARGMAX
    buf = ctypes.create_string_buffer(argmax.value)
    size = ctypes.c_size_t(argmax.value)
    if libc.sysctl((ctypes.c_int * 3)(1, 49, int(pid)), 3, buf, ctypes.byref(size), None, 0) != 0:
        return None  # CTL_KERN, KERN_PROCARGS2
    return parse_procargs(buf.raw[:size.value])


# Windows ------------------------------------------------------------------

_K32 = None
SYNCHRONIZE = 0x00100000
QUERY_LIMITED = 0x1000
WAIT_TIMEOUT = 0x102


def _kernel32():
    global _K32
    if _K32 is None:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = wintypes.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.CreateToolhelp32Snapshot.restype = handle
        k.Process32FirstW.argtypes = [handle, ctypes.c_void_p]
        k.Process32FirstW.restype = wintypes.BOOL
        k.Process32NextW.argtypes = [handle, ctypes.c_void_p]
        k.Process32NextW.restype = wintypes.BOOL
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = handle
        k.WaitForSingleObject.argtypes = [handle, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.GetProcessTimes.argtypes = [handle, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        k.GetProcessTimes.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [handle]
        k.CloseHandle.restype = wintypes.BOOL
        k.QueryFullProcessImageNameW.argtypes = [handle, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        _K32 = k
    return _K32


def _windows_table():
    """pid -> (name, parent pid) of every process, from one snapshot."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    k = _kernel32()
    snap = k.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return {}
    table = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = (base_name(entry.szExeFile), entry.th32ParentProcessID)
            ok = k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return table


def _windows_start(pid):
    import ctypes
    from ctypes import wintypes
    k = _kernel32()
    h = k.OpenProcess(SYNCHRONIZE | QUERY_LIMITED, False, int(pid))
    if not h:
        return None
    try:
        if k.WaitForSingleObject(h, 0) != WAIT_TIMEOUT:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(h, *[ctypes.byref(t) for t in times]):
            return None
        return str(times[0].dwHighDateTime << 32 | times[0].dwLowDateTime)
    finally:
        k.CloseHandle(h)


def _windows_process(pid, table=None):
    table = _windows_table() if table is None else table
    found = table.get(int(pid))
    started = _windows_start(pid) if found else None
    if not started:
        return None
    return found[0], found[1], started


# Every system -------------------------------------------------------------

def process(pid, table=None):
    """(name, parent pid, start time) of a live process; None once it is gone.
    The name is the executable's, lower case, without .exe."""
    try:
        if WINDOWS:
            return _windows_process(pid, table)
        if LINUX:
            return _linux_process(pid)
        return _ps_process(pid)
    except Exception:
        return None


def start_time(pid):
    """A live process's start time; None once it is gone."""
    try:
        if WINDOWS:
            return _windows_start(pid)
    except Exception:
        return None
    found = process(pid)
    return found[2] if found else None


def claude_process():
    """The Claude Code process this hook runs under, as (pid, start time).
    The walk passes the shells between Claude Code and the hook and stops at
    the first other process: Claude Code's own name, or the pid Claude Code
    gives its hooks as CLAUDE_PID (a Claude Code started through node or a
    renamed binary). A hook run by hand from a Claude Code shell finds that
    enclosing process; the caller refuses it by its session id."""
    want = os.environ.get("CLAUDE_PID") or ""
    table = None
    if WINDOWS:
        try:
            table = _windows_table()
        except Exception:
            return None
    pid = os.getppid()
    for _ in range(10):
        found = process(pid, table)
        if not found:
            return None
        name, parent, started = found
        if name == "claude" or str(pid) == want:
            return pid, started
        if name not in SHELLS:
            return None
        pid = parent
    return None


def is_child(pid):
    """True when Claude Code process `pid` was started by another session (a
    workflow agent, or `claude` from a session's shell). Claude Code puts
    CLAUDE_CODE_CHILD_SESSION into every hook's environment, top-level
    sessions included, so only the process's own environment tells them
    apart. Windows lets no process read another's environment without
    walking its memory, so there a Claude Code among its ancestors says so."""
    try:
        if WINDOWS:
            return _windows_claude_above(pid)
        env = _linux_environ(pid) if LINUX else _macos_environ(pid) if sys.platform == "darwin" else None
    except Exception:
        return False
    return any(v.startswith(CHILD_MARK) for v in env or [])


def _windows_image(pid):
    """The full path of process `pid`'s executable, case-folded, or None."""
    import ctypes
    from ctypes import wintypes

    k = _kernel32()
    h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return None
        return os.path.normcase(buf.value)
    finally:
        k.CloseHandle(h)


def _windows_claude_above(pid):
    """Another Claude Code above `pid`. Only one running the same executable
    counts: Claude Desktop's process is Claude.exe too, and the sessions it
    starts are top-level ones."""
    table = _windows_table()
    own = _windows_start(pid)
    image = _windows_image(pid)
    seen = {int(pid)}
    current = table.get(int(pid), (None, None))[1]
    for _ in range(12):
        if not current or current in seen or current not in table:
            return False
        seen.add(current)
        name, parent = table[current]
        started = _windows_start(current)
        # A parent id Windows has since given to a newer process is no parent.
        if started is None or own is not None and int(started) > int(own):
            return False
        if name == "claude" and (image is None or _windows_image(current) in (image, None)):
            return True
        current = parent
    return False


def detach(args, background=False):
    """Starts this Python with `args`, in no console, process group or job of
    the hook's, with nothing of its streams, so Claude Code neither waits
    for it nor ends it with the hook. `background` lowers its priority on
    Windows (POSIX lowers its own)."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True, cwd=os.path.abspath(os.sep))
    exe = sys.executable
    # The hook runs in UTF-8 mode (session-status.sh); what it starts does too.
    args = ["-X", "utf8", *args]
    if not WINDOWS:
        return subprocess.Popen([exe, *args], start_new_session=True, **kw)
    windowless = Path(exe).with_name("pythonw.exe")
    if windowless.is_file():
        exe = str(windowless)
    flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW
    if background:
        flags |= 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
    try:
        return subprocess.Popen([exe, *args], creationflags=flags | 0x01000000, **kw)  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        # A job that forbids breaking away still lets the process start inside it.
        return subprocess.Popen([exe, *args], creationflags=flags, **kw)
