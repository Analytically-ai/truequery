"""Offline DoD: the check path makes zero network calls.

Scans product source for network imports and runs the CLI end to end
with socket creation blocked. Also asserts byte-identical output.
"""

import os
import re
import socket

from truequery import cli as CLI

SRC = os.path.join(os.path.dirname(__file__), "..", "plugin", "engine", "src", "truequery")
NETWORK_IMPORTS = re.compile(
    r"^\s*(import|from)\s+(urllib|requests|http\.client|socket)\b",
    re.MULTILINE)


def _product_files():
    for name in sorted(os.listdir(SRC)):
        if name.endswith(".py"):
            yield os.path.join(SRC, name)
    for sub in ("analyzers", "checks"):
        d = os.path.join(SRC, sub)
        for name in sorted(os.listdir(d)):
            if name.endswith(".py"):
                yield os.path.join(d, name)


def _blocked(*args, **kwargs):
    raise AssertionError("network call attempted in check path")


def test_no_network_imports_in_product_source():
    offenders = [p for p in _product_files()
                 if NETWORK_IMPORTS.search(
                     open(p, encoding="utf-8").read())]
    assert offenders == []


def test_cli_identical_with_sockets_blocked(monkeypatch, tmp_path,
                                            capsys):
    sql = tmp_path / "q.sql"
    sql.write_text("DELETE FROM t;\nSELECT * FROM a, b;\n",
                   encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert CLI.main(["detect", "q.sql"]) == 1
    first = capsys.readouterr().out
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setattr(socket, "socket", _blocked)
    assert CLI.main(["detect", "q.sql"]) == 1
    second = capsys.readouterr().out
    assert second == first
    assert "unguarded-update-delete" in second
