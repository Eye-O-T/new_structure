import pickle
import tempfile
import unittest
import zipfile
from pathlib import Path

from server.installer.models import _validated_local_model, is_par_bundle
from server.operations.compose import migrate_detection_model


class DetectionModelValidationTests(unittest.TestCase):
    def _checkpoint(self, path: Path, marker: str) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("checkpoint/data.pkl", pickle.dumps({"format": marker}))

    def test_rejects_par_bundle_even_when_renamed(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "custom-name.pt"
            self._checkpoint(model, "par-serving-bundle")
            self.assertTrue(is_par_bundle(model))
            with self.assertRaisesRegex(ValueError, "PAR attribute bundles"):
                _validated_local_model(model)

    def test_accepts_other_pt_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "custom-name.pt"
            self._checkpoint(model, "ultralytics")
            self.assertFalse(is_par_bundle(model))
            self.assertEqual(_validated_local_model(model), model.resolve())

    def test_migrates_only_known_par_bundle_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "attributes.pt"
            self._checkpoint(model, "par-serving-bundle")
            (root / "yolo11n.pt").write_bytes(b"known-good-model")
            env = root / "compose.env"
            env.write_text(f"MODELS_DIR={root}\nMODEL_FILE=attributes.pt\n", encoding="utf-8")

            self.assertTrue(migrate_detection_model(env))
            self.assertIn("MODEL_FILE=yolo11n.pt", env.read_text(encoding="utf-8"))
            self.assertTrue(env.with_name("compose.env.bak").is_file())


if __name__ == "__main__":
    unittest.main()
