import os
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ai_cctv_edge.config import EdgeConfig, render_toml
from ai_cctv_edge import repair


class RepairSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "etc" / "ai-cctv-edge" / "config.toml"
        source = EdgeConfig.load(Path(__file__).parents[1] / "config" / "config.example.toml")
        self.config = replace(
            source,
            rtsp=replace(source.rtsp, password_file=self.config_path.parent / "publish.password"),
            recovery=replace(source.recovery, token_file=self.config_path.parent / "recovery.token"),
            control=replace(source.control, token_file=self.config_path.parent / "control.token"),
        )
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(render_toml(self.config), encoding="utf-8")
        self.config.rtsp.password_file.write_text("publish-secret\n", encoding="utf-8")
        self.config.recovery.token_file.write_text("recovery-secret\n", encoding="utf-8")
        (self.config_path.parent / ".configured").write_text("configured\n", encoding="utf-8")
        self.state_root = self.root / "state"
        self.state_root.mkdir()
        (self.state_root / "video-profile.json").write_text('{"profile":"hd","generation":3}\n', encoding="utf-8")
        self.old_state = os.environ.get("AI_CCTV_EDGE_STATE_ROOT")
        os.environ["AI_CCTV_EDGE_STATE_ROOT"] = str(self.state_root)

    def tearDown(self):
        if self.old_state is None:
            os.environ.pop("AI_CCTV_EDGE_STATE_ROOT", None)
        else:
            os.environ["AI_CCTV_EDGE_STATE_ROOT"] = self.old_state
        self.temp.cleanup()

    def test_snapshot_records_existing_and_absent_targets(self):
        backup = self.root / "backup"
        backup.mkdir()
        missing = self.root / "missing.token"
        entries = repair._snapshot([self.config.rtsp.password_file, missing], backup)
        self.assertTrue(entries[0]["existed"])
        self.assertEqual((backup / entries[0]["backup"]).read_text(encoding="utf-8"), "publish-secret\n")
        self.assertFalse(entries[1]["existed"])

    def test_restore_reinstates_contents_marker_and_absent_files(self):
        backup = self.root / "backup"
        backup.mkdir()
        missing_control = self.config_path.parent / "control.token"
        targets = repair._targets(self.config, self.config_path)
        entries = repair._snapshot(targets, backup)
        missing_control.write_text("new-token\n", encoding="utf-8")
        journal = {"backup_dir": str(backup), "files": entries, "active_units": []}
        self.config_path.write_text("partial config", encoding="utf-8")
        self.config.rtsp.password_file.write_text("new-password", encoding="utf-8")
        self.config.recovery.token_file.write_text("new-token", encoding="utf-8")
        (self.config_path.parent / ".configured").unlink()
        (self.state_root / "video-profile.json").write_text("partial", encoding="utf-8")
        with patch.object(repair, "apply_persistent"), patch.object(repair.os, "chown", create=True):
            repair._restore(journal, self.config_path, start_services=False)
        self.assertEqual(EdgeConfig.load(self.config_path).device_id, self.config.device_id)
        self.assertEqual(self.config.rtsp.password_file.read_text(encoding="utf-8"), "publish-secret\n")
        self.assertEqual(self.config.recovery.token_file.read_text(encoding="utf-8"), "recovery-secret\n")
        self.assertFalse(missing_control.exists())
        self.assertTrue((self.config_path.parent / ".configured").exists())
        self.assertEqual((self.state_root / "video-profile.json").read_text(encoding="utf-8"), '{"profile":"hd","generation":3}\n')

    def test_boot_recovery_skips_only_during_verified_service_restart(self):
        repair_root = self.root / "repair"
        repair_root.mkdir()
        journal_path = repair_root / "journal.json"
        for phase, expected in (("LOCAL_CONFIGURED", False), ("COMMITTED", False)):
            journal_path.write_text(json.dumps({"phase": phase}), encoding="utf-8")
            with patch.object(repair, "repair_lock", side_effect=RuntimeError("busy")):
                self.assertIs(repair.recover_pending(self.config_path, repair_root, start_services=False), expected)
        journal_path.write_text(json.dumps({"phase": "PAIRING"}), encoding="utf-8")
        with patch.object(repair, "repair_lock", side_effect=RuntimeError("busy")):
            with self.assertRaisesRegex(RuntimeError, "busy"):
                repair.recover_pending(self.config_path, repair_root, start_services=False)

    def test_restore_preserves_an_absent_configured_marker(self):
        marker = self.config_path.parent / ".configured"
        marker.unlink()
        backup = self.root / "backup-absent-marker"
        backup.mkdir()
        journal = {"backup_dir": str(backup), "files": repair._snapshot(repair._targets(self.config, self.config_path), backup), "active_units": []}
        marker.write_text("new marker\n", encoding="utf-8")
        with patch.object(repair, "apply_persistent"), patch.object(repair.os, "chown", create=True):
            repair._restore(journal, self.config_path, start_services=False)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
