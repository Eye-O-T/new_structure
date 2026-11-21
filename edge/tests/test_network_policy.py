import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ai_cctv_edge import network_policy as policy
from ai_cctv_edge.config import EdgeConfig
from ai_cctv_edge.network import network_details


class PolicyCompatibilityTests(unittest.TestCase):
    def test_replacement_checks_and_applies_one_bookworm_compatible_batch(self):
        rules = policy.render_ruleset(interface="eth0", ports=(8003, 8002), service_uid=1001)
        with patch.object(policy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            policy._replace_table(rules)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], ["nft", "--check", "-f", "-"])
        self.assertEqual(run.call_args_list[1].args[0], ["nft", "-f", "-"])
        batch = run.call_args_list[1].kwargs["input"]
        self.assertEqual(batch, f"add table inet {policy.TABLE}\ndelete table inet {policy.TABLE}\n" + rules)
        self.assertEqual(run.call_args_list[0].kwargs["input"], batch)

    def test_invalid_batch_is_never_applied(self):
        result = subprocess.CompletedProcess([], 1, stderr="invalid policy")
        with patch.object(policy.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, "validation failed"):
                policy._replace_table("invalid policy")
        run.assert_called_once()

    def test_all_policy_modes_use_compatible_replacement(self):
        original = EdgeConfig.load(Path(__file__).parents[1] / "config" / "config.example.toml")
        for interface in ("eth0", None):
            with self.subTest(interface=interface), tempfile.TemporaryDirectory() as directory:
                config = replace(original, network=replace(original.network, interface=interface))
                shown = subprocess.CompletedProcess([], 0, stdout="meta skuid 1001 drop\nct mark set ct mark | 0x40000000")
                with (
                    patch.object(policy, "policy_lock", return_value=nullcontext()),
                    patch.object(policy, "STATE_PATH", Path(directory) / "applied.json"),
                    patch.object(policy, "_service_uid", return_value=1001),
                    patch.object(policy, "_service_gid", return_value=1001),
                    patch.object(policy.os, "chown", create=True),
                    patch.object(policy.subprocess, "run", return_value=shown),
                    patch.object(policy, "_replace_table") as replace_table,
                ):
                    policy.apply_persistent(config)
                replace_table.assert_called_once()
                self.assertNotIn("destroy table", replace_table.call_args.args[0])
        with (
            patch.object(policy, "policy_lock", return_value=nullcontext()),
            patch.object(policy, "selected_interface_ipv4", return_value=network_details("eth0", "192.0.2.2", "255.255.255.0")),
            patch.object(policy, "_replace_table") as replace_table,
        ):
            policy.apply_pairing("eth0", "192.0.2.2", 8003)
        replace_table.assert_called_once()

    def test_removal_failure_preserves_policy_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "applied.json"
            state.write_text("{}", encoding="utf-8")
            with (
                patch.object(policy, "policy_lock", return_value=nullcontext()),
                patch.object(policy, "STATE_PATH", state),
                patch.object(policy, "_replace_table", side_effect=RuntimeError("nft failed")),
            ):
                with self.assertRaisesRegex(RuntimeError, "nft failed"):
                    policy.remove_policy()
            self.assertTrue(state.exists())


@unittest.skipUnless(
    os.name == "posix" and os.geteuid() == 0 and shutil.which("nft") and shutil.which("unshare"),
    "requires Linux root, nftables and unshare; runs in an isolated network namespace",
)
class KernelPolicyCompatibilityTests(unittest.TestCase):
    def test_first_install_replacement_failed_batch_and_repeated_removal(self):
        # Run only in a new network namespace; leave the host firewall untouched.
        first = policy.render_ruleset(interface="eth0", ports=(8002, 8003), service_uid=1001)
        second = policy.render_ruleset(interface="wlan0", ports=(8002, 8003), service_uid=1001)
        prefix = f"add table inet {policy.TABLE}\ndelete table inet {policy.TABLE}\n"
        program = """
import subprocess
def apply(text, check=False):
    return subprocess.run(['nft', *(['--check'] if check else []), '-f', '-'], input=text, text=True, capture_output=True)
for batch in BATCHES:
    result = apply(batch, check=True)
    assert result.returncode == 0, result.stderr
    result = apply(batch)
    assert result.returncode == 0, result.stderr
assert apply(PREFIX + 'invalid policy').returncode != 0
shown = subprocess.run(['nft', 'list', 'table', 'inet', TABLE], capture_output=True, text=True, check=True).stdout
assert 'wlan0' in shown and 'eth0' not in shown, shown
for _ in range(2):
    result = apply(PREFIX)
    assert result.returncode == 0, result.stderr
assert subprocess.run(['nft', 'list', 'table', 'inet', TABLE], capture_output=True).returncode != 0
"""
        program = f"BATCHES = {[prefix + first, prefix + second]!r}\nPREFIX = {prefix!r}\nTABLE = {policy.TABLE!r}\n" + program
        result = subprocess.run(["unshare", "--net", sys.executable, "-c", program], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
