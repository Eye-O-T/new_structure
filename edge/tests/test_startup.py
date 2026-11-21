import hashlib
import json
import os
import shutil
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
        # Hosted runner checkout parents may be inaccessible after dropping UID.
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o755)
        package = self.root / "ai_cctv_edge"
        shutil.copytree(
            Path(startup.__file__).parent,
            package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        for path in (package, *package.rglob("*")):
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
        self.config = self.root / "config.toml"
        shutil.copyfile(Path(__file__).parents[1] / "config" / "config.example.toml", self.config)
        os.chmod(self.config, 0o644)
        self.private = self.root / "repair"
        self.private.mkdir(mode=0o700)
        os.chmod(self.private, 0o700)
        self.startup_dir = self.root / "runtime"
        account = pwd.getpwnam("nobody")
        self.startup_dir.mkdir(mode=0o750)
        os.chown(self.startup_dir, 0, account.pw_gid)
        os.chmod(self.startup_dir, 0o750)
        self.startup_file = self.startup_dir / "startup.json"
        self.startup_file.write_text(json.dumps({
            "version": 1,
            "allowed": True,
            "entrypoints": ["run"],
            "config_sha256": hashlib.sha256(self.config.read_bytes()).hexdigest(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }), encoding="utf-8")
        os.chown(self.startup_file, 0, account.pw_gid)
        os.chmod(self.startup_file, 0o440)

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
            cwd=self.root,
            env={**os.environ, "PYTHONPATH": str(self.root)},
            capture_output=True,
            text=True,
            user=account.pw_uid,
            group=account.pw_gid,
            extra_groups=(),
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
        self.assertIn("PermissionError", result.stderr)
        self.assertTrue(json.loads(self.startup_file.read_text(encoding="utf-8"))["allowed"])

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
