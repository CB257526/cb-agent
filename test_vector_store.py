"""测试 VectorStoreManager 和 ZvecVectorStore 功能

运行方式: python test_vector_store.py
"""

import sys
import os
import shutil
import uuid
import random

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ============================================================
# 测试 1: 导入检查
# ============================================================
print("=" * 60)
print("测试 1: 导入检查")
print("=" * 60)

from memory.storage.vector_store_base import VectorStoreBase
from memory.storage.vector_store_manager import VectorStoreManager
from memory.storage.zvec_store import ZvecVectorStore, _dict_to_filter_string
from memory.storage.qdrant_store import QdrantVectorStore, QdrantConnectionManager

print("  VectorStoreBase         ✓")
print("  VectorStoreManager      ✓")
print("  ZvecVectorStore         ✓")
print("  QdrantVectorStore       ✓")

# 验证继承关系
assert issubclass(ZvecVectorStore, VectorStoreBase), "ZvecVectorStore 必须继承 VectorStoreBase"
assert issubclass(QdrantVectorStore, VectorStoreBase), "QdrantVectorStore 必须继承 VectorStoreBase"
print("  继承关系检查             ✓")

# ============================================================
# 测试 2: 后端自动探测
# ============================================================
print("\n" + "=" * 60)
print("测试 2: 后端自动探测 (_resolve_store_type)")
print("=" * 60)

# 2.1 显式指定
assert VectorStoreManager._resolve_store_type("zvec") == "zvec"
assert VectorStoreManager._resolve_store_type("qdrant") == "qdrant"
assert VectorStoreManager._resolve_store_type("ZVEC") == "zvec"  # 大小写不敏感
print("  2.1 显式指定               ✓")

# 2.2 url/api_key/path 不影响后端选择（仅作连接配置）
# 即使传了 Qdrant 的连接参数，没有显式指定后端时仍默认 Zvec
assert VectorStoreManager._resolve_store_type(url="http://localhost:6333") == "zvec"
assert VectorStoreManager._resolve_store_type(api_key="sk-xxx") == "zvec"
assert VectorStoreManager._resolve_store_type(path="./data") == "zvec"
print("  2.2 kwargs 不影响选择       ✓")

# 2.3 环境变量
os.environ["VECTOR_STORE_TYPE"] = "qdrant"
assert VectorStoreManager._resolve_store_type() == "qdrant"
del os.environ["VECTOR_STORE_TYPE"]
print("  2.3 环境变量 VECTOR_STORE_TYPE ✓")

# 2.4 默认（无参数、无环境变量）
assert VectorStoreManager._resolve_store_type() == "zvec"
print("  2.4 默认 → zvec            ✓")

# ============================================================
# 测试 3: 单例 Key 构建
# ============================================================
print("\n" + "=" * 60)
print("测试 3: 单例 Key 构建 (_build_instance_key)")
print("=" * 60)

key_qdrant = VectorStoreManager._build_instance_key("qdrant", "test_coll", url="http://localhost:6333")
assert key_qdrant == "qdrant:http://localhost:6333:test_coll", f"got: {key_qdrant}"
print(f"  Qdrant key: {key_qdrant}")

key_zvec = VectorStoreManager._build_instance_key("zvec", "test_coll", path="./data")
assert key_zvec == "zvec:./data:test_coll", f"got: {key_zvec}"
print(f"  Zvec key:   {key_zvec}")

key_qdrant_local = VectorStoreManager._build_instance_key("qdrant", "test_coll")
assert key_qdrant_local == "qdrant:local:test_coll"
print(f"  Qdrant local key: {key_qdrant_local}")

print("  全部通过 ✓")

# ============================================================
# 测试 4: ZvecVectorStore 基本操作
# ============================================================
print("\n" + "=" * 60)
print("测试 4: ZvecVectorStore 基本操作")
print("=" * 60)

TEST_PATH = "./test_zvec_data"
TEST_COLL = "test_collection"
VECTOR_DIM = 384

# 清理旧数据
if os.path.exists(TEST_PATH):
    shutil.rmtree(TEST_PATH)

# 4.1 创建实例
store = ZvecVectorStore(
    path=TEST_PATH,
    collection_name=TEST_COLL,
    vector_size=VECTOR_DIM,
    distance="cosine",
)
assert store.store_type == "zvec"
assert store.collection_name == TEST_COLL
assert store.vector_size == VECTOR_DIM
print("  4.1 创建实例              ✓")

# 4.2 健康检查
assert store.health_check() is True
print("  4.2 健康检查              ✓")

# 4.3 写入向量
vectors = [[random.random() for _ in range(VECTOR_DIM)] for _ in range(5)]
metadata = [
    {
        "memory_id": f"mem_{i}",
        "user_id": "test_user",
        "memory_type": "episodic",
        "content": f"这是第{i}条测试记忆",
        "importance": 0.5 + i * 0.1,
        "timestamp": 1717000000 + i,
        "custom_field": f"extra_{i}",  # 自定义字段 → 进 payload_json
    }
    for i in range(5)
]
ids = [f"mem_{i}" for i in range(5)]

