"""Lightweight Skill model.

A skill is a local directory with a required SKILL.md file. The frontmatter is
only an index; the markdown body is an instruction manual loaded on demand.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Skill:
    """Parsed SKILL.md content with only the metadata needed for discovery."""

    name: str
    description: str
    body: str
    skill_dir: Path
    short_description: Optional[str] = None

    @property
    def skill_file(self) -> Path:
        return self.skill_dir / "SKILL.md"

    @property
    def source_locator(self) -> str:
        return f"file:{self.skill_file}"

    @property
    def scripts_dir(self) -> Path:
        return self.skill_dir / "scripts"

    def render(self, args: str = "") -> str:
        """Render the body with the two compatibility substitutions we keep."""

        return (
            self.body
            .replace("${SKILL_DIR}", str(self.skill_dir))
            .replace("$ARGUMENTS", args or "")
        )

    def get_reference_paths(self) -> dict[str, Path]:
        """Return reference markdown files.

        New skills should use references/*.md. The root-level *.md lookup is
        kept so bundled legacy skills such as pdf/forms.md keep working.
        """

        refs: dict[str, Path] = {}
        if not self.skill_dir.is_dir():
            return refs

        for base_dir in (self.skill_dir, self.skill_dir / "references"):
            if not base_dir.is_dir():
                continue
            for md_file in sorted(base_dir.glob("*.md")):
                if md_file.name.lower() == "skill.md":
                    continue
                refs[md_file.stem] = md_file
        return refs

    def get_references(self) -> dict[str, str]:
        refs: dict[str, str] = {}
        for name, path in self.get_reference_paths().items():
            try:
                refs[name] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        return refs

    def get_script_paths(self) -> dict[str, Path]:
        """Return files below scripts/ keyed by relative path without suffix."""

        scripts: dict[str, Path] = {}
        if not self.scripts_dir.is_dir():
            return scripts
        for path in sorted(self.scripts_dir.rglob("*")):
            if not path.is_file() or path.name == "__init__.py":
                continue
            rel = path.relative_to(self.scripts_dir)
            key = str(rel.with_suffix(""))
            scripts[key] = path
        return scripts

    def get_scripts(self) -> dict[str, Path]:
        """Compatibility alias for older callers/tests."""

        return self.get_script_paths()
