"""Finding and diagnostic shapes. One shape for the CLI, the hook, and tests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Finding:
    rule: str
    severity: str
    mode: str
    evidence: str
    file: str
    line: int
    col: int
    snippet: str
    why: str
    fix: str

    def sort_key(self):
        return (self.file, self.line, self.col, self.rule)


@dataclass
class Diagnostic:
    file: str
    message: str
    detail: str = ""

    def sort_key(self):
        return (self.file, self.message)