result = store.add_vectors(vectors=vectors, metadata=metadata, ids=ids)
assert result is True
print("  4.3 写入5条向量           ✓")

# 4.4 验证存储（通过 stats）
stats = store.get_collection_stats()
assert stats["store_type"] == "zvec"
assert stats["name"] == TEST_COLL
# Zvec 的 upsert 后数据立即可查
print(f"  4.4 集合统计: vectors={stats.get('vectors_count')}, store_type={stats['store_type']}  ✓")

# 4.5 搜索
query_vec = vectors[0]  # 用第一条向量查，应该能找回自己
results = store.search_similar(query_vector=query_vec, limit=3)
assert len(results) > 0, "搜索结果为空！"
assert results[0]["id"] == "mem_0", f"最相似应该是mem_0，实际是{results[0]['id']}"
# 第一个结果应该是 mem_0 自己（余弦距离 ≈ 1.0）
print(f"  4.5 搜索: top-1 id={results[0]['id']}, score={results[0]['score']:.4f}  ✓")

# 4.6 验证元数据还原（含自定义字段）
first_meta = results[0]["metadata"]
assert first_meta["memory_id"] == "mem_0"
assert first_meta["memory_type"] == "episodic"
assert first_meta["custom_field"] == "extra_0", f"自定义字段丢失！got: {first_meta}"
print(f"  4.6 元数据还原: memory_id={first_meta['memory_id']}, custom_field={first_meta.get('custom_field')}  ✓")

# 4.7 过滤搜索
results_filtered = store.search_similar(
    query_vector=query_vec,
    limit=5,
    where={"memory_type": "episodic"},
)
assert len(results_filtered) > 0
print(f"  4.7 过滤搜索: {len(results_filtered)} 条结果  ✓")

# 4.8 按 ID 删除
store.delete_vectors(["mem_4"])
# 验证: 再搜应该最多4条
results_after_del = store.search_similar(query_vector=query_vec, limit=10)
assert len(results_after_del) <= 4
print("  4.8 按ID删除              ✓")

# 4.9 按条件删除
store.delete_by_filter({"memory_type": "episodic"})
results_after_filter_del = store.search_similar(query_vector=query_vec, limit=10)
assert len(results_after_filter_del) == 0, f"应该全部删除，但还剩{len(results_after_filter_del)}条"
print("  4.9 按条件删除            ✓")

# 4.10 get_collection_info
info = store.get_collection_info()
assert info["name"] == TEST_COLL
assert "config" in info
print(f"  4.10 集合信息: name={info['name']}, config={info['config']}  ✓")

# 4.11 清空
store.clear_collection()
results_empty = store.search_similar(query_vector=query_vec, limit=10)
assert len(results_empty) == 0
print("  4.11 清空集合              ✓")

# 清理（先 close 释放文件锁，再删目录）
store.close()
if os.path.exists(TEST_PATH):
    shutil.rmtree(TEST_PATH)
print("\n  ZvecVectorStore 全部测试通过 ✓")

# ============================================================
# 测试 5: VectorStoreManager 单例管理
# ============================================================
print("\n" + "=" * 60)
print("测试 5: VectorStoreManager 单例管理")
print("=" * 60)

TEST_PATH2 = "./test_zvec_manager"
if os.path.exists(TEST_PATH2):
    shutil.rmtree(TEST_PATH2)

# 清理可能残余
VectorStoreManager.clear_all()

# 5.1 首次获取（zvec 自动）
store1 = VectorStoreManager.get_instance(
    path=TEST_PATH2,
    collection_name="test_singleton",
    vector_size=VECTOR_DIM,
)
assert store1.store_type == "zvec"
print("  5.1 首次获取实例           ✓")

# 5.2 再次获取 → 应返回相同实例
store2 = VectorStoreManager.get_instance(
    path=TEST_PATH2,
    collection_name="test_singleton",
    vector_size=VECTOR_DIM,
)
assert store1 is store2, "两次获取的不是同一个实例！单例模式失效！"
print("  5.2 单例复用               ✓")

# 5.3 不同 collection → 不同实例
store3 = VectorStoreManager.get_instance(
    path=TEST_PATH2,
    collection_name="test_other_collection",
    vector_size=VECTOR_DIM,
)
assert store1 is not store3, "不同 collection 应该是不同实例！"
print("  5.3 不同collection隔离     ✓")

# 5.4 list_instances
instances = VectorStoreManager.list_instances()
assert len(instances) == 2, f"应该有2个实例，实际有{len(instances)}"
print(f"  5.4 实例列表: {list(instances.keys())}  ✓")

# 5.5 写入+搜索（通过manager获取的实例）
store1.add_vectors(
    vectors=[[random.random() for _ in range(VECTOR_DIM)]],
    metadata=[{
        "memory_id": "mgr_test",
        "user_id": "u1",
        "memory_type": "episodic",
        "content": "manager test",
    }],
    ids=["mgr_test"],
)
results = store1.search_similar(
    query_vector=[random.random() for _ in range(VECTOR_DIM)],
    limit=1,
    where={"memory_type": "episodic"},
)
assert len(results) > 0
assert results[0]["metadata"]["memory_id"] == "mgr_test"
print("  5.5 Manager实例读写        ✓")

