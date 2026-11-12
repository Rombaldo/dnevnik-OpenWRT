"""AGENTS.md и CLAUDE.md: инструкция для ИИ-агента не должна расходиться с настоящими файлами и командами, и в ней не должно быть личного."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")


class AgentsDocTests(unittest.TestCase):
    def test_files_exist_and_claude_md_imports_agents_md(self):
        self.assertGreater(len(AGENTS), 4000)
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(claude.startswith("@AGENTS.md"))                                  # Claude Code подключает общий файл, а не дублирует его

    def test_every_repository_path_mentioned_exists(self):
        paths = set(re.findall(r"`((?:router|tools|luci|web|tests)/[A-Za-z0-9_./-]+)`", AGENTS)) | {"README.md", "dev.sh"}
        paths |= set(re.findall(r"`(setup-windows\.bat|login-windows\.bat|Установить-на-роутер\.command)`", AGENTS))
        self.assertGreaterEqual(len(paths), 6)
        for p in paths:
            self.assertTrue((ROOT / p).exists(), f"в AGENTS.md упомянут {p}, а такого файла нет")

    def test_commands_exist_in_the_scripts(self):
        remote = (ROOT / "router" / "remote.sh").read_text(encoding="utf-8")
        for sub in ("install)", "status)", "proxy)", "funnel)"):
            self.assertIn(sub, remote)
        for phrase in ("remote.sh proxy on", "remote.sh proxy off", "remote.sh funnel off"):
            self.assertIn(phrase, AGENTS)
        self.assertIn("proxy_on", remote)
        doctor = (ROOT / "router" / "doctor.py").read_text(encoding="utf-8")
        for flag in ("--summary", "--report", "--json"):
            self.assertIn(flag, AGENTS)
            self.assertIn(flag, doctor)
        password = (ROOT / "router" / "password.py").read_text(encoding="utf-8")
        for sub in ('"status"', '"set"', '"set-env"', '"off"'):
            self.assertIn(sub, password)
        for sub in ("`status`", "`set`", "`set-env`", "`off`"):
            self.assertIn(sub, AGENTS)
        installer = (ROOT / "router" / "install.sh").read_text(encoding="utf-8")
        for flag in ("--luci", "--no-luci", "SRC_TARBALL"):
            self.assertIn(flag, AGENTS)
            self.assertIn(flag, installer)
        sources = remote + (ROOT / "tools" / "setup_computer.py").read_text(encoding="utf-8")
        for v in ("DNEVNIK_MACHINE", "@@LOGIN_URL", "@@NEEDS funnel_enable", "@@ADDRESS"):
            self.assertIn(v, AGENTS)
            self.assertIn(v.replace("@@", ""), sources, v)                                    # скрипт печатает метку через mark "LOGIN_URL …", а там её добавляется «@@»

    def test_proxy_line_is_exactly_what_the_script_writes(self):
        remote = (ROOT / "router" / "remote.sh").read_text(encoding="utf-8")
        line = "HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890 NO_PROXY=127.0.0.1,localhost,192.168.0.0/16"
        self.assertIn(line, AGENTS)
        self.assertIn("NO_PROXY=127.0.0.1,localhost,192.168.0.0/16", remote)
        self.assertIn("7890", remote)

    def test_the_safety_rules_are_there(self):
        for needle in ("Пароли и логины не просят", "Без пароля семьи в интернет", "Резервная копия", "Честность", "set-env", "doctor.py --report",
                       "навсегда попадёт в журнал сертификатов", "проверено только на OpenWrt 25.12"):
            self.assertIn(needle, AGENTS, needle)

    def test_all_steps_are_present_in_order(self):
        steps = [AGENTS.index(f"### Шаг {n}.") for n in range(8)]
        self.assertEqual(steps, sorted(steps))

    def test_no_personal_data(self):
        for text in (AGENTS, (ROOT / "CLAUDE.md").read_text(encoding="utf-8")):
            self.assertIsNone(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", text))      # ни одной почты
            self.assertNotRegex(text, r"tail[0-9a-f]{6}|100\.(109|68|83)\.|/Users/|fd7a:")

    def test_readme_tells_people_how_to_use_it(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("AGENTS.md", readme)
        self.assertIn("ИИ-агент", readme)


if __name__ == "__main__":
    unittest.main()
