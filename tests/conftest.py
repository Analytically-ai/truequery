import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin", "engine", "src"))


import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Never read or write the developer's real ~/.claude or user config."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home
