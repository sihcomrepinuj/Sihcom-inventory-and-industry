"""Smoke test for the CLI `plan` command (Task 7).

Exercises the command wiring without touching SDE/ESI/network. The empty
build-list path returns before opening the SDE or doing any auth, so it runs
anywhere and asserts the friendly empty message prints without crashing.
"""
import build_list
import eve_inventory


def test_cmd_plan_empty_build_list(capsys, monkeypatch):
    """`plan` with an empty build list prints the friendly message, no crash."""
    monkeypatch.setattr(build_list, "load", lambda *a, **k: {"targets": [], "buy_set": []})
    eve_inventory.cmd_plan()
    out = capsys.readouterr().out
    assert "Build list is empty" in out
