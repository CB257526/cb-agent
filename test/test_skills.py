"""Tests for the lightweight Skill system."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills import SkillManager
from tools.tools.bash_tool import BashTool


def _write_skill(
    root: Path,
    dirname: str,
    *,
    name: str | None = None,
    description: str = "demo skill",
    body: str = "body",
    extra_frontmatter: str = "",
) -> Path:
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True)
    skill_name = name or dirname
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        f"name: {skill_name}\n"
        f"description: {description}\n"
        f"{extra_frontmatter}"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return skill_dir.resolve()


def test_frontmatter_colon_repair_and_old_fields_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skill_dir = _write_skill(
            root,
            "aws-skill",
            description="Build for AWS: ECS",
            body="Use ${SKILL_DIR}; args=$ARGUMENTS",
            extra_frontmatter=(
                "allowed-tools:\n"
                "  - bash\n"
                "user-invocable: false\n"
                "metadata:\n"
                "  short-description: AWS: ECS\n"
            ),
        )

        manager = SkillManager(skills_dir=root)
        skill = manager.get_skill("aws-skill")

        assert skill is not None
        assert skill.description == "Build for AWS: ECS"
        assert skill.short_description == "AWS: ECS"
        assert not hasattr(skill, "allowed_tools")
        assert not hasattr(skill, "user_invocable")
        assert str(skill_dir) in skill.render("hello")
        assert "args=hello" in skill.render("hello")


def test_recursive_discovery_and_later_root_overrides_earlier_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        user_root = root / "user"
        repo_root = root / "repo"
        _write_skill(user_root / "nested", "shared", description="user version")
        _write_skill(repo_root, "shared", description="repo version")
        _write_skill(user_root, "user-only", description="user only")

        manager = SkillManager(skills_dir=[user_root, repo_root])

        assert {skill.name for skill in manager.list_skills()} == {"shared", "user-only"}
        assert manager.get_skill("shared").description == "repo version"


def test_later_root_override_removes_stale_script_index():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        low_root = root / "low"
        high_root = root / "high"
        low_skill = _write_skill(low_root, "shared", name="shared", description="low version")
        high_skill = _write_skill(high_root, "shared", name="shared", description="high version")
        low_scripts = low_skill / "scripts"
        high_scripts = high_skill / "scripts"
        low_scripts.mkdir()
        high_scripts.mkdir()
        old_script = low_scripts / "old.py"
        new_script = high_scripts / "new.py"
        old_script.write_text("print('old')\n", encoding="utf-8")
        new_script.write_text("print('new')\n", encoding="utf-8")

        manager = SkillManager(skills_dir=[low_root, high_root])

        assert manager.get_skill("shared").skill_dir == high_skill
        assert manager.record_script_hits(f"{sys.executable} {old_script}", cwd=root) == []
        new_hits = manager.record_script_hits(f"{sys.executable} {new_script}", cwd=root)
        assert [hit["skill_file"] for hit in new_hits] == [str(high_skill / "SKILL.md")]


def test_budgeted_overview_degrades_without_body():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        long_desc = "This description is intentionally long. " * 20
        for idx in range(8):
            _write_skill(root, f"skill-{idx}", description=long_desc, body=f"body-{idx}")

        manager = SkillManager(skills_dir=root)
        full = manager.build_skills_overview(max_chars=20_000)
        tiny = manager.build_skills_overview(max_chars=500)

        assert "- skill-0:" in full
        assert "body-0" not in full
        assert len(tiny) <= 500
        assert "compressed" in tiny or "omitted" in tiny
        assert "body-0" not in tiny


def test_load_skill_content_wraps_body_without_frontmatter_and_lists_resources():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skill_dir = _write_skill(root, "resource-skill", body="manual")
        refs_dir = skill_dir / "references"
        scripts_dir = skill_dir / "scripts"
        refs_dir.mkdir()
        scripts_dir.mkdir()
        (skill_dir / "root-ref.md").write_text("root ref", encoding="utf-8")
        (refs_dir / "guide.md").write_text("guide ref", encoding="utf-8")
        (scripts_dir / "helper.py").write_text("print('ok')\n", encoding="utf-8")

        manager = SkillManager(skills_dir=root)
        content = manager.load_skill_content("resource-skill")
        reference = manager.load_skill_reference("resource-skill", "guide")

        assert content.startswith("<skill name=\"resource-skill\">")
        assert "---" not in content
        assert "manual" in content
        assert f"file: {skill_dir / 'SKILL.md'}" in content
        assert f"- guide: file:{refs_dir / 'guide.md'}" in content
        assert f"- helper: file:{scripts_dir / 'helper.py'}" in content
        assert "guide ref" in reference


def test_explicit_mentions_match_by_name_plain_name_and_markdown_path():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skill_dir = _write_skill(root, "demo", name="vendor:demo")
        other_dir = _write_skill(root, "other", name="other")

        manager = SkillManager(skills_dir=root)

        assert [s.name for s in manager.collect_explicit_mentions("use $vendor:demo")] == ["vendor:demo"]
        assert [s.name for s in manager.collect_explicit_mentions("use $demo")] == ["vendor:demo"]
        linked = manager.collect_explicit_mentions(f"use [$whatever]({other_dir / 'SKILL.md'})")
        assert [s.name for s in linked] == ["other"]
        path_wins = manager.collect_explicit_mentions(f"use [$demo]({other_dir / 'SKILL.md'})")
        assert [s.name for s in path_wins] == ["other"]
        assert manager.resolve_path(str(skill_dir / "references" / "missing.md")).name == "vendor:demo"


def test_plain_name_mentions_must_be_unique():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_skill(root, "one", name="a:demo")
        _write_skill(root, "two", name="b:demo")

        manager = SkillManager(skills_dir=root)

        assert manager.collect_explicit_mentions("use $demo") == []
        assert [s.name for s in manager.collect_explicit_mentions("use $a:demo")] == ["a:demo"]


def test_agent_session_appends_explicit_skill_content_to_current_turn_only():
    from agent.session import AgentSession

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_skill(root, "manual-skill", body="manual body")
        manager = SkillManager(skills_dir=root)
        session = AgentSession.__new__(AgentSession)
        session.skill_manager = manager

        text_result = session._append_explicit_skill_content("please use $manual-skill", "please use $manual-skill")
        list_result = session._append_explicit_skill_content(
            [{"type": "text", "text": "please use $manual-skill"}],
            "please use $manual-skill",
        )

        assert "<skill name=\"manual-skill\">" in text_result
        assert text_result.startswith("please use $manual-skill")
        assert list_result[-1]["type"] == "text"
        assert "<skill name=\"manual-skill\">" in list_result[-1]["text"]


def test_bash_skill_script_observer_records_hits():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skill_dir = _write_skill(root, "script-skill")
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "hello.py"
        script.write_text("print('skill script ran')\n", encoding="utf-8")

        manager = SkillManager(skills_dir=root)
        bash = BashTool(
            skill_observer=manager,
            dangerously_skip_permissions=True,
        )
        result = json.loads(bash.run({
            "command": f"{sys.executable} {script}",
            "timeout": 120000,
        }))

        assert result["exit_code"] == 0
        assert "skill script ran" in result["stdout"]
        assert result["skill_script_hits"][0]["skill_name"] == "script-skill"
        assert manager.script_hits()[0]["script_path"] == str(script.resolve())


def test_deleted_skill_tools_are_not_importable():
    import importlib.util

    assert importlib.util.find_spec("skills.skill_executor") is None
    assert importlib.util.find_spec("tools.tools.skill_tool") is None
    assert importlib.util.find_spec("tools.tools.run_skill_script_tool") is None
