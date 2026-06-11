"""RAG 工具综合测试脚本

测试范围:
  1. 文本文档 (PDF) 的添加与搜索
  2. 图片 (PNG) 的 OCR 识别添加与搜索
  3. 音频 (MP3) 的 ASR 转录添加与搜索
  4. 统计、智能问答、边界条件

运行方式:
  cd cb-agent
  python test/test_rag_operations.py

前置条件:
  - .env 中配置了 EMBEDDING_API_KEY 等嵌入模型配置
  - OCR/ASR 测试需要配置 OCR_API_KEY / ASR_API_KEY（未配置时自动跳过）
  - PDF 测试需要安装 markitdown 库
"""

import os
import sys
import shutil
import traceback

# 路径设定：确保 cb-agent 目录在 sys.path 中，且 CWD 在项目根
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PARENT = os.path.dirname(_PROJECT_ROOT)
sys.path.insert(0, _PARENT)
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv()
os.environ.setdefault("CBAGENT_ENABLE_FULL_MEMORY", "1")

# 测试资源目录（项目目录外部）
_SOURCE_DIR = os.path.join(_PARENT, "source")

# ============================================================
# 测试框架
# ============================================================

_passed = 0
_failed = 0
_skipped = 0


def check(cond, desc):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS: {desc}")
    else:
        _failed += 1
        print(f"  FAIL: {desc}")
        traceback.print_stack(limit=2)


def skip(desc):
    global _skipped
    _skipped += 1
    print(f"  SKIP: {desc}")


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def cleanup_store_data():
    """清理本地存储目录，确保每次测试从干净状态开始"""
    for d in ["./zvec_data", "./graph_data", "./memory_data", "./test_rag_data"]:
        if os.path.exists(d):
            try:
                shutil.rmtree(d, ignore_errors=True)
            except PermissionError:
                print(f"  (无法删除 {d}，可能被其他进程占用)")


# ============================================================
# 依赖检测
# ============================================================

def check_dependencies():
    """检测各模态依赖是否就绪"""
    print("\n--- 依赖检测 ---")

    deps = {}

    # MarkItDown（PDF 文本提取）
    try:
        from markitdown import MarkItDown
        deps["markitdown"] = True
        print("  MarkItDown: 可用")
    except ImportError:
        deps["markitdown"] = False
        print("  MarkItDown: 不可用 (PDF测试将跳过)")

    # 嵌入模型
    try:
        from memory.embedding import get_text_embedder_model
        model = get_text_embedder_model()
        test_vec = model.encode("test")
        deps["embedding"] = True
        dim = len(test_vec) if hasattr(test_vec, '__len__') else "?"
        print(f"  嵌入模型: 可用 (维度={dim})")
    except Exception as e:
        deps["embedding"] = False
        print(f"  嵌入模型: 不可用 ({e})")

    # OCR API
    ocr_key = os.getenv("OCR_API_KEY", "").strip()
    ocr_url = os.getenv("OCR_BASE_URL", "").strip()
    deps["ocr"] = bool(ocr_key and ocr_url)
    print(f"  OCR API: {'可用' if deps['ocr'] else '不可用 (图片测试将跳过)'}")

    # ASR API
    asr_key = os.getenv("ASR_API_KEY", "").strip()
    asr_url = os.getenv("ASR_BASE_URL", "").strip()
    deps["asr"] = bool(asr_key and asr_url)
    print(f"  ASR API: {'可用' if deps['asr'] else '不可用 (音频测试将跳过)'}")

    # 测试资源文件
    deps["pdf_path"] = os.path.join(_SOURCE_DIR, "个人简历.pdf")
    deps["png_path"] = os.path.join(_SOURCE_DIR, "屏幕截图 2026-05-26 164742.png")
    deps["mp3_path"] = os.path.join(_SOURCE_DIR, "西格莉卡Lght.mp3")

    for key, path in [("pdf_path", deps["pdf_path"]), ("png_path", deps["png_path"]), ("mp3_path", deps["mp3_path"])]:
        exists = os.path.exists(path)
        deps[key + "_exists"] = exists
        print(f"  资源 {key}: {'存在' if exists else '不存在'} ({path})")

    return deps


# ============================================================
# 第一部分: 初始化与文本文档
# ============================================================

