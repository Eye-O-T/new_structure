import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


import numpy as np
import torch

from ai_cctv_core.processing.plugins import load_factory
from server.services.analysis.processors.attributes import (
    BUNDLE_FORMAT,
    _MultiHeadNetwork,
    AnalysisModelStartupError,
    PersonAttributeAnalyzer,
    _load_bundle,
    _select_multi_label,
)


def valid_bundle():
    return {
        "format": BUNDLE_FORMAT,
        "format_version": 1,
        "backbone": "resnet34",
        "input_size": [256, 128],
        "normalize": {
            "mean": [0.485, 0.456, 0.406],
            "std": [0.229, 0.224, 0.225],
        },
        "attributes": {"hat": ["No Hat", "Hat"]},
        "multi_label": [],
        "display_rules": {},
        "model_version": "test",
        "state_dict": {"weight": torch.ones(1)},
    }


class AttributeBundleTests(unittest.TestCase):
    def test_cuda_request_without_gpu_mode_fails_with_safe_device_code(self):
        with patch.dict("os.environ", {"ANALYSIS_DEVICE": "cuda:0", "PREPROCESSING_GPU": "false"}, clear=True):
            with self.assertRaises(AnalysisModelStartupError) as caught:
                PersonAttributeAnalyzer()
        self.assertEqual(caught.exception.startup_error_code, "ANALYSIS_DEVICE_UNAVAILABLE")

    def test_explicit_cpu_runs_par_and_reports_the_execution_device(self):
        bundle = valid_bundle()
        bundle["state_dict"] = _MultiHeadNetwork(bundle["attributes"]).state_dict()
        with tempfile.TemporaryDirectory() as directory:
            path = self.save(bundle, directory)
            with patch.dict("os.environ", {
                "PAR_MODEL_PATH": str(path),
                "ANALYSIS_DEVICE": "cpu",
                "PREPROCESSING_GPU": "false",
            }, clear=True):
                analyzer = PersonAttributeAnalyzer()
                with patch(
                    "server.services.analysis.processors.attributes.load_observation_crop",
                    return_value=np.zeros((32, 32, 3), dtype=np.uint8),
                ):
                    result = analyzer.process({}, path)
        self.assertEqual(analyzer.runtime_metadata()["execution_device"], "cpu")
        self.assertEqual(result["outcome"], "complete")
        self.assertIn("hat", result["metadata"]["attributes"])

    def save(self, bundle, directory):
        path = Path(directory) / "model.pt"
        torch.save(bundle, path)
        return path

    def test_restricted_loader_accepts_supported_tensor_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = _load_bundle(self.save(valid_bundle(), directory))

        self.assertEqual(loaded["backbone"], "resnet34")
        self.assertEqual(loaded["attributes"], {"hat": ["No Hat", "Hat"]})

    def test_loader_rejects_unknown_backbone_and_invalid_threshold(self):
        cases = []
        unknown_backbone = valid_bundle()
        unknown_backbone["backbone"] = "resnet50"
        cases.append((unknown_backbone, "backbone"))
        invalid_threshold = valid_bundle()
        invalid_threshold["multi_label"] = ["hat"]
        invalid_threshold["display_rules"] = {
            "hat": {"always_top": False, "threshold": 1.1}
        }
        cases.append((invalid_threshold, "display rule"))

        with tempfile.TemporaryDirectory() as directory:
            for index, (bundle, message) in enumerate(cases):
                with self.subTest(message=message):
                    path = Path(directory) / f"model-{index}.pt"
                    torch.save(bundle, path)
                    with self.assertRaisesRegex(ValueError, message):
                        _load_bundle(path)

    def test_multi_label_rule_applies_threshold_and_always_top(self):
        scores = np.array([0.2, 0.6, 0.4])

        selected = _select_multi_label(
            scores, {"always_top": True, "threshold": 0.7}
        )

        self.assertEqual(selected.tolist(), [False, True, False])

    def test_processors_package_aliases_local_appearance_to_par_analyzer(self):
        from server.services.analysis.processors import (
            LocalAppearanceAnalyzer,
            PersonAttributeAnalyzer,
        )
        from server.services.analysis.processors.appearance import (
            LocalAppearanceAnalyzer as LegacyAppearanceAlias,
        )

        self.assertIs(LocalAppearanceAnalyzer, PersonAttributeAnalyzer)
        self.assertIs(LegacyAppearanceAlias, PersonAttributeAnalyzer)

    def test_all_configured_plugin_references_resolve_to_par_analyzer(self):
        references = (
            "server.services.analysis.processors.attributes:PersonAttributeAnalyzer",
            "server.services.analysis.processors:PersonAttributeAnalyzer",
            "server.services.analysis.processors:LocalAppearanceAnalyzer",
            "server.services.analysis.processors.appearance:LocalAppearanceAnalyzer",
        )

        for reference in references:
            with self.subTest(reference=reference):
                self.assertIs(load_factory(reference), PersonAttributeAnalyzer)

    def test_missing_model_file_raises_file_not_found_error(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing_par.pt"
            with self.assertRaises(FileNotFoundError):
                _load_bundle(missing)


if __name__ == "__main__":
    unittest.main()
