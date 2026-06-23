"""Skills 系统测试脚本

测试 Skill 发现、解析、内容加载、变量替换和工具集成。
"""

import sys
import os

# 添加项目根目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import Skill, SkillManager, SkillExecutor
from tools.tools.skill_tool import SkillTool
from tools.tools.run_skill_script_tool import RunSkillScriptTool
from tools.toolRegistry import ToolRegistry


def test_skill_manager():
    """测试 SkillManager 的发现和解析"""
    print("=" * 60)
    print("测试 1: SkillManager 发现和解析")
    print("=" * 60)

    manager = SkillManager()
    skills = manager.list_skills()

    print(f"发现 {len(skills)} 个 Skill:")
    for skill in skills:
        print(f"  - {skill.name}: {skill.description[:60]}...")
        print(f"    when_to_use: {skill.when_to_use[:60] if skill.when_to_use else '(无)'}...")
        print(f"    skill_dir: {skill.skill_dir}")
        print(f"    references: {list(skill.get_references().keys())}")
        print(f"    scripts: {list(skill.get_scripts().keys())}")
        print()

    assert len(skills) >= 2, f"应至少发现 2 个 Skill，实际发现 {len(skills)}"
    print("[PASS] SkillManager 发现和解析测试通过\n")


def test_skills_overview():
    """测试 L1 概览生成"""
    print("=" * 60)
    print("测试 2: L1 Skills 概览")
    print("=" * 60)

    manager = SkillManager()
    overview = manager.build_skills_overview()

    print(overview)
    print()

    assert "<available-skills>" in overview, "概览应包含 <available-skills> 标签"
    assert "pdf" in overview, "概览应包含 pdf skill"
    assert "skill-creator" in overview, "概览应包含 skill-creator skill"
    print("[PASS] L1 Skills 概览测试通过\n")


def test_load_skill_content():
    """测试 L2 内容加载（仅正文，不含参考文档）"""
    print("=" * 60)
    print("测试 3: L2 Skill 内容加载")
    print("=" * 60)

    manager = SkillManager()

    # 测试加载 pdf skill（只加载 SKILL.md 正文）
    content = manager.load_skill_content("pdf")
    print(f"pdf skill 内容长度: {len(content)} 字符")
    print(f"前 200 字符:\n{content[:200]}")
    print()

    assert "## Skill: pdf" in content, "内容应包含 Skill 标题"
    assert "PDF Processing Guide" in content, "内容应包含正文"
    assert "可用参考文档" in content, "内容应提示可用的参考文档"
    assert "load_skill_reference" not in content, "不应提示不存在的 load_skill_reference 工具"

    # 测试加载指定参考文档
    ref_content = manager.load_skill_reference("pdf", "forms")
    print(f"forms 参考文档长度: {len(ref_content)} 字符")
    print()

    assert len(ref_content) > 100, "参考文档应有内容"
    assert "SKILL" not in manager.get_skill("pdf").get_references(), "SKILL.md 不应暴露为参考文档"

    # 测试加载不存在的参考文档
    err = manager.load_skill_reference("pdf", "nonexistent")
    print(f"不存在的文档: {err}")
    assert "未找到" in err

    print("[PASS] L2 Skill 内容加载测试通过\n")


def test_references_directory_and_alias_dedupe():
    """测试 references/ 目录和别名去重。"""
    print("=" * 60)
    print("测试 4: 参考文档目录和别名去重")
    print("=" * 60)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        skill_dir = root / "demo-skill"
        refs_dir = skill_dir / "references"
        refs_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: demo-skill\n"
            "description: demo skill\n"
            "aliases:\n"
            "  - demo\n"
            "---\n"
            "body\n",
            encoding="utf-8",
        )
        (skill_dir / "root-ref.md").write_text("root reference", encoding="utf-8")
        (refs_dir / "nested-ref.md").write_text("nested reference", encoding="utf-8")

        manager = SkillManager(skills_dir=root)
        skill = manager.get_skill("demo")
        refs = skill.get_references()
        skills = manager.list_skills()

        print(f"refs: {list(refs.keys())}")
        print(f"skills: {[s.name for s in skills]}")

        assert skill is not None
        assert "root-ref" in refs
        assert "nested-ref" in refs
        assert "SKILL" not in refs
        assert [s.name for s in skills] == ["demo-skill"]

    print("[PASS] 参考文档目录和别名去重测试通过\n")