def test_text_document(deps):
    section("第一部分: 文本模态 (PDF)")

    if not deps["embedding"]:
        skip("嵌入模型不可用，跳过全部测试")
        return None

    cleanup_store_data()

    from tools.tools.rag_tool import RAGTool

    # ---- 1.1 初始化 RAGTool ----
    print("\n--- 1.1 初始化 RAGTool ---")
    try:
        rag = RAGTool(
            knowledge_base_path="./test_rag_data",
            rag_namespace="test_text"
        )
        check(rag.initialized, "RAGTool 初始化成功")
    except Exception as e:
        check(False, f"RAGTool 初始化: {e}")
        return None

    # ---- 1.2 添加 PDF 文档 ----
    print("\n--- 1.2 添加 PDF 文档 ---")
    if not deps.get("pdf_path_exists"):
        skip("PDF 资源文件不存在")
    elif not deps["markitdown"]:
        skip("MarkItDown 不可用")
    else:
        result = rag.run({
            "action": "add_document",
            "file_path": deps["pdf_path"],
            "namespace": "test_text",
        })
        print(f"  结果: {result[:200]}...")
        check("已添加" in result or "分块数量" in result, f"PDF add_document 成功")

    # ---- 1.3 搜索 PDF 内容 ----
    print("\n--- 1.3 搜索 PDF 内容 ---")
    result = rag.run({
        "action": "search",
        "query": "工作经历",
        "namespace": "test_text",
        "limit": 3,
    })
    print(f"  结果: {result[:300]}...")
    check("找到" in result or "搜索结果" in result, "search 返回结果")

    # ---- 1.4 智能问答 ----
    print("\n--- 1.4 智能问答 ---")
    result = rag.run({
        "action": "ask",
        "question": "这个人有什么技能？",
        "namespace": "test_text",
        "limit": 3,
    })
    print(f"  结果: {result[:300]}...")
    # ask 可能返回答案或未找到，都算正常（取决于 PDF 内容和嵌入质量）
    check(not result.startswith("❌"), "ask 未抛出异常")

    return rag


# ============================================================
# 第二部分: 图片模态
# ============================================================

def test_image_modality(deps):
    section("第二部分: 图片模态 (OCR)")

    if not deps["embedding"]:
        skip("嵌入模型不可用")
        return None

    if not deps.get("png_path_exists"):
        skip("PNG 资源文件不存在")
        return None

    if not deps["ocr"]:
        skip("OCR API 未配置 (OCR_API_KEY + OCR_BASE_URL)")
        return None

    cleanup_store_data()

    from tools.tools.rag_tool import RAGTool
    from utils.multimodal import MultimodalProcessor

    # ---- 2.1 OCR 处理器基础测试 ----
    print("\n--- 2.1 MultimodalProcessor 图片处理 ---")
    processor = MultimodalProcessor()
    result = processor.process_image(deps["png_path"])
    text = result.get("text", "")
    meta = result.get("metadata", {})
    print(f"  OCR 识别文本长度: {len(text)} 字符")
    print(f"  元数据: { {k: v for k, v in meta.items() if k != 'content_hash'} }")
    check(len(text) > 0, f"OCR 成功识别到文字 ({len(text)} 字符)")
    check(meta.get("modality") == "image", "元数据 modality == image")
    check(meta.get("file_path") is not None, "元数据包含原始文件路径")

    # ---- 2.2 RAGTool add_images ----
    print("\n--- 2.2 RAGTool add_images ---")
    rag = RAGTool(
        knowledge_base_path="./test_rag_data",
        rag_namespace="test_image"
    )
    check(rag.initialized, "RAGTool 初始化成功")

    result = rag.run({
        "action": "add_images",
        "file_path": deps["png_path"],
        "namespace": "test_image",
    })
    print(f"  结果: {result[:300]}")
    check("已添加" in result or "处理数量" in result, "add_images 成功")

    # ---- 2.3 搜索图片 ----
    print("\n--- 2.3 search_images ---")
    result = rag.run({
        "action": "search_images",
        "query": "截图",
        "namespace": "test_image",
        "limit": 3,
    })
    print(f"  结果: {result[:300]}...")
    has_result = "找到" in result or "搜索结果" in result
    check(has_result, "search_images 返回结果")
    # 验证返回结果包含文件路径
    if has_result:
        check("路径" in result or "file_path" in result.lower() or deps["png_path"] in result,
              "搜索结果包含原始文件路径")

    return rag


