"""
Unit tests for system configuration utilities.
"""

import sys
from unittest.mock import MagicMock, patch

from src.utils.system import configure_utf8_stdout


def test_configure_utf8_stdout_calls_reconfigure():
    """configure_utf8_stdout should call reconfigure on stdout when encoding is not utf-8."""
    mock_stdout = MagicMock()
    mock_stdout.encoding = "cp1252"
    with patch.object(sys, "stdout", mock_stdout):
        configure_utf8_stdout()
        mock_stdout.reconfigure.assert_called_with(encoding="utf-8")
