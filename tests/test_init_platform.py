from __future__ import annotations

import platform
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dedrm.adobe_state import DeviceState, state_dir
from dedrm.cli import EXIT_IO, EXIT_OK, _cmd_init


def test_init_windows_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    with patch("dedrm.adobekey.adeptkeys", return_value=[b"fakekey"]):
        mock_state = MagicMock()
        mock_state.exists.return_value = False
        mock_state.root = MagicMock()
        with (
            patch("dedrm.adobe_state.DeviceState", return_value=mock_state),
            patch("dedrm.adobe_state.state_dir", return_value=Path("/fake/state")),
        ):
            args = MagicMock()
            args.force = False
            args.key_output = None
            result = _cmd_init(args)
            assert result == EXIT_OK
            mock_state.root.__truediv__.assert_called_with("adobekey.der")


def test_init_windows_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    with patch("dedrm.adobekey.adeptkeys", return_value=[]):
        mock_state = MagicMock()
        mock_state.exists.return_value = False
        mock_state.root = MagicMock()
        with (
            patch("dedrm.adobe_state.DeviceState", return_value=mock_state),
            patch("dedrm.adobe_state.state_dir", return_value=Path("/fake/state")),
        ):
            args = MagicMock()
            args.force = False
            args.key_output = None
            result = _cmd_init(args)
            assert result == EXIT_IO


def test_init_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    mock_state = MagicMock()
    mock_state.exists.return_value = False
    mock_state.root = MagicMock()
    with (
        patch("dedrm.adobe_state.DeviceState", return_value=mock_state),
        patch("dedrm.adobe_state.state_dir", return_value=Path("/fake/state")),
    ):
        args = MagicMock()
        args.force = False
        args.key_output = None
        result = _cmd_init(args)
        assert result == EXIT_IO