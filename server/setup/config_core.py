"""Compatibility import for the relocated installer configuration module."""

from server.installer.configuration import *  # noqa: F401,F403
from server.installer.configuration import (  # noqa: F401
    _dotenv,
    _validate_public_base_url,
    _validate_request,
    _validate_tls_files,
)
