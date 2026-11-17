"""The computer's vitals: each system's readers fed fake /proc files, sysctl
values and kernel32 answers, so every reader runs on every system."""
import ctypes
import subprocess
import sys
import unittest
from collections import namedtuple

from inbox_support import HomeCase, inbox

Usage = namedtuple("Usage", "total used free")
DISK = Usage(200 * 2**30, 150 * 2**30, 50 * 2**30)

CGROUP = "/user.slice/user-1000.slice/user@1000.service/app.slice/unio-runner@org_x.service"
MOUNTINFO = ("24 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
             "33 24 0:28 / /sys/fs/cgroup rw,nosuid shared:9 - cgroup2 cgroup2 rw,nsdelegate\n")
PSI_FULL = "some avg10=0.00 avg60=0.00 avg300=0.00 total={some}\nfull avg10=0.00 avg60=0.00 avg300=0.00 total={full}\n"
MEMINFO = ("MemTotal:       32000000 kB\nMemFree:          100000 kB\nMemAvailable:    2000000 kB\n"
           "SwapTotal:       8000000 kB\nSwapFree:          400000 kB\n")
STAT = ("cpu  100 0 50 800 40 5 5 0 0 0\ncpu0 100 0 50 800 40 5 5 0 0 0\nintr 1\n"
        "procs_running 3\nprocs_blocked 1\n")
VMSTAT = "nr_free_pages 1\npswpin 1200\npswpout 3\npgmajfault 900\n"


def psi(some, full):
    return PSI_FULL.format(some=some, full=full)


def linux_files(cgroup=True, system=True):
    files = {"/proc/self/cgroup": f"0::{CGROUP}\n", "/proc/self/mountinfo": MOUNTINFO,
             "/proc/meminfo": MEMINFO, "/proc/stat": STAT, "/proc/vmstat": VMSTAT}
    if system:
        files.update({"/proc/pressure/memory": psi(9_000_000, 7_000_000),
                      "/proc/pressure/io": psi(8_000_000, 6_000_000),
                      "/proc/pressure/cpu": psi(5_000_000, 0)})
    if cgroup:
        base = "/sys/fs/cgroup" + CGROUP
        files.update({base + "/memory.pressure": psi(3_000_000, 2_000_000),
                      base + "/io.pressure": psi(1_500_000, 1_000_000),
                      base + "/cpu.pressure": psi(4_000_000, 0),
                      base + "/memory.events": "low 0\nhigh 4\nmax 1\noom 0\noom_kill 0\n",
                      base + "/memory.high": "805306368\n"})
    return files


class Files:
    def __init__(self, files):
        self.files = dict(files)
        self.reads = []

    def __call__(self, path):
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(2, "No such file", path)
        return self.files[path]


class CgroupDiscovery(unittest.TestCase):
    def test_the_runner_unit_cgroup_under_the_cgroup2_mount(self):
        self.assertEqual(inbox.cgroup_dir(Files(linux_files())), "/sys/fs/cgroup" + CGROUP)

    def test_the_mount_comes_from_mountinfo_with_its_escapes(self):
        files = linux_files()
        files["/proc/self/mountinfo"] = "40 1 0:30 / /run/my\\040cgroups rw - cgroup2 none rw\n"
        self.assertEqual(inbox.cgroup_dir(Files(files)), "/run/my cgroups" + CGROUP)

    def test_a_mount_rooted_below_the_tree_is_cut_from_the_path(self):
        files = linux_files()
        files["/proc/self/mountinfo"] = "40 1 0:30 /user.slice /mnt/cg rw - cgroup2 none rw\n"
        self.assertEqual(inbox.cgroup_dir(Files(files)), "/mnt/cg" + CGROUP[len("/user.slice"):])

    def test_any_other_cgroup_reads_the_system(self):
        files = linux_files()
        files["/proc/self/cgroup"] = "0::/user.slice/user-1000.slice/session-2.scope\n"
        self.assertIsNone(inbox.cgroup_dir(Files(files)))
        files["/proc/self/cgroup"] = "12:memory:/system.slice/unio-runner@org_x.service\n"
        self.assertIsNone(inbox.cgroup_dir(Files(files)))
        files = linux_files()
        files["/proc/self/mountinfo"] = MOUNTINFO.splitlines()[0] + "\n"
        self.assertIsNone(inbox.cgroup_dir(Files(files)))


