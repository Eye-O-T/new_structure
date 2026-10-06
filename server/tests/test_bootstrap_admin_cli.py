import subprocess
import sys
import unittest
from pathlib import Path


class BootstrapAdminCliTests(unittest.TestCase):
    def test_direct_script_help_does_not_require_server_package_import(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "services" / "external" / "tools" / "bootstrap_admin.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=root.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()
