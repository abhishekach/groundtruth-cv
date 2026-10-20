"""The workspace: one folder holding your fact sheet, settings and every application. Nothing leaves it except the
model calls you configure."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from groundtruth import settings as S


@dataclass
class Workspace:
    root: Path

    @classmethod
    def find(cls, path: str | None = None) -> "Workspace":
        root = Path(path or os.environ.get("GROUNDTRUTH_HOME") or Path.cwd()).expanduser().resolve()
        try:
            from dotenv import load_dotenv
            load_dotenv(root / ".env")
        except ImportError:
            pass
        return cls(root)

    @property
    def fact_sheet(self) -> Path:
        return self.root / "fact_sheet.md"

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.yaml"

    @property
    def applications(self) -> Path:
        return self.root / "applications"

    def settings(self) -> S.Settings:
        return S.load(self.settings_file)

    def facts_text(self) -> str:
        if not self.fact_sheet.exists():
            raise FileNotFoundError(f"No fact sheet at {self.fact_sheet}. Run `groundtruth init` first, then fill it in.")
        return self.fact_sheet.read_text(encoding="utf-8")

    def photo(self) -> Path | None:
        p = self.settings().photo
        if not p:
            return None
        f = Path(p).expanduser()
        f = f if f.is_absolute() else self.root / f
        return f if f.exists() else None