# ============================================================
# 第三部分: 音频模态
# ============================================================

def test_audio_modality(deps):
    section("第三部分: 音频模态 (ASR)")

    if not deps["embedding"]:
        skip("嵌入模型不可用")
        return None

    if not deps.get("mp3_path_exists"):
        skip("MP3 资源文件不存在")
        return None

    if not deps["asr"]:
        skip("ASR API 未配置 (ASR_API_KEY + ASR_BASE_URL)")
        return None

    cleanup_store_data()

    from tools.tools.rag_tool import RAGTool
    from utils.multimodal import MultimodalProcessor

    # ---- 3.1 ASR 处理器基础测试 ----
    print("\n--- 3.1 MultimodalProcessor 音频处理 ---")
    processor = MultimodalProcessor()
    result = processor.process_audio(deps["mp3_path"])
    text = result.get("text", "")
    meta = result.get("metadata", {})
    print(f"  ASR 转录文本长度: {len(text)} 字符")
    print(f"  转录文本预览: {text[:100]}...")
    check(len(text) > 0, f"ASR 成功转录 ({len(text)} 字符)")
    check(meta.get("modality") == "audio", "元数据 modality == audio")
    check(meta.get("file_path") is not None, "元数据包含原始文件路径")

    # ---- 3.2 RAGTool add_audio ----
    print("\n--- 3.2 RAGTool add_audio ---")
    rag = RAGTool(
        knowledge_base_path="./test_rag_data",
        rag_namespace="test_audio"
    )
    check(rag.initialized, "RAGTool 初始化成功")

    result = rag.run({
        "action": "add_audio",
        "file_path": deps["mp3_path"],
        "namespace": "test_audio",
    })
    print(f"  结果: {result[:300]}")
    check("已添加" in result or "处理数量" in result, "add_audio 成功")

    # ---- 3.3 搜索音频 ----
    print("\n--- 3.3 search_audio ---")
    result = rag.run({
        "action": "search_audio",
        "query": "语音",
        "namespace": "test_audio",
        "limit": 3,
    })
    print(f"  结果: {result[:300]}...")
    has_result = "找到" in result or "搜索结果" in result
    check(has_result, "search_audio 返回结果")
    if has_result:
        check("路径" in result or "file_path" in result.lower() or deps["mp3_path"] in result,
              "搜索结果包含原始文件路径")

    return rag


# ============================================================
# 第四部分: 统计与边界条件
# ============================================================

