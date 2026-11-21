import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_cctv_edge import repair, startup

try:
    import pwd
except ImportError:  # pragma: no cover - Windows development environment
    pwd = None


@unittest.skipUnless(pwd is not None and os.name == "posix" and os.geteuid() == 0, "requires Linux root for UID permission checks")
class StartupPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o755)
        self.config = self.root / "config.toml"
        self.config.write_text("test configuration\n", encoding="utf-8")
        self.private = self.root / "repair"
        self.private.mkdir(mode=0o700)
        os.chmod(self.private, 0o700)
        self.startup_dir = self.root / "runtime"
        self.startup_dir.mkdir(mode=0o755)
        os.chmod(self.startup_dir, 0o755)
        self.startup_file = self.startup_dir / "startup.json"
        self.startup_file.write_text(json.dumps({
            "version": 1,
            "allowed": True,
            "entrypoints": ["run"],
            "config_sha256": hashlib.sha256(self.config.read_bytes()).hexdigest(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }), encoding="utf-8")
        os.chown(self.startup_file, 0, pwd.getpwnam("nobody").pw_gid)
        os.chmod(self.startup_file, 0o440)

    def tearDown(self):
        self.temp.cleanup()

    def _as_service_user(self, source):
        account = pwd.getpwnam("nobody")
        code = (
            "from pathlib import Path; "
            "from ai_cctv_edge import startup; "
            f"startup.STARTUP_PATH=Path({str(self.startup_file)!r}); "
            "startup.verify_persistent=lambda config: None; "
            + source
        )
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            preexec_fn=lambda: (os.setgid(account.pw_gid), os.setuid(account.pw_uid)),
            check=False,
        )

    def test_unprivileged_startup_does_not_probe_private_journal(self):
        code = (
            f"startup.require_startup(Path({str(self.config)!r}), 'run')"
        )
        result = self._as_service_user(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.private.stat().st_mode & 0o700 == 0o700)

    def test_service_user_cannot_write_startup_authorization(self):
        code = (
            "from pathlib import Path; "
            "Path(startup.STARTUP_PATH).write_text('forged')"
        )
        result = self._as_service_user(code)
        self.assertNotEqual(result.returncode, 0)

    def test_config_change_invalidates_startup_authorization(self):
        self.config.write_text("changed configuration\n", encoding="utf-8")
        result = self._as_service_user(
            f"startup.require_startup(Path({str(self.config)!r}), 'run')"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)

    def test_root_preflight_rejects_incomplete_repair(self):
        journal = self.private / "journal.json"
        journal.write_text(json.dumps({"phase": "APPLYING"}), encoding="utf-8")
        transition = self.private / "package-transition"
        with (
            patch.object(startup, "STARTUP_PATH", self.startup_file),
            patch.object(repair, "REPAIR_ROOT", self.private),
            patch.object(repair, "PACKAGE_TRANSITION", transition),
            patch.object(startup.EdgeConfig, "load", side_effect=AssertionError("config must not be inspected")),
        ):
            with self.assertRaisesRegex(RuntimeError, "Repair recovery must finish"):
                startup.verify_startup(self.config, "run")


if __name__ == "__main__":
    unittest.main()
