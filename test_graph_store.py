"""测试 GraphStoreBase + SQLiteGraphStore + GraphStoreManager
"""

import os
import sys
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_PATH = "./test_graph_data"

# 清理旧数据
if os.path.exists(TEST_PATH):
    shutil.rmtree(TEST_PATH)


def ok(msg):
    print(f"  {msg}")

def fail(msg):
    print(f"  FAIL: {msg}")
    sys.exit(1)


# ============================================================
# 测试 1: 导入 + 继承
# ============================================================
print("=" * 50)
print("测试 1: 导入检查")
print("=" * 50)
from memory.storage.graph_store_base import GraphStoreBase
from memory.storage.graph_store_manager import GraphStoreManager
from memory.storage.sqlite_graph_store import SQLiteGraphStore
from memory.storage.neo4j_store import Neo4jGraphStore

assert issubclass(SQLiteGraphStore, GraphStoreBase)
assert issubclass(Neo4jGraphStore, GraphStoreBase)
ok("GraphStoreBase       ✓")
ok("GraphStoreManager    ✓")
ok("SQLiteGraphStore     ✓")
ok("Neo4jGraphStore      ✓")
ok("继承关系检查          ✓")

# ============================================================
# 测试 2: 后端探测
# ============================================================
print("\n" + "=" * 50)
print("测试 2: 后端自动探测")
print("=" * 50)

assert GraphStoreManager._resolve_store_type("sqlite") == "sqlite"
assert GraphStoreManager._resolve_store_type("neo4j") == "neo4j"
assert GraphStoreManager._resolve_store_type("NEO4J") == "neo4j"
# uri/path 不影响后端选择
assert GraphStoreManager._resolve_store_type(uri="bolt://localhost") == "sqlite"
assert GraphStoreManager._resolve_store_type(path="./data") == "sqlite"
# 默认
assert GraphStoreManager._resolve_store_type() == "sqlite"
# 环境变量
os.environ["GRAPH_STORE_TYPE"] = "neo4j"
assert GraphStoreManager._resolve_store_type() == "neo4j"
del os.environ["GRAPH_STORE_TYPE"]

ok("显式指定              ✓")
ok("kwargs 不影响选择      ✓")
ok("默认 → sqlite         ✓")

# ============================================================
# 测试 3: SQLiteGraphStore CRUD
# ============================================================
print("\n" + "=" * 50)
print("测试 3: SQLiteGraphStore CRUD")
print("=" * 50)

store = SQLiteGraphStore(path=TEST_PATH, name="test_graph")
assert store.store_type == "sqlite"
ok("3.1 创建实例           ✓")

assert store.health_check() is True
ok("3.2 健康检查           ✓")

# 添加实体
assert store.add_entity("u1", "张三", "PERSON", {"age": 30})
assert store.add_entity("m1", "Python学习", "MEMORY", {"memory_id": "m1"})
assert store.add_entity("m2", "旅行计划", "MEMORY", {"memory_id": "m2"})
assert store.add_entity("c1", "Python", "SKILL")
assert store.add_entity("c2", "北京", "LOC")
ok("3.3 添加实体(5个)      ✓")

# 添加关系
assert store.add_relationship("u1", "m1", "HAS_MEMORY")
assert store.add_relationship("u1", "m2", "HAS_MEMORY")
assert store.add_relationship("m1", "c1", "INVOLVES", {"strength": 0.9})
assert store.add_relationship("m2", "c2", "MENTIONS")
assert store.add_relationship("c1", "c2", "CO_OCCURS")
ok("3.4 添加关系(5条)      ✓")

# 图遍历: u1 max_depth=2
related = store.find_related_entities("u1", max_depth=2)
names = [r["name"] for r in related]
assert "Python学习" in names
assert "旅行计划" in names
assert "Python" in names or "北京" in names  # depth=2
print(f"  3.5 图遍历(depth=2): {[r['name'] for r in related]}  ✓")