def test_stats_and_edge_cases(deps):
    section("第四部分: 统计与边界条件")

    cleanup_store_data()

    from tools.tools.rag_tool import RAGTool

    # ---- 4.1 初始化 RAGTool ----
    print("\n--- 4.1 初始化 ---")
    rag = RAGTool(
        knowledge_base_path="./test_rag_data",
        rag_namespace="test_edge"
    )
    check(rag.initialized, "RAGTool 初始化成功")

    # ---- 4.2 未添加文档时搜索 ----
    print("\n--- 4.2 空知识库搜索 ---")
    result = rag.run({
        "action": "search",
        "query": "不存在的内容",
        "namespace": "test_edge",
    })
    print(f"  结果: {result[:200]}")
    check("未找到" in result or "❌" not in result[:5], "空知识库搜索无异常")

    # ---- 4.3 获取统计 ----
    print("\n--- 4.3 知识库统计 ---")
    result = rag.run({
        "action": "stats",
        "namespace": "test_edge",
    })
    print(f"  结果: {result[:300]}...")
    check("📊" in result or "统计" in result or "RAG" in result, "stats 返回统计信息")

    # ---- 4.4 参数验证 ----
    print("\n--- 4.4 validate_parameters ---")
    check(rag.validate_parameters({"action": "add_document"}) == False,
          "add_document 缺 file_path → False")
    check(rag.validate_parameters({"action": "add_document", "file_path": "a.pdf"}) == True,
          "add_document 有 file_path → True")
    check(rag.validate_parameters({"action": "add_images"}) == False,
          "add_images 缺参数 → False")
    check(rag.validate_parameters({"action": "add_images", "file_path": "a.png"}) == True,
          "add_images 有 file_path → True")
    check(rag.validate_parameters({"action": "add_audio"}) == False,
          "add_audio 缺参数 → False")
    check(rag.validate_parameters({"action": "add_audio", "file_path": "a.mp3"}) == True,
          "add_audio 有 file_path → True")
    check(rag.validate_parameters({"action": "search"}) == False,
          "search 缺 query → False")
    check(rag.validate_parameters({"action": "search", "query": "test"}) == True,
          "search 有 query → True")
    check(rag.validate_parameters({"action": "search_images"}) == False,
          "search_images 缺 query → False")
    check(rag.validate_parameters({"action": "search_images", "query": "test"}) == True,
          "search_images 有 query → True")
    check(rag.validate_parameters({"action": "search_audio"}) == False,
          "search_audio 缺 query → False")
    check(rag.validate_parameters({"action": "search_audio", "query": "test"}) == True,
          "search_audio 有 query → True")
    check(rag.validate_parameters({"action": "invalid_action"}) == False,
          "无效 action → False")
    check(rag.validate_parameters({"action": "stats"}) == True,
          "stats 无额外参数 → True")

    # ---- 4.5 不存在的文件 ----
    print("\n--- 4.5 不存在的文件 ---")
    result = rag.run({
        "action": "add_document",
        "file_path": "/nonexistent/file.pdf",
        "namespace": "test_edge",
    })
    check("不存在" in result or "not found" in result.lower(), "不存在的文件返回错误提示")

    # ---- 4.6 空查询 ----
    print("\n--- 4.6 空查询搜索 ---")
    result = rag.run({
        "action": "search",
        "query": "",
        "namespace": "test_edge",
    })
    check("查询不能为空" in result or "❌" in result, "空查询返回提示")

    # ---- 4.7 clear 清空确认 ----
    print("\n--- 4.7 clear 需确认 ---")
    result = rag.run({
        "action": "clear",
        "namespace": "test_edge",
    })
    check("confirm=true" in result.lower() or "确认" in result, "clear 未确认时返回提示")

    return rag


# ============================================================
# 第五部分: 管线层直接测试
# ============================================================

