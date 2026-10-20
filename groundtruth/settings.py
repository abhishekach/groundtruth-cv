"""Your rules, read from settings.yaml in the workspace. Everything personal lives here or in the fact sheet, never in code."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class Playbook(BaseModel):
    when: str = Field(description="the kind of role, e.g. 'data engineering or analytics engineering roles'")
    lead_with: str = Field(description="what the CV leads with for that kind of role, citing fact IDs")


class Item(BaseModel):
    fact: str = Field(description="a fact ID from the fact sheet, e.g. 'C2'")
    describe: str = Field(default="", description="how the writer should use it, or why it is never used")


class PermitLine(BaseModel):
    fact: str = Field(description="fact ID whose text holds the permit line, e.g. 'A5'")
    countries: list[str] = Field(description="ISO country codes of jobs where the line is shown under your contact details")


class Settings(BaseModel):
    target: str = Field(default="", description="one paragraph: who you are and which roles you are going for")
    playbooks: list[Playbook] = Field(default_factory=list)
    must_include: list[Item] = Field(default_factory=list, description="facts every job CV must show")
    never_include: list[Item] = Field(default_factory=list, description="facts never shown on a job CV")
    avoid_wording: list[str] = Field(default_factory=list, description="phrases (regular expressions) never used on the CV")
    languages: dict[str, str] = Field(default_factory=lambda: {"English": "C1"},
                                      description="your levels: CEFR A1-C2 or 'native'")
    work_rights: list[str] = Field(default_factory=list, description="ISO codes of countries where you can already work")
    needs_sponsorship_elsewhere: bool = True
    hard_rules: list[str] = Field(default_factory=list, description="extra rules for the fit judge")
    start_after_months: int = Field(default=1, ge=0, le=12, description="fills {EARLIEST_START} in the fact sheet")
    permit_line: PermitLine | None = None
    photo: str | None = Field(default=None, description="path to a portrait photo, used only where a photo is expected")
    english: Literal["international", "british", "american"] = "international"
    cv_max_pages: int = 2
    letter_words: tuple[int, int] = (250, 400)


def load(path: Path) -> Settings:
    if not path.exists():
        return Settings()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(data)
