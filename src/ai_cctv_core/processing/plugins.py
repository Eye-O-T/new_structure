"""Load explicitly configured Python factories without importing model SDKs eagerly."""

from importlib import import_module
from pathlib import Path
from typing import Protocol


class ObjectProcessor(Protocol):
    def process(self, job: dict, crop_path: Path) -> dict:
        """Return an ObjectJobCompletion mapping without its transport lease_id."""
        ...


def load_factory(reference: str):
    module_name, separator, factory_name = reference.partition(":")
    if not separator or not module_name or not factory_name:
        raise ValueError("Plugin must be an importable module:factory reference")
    factory = getattr(import_module(module_name), factory_name)
    if not callable(factory):
        raise ValueError("Plugin factory must be callable")
    return factory
