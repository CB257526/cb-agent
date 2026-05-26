"""记忆系统综合测试脚本

测试范围:
  1. 四种记忆类型（working, episodic, semantic, perceptual）的所有操作
  2. 默认后端（Zvec + SQLite）的正确性
  3. 切换后端后（Qdrant + Neo4j）的兼容性

运行方式:
  cd cb-agent
  python test/test_memory_operations.py
"""

import os
import sys
import time
import shutil
import traceback

# cb-agent 目录名含连字符不是合法 Python 包名，因此将其父目录加入路径，
# 这样 memory/、core/ 等作为顶级包导入时相对路径能正确解析。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PARENT = os.path.dirname(_PROJECT_ROOT)
sys.path.insert(0, _PARENT)
sys.path.insert(0, _PROJECT_ROOT)
# 同时把项目根作为工作目录，让相对路径 (./zvec_data 等) 落在项目内
os.chdir(_PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv()

from memory.base import MemoryConfig
from memory.manager import MemoryManager

# ============================================================
# 测试框架
# ============================================================

_passed = 0
_failed = 0

def check(cond, desc):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✓ {desc}")
    else:
        _failed += 1
        print(f"  ✗ FAIL: {desc}")
        traceback.print_stack(limit=2)


def section(title):
    print(f"\n{'─' * 50}")
    print(f"  {title}")
    print(f"{'─' * 50}")


def cleanup_store_data():
    """清理本地存储目录，确保每次测试从干净状态开始"""
    for d in ["./zvec_data", "./graph_data", "./memory_data", "./test_mem_data"]:
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 第一部分: 默认后端（Zvec + SQLite）
# ============================================================

def test_default_backends():
    section("第一部分: 默认后端 (Zvec + SQLite)")

    # 确保环境变量未设置（使用默认）
    for var in ["VECTOR_STORE_TYPE", "GRAPH_STORE_TYPE", "QDRANT_URL", "QDRANT_API_KEY"]:
        os.environ.pop(var, None)

    cleanup_store_data()

    config = MemoryConfig(storage_path="./test_mem_data")

    # ---- 1.1 创建 MemoryManager ----
    print("\n--- 1.1 初始化 MemoryManager ---")
    mm = MemoryManager(
        config=config,
        user_id="test_user",
        enable_working=True,
        enable_episodic=True,
        enable_semantic=True,
        enable_perceptual=True,
    )
    check("working" in mm.memory_types, "WorkingMemory 已启用")
    check("episodic" in mm.memory_types, "EpisodicMemory 已启用")
    check("semantic" in mm.memory_types, "SemanticMemory 已启用")
    check("perceptual" in mm.memory_types, "PerceptualMemory 已启用")

    # ---- 1.2 验证默认后端类型 ----
    print("\n--- 1.2 验证默认后端 ---")
    em = mm.memory_types["episodic"]
    sm = mm.memory_types["semantic"]
    pm = mm.memory_types["perceptual"]

    # episodic 用 VectorStoreManager → 默认 zvec
    evs_type = em.vector_store.store_type
    print(f"  episodic.vector_store   = {evs_type}")
    check(evs_type == "zvec", "episodic 向量后端为 zvec")

    # semantic 也用 VectorStoreManager
    svs_type = sm.vector_store.store_type
    sgs_type = sm.graph_store.store_type
    print(f"  semantic.vector_store   = {svs_type}")
    print(f"  semantic.graph_store    = {sgs_type}")
    check(svs_type == "zvec", "semantic 向量后端为 zvec")
    check(sgs_type == "sqlite", "semantic 图后端为 sqlite")

    # perceptual 按模态分存储
    for mod in ["text", "image", "audio"]:
        pvs_type = pm.vector_stores[mod].store_type
        print(f"  perceptual.vector_stores[{mod}] = {pvs_type}")
        check(pvs_type == "zvec", f"perceptual({mod}) 向量后端为 zvec")

    # working 是纯内存，没有数据库
    wm = mm.memory_types["working"]

    # ---- 1.3 添加记忆 ----
    print("\n--- 1.3 添加记忆 ---")

    # Working
    wid = mm.add_memory("今天学习了Python异步编程", memory_type="working", importance=0.7)
    check(bool(wid), f"添加 working 记忆: {wid[:8]}...")

    # Episodic
    eid = mm.add_memory(
        "昨天和同事讨论了项目架构重构方案",
        memory_type="episodic",
        importance=0.8,
        metadata={"session_id": "meeting_001", "participants": ["张三", "李四"]},
    )
    check(bool(eid), f"添加 episodic 记忆: {eid[:8]}...")

    # Semantic
    sid = mm.add_memory(
        "Python的asyncio库提供了协程、事件循环、Future等核心组件，用于编写异步并发代码",
        memory_type="semantic",
        importance=0.9,
    )
    check(bool(sid), f"添加 semantic 记忆: {sid[:8]}...")

    # Perceptual (text 模态)
    pid = mm.add_memory(
        "这是一张日落的风景照片描述",
        memory_type="perceptual",
        importance=0.6,
        metadata={"modality": "text"},
    )
    check(bool(pid), f"添加 perceptual 记忆: {pid[:8]}...")

    # ---- 1.4 搜索记忆 ----
    print("\n--- 1.4 搜索记忆 ---")

    # 跨类型搜索
    results = mm.retrieve_memories(query="Python", limit=10)
    check(len(results) > 0, f"跨类型搜索 'Python': {len(results)} 条")
    # 应该能找到包含 Python 的语义记忆
    found_semantic = any("asyncio" in r.content or "Python" in r.content for r in results)
    check(found_semantic, "搜索结果包含语义记忆中的Python内容")

    # 按类型搜索
    episodic_results = mm.retrieve_memories(query="项目", memory_types=["episodic"], limit=5)
    check(len(episodic_results) > 0, f"类型搜索 '项目' (episodic): {len(episodic_results)} 条")

    # ---- 1.5 统计 ----
    print("\n--- 1.5 统计 ---")
    stats = mm.get_memory_stats()
    print(f"  总记忆数: {stats['total_memories']}")
    check(stats["total_memories"] >= 4, f"总记忆数 >= 4: {stats['total_memories']}")
    for mt, ms in stats["memories_by_type"].items():
        print(f"    {mt}: count={ms.get('count')}")

    # 验证 episodic 的统计包含 vector_store 信息
    ep_stats = mm.memory_types["episodic"].get_stats()
    check("vector_store" in ep_stats, "episodic 统计含 vector_store")
    check(ep_stats["vector_store"].get("store_type") == "zvec", "episodic vector_store 类型为 zvec")

    # 验证 semantic 的统计包含图信息
    sem_stats = mm.memory_types["semantic"].get_stats()
    check(sem_stats.get("graph_nodes", -1) >= 0, "semantic 统计含图节点数")

    # ---- 1.6 更新记忆 ----
    print("\n--- 1.6 更新记忆 ---")
    result = mm.update_memory(wid, content="今天深入学习了Python异步编程和FastAPI框架")
    check(result, f"更新 working 记忆: {'成功' if result else '失败'}")

    # 验证更新后的内容
    wm_stats = mm.memory_types["working"].get_stats()
    print(f"  working 更新后 count={wm_stats.get('count')}")

    # ---- 1.7 遗忘 ----
    print("\n--- 1.7 遗忘（importance_based） ---")
    forgotten = mm.forget_memories(strategy="importance_based", threshold=0.95)
    print(f"  遗忘 {forgotten} 条记忆（阈值 0.95）")

    # ---- 1.8 整合 ----
    print("\n--- 1.8 整合（working → episodic） ---")
    consolidated = mm.consolidate_memories(
        from_type="working",
        to_type="episodic",
        importance_threshold=0.5,
    )
    print(f"  整合 {consolidated} 条记忆")

    # ---- 1.9 清空类型 ----
    print("\n--- 1.9 清空各类型 ---")
    mm.memory_types["working"].clear()
    wm_stats = mm.memory_types["working"].get_stats()
    check(wm_stats.get("count") == 0, f"working 清空后: {wm_stats.get('count')} 条")

    mm.memory_types["episodic"].clear()
    mm.memory_types["semantic"].clear()
    mm.memory_types["perceptual"].clear()

    # 最终全清
    mm.clear_all_memories()
    final_stats = mm.get_memory_stats()
    check(final_stats["total_memories"] == 0, f"全部清空后: {final_stats['total_memories']} 条")

    print("\n  默认后端测试完成")
    return True


# ============================================================
# 第二部分: 显式切换后端测试
# ============================================================

def test_explicit_backends():
    section("第二部分: 显式指定后端 (Qdrant/Neo4j 可选)")

    # 检查 Qdrant 是否可用
    qdrant_available = False
    neo4j_available = False

    qdrant_url = os.getenv("QDRANT_URL", "").strip()
    qdrant_key = os.getenv("QDRANT_API_KEY", "").strip()

    if qdrant_url and qdrant_key:
        try:
            from qdrant_client import QdrantClient
            client = QdrantClient(url=qdrant_url, api_key=qdrant_key, timeout=10)
            client.get_collections()
            qdrant_available = True
            print(f"\n  Qdrant 云服务可用: {qdrant_url}")
        except Exception as e:
            print(f"\n  Qdrant 不可用，跳过该项测试: {e}")

    neo4j_uri = os.getenv("NEO4J_URI", "").strip()
    neo4j_user = os.getenv("NEO4J_USERNAME", "").strip()
    neo4j_pass = os.getenv("NEO4J_PASSWORD", "").strip()

    if neo4j_uri and neo4j_user and neo4j_pass:
        try:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_pass))
            driver.verify_connectivity()
            driver.close()
            neo4j_available = True
            print(f"  Neo4j 可用: {neo4j_uri}")
        except Exception as e:
            print(f"  Neo4j 不可用，跳过该项测试: {e}")

    # ---- 2.1 显式切换到 Qdrant ----
    if qdrant_available:
        print("\n--- 2.1 测试 Qdrant 后端 ---")
        os.environ["VECTOR_STORE_TYPE"] = "qdrant"
        os.environ["GRAPH_STORE_TYPE"] = "sqlite"  # 图仍用 SQLite

        cleanup_store_data()
        config = MemoryConfig(storage_path="./test_mem_data")

        from memory.storage import VectorStoreManager, GraphStoreManager
        VectorStoreManager.clear_all()
        GraphStoreManager.clear_all()

        # 用 VectorStoreManager 直接创建 Qdrant 实例
        from memory.storage import VectorStoreManager
        qdrant_store = VectorStoreManager.get_instance(
            store_type="qdrant",
            url=qdrant_url,
            api_key=qdrant_key,
            collection_name="test_memory_ops_qdrant",
            vector_size=384,
        )
        check(qdrant_store.store_type == "qdrant", f"显式指定后 store_type={qdrant_store.store_type}")
        check(qdrant_store.health_check(), "Qdrant 健康检查通过")

        # 清理 Qdrant 中的测试数据
        try:
            qdrant_store.clear_collection()
        except Exception:
            pass

        # 写入并搜索
        qdrant_store.add_vectors(
            vectors=[[0.1] * 384, [0.9] * 384],
            metadata=[
                {"memory_id": "q1", "user_id": "u", "memory_type": "test", "content": "Qdrant测试数据A"},
                {"memory_id": "q2", "user_id": "u", "memory_type": "test", "content": "Qdrant测试数据B"},
            ],
            ids=["q1", "q2"],
        )
        results = qdrant_store.search_similar(query_vector=[0.15] * 384, limit=2)
        check(len(results) > 0, f"Qdrant 搜索返回 {len(results)} 条")

        # 清理
        qdrant_store.clear_collection()
        VectorStoreManager.clear_all()
        print("  Qdrant 测试通过")

    else:
        print("\n--- 2.1 Qdrant 后端 ---")
        print("  (跳过: Qdrant 不可用, 需要有效的 QDRANT_URL + QDRANT_API_KEY)")

    # ---- 2.2 显式切换到 Neo4j ----
    if neo4j_available:
        print("\n--- 2.2 测试 Neo4j 后端 ---")
        os.environ["VECTOR_STORE_TYPE"] = "zvec"
        os.environ["GRAPH_STORE_TYPE"] = "neo4j"

        from memory.storage import GraphStoreManager
        GraphStoreManager.clear_all()

        neo4j_store = GraphStoreManager.get_instance(
            store_type="neo4j",
            uri=neo4j_uri,
            username=neo4j_user,
            password=neo4j_pass,
        )
        check(neo4j_store.store_type == "neo4j", f"显式指定后 store_type={neo4j_store.store_type}")
        check(neo4j_store.health_check(), "Neo4j 健康检查通过")

        # 写入实体和关系
        check(neo4j_store.add_entity("n1", "Neo4jTest", "TEST"), "Neo4j 添加实体")
        check(neo4j_store.add_relationship("n1", "n1", "SELF_TEST"), "Neo4j 添加关系")

        # 搜索
        entities = neo4j_store.search_entities_by_name("Neo4j")
        check(len(entities) > 0, f"Neo4j 按名称搜索: {len(entities)} 条")

        # 图遍历
        related = neo4j_store.find_related_entities("n1", max_depth=1)
        print(f"  Neo4j 图遍历(depth=1): {len(related)} 条")

        # 清理
        neo4j_store.clear_all()
        GraphStoreManager.clear_all()
        print("  Neo4j 测试通过")

    else:
        print("\n--- 2.2 Neo4j 后端 ---")
        print("  (跳过: Neo4j 不可用, 需要有效的 NEO4J_URI + NEO4J_USERNAME + NEO4J_PASSWORD)")

    # ---- 2.3 恢复默认环境变量 ----
    for var in ["VECTOR_STORE_TYPE", "GRAPH_STORE_TYPE"]:
        os.environ.pop(var, None)

    print("\n  显式后端测试完成")
    return True


