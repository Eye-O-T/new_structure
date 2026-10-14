"""First-time AI CCTV server deployment creation.

The package initializer intentionally avoids importing the workflow because model
and configuration helpers are also used by lightweight deployment tools.
"""

from .configuration import InstallRequest, InstallResult, initialize

__all__ = ["InstallRequest", "InstallResult", "initialize"]
