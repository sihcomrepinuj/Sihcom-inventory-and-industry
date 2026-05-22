"""Tests for setup_sde.py build-tracking and auto-refresh logic.

The fileystem-touching tests redirect BUILD_FILE to a tmp_path so they
don't clobber the real data/sde-build.txt. The network-touching test
for is_sde_current monkeypatches get_latest_build so no real CCP call
is made.
"""

import pytest

import setup_sde


@pytest.fixture
def isolated_build_file(tmp_path, monkeypatch):
    """Redirect BUILD_FILE to a temp location for the duration of the test."""
    fake = tmp_path / "sde-build.txt"
    monkeypatch.setattr(setup_sde, "BUILD_FILE", str(fake))
    monkeypatch.setattr(setup_sde, "DATA_DIR", str(tmp_path))
    return fake


def test_read_local_build_missing(isolated_build_file):
    assert setup_sde.read_local_build() is None


def test_read_local_build_returns_value(isolated_build_file):
    isolated_build_file.write_text("3351823")
    assert setup_sde.read_local_build() == "3351823"


def test_read_local_build_strips_whitespace(isolated_build_file):
    isolated_build_file.write_text("  3351823  \n")
    assert setup_sde.read_local_build() == "3351823"


def test_read_local_build_empty_file_returns_none(isolated_build_file):
    isolated_build_file.write_text("")
    assert setup_sde.read_local_build() is None


def test_write_then_read_round_trip(isolated_build_file):
    setup_sde.write_local_build("9999999")
    assert setup_sde.read_local_build() == "9999999"


def test_is_sde_current_matches(isolated_build_file, monkeypatch):
    isolated_build_file.write_text("3351823")
    monkeypatch.setattr(setup_sde, "get_latest_build", lambda: "3351823")
    assert setup_sde.is_sde_current() is True


def test_is_sde_current_mismatch(isolated_build_file, monkeypatch):
    isolated_build_file.write_text("3000000")
    monkeypatch.setattr(setup_sde, "get_latest_build", lambda: "3351823")
    assert setup_sde.is_sde_current() is False


def test_is_sde_current_no_local_build(isolated_build_file, monkeypatch):
    monkeypatch.setattr(setup_sde, "get_latest_build", lambda: "3351823")
    assert setup_sde.is_sde_current() is False


def test_is_sde_current_network_error_fails_open(isolated_build_file, monkeypatch):
    """Don't block startup on transient CCP outages — use what's on disk."""
    isolated_build_file.write_text("3000000")

    def fail(*args, **kwargs):
        raise ConnectionError("CCP unreachable")

    monkeypatch.setattr(setup_sde, "get_latest_build", fail)
    assert setup_sde.is_sde_current() is True