# ============================================================
# 第三部分: 边界条件测试
# ============================================================

def test_edge_cases():
    section("第三部分: 边界条件测试")

    cleanup_store_data()
    config = MemoryConfig(storage_path="./test_mem_data")

    mm = MemoryManager(
        config=config,
        user_id="edge_test_user",
        enable_working=True,
        enable_episodic=True,
        enable_semantic=False,   # 故意关闭
        enable_perceptual=False,  # 故意关闭
    )

    # ---- 3.1 关闭某些类型 ----
    print("\n--- 3.1 只启用了 working + episodic ---")
    check(len(mm.memory_types) == 2, f"启用的类型数: {len(mm.memory_types)}")

    # ---- 3.2 搜索空记忆 ----
    print("\n--- 3.2 搜索空记忆 ---")
    results = mm.retrieve_memories(query="不存在的内容", limit=5)
    check(len(results) == 0, f"空搜索返回 {len(results)} 条")

    # ---- 3.3 更新不存在的记忆 ----
    print("\n--- 3.3 更新不存在记忆 ---")
    result = mm.update_memory("不存在的ID", content="新内容")
    check(result is False, "不存在的ID返回 False")

    # ---- 3.4 删除不存在的记忆 ----
    print("\n--- 3.4 删除不存在记忆 ---")
    result = mm.remove_memory("不存在的ID")
    check(result is False, "不存在的ID返回 False")

    # ---- 3.5 空内容添加 ----
    print("\n--- 3.5 空内容添加 ---")
    mid = mm.add_memory("", memory_type="working", importance=0.5)
    check(bool(mid), "空内容添加不崩溃")

    # ---- 3.6 重复添加相同记忆 ----
    print("\n--- 3.6 重复添加 ---")
    id1 = mm.add_memory("重复测试内容", memory_type="working", importance=0.5)
    id2 = mm.add_memory("重复测试内容", memory_type="working", importance=0.5)
    check(id1 != id2, f"两次添加产生不同ID: {id1[:8]} != {id2[:8]}")

    # ---- 3.7 整合不存在的类型 ----
    print("\n--- 3.7 整合不存在的类型 ---")
    count = mm.consolidate_memories(from_type="semantic", to_type="episodic", importance_threshold=0.5)
    check(count == 0, f"目标类型不存在时返回 0: {count}")

    # ---- 3.8 遗忘无匹配记忆 ----
    print("\n--- 3.8 遗忘无匹配记忆 ---")
    count = mm.forget_memories(strategy="importance_based", threshold=0.99)
    print(f"  遗忘 {count} 条")

    mm.clear_all_memories()

    print("\n  边界条件测试完成")
    return True


