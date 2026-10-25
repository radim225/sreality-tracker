"""Pytest spouští každý ``test_*.py`` jako vlastní proces.

Sada je sada skriptů: část běží při importu, část jen pod ``__main__`` a při
selhání volá ``sys.exit``. Import všech do jednoho procesu sdílí stav
``scrape`` (konstanty se čtou při importu) a sběr testů spadne. Každý soubor
proto běží stejným příkazem jako krok Run tests ve ``scrape.yml``:
``python test_….py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Soubor sám sahá na živé Sreality a jeho docstring říká, že v CI před scrapem
# není. Ve scrape.yml v seznamu testů chybí. Tady je viditelně přeskočený,
# ať tichá mezera v seznamu nevypadá jako zapomenutý test.
_NOT_IN_CI = frozenset({"test_garage_links.py"})


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makemodule(module_path: Path, parent):
    if module_path.name.startswith("test_") and module_path.suffix == ".py":
        return ScriptFile.from_parent(parent, path=module_path)
    return None


class ScriptFile(pytest.File):
    def collect(self):
        yield ScriptItem.from_parent(self, name="script")


class ScriptItem(pytest.Item):
    def runtest(self) -> None:
        if self.path.name in _NOT_IN_CI:
            pytest.skip(
                "živé HTTP na Sreality; v CI před scrapem se nespouští"
            )
        env = os.environ.copy()
        env["GEOCODE_DISABLED"] = "1"
        try:
            completed = subprocess.run(
                [sys.executable, self.path.name],
                cwd=self.path.parent,
                env=env,
                check=False,
                timeout=240,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(
                f"{self.path.name} překročil 240 s"
            ) from exc
        if completed.returncode != 0:
            raise AssertionError(
                f"{self.path.name} skončil s kódem {completed.returncode}"
            )
