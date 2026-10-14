"""Host-side lifecycle, network configuration, and diagnostics."""

from .compose import ComposeAdapter
from .services import run_service_action

__all__ = ["ComposeAdapter", "run_service_action"]