# 5.6 清理单例
# 先显式关闭 collection（释放文件锁），再移除实例和目录
store1.close()
store3.close()
VectorStoreManager.remove_instance(path=TEST_PATH2, collection_name="test_singleton")
VectorStoreManager.remove_instance(path=TEST_PATH2, collection_name="test_other_collection")
remaining = VectorStoreManager.list_instances()
assert len(remaining) == 0, f"应该有0个实例，实际有{len(remaining)}"
print("  5.6 移除实例               ✓")

# 清理目录（close 后文件锁已释放，可以安全删除）
if os.path.exists(TEST_PATH2):
    shutil.rmtree(TEST_PATH2)

print("\n  VectorStoreManager 全部测试通过 ✓")

# ============================================================
# 测试 6: 过滤字符串转换
# ============================================================
print("\n" + "=" * 60)
print("测试 6: _dict_to_filter_string 过滤转换")
print("=" * 60)

# 6.1 单条件
s1 = _dict_to_filter_string({"memory_type": "episodic"})
assert s1 == "memory_type = 'episodic'", f"got: {s1}"
print(f"  6.1 单条件: {s1}")

# 6.2 多条件
s2 = _dict_to_filter_string({"memory_type": "episodic", "user_id": "abc"})
assert "memory_type = 'episodic'" in s2
assert "user_id = 'abc'" in s2
assert " AND " in s2
print(f"  6.2 多条件: {s2}")

# 6.3 IN 列表
s3 = _dict_to_filter_string({"memory_id": ["id1", "id2", "id3"]})
assert "memory_id IN ('id1', 'id2', 'id3')" == s3, f"got: {s3}"
print(f"  6.3 IN列表: {s3}")

# 6.4 数值
s4 = _dict_to_filter_string({"importance": 0.8})
assert s4 == "importance = 0.8", f"got: {s4}"
print(f"  6.4 数值: {s4}")

# 6.5 布尔
s5 = _dict_to_filter_string({"is_active": True})
assert s5 == "is_active = true", f"got: {s5}"
print(f"  6.5 布尔: {s5}")

# 6.6 混合
s6 = _dict_to_filter_string({"memory_type": "episodic", "user_id": "u1"})
print(f"  6.6 混合: {s6}")

print("\n  过滤转换全部通过 ✓")

# ============================================================
# 测试 7: store_type 属性
# ============================================================
print("\n" + "=" * 60)
print("测试 7: store_type / collection_name / vector_size 属性")
print("=" * 60)

TEST_PATH3 = "./test_zvec_props"
if os.path.exists(TEST_PATH3):
    shutil.rmtree(TEST_PATH3)

s = ZvecVectorStore(path=TEST_PATH3, collection_name="props_test", vector_size=128)
assert s.store_type == "zvec"
assert s.collection_name == "props_test"
assert s.vector_size == 128
print(f"  store_type:      {s.store_type}")
print(f"  collection_name: {s.collection_name}")
print(f"  vector_size:     {s.vector_size}")
print("  属性检查全部通过 ✓")

s.close()
if os.path.exists(TEST_PATH3):
    shutil.rmtree(TEST_PATH3)

# ============================================================
# 测试 8: 错误处理
# ============================================================
print("\n" + "=" * 60)
print("测试 8: 错误处理")
print("=" * 60)

TEST_PATH4 = "./test_zvec_errors"
if os.path.exists(TEST_PATH4):
    shutil.rmtree(TEST_PATH4)

store_err = ZvecVectorStore(path=TEST_PATH4, collection_name="err_test", vector_size=16)

# 8.1 空向量列表
assert store_err.add_vectors(vectors=[], metadata=[]) is False
print("  8.1 空向量列表 → False     ✓")

# 8.2 维度不匹配
assert store_err.add_vectors(
    vectors=[[1.0, 2.0, 3.0]],  # 3维，期望16维
    metadata=[{"memory_id": "x", "user_id": "u", "memory_type": "t"}],
) is False
print("  8.2 维度不匹配 → False     ✓")

# 8.3 不存在的后端
try:
    VectorStoreManager._create_instance("nonexistent", "test", 384, "cosine")
    assert False, "应该抛出 ValueError"
except ValueError:
    print("  8.3 不存在后端 → ValueError ✓")

store_err.close()
if os.path.exists(TEST_PATH4):
    shutil.rmtree(TEST_PATH4)

print("\n  错误处理全部通过 ✓")

# ============================================================
# 总结
# ============================================================
print("\n" + "=" * 60)
print("🎉 全部测试通过！")
print("=" * 60)
print(f"""
  测试覆盖:
    - VectorStoreBase 抽象基类定义
    - ZvecVectorStore 完整 CRUD + 搜索 + 过滤
    - VectorStoreManager 后端探测 + 单例管理
    - _dict_to_filter_string 过滤格式转换
    - 错误处理（空数据、维度不匹配、无效后端）
    - 元数据序列化/反序列化（含自定义字段）
""")
