import pickle
import tempfile
import unittest
import zipfile
from pathlib import Path

from server.installer.models import (
    ANALYSIS_PLUGIN,
    _validated_local_model,
    deployed_analysis_model,
    is_par_bundle,
    validate_analysis_model,
)
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

    def test_validate_and_deploy_analysis_model_requires_par_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            par_model = root / "par_serving_exp3.pt"
            self._checkpoint(par_model, "par-serving-bundle")
            self.assertEqual(validate_analysis_model(par_model), par_model.resolve())
            self.assertEqual(
                deployed_analysis_model({}, root),
                par_model.resolve(),
            )
            self.assertEqual(
                ANALYSIS_PLUGIN,
                "server.services.analysis.processors.attributes:PersonAttributeAnalyzer",
            )

            non_par = root / "yolo11n.pt"
            self._checkpoint(non_par, "ultralytics")
            with self.assertRaisesRegex(ValueError, "PAR attribute bundle"):
                validate_analysis_model(non_par)
            with self.assertRaisesRegex(ValueError, "does not exist"):
                validate_analysis_model(root / "missing.pt")

    def test_deployment_prerequisites_check_par_model_when_analysis_enabled(self):
        from unittest.mock import patch
        from server.operations.compose import ComposeAdapter, Prerequisite

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "server"
            server.mkdir()
            (server / "compose.yml").write_text("services: {}\n", encoding="utf-8")
            models = root / "models"
            models.mkdir()
            env = root / "compose.env"
            env.write_text(
                f"MODELS_DIR={models}\n"
                "MODEL_FILE=yolo11n.pt\n"
                "PUBLIC_SCHEME=http\n",
                encoding="utf-8",
            )
            self._checkpoint(models / "yolo11n.pt", "ultralytics")
            adapter = ComposeAdapter(server, env)

            with patch(
                "server.operations.compose.installation_prerequisites",
                return_value=[Prerequisite(True, "Docker Engine", "running")],
            ):
                missing_checks = {
                    item.name: item
                    for item in adapter.deployment_prerequisites(
                        require_ai_models=True, require_analysis_model=True
                    )
                }
                self.assertIn("PAR attribute model", missing_checks)
                self.assertFalse(missing_checks["PAR attribute model"].ok)

                self._checkpoint(models / "par_serving_exp3.pt", "par-serving-bundle")
                ready_checks = {
                    item.name: item
                    for item in adapter.deployment_prerequisites(
                        require_ai_models=True, require_analysis_model=True
                    )
                }
                self.assertTrue(ready_checks["PAR attribute model"].ok)


if __name__ == "__main__":
    unittest.main()
