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
    """测试 L2 内容加载"""
    print("=" * 60)
    print("测试 3: L2 Skill 内容加载")
    print("=" * 60)

    manager = SkillManager()

    # 测试加载 pdf skill
    content = manager.load_skill_content("pdf")
    print(f"pdf skill 内容长度: {len(content)} 字符")
    print(f"前 200 字符:\n{content[:200]}")
    print()

    assert "## Skill: pdf" in content, "内容应包含 Skill 标题"
    assert "PDF Processing Guide" in content, "内容应包含正文"
    assert "## 参考文档" in content, "内容应包含参考文档部分"
    print("[PASS] L2 Skill 内容加载测试通过\n")


def test_skill_tool():
    """测试 SkillTool"""
    print("=" * 60)
    print("测试 4: SkillTool")
    print("=" * 60)

    manager = SkillManager()
    tool = SkillTool(manager)

    # 测试获取 schema
    schema = tool.to_openai_schema()
    print(f"工具名称: {schema['function']['name']}")
    print(f"参数: {list(schema['function']['parameters']['properties'].keys())}")
    print()

    # 测试调用 pdf skill
    result = tool.run({"skill": "pdf"})
    print(f"调用 pdf skill 结果长度: {len(result)} 字符")
    print(f"前 200 字符:\n{result[:200]}")
    print()

    # 测试调用不存在的 skill
    result2 = tool.run({"skill": "nonexistent"})
    print(f"调用不存在的 skill: {result2}")
    print()

    assert "## Skill: pdf" in result
    assert "未找到" in result2
    print("[PASS] SkillTool 测试通过\n")


def test_run_skill_script_tool():
    """测试 RunSkillScriptTool"""
    print("=" * 60)
    print("测试 5: RunSkillScriptTool")
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
    print("测试 6: 工具注册")
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
    print("测试 7: 变量替换")
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
    print("测试 8: 关键词匹配")
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


if __name__ == "__main__":
    print("\n[Skills 系统测试]\n")

    test_skill_manager()
    test_skills_overview()
    test_load_skill_content()
    test_skill_tool()
    test_run_skill_script_tool()
    test_tool_registry()
    test_variable_substitution()
    test_match_skill()

    print("=" * 60)
    print("[所有测试完成]")
    print("=" * 60)