def test_pipeline_layer(deps):
    """直接测试 pipeline.py 中的函数（绕过 RAGTool）"""
    section("第五部分: 管线层直接测试")

    cleanup_store_data()

    from memory.rag.pipeline import (
        _create_default_vector_store,
        load_and_chunk_texts,
        index_chunks,
        search_vectors,
        embed_query,
    )

    # ---- 5.1 默认向量存储创建 ----
    print("\n--- 5.1 默认向量存储 ---")
    try:
        store = _create_default_vector_store()
        store_type = store.store_type
        print(f"  默认 store_type = {store_type}")
        check(store_type in ("zvec", "qdrant"), f"默认 store_type 为 {store_type}")
    except Exception as e:
        check(False, f"创建向量存储失败: {e}")
        return

    # ---- 5.2 query 嵌入 ----
    print("\n--- 5.2 query 嵌入 ---")
    try:
        vec = embed_query("测试查询文本")
        check(len(vec) > 0, f"嵌入成功，维度={len(vec)}")
        check(all(isinstance(x, float) for x in vec), "向量元素为 float")
    except Exception as e:
        check(False, f"嵌入查询失败: {e}")

    # ---- 5.3 PDF 分块 ----
    print("\n--- 5.3 PDF 分块 ---")
    if not deps.get("pdf_path_exists") or not deps.get("markitdown"):
        skip("PDF 或 MarkItDown 不可用，跳过分块测试")
    else:
        chunks = load_and_chunk_texts(
            paths=[deps["pdf_path"]],
            chunk_size=800,
            chunk_overlap=100,
            namespace="test_pipeline",
        )
        check(len(chunks) > 0, f"PDF 分块成功: {len(chunks)} 个分块")
        if chunks:
            check("id" in chunks[0], "分块含 id")
            check("content" in chunks[0], "分块含 content")
            check("metadata" in chunks[0], "分块含 metadata")

            # ---- 5.4 索引分块 ----
            print("\n--- 5.4 索引分块 ---")
            try:
                index_chunks(
                    store=store,
                    chunks=chunks,
                    rag_namespace="test_pipeline",
                )
                check(True, "index_chunks 成功")
            except Exception as e:
                check(False, f"index_chunks 失败: {e}")

            # ---- 5.5 向量搜索 ----
            print("\n--- 5.5 向量搜索 ---")
            results = search_vectors(
                store=store,
                query="工作经历",
                top_k=3,
                rag_namespace="test_pipeline",
            )
            check(len(results) > 0, f"search_vectors 返回 {len(results)} 条")
            if results:
                check("score" in results[0], "结果含 score")
                check("metadata" in results[0], "结果含 metadata")

    # ---- 5.6 搜索过滤（文本 vs 图片 vs 音频） ----
    print("\n--- 5.6 按模态过滤搜索 ---")
    try:
        # 无模态过滤 → 搜所有
        r_all = search_vectors(store=store, query="工作经历", top_k=5,
                               rag_namespace="test_pipeline")
        # 图片过滤 → 只搜图片模态
        r_img = search_vectors(store=store, query="工作经历", top_k=5,
                               rag_namespace="test_pipeline", only_rag_data=False,
                               modality="image")
        # 音频过滤 → 只搜音频模态
        r_aud = search_vectors(store=store, query="工作经历", top_k=5,
                               rag_namespace="test_pipeline", only_rag_data=False,
                               modality="audio")
        print(f"  所有模态: {len(r_all)} 条, 图片: {len(r_img)} 条, 音频: {len(r_aud)} 条")
        check(True, "按模态过滤搜索无异常")
    except Exception as e:
        check(False, f"模态过滤搜索失败: {e}")

    # ---- 5.7 统计信息 ----
    print("\n--- 5.7 向量存储统计 ---")
    try:
        stats = store.get_collection_stats()
        print(f"  store_type = {stats.get('store_type', 'unknown')}")
        check("store_type" in stats, "stats 包含 store_type")
        check(stats["store_type"] in ("zvec", "qdrant"), f"store_type = {stats['store_type']}")
    except Exception as e:
        check(False, f"get_collection_stats 失败: {e}")

    # 清理
    try:
        store.clear_collection()
    except Exception:
        pass


# ============================================================
# 主函数
# ============================================================

def main():
    print("=" * 60)
    print("  RAG 工具综合测试")
    print(f"  资源目录: {_SOURCE_DIR}")
    print(f"  VECTOR_STORE_TYPE = {os.getenv('VECTOR_STORE_TYPE', '(默认 zvec)')}")
    print(f"  GRAPH_STORE_TYPE  = {os.getenv('GRAPH_STORE_TYPE', '(默认 sqlite)')}")
    print("=" * 60)

    # 检测依赖
    deps = check_dependencies()

    # 按顺序运行各测试部分
    try:
        test_text_document(deps)
    except Exception as e:
        print(f"\n  !! 文本模态测试异常: {e}")
        traceback.print_exc()
        global _failed
        _failed += 1

    try:
        test_image_modality(deps)
    except Exception as e:
        print(f"\n  !! 图片模态测试异常: {e}")
        traceback.print_exc()
        _failed += 1

    try:
        test_audio_modality(deps)
    except Exception as e:
        print(f"\n  !! 音频模态测试异常: {e}")
        traceback.print_exc()
        _failed += 1

    try:
        test_stats_and_edge_cases(deps)
    except Exception as e:
        print(f"\n  !! 边界条件测试异常: {e}")
        traceback.print_exc()
        _failed += 1

    try:
        test_pipeline_layer(deps)
    except Exception as e:
        print(f"\n  !! 管线层测试异常: {e}")
        traceback.print_exc()
        _failed += 1

    # 清理
    cleanup_store_data()

    # 报告
    total = _passed + _failed + _skipped
    print(f"\n{'=' * 60}")
    print(f"  测试结果: {_passed} 通过", end="")
    if _failed > 0:
        print(f", {_failed} 失败", end="")
    if _skipped > 0:
        print(f", {_skipped} 跳过", end="")
    total_tests = _passed + _failed
    if total_tests > 0:
        print(f"  ({_passed}/{total_tests})", end="")
    print()
    print(f"{'=' * 60}")

    return _failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