def test_skill_overview_includes_source_locator_and_usage_rule():
    """测试 L1 概览会告诉模型 Skill 来源和调用规则。"""
    print("=" * 60)
    print("测试 5: Skill 来源定位符")
    print("=" * 60)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        skill_dir = root / "locator-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: locator-skill\n"
            "description: locator skill\n"
            "---\n"
            "body\n",
            encoding="utf-8",
        )

        manager = SkillManager(skills_dir=root)
        overview = manager.build_skills_overview(max_chars=4000)
        print(overview)

        assert "locator-skill" in overview
        assert f"source=file:{skill_dir / 'SKILL.md'}" in overview
        assert "不要用 bash/grep/ls/npx 去搜索、安装或验证这个 Skill 是否存在" in overview

    print("[PASS] Skill 来源定位符测试通过\n")


def test_multiple_skill_dirs_project_overrides_user():
    """测试默认设计中的多目录扫描和后扫描目录覆盖。"""
    print("=" * 60)
    print("测试 5: 多目录扫描和项目级覆盖")
    print("=" * 60)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        user_dir = root / "user" / "skills"
        project_dir = root / "project" / "skills"
        (user_dir / "shared").mkdir(parents=True)
        (project_dir / "shared").mkdir(parents=True)
        (user_dir / "user-only").mkdir(parents=True)

        (user_dir / "shared" / "SKILL.md").write_text(
            "---\n"
            "name: shared\n"
            "description: user shared\n"
            "aliases:\n"
            "  - shared-alias\n"
            "---\n"
            "user body\n",
            encoding="utf-8",
        )
        (project_dir / "shared" / "SKILL.md").write_text(
            "---\n"
            "name: shared\n"
            "description: project shared\n"
            "aliases:\n"
            "  - shared-alias\n"
            "---\n"
            "project body\n",
            encoding="utf-8",
        )
        (user_dir / "user-only" / "SKILL.md").write_text(
            "---\nname: user-only\ndescription: user only\n---\nuser only body\n",
            encoding="utf-8",
        )

        manager = SkillManager(skills_dir=[user_dir, project_dir])
        names = [s.name for s in manager.list_skills()]
        shared = manager.get_skill("shared")
        alias = manager.get_skill("shared-alias")

        print(f"skills: {names}")
        print(f"shared desc: {shared.description}")

        assert set(names) == {"shared", "user-only"}
        assert shared.description == "project shared"
        assert alias is shared
        assert "project body" in manager.load_skill_content("shared")

    print("[PASS] 多目录扫描和项目级覆盖测试通过\n")


def test_skill_tool():
    """测试 SkillTool"""
    print("=" * 60)
    print("测试 6: SkillTool")
    print("=" * 60)

    manager = SkillManager()
    tool = SkillTool(manager)

    # 测试获取 schema
    schema = tool.to_openai_schema()
    print(f"工具名称: {schema['function']['name']}")
    print(f"参数: {list(schema['function']['parameters']['properties'].keys())}")
    print()

    # 测试调用 pdf skill（只加载正文）
    result = tool.run({"skill": "pdf"})
    print(f"调用 pdf skill 结果长度: {len(result)} 字符")
    print(f"前 200 字符:\n{result[:200]}")
    print()

    # 测试加载指定参考文档
    result_ref = tool.run({"skill": "pdf", "document": "forms"})
    print(f"加载 forms 文档长度: {len(result_ref)} 字符")
    print()

    # 测试调用不存在的 skill
    result2 = tool.run({"skill": "nonexistent"})
    print(f"调用不存在的 skill: {result2}")
    print()

    assert "## Skill: pdf" in result
    assert "<skill-source>" in result
    assert "source: file:" in result
    assert len(result_ref) > 100
    assert "<skill-reference-source>" in result_ref
    assert "未找到" in result2
    print("[PASS] SkillTool 测试通过\n")


