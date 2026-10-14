import tempfile
import unittest
from pathlib import Path

from server.operations.compose import ComposeAdapter, START_ARGUMENTS


class ComposeAdapterCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.server = self.root / "server"
        self.server.mkdir()
        (self.server / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        self.env = self.root / "config" / "compose.env"
        self.env.parent.mkdir()
        self.env.write_text("PREPROCESSING_GPU=false\n", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_base_command_uses_selected_deployment(self):
        adapter = ComposeAdapter(self.server, self.env)

        command = adapter.command(*START_ARGUMENTS)

        self.assertEqual(
            command,
            [
                "docker",
                "compose",
                "--env-file",
                str(self.env.resolve()),
                "-f",
                str((self.server / "compose.yml").resolve()),
                "up",
                "-d",
                "--build",
                "--wait",
            ],
        )

    def test_push_and_gpu_overrides_are_selected_from_env(self):
        (self.server / "compose.push.yml").write_text("services: {}\n", encoding="utf-8")
        (self.server / "compose.gpu.yml").write_text("services: {}\n", encoding="utf-8")
        self.env.write_text("PREPROCESSING_GPU=true\n", encoding="utf-8")
        push = self.env.with_name("push.env")
        push.write_text("PUSH_ENABLED=true\n", encoding="utf-8")

        command = ComposeAdapter(self.server, self.env).command("config", "--quiet")

        self.assertIn(str(push.resolve()), command)
        self.assertIn(str((self.server / "compose.push.yml").resolve()), command)
        self.assertIn(str((self.server / "compose.gpu.yml").resolve()), command)
        self.assertEqual(command[-2:], ["config", "--quiet"])


if __name__ == "__main__":
    unittest.main()