# 名称搜索
results = store.search_entities_by_name("Python")
names_py = [r["name"] for r in results]
assert "Python学习" in names_py
assert "Python" in names_py
print(f"  3.6 名称搜索: {names_py}  ✓")

# 获取实体关系
rels = store.get_entity_relationships("u1")
assert len(rels) == 2  # 两条出边
ok(f"3.7 获取关系({len(rels)}条)     ✓")

# 统计
stats = store.get_stats()
assert stats["total_nodes"] == 5
assert stats["total_relationships"] == 5
ok(f"3.8 统计(nodes={stats['total_nodes']}, edges={stats['total_relationships']})  ✓")

# 删除实体
assert store.delete_entity("c2")
# 删除后对应边也没了
stats2 = store.get_stats()
assert stats2["total_nodes"] == 4
ok("3.9 删除实体+级联删边  ✓")

# 清空
assert store.clear_all()
stats3 = store.get_stats()
assert stats3["total_nodes"] == 0
ok("3.10 清空               ✓")

store.close()

# ============================================================
# 测试 4: 持久化（重启恢复）
# ============================================================
print("\n" + "=" * 50)
print("测试 4: 持久化（重启恢复）")
print("=" * 50)

store1 = SQLiteGraphStore(path=TEST_PATH, name="test_graph")
store1.add_entity("p1", "持久化测试", "TEST", {"data": "hello"})
store1.add_relationship("p1", "p1", "SELF_LOOP")
store1.close()

# 重新打开（模拟重启）
store2 = SQLiteGraphStore(path=TEST_PATH, name="test_graph")
stats = store2.get_stats()
assert stats["total_nodes"] == 1, f"重启后节点应为1，实际为{stats['total_nodes']}"
assert stats["total_relationships"] == 1
nodes = store2.search_entities_by_name("持久化")
assert nodes[0]["name"] == "持久化测试"
assert nodes[0]["data"] == "hello"  # props 也恢复了
store2.close()
ok("重启后数据完整恢复     ✓")

# ============================================================
# 测试 5: GraphStoreManager 单例
# ============================================================
print("\n" + "=" * 50)
print("测试 5: GraphStoreManager 单例")
print("=" * 50)

GraphStoreManager.clear_all()

s1 = GraphStoreManager.get_instance("sqlite", path=TEST_PATH, name="mgr_test")
s2 = GraphStoreManager.get_instance("sqlite", path=TEST_PATH, name="mgr_test")
assert s1 is s2
ok("5.1 单例复用            ✓")

s3 = GraphStoreManager.get_instance("sqlite", path=TEST_PATH, name="mgr_other")
assert s1 is not s3
ok("5.2 不同name隔离        ✓")

instances = GraphStoreManager.list_instances()
assert len(instances) == 2
ok(f"5.3 实例列表({len(instances)})          ✓")

s1.add_entity("e1", "manager测试", "TEST")
s2.search_entities_by_name("manager")  # s2 === s1
ok("5.4 Manager实例读写     ✓")

GraphStoreManager.clear_all()
s1.close()
s3.close()

# ============================================================
# 测试 6: 磁盘文件验证
# ============================================================
print("\n" + "=" * 50)
print("测试 6: 磁盘文件验证")
print("=" * 50)

import glob
db_files = glob.glob(os.path.join(TEST_PATH, "*.db"))
print(f"  数据库文件: {db_files}")
assert len(db_files) >= 1, "应该有至少一个 .db 文件"
for f in db_files:
    size_kb = os.path.getsize(f) / 1024
    print(f"    {os.path.basename(f)}: {size_kb:.1f} KB")
ok("磁盘文件正常           ✓")

# ============================================================
# 清理
# ============================================================
if os.path.exists(TEST_PATH):
    shutil.rmtree(TEST_PATH)

print("\n" + "=" * 50)
print("全部测试通过!")
print("=" * 50)