def test_load_skill_content_includes_resource_paths():
    """测试 L2 Skill 内容会明确给出资源路径，避免模型自行搜索。"""
    print("=" * 60)
    print("测试 7: L2 Skill 资源路径")
    print("=" * 60)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        skill_dir = root / "resource-skill"
        refs_dir = skill_dir / "references"
        scripts_dir = skill_dir / "scripts"
        refs_dir.mkdir(parents=True)
        scripts_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: resource-skill\n"
            "description: resource skill\n"
            "---\n"
            "body\n",
            encoding="utf-8",
        )
        (refs_dir / "guide.md").write_text("guide body", encoding="utf-8")
        (scripts_dir / "helper.py").write_text("print('ok')\n", encoding="utf-8")

        manager = SkillManager(skills_dir=root)
        content = manager.load_skill_content("resource-skill")
        ref = manager.load_skill_reference("resource-skill", "guide")

        print(content)
        print(ref)

        assert f"skill_dir: {skill_dir}" in content
        assert f"skill_file: {skill_dir / 'SKILL.md'}" in content
        assert f"- guide: file:{refs_dir / 'guide.md'}" in content
        assert f"- helper: file:{scripts_dir / 'helper.py'}" in content
        assert "do not search for or install it with shell commands" in content
        assert f"source: file:{refs_dir / 'guide.md'}" in ref
        assert "guide body" in ref

    print("[PASS] L2 Skill 资源路径测试通过\n")


def test_run_skill_script_tool():
    """测试 RunSkillScriptTool"""
    print("=" * 60)
    print("测试 7: RunSkillScriptTool")
    print("=" * 60)

    manager = SkillManager()
    executor = SkillExecutor()
    tool = RunSkillScriptTool(manager, executor)

    # 测试获取 schema
    schema = tool.to_openai_schema()
    print(f"工具名称: {schema['function']['name']}")
    print(f"参数: {list(schema['function']['parameters']['properties'].keys())}")
    print()

    # 测试列出 pdf skill 的脚本
    skill = manager.get_skill("pdf")
    scripts = skill.get_scripts()
    print(f"pdf skill 的脚本: {list(scripts.keys())}")
    print()

    # 测试调用不存在的脚本
    result = tool.run({"skill_name": "pdf", "script_name": "nonexistent"})
    print(f"调用不存在的脚本: {result}")
    print()

    assert "未找到" in result
    print("[PASS] RunSkillScriptTool 测试通过\n")


def test_tool_registry():
    """测试工具注册到 ToolRegistry"""
    print("=" * 60)
    print("测试 8: 工具注册")
    print("=" * 60)

    manager = SkillManager()
    executor = SkillExecutor()

    registry = ToolRegistry()
    registry.register_tool(SkillTool(manager))
    registry.register_tool(RunSkillScriptTool(manager, executor))

    tools_list = registry.list_tools()
    print(f"注册的工具: {tools_list}")

    schemas = registry.get_tools_description_openai_schema()
    print(f"OpenAI schema 数量: {len(schemas)}")
    for s in schemas:
        print(f"  - {s['function']['name']}")

    assert "skill" in tools_list
    assert "run_skill_script" in tools_list
    print("[PASS] 工具注册测试通过\n")