class LinuxReadings(HomeCase):
    def reader(self, files):
        return inbox.LinuxReader(self.tmp, read_file=Files(files), disk_usage=lambda path: DISK, ncpu=4)

    def test_a_sample_reads_the_bot_cgroup_and_the_system(self):
        r = self.reader(linux_files())
        s = r.sample()
        self.assertEqual(s["psiSource"], "cgroup")
        self.assertEqual(s["psi"], {"memory": 2.0, "io": 1.0, "cpu": 4.0})
        self.assertEqual(s["sysPsi"], {"memory": 7.0, "io": 6.0, "cpu": 5.0})
        self.assertEqual(s["cpu"], {"busy": 160, "iowait": 40, "total": 1000})
        self.assertEqual((s["running"], s["blocked"], s["ncpu"]), (3, 1, 4))
        self.assertEqual(s["vm"], {"pswpin": 1200, "pgmajfault": 900})
        self.assertEqual(s["mem"], {"avail": 2_048_000_000, "total": 32_768_000_000,
                                    "swapTotal": 8_192_000_000, "swapFree": 409_600_000})
        self.assertEqual((s["events"], s["cap"]), (5, 805306368))
        self.assertEqual(s["disk"], {"free": DISK.free, "total": DISK.total})
        self.assertEqual(r.off, {})

    def test_without_the_cgroup_files_the_system_psi_is_the_bots(self):
        s = self.reader(linux_files(cgroup=False)).sample()
        self.assertEqual(s["psiSource"], "system")
        self.assertEqual(s["psi"], s["sysPsi"])
        self.assertNotIn("events", s)

    def test_a_missing_file_switches_its_signal_off_once(self):
        files = linux_files(cgroup=False, system=False)
        del files["/proc/vmstat"]
        r = self.reader(files)
        first = r.sample()
        self.assertNotIn("psi", first)
        self.assertNotIn("vm", first)
        self.assertEqual(set(r.off), {"psi", "sysPsi", "vm"})
        reads = len(r.read_file.reads)
        r.sample()
        later = r.read_file.reads[reads:]
        self.assertNotIn("/proc/vmstat", later)
        self.assertNotIn("/proc/pressure/memory", later)
        self.assertIn("/proc/meminfo", later)

    def test_garbage_never_raises(self):
        files = {k: "\x00garbage\n" for k in linux_files()}
        s = self.reader(files).sample()
        self.assertEqual(set(s), {"disk", "ncpu"})


class Structs(unittest.TestCase):
    def test_native_struct_sizes_hold_on_every_system(self):
        self.assertEqual(ctypes.sizeof(inbox.MEMORYSTATUSEX), 64)
        self.assertEqual(ctypes.sizeof(inbox.SYSTEM_POWER_STATUS), 12)
        self.assertEqual(ctypes.sizeof(inbox.XSW_USAGE), 32)
        self.assertEqual(ctypes.sizeof(inbox.FILETIME), 8)


class FakeSysctl:
    def __init__(self, values):
        self.values = values

    def __call__(self, name, ctype):
        if name not in self.values:
            raise OSError(2, f"sysctlbyname {name}")
        value = self.values[name]
        if isinstance(value, dict):
            return ctype(**value)
        return ctype(value)


class FakePmset:
    def __init__(self, general="", therm=""):
        self.general, self.therm = general, therm
        self.calls = 0
        self.fail = False

    def __call__(self, *args):
        self.calls += 1
        if self.fail:
            raise subprocess.TimeoutExpired("pmset", 3)
        return self.therm if args == ("therm",) else self.general


MAC_VALUES = {"kern.memorystatus_vm_pressure_level": 2, "hw.memsize": 16 * 2**30, "kern.memorystatus_level": 25,
              "vm.swapusage": {"xsu_total": 4 * 2**30, "xsu_avail": 2**30, "xsu_used": 3 * 2**30,
                               "xsu_pagesize": 16384, "xsu_encrypted": 1}}