# ============================================================
# 主函数
# ============================================================

def main():
    print("=" * 60)
    print("  记忆系统综合测试")
    print(f"  VECTOR_STORE_TYPE = {os.getenv('VECTOR_STORE_TYPE', '(默认 zvec)')}")
    print(f"  GRAPH_STORE_TYPE  = {os.getenv('GRAPH_STORE_TYPE', '(默认 sqlite)')}")
    print("=" * 60)

    # 先保存原始环境变量
    orig_env = {}
    for var in ["VECTOR_STORE_TYPE", "GRAPH_STORE_TYPE", "QDRANT_URL", "QDRANT_API_KEY",
                "NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"]:
        orig_env[var] = os.environ.get(var)

    try:
        test_default_backends()
    except Exception as e:
        print(f"\n  !! 默认后端测试异常: {e}")
        traceback.print_exc()
        global _failed
        _failed += 1

    try:
        test_explicit_backends()
    except Exception as e:
        print(f"\n  !! 显式后端测试异常: {e}")
        traceback.print_exc()
        _failed += 1

    try:
        test_edge_cases()
    except Exception as e:
        print(f"\n  !! 边界条件测试异常: {e}")
        traceback.print_exc()
        _failed += 1

    # 恢复环境变量
    for var, val in orig_env.items():
        if val is not None:
            os.environ[var] = val
        else:
            os.environ.pop(var, None)

    # 清理测试数据
    cleanup_store_data()

    # 报告
    total = _passed + _failed
    print(f"\n{'=' * 60}")
    print(f"  测试结果: {_passed}/{total} 通过", end="")
    if _failed > 0:
        print(f", {_failed} 失败")
    else:
        print(" ✓")
    print(f"{'=' * 60}")

    return _failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