def test_variable_substitution():
    """测试变量替换"""
    print("=" * 60)
    print("测试 9: 变量替换")
    print("=" * 60)

    # 创建一个测试用的 Skill
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "test-skill"
        skill_dir.mkdir()

        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(
            '---\n'
            'name: test-var\n'
            'description: 测试变量替换\n'
            'arguments:\n'
            '  - filename\n'
            '  - format\n'
            '---\n'
            '\n'
            '处理文件: $filename\n'
            '格式: $format\n'
            '所有参数: $ARGUMENTS\n'
            'Skill 目录: ${SKILL_DIR}\n',
            encoding='utf-8'
        )

        manager = SkillManager(skills_dir=Path(tmpdir))
        skill = manager.get_skill("test-var")

        assert skill is not None, "应找到 test-var skill"

        # 测试变量替换
        rendered = skill.render(args='--filename="test.pdf" --format="json"')
        print(f"渲染结果:\n{rendered}")

        assert "test.pdf" in rendered, "filename 变量应被替换"
        assert "json" in rendered, "format 变量应被替换"
        assert str(skill_dir) in rendered, "${SKILL_DIR} 应被替换"

    print("[PASS] 变量替换测试通过\n")


def test_match_skill():
    """测试关键词匹配"""
    print("=" * 60)
    print("测试 10: 关键词匹配")
    print("=" * 60)

    manager = SkillManager()

    test_cases = [
        ("帮我处理这个PDF文件", "pdf"),
        ("我想创建一个新的skill", "skill-creator"),
        ("你好", None),  # 不应该匹配任何 skill
    ]

    for message, expected in test_cases:
        result = manager.match_skill(message)
        status = "[OK]" if result == expected else "[FAIL]"
        print(f"  {status} '{message}' -> {result} (期望: {expected})")

    print("[PASS] 关键词匹配测试完成\n")


def test_hot_reload():
    """测试 SkillManager 会在读取时热重载。"""
    print("=" * 60)
    print("测试 11: 热重载")
    print("=" * 60)

    import tempfile
    import time
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        skill_dir = root / "reload-skill"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(
            "---\nname: reload-skill\ndescription: old desc\n---\nold body\n",
            encoding="utf-8",
        )
        manager = SkillManager(skills_dir=root)
        assert manager.get_skill("reload-skill").description == "old desc"

        time.sleep(1.1)
        skill_md.write_text(
            "---\nname: reload-skill\ndescription: new desc\n---\nnew body\n",
            encoding="utf-8",
        )
        assert manager.get_skill("reload-skill").description == "new desc"
        assert "new body" in manager.load_skill_content("reload-skill")

    print("[PASS] 热重载测试通过\n")


def test_slash_skill_command():
    """测试 /skill 手动触发入口。"""
    print("=" * 60)
    print("测试 12: /skill 手动触发")
    print("=" * 60)

    import tempfile
    from pathlib import Path
    from run_agent import AgentRunner

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        skill_dir = root / "manual-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: manual-skill\ndescription: manual skill\n---\nmanual body $ARGUMENTS\n",
            encoding="utf-8",
        )
        manager = SkillManager(skills_dir=root)
        runner = AgentRunner.__new__(AgentRunner)
        runner._skill_manager = manager

        assert runner._handle_command("/skill") is True
        assert runner._handle_command("/skill manual-skill hello") is True
        assert runner._handle_command("/manual-skill hello") is True

    print("[PASS] /skill 手动触发测试通过\n")


if __name__ == "__main__":
    print("\n[Skills 系统测试]\n")

    test_skill_manager()
    test_skills_overview()
    test_load_skill_content()
    test_references_directory_and_alias_dedupe()
    test_skill_overview_includes_source_locator_and_usage_rule()
    test_multiple_skill_dirs_project_overrides_user()
    test_skill_tool()
    test_load_skill_content_includes_resource_paths()
    test_run_skill_script_tool()
    test_tool_registry()
    test_variable_substitution()
    test_match_skill()
    test_hot_reload()
    test_slash_skill_command()

    print("=" * 60)
    print("[所有测试完成]")
    print("=" * 60)