class MacReadings(HomeCase):
    def reader(self, pmset, values=MAC_VALUES):
        self.now = 1000.0
        return inbox.MacReader(self.tmp, sysctl=FakeSysctl(values), loadavg=lambda: (12.0, 8.0, 4.0), ncpu=8,
                               pmset=pmset, disk_usage=lambda path: DISK, clock=lambda: self.now)

    def test_a_sample_reads_pressure_memory_swap_load_and_power(self):
        pm = FakePmset(" lowpowermode         1\n", "Note: No thermal warning level has been recorded\n")
        s = self.reader(pm).sample()
        self.assertEqual(s["level"], 2)
        self.assertEqual(s["mem"], {"total": 16 * 2**30, "avail": 4 * 2**30, "swapTotal": 4 * 2**30,
                                    "swapFree": 2**30})
        self.assertEqual(s["load"], 1.5)
        self.assertIs(s["power"], True)
        self.assertNotIn("speedLimit", s)

    def test_pmset_runs_at_most_every_five_minutes(self):
        pm = FakePmset(" powermode            0\n", "CPU_Speed_Limit \t= 100\n")
        r = self.reader(pm)
        self.assertIs(r.sample()["power"], False)
        self.now += 299
        r.sample()
        self.assertEqual(pm.calls, 2)
        pm.therm = "CPU_Speed_Limit \t= 70\n"
        self.now += 1
        s = r.sample()
        self.assertEqual(pm.calls, 4)
        self.assertEqual((s["power"], s["speedLimit"]), (True, 70))

    def test_a_missing_key_is_unknown_never_normal(self):
        s = self.reader(FakePmset(" sleep 1\n", "")).sample()
        self.assertNotIn("power", s)
        self.assertEqual(inbox.parse_pmset(" powermode 1\n", "")["lowPower"], True)
        self.assertEqual(inbox.parse_pmset("", "CPU_Speed_Limit = 100"), {"lowPower": None, "speedLimit": 100})

    def test_a_failed_pmset_is_retried_later_and_never_raises(self):
        pm = FakePmset(" lowpowermode 1\n")
        pm.fail = True
        r = self.reader(pm)
        self.assertNotIn("power", r.sample())
        self.assertIn("power", r.failed)
        pm.fail = False
        self.now += 300
        self.assertIs(r.sample()["power"], True)

    def test_a_failing_sysctl_switches_off_only_its_signal(self):
        values = {k: v for k, v in MAC_VALUES.items() if k != "kern.memorystatus_vm_pressure_level"}
        s = self.reader(FakePmset(), values).sample()
        self.assertNotIn("level", s)
        self.assertIn("mem", s)
        self.assertEqual(list(self.reader(FakePmset(), {}).sample()), ["disk", "load"])


class FakeKernel32:
    """kernel32 as WinReader calls it: each function fills the structure it is pointed at."""

    def __init__(self):
        self.ok = True
        self.times = [(0, 1000), (0, 1500), (0, 500)]
        self.saver = 0

    def GlobalMemoryStatusEx(self, p):
        m = p.contents
        assert m.dwLength == 64, "dwLength must be set before the call"
        m.dwMemoryLoad, m.ullTotalPhys, m.ullAvailPhys = 95, 16 * 2**30, 2**29
        m.ullTotalPageFile, m.ullAvailPageFile = 20 * 2**30, 2**30
        return 1 if self.ok else 0

    def GetSystemTimes(self, idle, kernel, user):
        for p, (high, low) in zip((idle, kernel, user), self.times):
            p.contents.dwHighDateTime, p.contents.dwLowDateTime = high, low
        return 1 if self.ok else 0

    def GetSystemPowerStatus(self, p):
        p.contents.SystemStatusFlag = self.saver
        return 1 if self.ok else 0


class WindowsReadings(HomeCase):
    def test_a_sample_reads_memory_cpu_times_and_the_saver(self):
        k = FakeKernel32()
        k.saver = 1
        r = inbox.WinReader(self.tmp, kernel32=k, disk_usage=lambda path: DISK)
        s = r.sample()
        self.assertEqual(s["mem"], {"avail": 2**29, "total": 16 * 2**30, "pageAvail": 2**30,
                                    "pageTotal": 20 * 2**30, "load": 95})
        self.assertEqual(s["cpu"], {"busy": 1000, "total": 2000})
        self.assertIs(s["power"], True)
        k.times = [(1, 0), (1, 0), (0, 0)]
        self.assertEqual(r.sample()["cpu"], {"busy": 0, "total": 2**32})

    def test_a_failing_call_switches_off_without_raising(self):
        k = FakeKernel32()
        k.ok = False
        r = inbox.WinReader(self.tmp, kernel32=k, disk_usage=lambda path: DISK)
        self.assertEqual(list(r.sample()), ["disk"])
        self.assertEqual(set(r.off), {"mem", "cpu", "power"})


class NativeSmoke(HomeCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_linux(self):
        r = inbox.vitals_reader(self.tmp)
        s = r.sample()
        self.assertIsInstance(r, inbox.LinuxReader)
        self.assertIn("disk", s)
        self.assertIn("mem", s)

    @unittest.skipUnless(sys.platform == "darwin", "macOS only")
    def test_macos(self):
        r = inbox.vitals_reader(self.tmp)
        s = r.sample()
        self.assertIn(s.get("level"), (1, 2, 4))
        self.assertGreater(s["mem"]["total"], 0)
        self.assertIn("load", s)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_windows(self):
        r = inbox.vitals_reader(self.tmp)
        s = r.sample()
        self.assertGreater(s["mem"]["total"], 0)
        self.assertGreater(s["cpu"]["total"], 0)
        self.assertIn("power", s)


if __name__ == "__main__":
    unittest.main()
