"""The owner's detail: the heaviest programs, their names made safe, and the
pt-BR line that goes beside a slow cause."""
import os
import sys
import threading
import unittest

from inbox_support import HomeCase, inbox

GB = 2**30


class ProgramNames(unittest.TestCase):
    def test_hostile_names_are_made_safe(self):
        clean = inbox.clean_program
        self.assertEqual(clean(b"node\n"), "node")
        self.assertEqual(clean("\x1b[31mred"), "31mred")
        self.assertEqual(clean("line\nbreak"), "line")
        self.assertEqual(clean(b"caf\xe9\xff"), "caf")
        self.assertEqual(clean("evil.com/login"), "evil.comlogin")
        self.assertEqual(clean("gunicorn: worke"), "gunicorn")
        self.assertEqual(clean("a-very-long-program-name"), "a-very-long-pro")
        self.assertEqual(clean("\n"), "?")
        self.assertEqual(clean("¿¡"), "?")


class LinuxScan(HomeCase):
    def proc(self, entries):
        root = self.tmp / "proc"
        for pid, (comm, pages) in entries.items():
            d = root / pid
            d.mkdir(parents=True)
            (d / "comm").write_bytes(comm)
            (d / "statm").write_text(f"{pages * 2} {pages} 10 1 0 5 0\n")
        (root / "self").mkdir(parents=True, exist_ok=True)
        return root

    @unittest.skipIf(os.name == "nt", "no uid on Windows")
    def test_this_users_processes_by_name(self):
        root = self.proc({"10": (b"node\n", 1000), "11": (b"node\n", 500), "12": (b"claude\n", 700),
                          "13": (b"\x1b]0;evil\x07\n", 1)})
        sizes = inbox.scan_linux(str(root), uid=os.getuid(), page=4096)
        self.assertEqual(sizes, {"node": 1500 * 4096, "claude": 700 * 4096, "0evil": 4096})
        self.assertEqual(inbox.scan_linux(str(root), uid=os.getuid() + 1, page=4096), {})

    def test_macos_ps_asks_for_this_user_and_ucomm(self):
        seen = []

        def run(argv):
            seen.append(argv)
            return "  204800 node\n 102400 Google Chrome He\n  1024 node\nbad line\n"
        self.assertEqual(inbox.scan_ps(run, uid=501), {"node": 205824 * 1024, "Google": 102400 * 1024})
        self.assertEqual(seen, [["ps", "-x", "-U", "501", "-o", "rss=,ucomm="]])


class Scanning(unittest.TestCase):
    def test_the_top_three_by_size(self):
        scan = inbox.ProgramScan(lambda: {"a": 1, "b": 5 * GB, "c": 3 * GB, "d": 4 * GB, "z": 0})
        self.assertEqual(scan.run(), [("b", 5 * GB), ("d", 4 * GB), ("c", 3 * GB)])

    def test_a_slow_scan_gives_nothing_and_no_second_one_starts(self):
        release = threading.Event()
        calls = []

        def stuck():
            calls.append(1)
            release.wait(5)
            return {"x": 1}
        scan = inbox.ProgramScan(stuck, deadline=0.05)
        self.assertIsNone(scan.run())
        self.assertIsNone(scan.run())
        self.assertEqual(len(calls), 1)
        release.set()
        scan.thread.join(2)

    def test_a_failing_scan_gives_nothing(self):
        def broken():
            raise PermissionError("no")
        self.assertIsNone(inbox.ProgramScan(broken).run())

    def test_the_opt_out(self):
        self.assertTrue(inbox.share_programs({}))
        self.assertFalse(inbox.share_programs({"UNIO_INBOX_SHARE_PROGRAMS": "0"}))

    def test_windows_names_no_programs(self):
        self.assertIsNone(inbox.program_scanner("win32"))
        self.assertIs(inbox.program_scanner("darwin"), inbox.scan_ps)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_a_real_scan_finds_this_process(self):
        top = inbox.ProgramScan(inbox.scan_linux, deadline=10).run()
        self.assertTrue(top)


LINUX_VIEW = {"mem": {"avail": 1.2 * GB, "total": 31.4 * GB, "swapTotal": 8 * GB, "swapFree": 0.16 * GB},
              "values": {"psi_io": 0.414, "psi_cpu": 0.3, "psi_memory": 0.2},
              "disk": {"free": 1.5 * GB, "total": 200 * GB}}


class OwnerMessage(unittest.TestCase):
    def test_the_example_line(self):
        text = inbox.owner_message(["slow_memory", "slow_disk"], LINUX_VIEW,
                                   [("node", 6.9 * GB), ("claude", 3.1 * GB)])
        self.assertEqual(text, "Memória: 1,2 de 31,4 GB livres, swap 98% · Disco em espera 41% do tempo · "
                               "Mais pesados: node 6,9 GB, claude 3,1 GB")

    def test_jitter_does_not_change_the_text(self):
        a = inbox.owner_message(["slow_memory"], LINUX_VIEW)
        jittered = dict(LINUX_VIEW, mem=dict(LINUX_VIEW["mem"], avail=1.21 * GB))
        self.assertEqual(a, inbox.owner_message(["slow_memory"], jittered))

    def test_each_cause_has_its_words(self):
        view = dict(LINUX_VIEW, cap=True, capBytes=768 * 2**20, p90=4.25, retries=3, speedLimit=70)
        text = inbox.owner_message(["updating_code", "slow_network", "slow_unio", "slow_ai", "low_disk_space"], view)
        self.assertEqual(text, "Atualizando a cópia do código · Rede: falhas de conexão · "
                               "Unio: 90% das respostas em até 4,2 s · Serviço de IA: 3 novas tentativas · "
                               "Disco: 1,5 de 200,0 GB livres")
        text = inbox.owner_message(["slow_memory", "slow_cpu", "power_saving"], view)
        self.assertEqual(text, "Memória: 1,2 de 31,4 GB livres, swap 98%, serviço no limite de memória (768 MB) · "
                               "Processador em espera 30% do tempo · Processador limitado a 70% da velocidade")

    def test_a_long_detail_drops_whole_parts(self):
        view = dict(LINUX_VIEW, cap=True, capBytes=768 * 2**20, p90=4.25, retries=3, speedLimit=70)
        codes = ["updating_code", "slow_network", "slow_unio", "slow_ai", "low_disk_space", "slow_memory",
                 "slow_cpu", "power_saving"]
        text = inbox.owner_message(codes, view, [("node", GB)])
        self.assertLessEqual(len(text), 300)
        self.assertTrue(text.endswith("Processador em espera 30% do tempo"), text)

    def test_windows_has_no_swap_figure_and_macos_shows_load(self):
        win = {"mem": {"avail": GB, "total": 16 * GB, "pageAvail": GB, "pageTotal": 20 * GB}, "values": {}}
        self.assertEqual(inbox.owner_message(["slow_memory"], win), "Memória: 1,0 de 16,0 GB livres")
        mac = {"values": {"load": 1.83}}
        self.assertEqual(inbox.owner_message(["slow_cpu"], mac), "Carga do processador 1,8 por núcleo")

    def test_nothing_to_say_is_none(self):
        self.assertIsNone(inbox.owner_message([], LINUX_VIEW))

    def test_names_are_cleaned_even_if_a_scan_missed_it(self):
        text = inbox.owner_message([], {}, [("evil\n\x1b[2J", GB)])
        self.assertEqual(text, "Mais pesados: evil 1,0 GB")


if __name__ == "__main__":
    unittest.main()
