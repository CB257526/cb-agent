from typing import List, Dict, Optional, Any
import os
import hashlib
import sqlite3
import time
import json
from pathlib import Path
from memory.embedding import get_text_embedder_model, get_dimension
from memory.feature_flags import full_memory_disabled_message, is_full_memory_enabled
from ..storage.vector_store_manager import VectorStoreManager
from ..storage.graph_store_manager import GraphStoreManager
from utils.multimodal import MultimodalProcessor


def _get_markitdown_instance():
    """获取 MarkItDown 文档转换器实例

    MarkItDown 是微软开源的通用文档转换工具，能将 PDF、Office、图片、音频等
    数十种格式统一转换为 Markdown 文本，作为 RAG 管线的文档解析入口。

    返回:
        MarkItDown 实例，若库未安装则返回 None
    """
    try:
        from markitdown import MarkItDown
        return MarkItDown()
    except ImportError:
        print("[RAG] MarkItDown 未安装，将回退为纯文本读取。安装命令: pip install markitdown")
        return None


def _is_markitdown_supported_format(path: str) -> bool:
    """检查文件格式是否被 MarkItDown 支持

    通过文件扩展名判断，支持以下类别：
    - 文档：PDF、Word(doc/docx)、Excel(xls/xlsx)、PPT(ppt/pptx)
    - 文本：txt、markdown、csv、json、xml、html
    - 图片：jpg、png、gif、bmp、tiff、webp（OCR + 元数据提取）
    - 音频：mp3、wav、m4a、aac、flac、ogg（转录 + 元数据提取）
    - 压缩包：zip、tar、gz、rar
    - 代码：py、js、ts、java、cpp、c、h、css、scss
    - 配置：log、conf、ini、cfg、yaml、yml、toml

    返回:
        True 表示格式受支持
    """
    ext = (os.path.splitext(path)[1] or '').lower()
    supported_formats = {
        # 办公文档
        '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        # 纯文本/标记格式
        '.txt', '.md', '.csv', '.json', '.xml', '.html', '.htm',
        # 图片（支持 OCR 文字识别 + EXIF 元数据提取）
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp',
        # 音频（支持语音转录 + ID3 元数据提取）
        '.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg',
        # 压缩归档
        '.zip', '.tar', '.gz', '.rar',
        # 源代码文件
        '.py', '.js', '.ts', '.java', '.cpp', '.c', '.h', '.css', '.scss',
        # 配置文件
        '.log', '.conf', '.ini', '.cfg', '.yaml', '.yml', '.toml'
    }
    return ext in supported_formats


def _convert_to_markdown(path: str) -> str:
    """通用文档转换器：将任意支持格式的文件转为 Markdown 文本

    这是 RAG 管线的文档解析入口。PDF 文件走增强处理管线（去噪 + 段落重组），
    其余格式直接调用 MarkItDown 转换。若 MarkItDown 不可用，回退到纯文本读取。

    返回:
        转换后的 Markdown 文本字符串，失败时返回空字符串
    """
    if not os.path.exists(path):
        print(f"[RAG] 文件不存在: {path}")
        return ""

    ext = (os.path.splitext(path)[1] or '').lower()
    # PDF 文件走增强处理管线：提取 → 去噪 → 短行合并 → 段落重组
    if ext == '.pdf':
        return _enhanced_pdf_processing(path)

    # 其他格式：MarkItDown 直接转换
    md_instance = _get_markitdown_instance()
    if md_instance is None:
        return _fallback_text_reader(path)

    try:
        result = md_instance.convert(path)
        text = getattr(result, "text_content", None)
        if isinstance(text, str) and text.strip():
            return text
        return ""
    except Exception as e:
        print(f"[RAG] MarkItDown 转换失败 ({path}): {e}")
        return _fallback_text_reader(path)

def _enhanced_pdf_processing(path: str) -> str:
    """PDF 增强处理管线

    流程：MarkItDown 提取原始文本 → 去噪清洗 → 短行合并 → 段落重组。
    针对 PDF 常见的页眉页脚、页码、断行等问题做了专门处理。
    """
    print(f"[RAG] PDF 增强处理开始: {path}")

    md_instance = _get_markitdown_instance()
    if md_instance is None:
        return _fallback_text_reader(path)

    try:
        result = md_instance.convert(path)
        raw_text = getattr(result, "text_content", None)
        if not raw_text or not raw_text.strip():
            print("[RAG] PDF 未提取到文本内容")
            return ""

        # 后处理管线：去噪 → 合并 → 重组
        cleaned_text = _post_process_pdf_text(raw_text)
        print(f"[RAG] PDF 后处理完成: {len(raw_text)} 字符 → {len(cleaned_text)} 字符")
        return cleaned_text

    except Exception as e:
        print(f"[RAG] PDF 增强处理失败 ({path}): {e}")
        return _fallback_text_reader(path)

def _post_process_pdf_text(text: str) -> str:
    """PDF 文本后处理：去噪 → 短行合并 → 段落重组

    三阶段处理管线：
    1. 行级清洗：去除空行、单字符噪音、页码、常见页眉页脚关键词
    2. 智能合并：将 PDF 转换产生的断行重新拼接为完整段落
    3. 段落重组：根据标题、冒号结尾、行长度等线索划分段落

    返回:
        清洗并重组后的文本
    """
    import re

    # ── 阶段 1: 行级清洗 ──
    lines = text.splitlines()
    cleaned_lines = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 单字符行（且非数字）通常是 PDF 页码或噪音
        if len(line) <= 2 and not line.isdigit():
            continue

        # 纯数字行通常是页码
        if re.match(r'^\d+$', line):
            continue
        # 常见 GitHub 页眉噪音（代码项目的 PDF 常含此类信息）
        if line.lower() in ['github', 'project', 'forks', 'stars', 'language']:
            continue

        cleaned_lines.append(line)

    # ── 阶段 2: 智能短行合并 ──
    # PDF 转换时常将一句话断成多行，这里将长度不足 60 字符的短行
    # 与下一行拼接，前提是两行都不是标题/列表项
    merged_lines = []
    i = 0

    while i < len(cleaned_lines):
        current_line = cleaned_lines[i]

        if len(current_line) < 60 and i + 1 < len(cleaned_lines):
            next_line = cleaned_lines[i + 1]

            # 以冒号结尾通常是列表项标题，不合并
            # 以 # 开头的是 Markdown 标题，不合并
            if (not current_line.endswith('：') and
                not current_line.endswith(':') and
                not current_line.startswith('#') and
                not next_line.startswith('#') and
                len(next_line) < 120):

                merged_line = current_line + " " + next_line
                merged_lines.append(merged_line)
                i += 2  # 跳过已合并的下一行
                continue

        merged_lines.append(current_line)
        i += 1

    # ── 阶段 3: 段落重组 ──
    # 根据以下信号判断新段落起始：
    #   - Markdown 标题标记 (#)
    #   - 中文/英文冒号结尾（通常为列表项标题）
    #   - 行长度 > 150 字符（长句通常是独立段落）
    paragraphs = []
    current_paragraph = []

    for line in merged_lines:
        if (line.startswith('#') or                # Markdown 标题
            line.endswith('：') or                 # 中文冒号结尾
            line.endswith(':') or                  # 英文冒号结尾
            len(line) > 150 or                     # 长句独立成段
            not current_paragraph):                # 第一行

            if current_paragraph:
                paragraphs.append(' '.join(current_paragraph))
                current_paragraph = []

            paragraphs.append(line)
        else:
            current_paragraph.append(line)

    if current_paragraph:
        paragraphs.append(' '.join(current_paragraph))

    return '\n\n'.join(paragraphs)


def _fallback_text_reader(path: str) -> str:
    """纯文本兜底读取器

    当 MarkItDown 不可用时，直接用 UTF-8 编码读取文件原始内容。
    UTF-8 失败则尝试 Latin-1 编码。

    返回:
        文件文本内容，完全无法读取时返回空字符串
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except Exception:
        try:
            with open(path, 'r', encoding='latin-1', errors='ignore') as f:
                return f.read()
        except Exception:
            return ""


def _detect_lang(sample: str) -> str:
    """检测文本语言

    使用 langdetect 库检测，取前 1000 字符作为样本（足够准确，节省开销）。
    失败时返回 "unknown"。
    """
    try:
        from langdetect import detect
        return detect(sample[:1000]) if sample else "unknown"
    except Exception:
        return "unknown"


def _is_cjk(ch: str) -> bool:
    """判断字符是否属于 CJK（中日韩）统一汉字区

    覆盖 Unicode 中主要的汉字区块：
    - 基本汉字 (CJK Unified Ideographs): 0x4E00-0x9FFF
    - 扩展 A: 0x3400-0x4DBF
    - 扩展 B-F: 0x20000-0x2CEAF
    - 兼容汉字: 0xF900-0xFAFF
    """
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF or
        0x3400 <= code <= 0x4DBF or
        0x20000 <= code <= 0x2A6DF or
        0x2A700 <= code <= 0x2B73F or
        0x2B740 <= code <= 0x2B81F or
        0x2B820 <= code <= 0x2CEAF or
        0xF900 <= code <= 0xFAFF
    )


def _approx_token_len(text: str) -> int:
    """近似估算文本的 Token 数量

    估算规则：
    - 每个 CJK 字符算 1 token（中文、日文、韩文以字为单位）
    - 其余文本按空白分词后统计词数（英文等以词为单位）

    这仅用于分块决策，不需要精确的 tokenizer。
    """
    cjk = sum(1 for ch in text if _is_cjk(ch))
    non_cjk_tokens = len([t for t in text.split() if t])
    return cjk + non_cjk_tokens


def _split_paragraphs_with_headings(text: str) -> List[Dict]:
    """将 Markdown 文本按标题+段落结构拆分

    解析 Markdown 标题层级 (# ## ### ...)，为每个段落记录其所在的
    标题路径（如 "第1章 > 1.1 概述 > 1.1.1 细节"），以及文本起止位置。

    标题栈（heading_stack）维护当前所处的嵌套路径：
    - 遇到同级或上级标题时，退栈到对应层级
    - 遇到空行时，将累积的文本行合并为一个段落输出

    返回:
        [{"content": "段落文本", "heading_path": "父标题 > 子标题", "start": N, "end": N}, ...]
    """
    lines = text.splitlines()
    heading_stack: List[str] = []   # 当前标题嵌套栈，如 ["概述", "架构"]
    paragraphs: List[Dict] = []
    buf: List[str] = []             # 当前段落文本行缓冲区
    char_pos = 0                    # 累计字符位置，用于记录段落在原文中的偏移

    def flush_buf(end_pos: int):
        """将缓冲区中的文本行合并为一个段落并输出"""
        if not buf:
            return
        content = "\n".join(buf).strip()
        if not content:
            return
        paragraphs.append({
            "content": content,
            "heading_path": " > ".join(heading_stack) if heading_stack else None,
            "start": max(0, end_pos - len(content)),
            "end": end_pos,
        })

    for ln in lines:
        raw = ln
        if raw.strip().startswith("#"):
            # ── 遇到标题行：先输出当前累积的段落，再更新标题栈 ──
            flush_buf(char_pos)
            # 计算标题层级：前缀 # 的数量
            level = len(raw) - len(raw.lstrip('#'))
            title = raw.lstrip('#').strip()
            if level <= 0:
                level = 1
            # 退栈：遇到同级或上级标题时，弹出更深的层级
            if level <= len(heading_stack):
                heading_stack = heading_stack[:level - 1]
            heading_stack.append(title)
            char_pos += len(raw) + 1
            continue

        # ── 空行作为段落分隔符 ──
        if raw.strip() == "":
            flush_buf(char_pos)
            buf = []
        else:
            buf.append(raw)
        char_pos += len(raw) + 1

    flush_buf(char_pos)
    # 兜底：如果没产出任何段落，将全文作为一个段落
    if not paragraphs:
        paragraphs = [{"content": text, "heading_path": None, "start": 0, "end": len(text)}]
    return paragraphs


def _chunk_paragraphs(paragraphs: List[Dict], chunk_tokens: int, overlap_tokens: int) -> List[Dict]:
    """将段落按照 Token 预算分块，支持重叠

    以段落为最小粒度（不跨段落切断），逐个追加直到累积 token 数达到 chunk_tokens
    上限，然后输出当前分块。overlap_tokens 控制相邻分块间保留的尾部段落数，
    避免关键上下文被分块边界截断。

    每个分块记录：
    - content: 分块文本
    - start/end: 在原文中的起止字符位置
    - heading_path: 所属标题路径（取自分块中最后一个有标题信息的段落）

    返回:
        [{"content": "...", "start": N, "end": N, "heading_path": "..."}, ...]
    """
    chunks: List[Dict] = []
    cur: List[Dict] = []      # 当前分块中已收集的段落
    cur_tokens = 0             # 当前分块的累计 token 数
    i = 0

    while i < len(paragraphs):
        p = paragraphs[i]
        p_tokens = _approx_token_len(p["content"]) or 1

        # 当前分块还未超出预算，或者分块为空（至少保留一个段落）
        if cur_tokens + p_tokens <= chunk_tokens or not cur:
            cur.append(p)
            cur_tokens += p_tokens
            i += 1
        else:
            # ── 输出当前分块 ──
            content = "\n\n".join(x["content"] for x in cur)
            start = cur[0]["start"]
            end = cur[-1]["end"]
            # 取最后一个有标题信息的段落作为该分块的标题路径
            heading_path = next((x["heading_path"] for x in reversed(cur) if x.get("heading_path")), None)
            chunks.append({
                "content": content,
                "start": start,
                "end": end,
                "heading_path": heading_path,
            })

            # ── 构建重叠区：从当前分块尾部保留 overlap_tokens 的段落 ──
            if overlap_tokens > 0 and cur:
                kept: List[Dict] = []
                kept_tokens = 0
                for x in reversed(cur):
                    t = _approx_token_len(x["content"]) or 1
                    if kept_tokens + t > overlap_tokens:
                        break
                    kept.append(x)
                    kept_tokens += t
                cur = list(reversed(kept))
                cur_tokens = kept_tokens
            else:
                cur = []
                cur_tokens = 0

    # ── 输出最后一个分块 ──
    if cur:
        content = "\n\n".join(x["content"] for x in cur)
        start = cur[0]["start"]
        end = cur[-1]["end"]
        heading_path = next((x["heading_path"] for x in reversed(cur) if x.get("heading_path")), None)
        chunks.append({
            "content": content,
            "start": start,
            "end": end,
            "heading_path": heading_path,
        })

    return chunks


def load_and_chunk_texts(paths: List[str], chunk_size: int = 800, chunk_overlap: int = 100, namespace: Optional[str] = None, source_label: str = "rag") -> List[Dict]:
    """通用文档加载与分块

    这是 RAG 管线的文档入库入口。流程：
    1. 遍历所有输入文件路径
    2. 通过 MarkItDown 统一转换为 Markdown 文本
    3. 按标题结构拆分为段落
    4. 按 Token 预算分块（保留重叠区）
    5. 内容哈希去重（跨文件重复内容只保留一份）
    6. 为每个分块生成唯一 ID 并附加元数据

    参数:
        paths: 文件路径列表
        chunk_size: 每个分块的目标 Token 数（默认 800）
        chunk_overlap: 相邻分块重叠的 Token 数（默认 100）
        namespace: 命名空间标识（用于多项目隔离）
        source_label: 来源标签

    返回:
        [{"id": "chunk_id", "content": "分块文本", "metadata": {...}}, ...]
    """
    print(f"[RAG] 文档加载开始: 文件数={len(paths)} 分块大小={chunk_size} 重叠={chunk_overlap} 命名空间={namespace or 'default'}")
    chunks: List[Dict] = []
    seen_hashes = set()  # 内容去重：相同哈希只保留一份

    for path in paths:
        if not os.path.exists(path):
            print(f"[RAG] 文件不存在: {path}")
            continue

        print(f"[RAG] 正在处理: {path}")
        ext = (os.path.splitext(path)[1] or '').lower()

        # ── 步骤 1: 转换为 Markdown ──
        markdown_text = _convert_to_markdown(path)
        if not markdown_text.strip():
            print(f"[RAG] 未提取到文本内容: {path}")
            continue

        # ── 步骤 2: 语言检测 + 文档 ID 生成 ──
        lang = _detect_lang(markdown_text)
        doc_id = hashlib.md5(f"{path}|{len(markdown_text)}".encode('utf-8')).hexdigest()

        # ── 步骤 3: 标题感知的段落拆分 ──
        para = _split_paragraphs_with_headings(markdown_text)
        # ── 步骤 4: Token 预算分块 ──
        token_chunks = _chunk_paragraphs(para, chunk_tokens=max(1, chunk_size), overlap_tokens=max(0, chunk_overlap))

        # ── 步骤 5: 生成分块元数据并去重 ──
        for ch in token_chunks:
            content = ch["content"]
            start = ch.get("start", 0)
            end = ch.get("end", start + len(content))
            norm = content.strip()
            if not norm:
                continue

            content_hash = hashlib.md5(norm.encode('utf-8')).hexdigest()
            if content_hash in seen_hashes:
                continue  # 跨文件重复内容，跳过
            seen_hashes.add(content_hash)

            # 唯一 ID = 文档ID + 位置 + 内容哈希
            chunk_id = hashlib.md5(f"{doc_id}|{start}|{end}|{content_hash}".encode('utf-8')).hexdigest()
            chunks.append({
                "id": chunk_id,
                "content": content,
                "metadata": {
                    "source_path": path,
                    "file_ext": ext,
                    "doc_id": doc_id,
                    "lang": lang,
                    "start": start,
                    "end": end,
                    "content_hash": content_hash,
                    "namespace": namespace or "default",
                    "source": source_label,
                    "external": True,
                    "heading_path": ch.get("heading_path"),
                    "format": "markdown",
                },
            })

    print(f"[RAG] 文档加载完成: 总分块数={len(chunks)}")
    return chunks


def build_graph_from_chunks(chunks: List[Dict]) -> None:
    """将文档分块写入图数据库（通过 GraphStoreManager 自动适配 SQLite/Neo4j）。"""
    graph_store = GraphStoreManager.get_instance()
    created_docs = set()
    for ch in chunks:
        mem_id = ch["id"]
        meta = ch.get("metadata", {})
        source_path = meta.get("source_path")
        doc_id = meta.get("doc_id")
        if doc_id and doc_id not in created_docs:
            created_docs.add(doc_id)
            try:
                graph_store.add_entity(
                    entity_id=doc_id,
                    name=os.path.basename(source_path or doc_id),
                    entity_type="Document",
                    properties={"source_path": source_path, "lang": meta.get("lang")}
                )
            except Exception:
                pass
        try:
            graph_store.add_entity(entity_id=mem_id, name=mem_id, entity_type="Memory", properties={
                "source_path": source_path,
                "doc_id": doc_id,
                "start": meta.get("start"),
                "end": meta.get("end"),
            })
        except Exception:
            pass
        if doc_id:
            try:
                graph_store.add_relationship(from_entity_id=doc_id, to_entity_id=mem_id,
                                             relationship_type="HAS_CHUNK", properties={})
            except Exception:
                pass


# ============================================================
# 图片/音频模态的索引与搜索
# ============================================================

# 全局多模态处理器实例（懒加载）
_multimodal_processor = None


def _get_multimodal_processor() -> MultimodalProcessor:
    """获取或创建多模态处理器单例"""
    global _multimodal_processor
    if _multimodal_processor is None:
        _multimodal_processor = MultimodalProcessor()
    return _multimodal_processor


def index_image(
    file_path: str,
    store = None,
    rag_namespace: str = "default",
) -> int:
    """处理单张图片：OCR 识别 → 分块 → 嵌入 → 入库

    流程:
    1. 调用视觉 LLM 对图片进行 OCR + 视觉描述
    2. 将识别结果作为文本分块
    3. 嵌入后存入向量存储，元数据标注 modality="image" 和原始文件路径

    参数:
        file_path: 图片文件路径
        store: 向量存储实例，None 时自动创建
        rag_namespace: 命名空间

    返回:
        入库的分块数量，失败时返回 0
    """
    processor = _get_multimodal_processor()
    result = processor.process_image(file_path)
    if not result["text"]:
        print(f"[RAG] 图片 OCR 未产出文本: {file_path}")
        return 0

    # 将识别文本作为一个分块，附加图片元数据
    chunk_content = result["text"]
    # 生成唯一文档 ID：文件路径 + 内容哈希
    doc_id = hashlib.md5(f"{file_path}|{len(chunk_content)}".encode('utf-8')).hexdigest()
    content_hash = hashlib.md5(chunk_content.encode('utf-8')).hexdigest()
    chunk_id = hashlib.md5(f"{doc_id}|0|{len(chunk_content)}|{content_hash}".encode('utf-8')).hexdigest()

    chunk = {
        "id": chunk_id,
        "content": chunk_content,
        "metadata": {
            "source_path": file_path,
            "file_ext": os.path.splitext(file_path)[1].lower(),
            "doc_id": doc_id,
            "lang": "zh",
            "start": 0,
            "end": len(chunk_content),
            "content_hash": content_hash,
            "namespace": rag_namespace,
            "source": "rag",
            "external": True,
            "format": "ocr_text",
            # ── 多模态特有元数据 ──
            "modality": "image",
            "original_file_path": str(Path(file_path).absolute()),
            "mime_type": result["metadata"].get("mime_type", ""),
            "file_size": result["metadata"].get("file_size", 0),
        },
    }

    index_chunks(store=store, chunks=[chunk], rag_namespace=rag_namespace)
    print(f"[RAG] 图片已索引: {os.path.basename(file_path)} → {len(chunk_content)} 字符")
    return 1


def index_audio(
    file_path: str,
    store = None,
    rag_namespace: str = "default",
) -> int:
    """处理单个音频：ASR 转录 → 分块 → 嵌入 → 入库

    流程:
    1. 调用语音识别 LLM 对音频进行转录
    2. 将转录文本分块
    3. 嵌入后存入向量存储，元数据标注 modality="audio" 和原始文件路径

    参数:
        file_path: 音频文件路径
        store: 向量存储实例，None 时自动创建
        rag_namespace: 命名空间

    返回:
        入库的分块数量，失败时返回 0
    """
    processor = _get_multimodal_processor()
    result = processor.process_audio(file_path)
    if not result["text"]:
        print(f"[RAG] 音频 ASR 未产出文本: {file_path}")
        return 0

    chunk_content = result["text"]
    doc_id = hashlib.md5(f"{file_path}|{len(chunk_content)}".encode('utf-8')).hexdigest()
    content_hash = hashlib.md5(chunk_content.encode('utf-8')).hexdigest()
    chunk_id = hashlib.md5(f"{doc_id}|0|{len(chunk_content)}|{content_hash}".encode('utf-8')).hexdigest()

    chunk = {
        "id": chunk_id,
        "content": chunk_content,
        "metadata": {
            "source_path": file_path,
            "file_ext": os.path.splitext(file_path)[1].lower(),
            "doc_id": doc_id,
            "lang": "zh",
            "start": 0,
            "end": len(chunk_content),
            "content_hash": content_hash,
            "namespace": rag_namespace,
            "source": "rag",
            "external": True,
            "format": "asr_text",
            # ── 多模态特有元数据 ──
            "modality": "audio",
            "original_file_path": str(Path(file_path).absolute()),
            "mime_type": result["metadata"].get("mime_type", ""),
            "file_size": result["metadata"].get("file_size", 0),
        },
    }

    index_chunks(store=store, chunks=[chunk], rag_namespace=rag_namespace)
    print(f"[RAG] 音频已索引: {os.path.basename(file_path)} → {len(chunk_content)} 字符")
    return 1


def load_and_index_images(
    file_paths: List[str],
    store = None,
    rag_namespace: str = "default",
) -> int:
    """批量处理图片：遍历文件列表，逐个 OCR 后入库

    返回:
        成功入库的图片总数
    """
    if store is None:
        store = _create_default_vector_store()

    total = 0
    for path in file_paths:
        if not os.path.exists(path):
            print(f"[RAG] 图片文件不存在: {path}")
            continue
        try:
            n = index_image(file_path=path, store=store, rag_namespace=rag_namespace)
            total += n
        except Exception as e:
            print(f"[RAG] 图片索引失败 ({path}): {e}")

    print(f"[RAG] 批量图片索引完成: {total} 张")
    return total


def load_and_index_audio(
    file_paths: List[str],
    store = None,
    rag_namespace: str = "default",
) -> int:
    """批量处理音频：遍历文件列表，逐个 ASR 转录后入库

    返回:
        成功入库的音频总数
    """
    if store is None:
        store = _create_default_vector_store()

    total = 0
    for path in file_paths:
        if not os.path.exists(path):
            print(f"[RAG] 音频文件不存在: {path}")
            continue
        try:
            n = index_audio(file_path=path, store=store, rag_namespace=rag_namespace)
            total += n
        except Exception as e:
            print(f"[RAG] 音频索引失败 ({path}): {e}")

    print(f"[RAG] 批量音频索引完成: {total} 个")
    return total


def search_images(
    query: str,
    store = None,
    top_k: int = 8,
    rag_namespace: Optional[str] = None,
    score_threshold: Optional[float] = None,
) -> List[Dict]:
    """搜索图片知识库：在 modality="image" 的数据中做向量检索

    返回结果中 metadata["original_file_path"] 即为原始图片的绝对路径，
    可供 Agent 直接返回给用户查看。
    """
    return search_vectors(
        store=store, query=query, top_k=top_k,
        rag_namespace=rag_namespace, only_rag_data=False,
        modality="image", score_threshold=score_threshold,
    )


def search_audio(
    query: str,
    store = None,
    top_k: int = 8,
    rag_namespace: Optional[str] = None,
    score_threshold: Optional[float] = None,
) -> List[Dict]:
    """搜索音频知识库：在 modality="audio" 的数据中做向量检索

    返回结果中 metadata["original_file_path"] 即为原始音频的绝对路径，
    可供 Agent 直接返回给用户。
    """
    return search_vectors(
        store=store, query=query, top_k=top_k,
        rag_namespace=rag_namespace, only_rag_data=False,
        modality="audio", score_threshold=score_threshold,
    )


def _preprocess_markdown_for_embedding(text: str) -> str:
    """为嵌入质量做 Markdown 文本预处理

    去除 Markdown 标记符号（##、**、` 等），保留纯语义文本，
    避免冗余的格式化字符干扰嵌入向量的语义质量。
    """
    import re
    
    # Remove markdown headers symbols but keep the text
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    
    # Remove markdown links but keep the text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    
    # Remove markdown emphasis markers
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # bold
    text = re.sub(r'\*([^*]+)\*', r'\1', text)      # italic
    text = re.sub(r'`([^`]+)`', r'\1', text)        # inline code
    
    # Remove markdown code blocks but keep content
    text = re.sub(r'```[^\n]*\n([\s\S]*?)```', r'\1', text)
    
    # Remove excessive whitespace
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    
    return text.strip()


def _create_default_vector_store(dimension: int = None):
    """通过 VectorStoreManager 创建默认向量存储，支持 Zvec/Qdrant 等后端。"""
    if dimension is None:
        dimension = get_dimension(384)

    qdrant_url = os.getenv("QDRANT_URL")
    qdrant_api_key = os.getenv("QDRANT_API_KEY")

    return VectorStoreManager.get_instance(
        url=qdrant_url,
        api_key=qdrant_api_key,
        collection_name="hello_agents_rag_vectors",
        vector_size=dimension,
        distance="cosine"
    )


# Cache functions removed - using unified embedder with internal caching


def index_chunks(
    store = None,
    chunks: List[Dict] = None,
    cache_db: Optional[str] = None,
    batch_size: int = 64,
    rag_namespace: str = "default"
) -> None:
    """将文档分块嵌入并写入向量存储

    这是 RAG 管线的向量化入库入口。流程：
    1. 对每个分块的 Markdown 内容预处理（去除标记符号，保留语义文本）
    2. 分批调用嵌入模型生成向量（批次大小 batch_size）
    3. 批次失败时自动缩小批大小重试，带 2 秒冷却间隔
    4. 维度不匹配时自动填充或截断
    5. 将向量 + 元数据写入向量存储

    参数:
        store: 向量存储实例，为 None 时自动创建默认存储
        chunks: load_and_chunk_texts 输出的分块列表
        batch_size: 嵌入模型批处理大小（默认 64）
        rag_namespace: RAG 命名空间，用于标记数据归属
    """
    if not chunks:
        print("[RAG] 无分块需要索引")
        return

    embedder = get_text_embedder_model()
    dimension = get_dimension(384)

    if store is None:
        store = _create_default_vector_store(dimension)
        print(f"[RAG] 创建默认向量存储，维度={dimension}")

    # ── 步骤 1: 嵌入前预处理 ──
    # 去除 Markdown 标记符号（##、**、` 等），保留纯语义文本
    processed_texts = []
    for c in chunks:
        processed_content = _preprocess_markdown_for_embedding(c["content"])
        processed_texts.append(processed_content)

    print(f"[RAG] 向量嵌入开始: 文本数={len(processed_texts)} 批次大小={batch_size}")

    # ── 步骤 2: 分批嵌入 ──
    vecs: List[List[float]] = []
    for i in range(0, len(processed_texts), batch_size):
        part = processed_texts[i:i + batch_size]
        try:
            part_vecs = embedder.encode(part)

            # ── 向量格式归一化：统一为 List[List[float]] ──
            # embedder.encode 可能返回: numpy 数组、list、嵌套 list 等多种格式
            if not isinstance(part_vecs, list):
                if hasattr(part_vecs, "tolist"):
                    part_vecs = [part_vecs.tolist()]
                else:
                    part_vecs = [list(part_vecs)]
            else:
                if part_vecs and not isinstance(part_vecs[0], (list, tuple)) and hasattr(part_vecs[0], "__len__"):
                    normalized_vecs = []
                    for v in part_vecs:
                        if hasattr(v, "tolist"):
                            normalized_vecs.append(v.tolist())
                        else:
                            normalized_vecs.append(list(v))
                    part_vecs = normalized_vecs
                elif part_vecs and not isinstance(part_vecs[0], (list, tuple)):
                    if hasattr(part_vecs, "tolist"):
                        part_vecs = [part_vecs.tolist()]
                    else:
                        part_vecs = [list(part_vecs)]

            for v in part_vecs:
                try:
                    if hasattr(v, "tolist"):
                        v = v.tolist()
                    v_norm = [float(x) for x in v]
                    # ── 维度对齐 ──
                    if len(v_norm) != dimension:
                        print(f"[RAG] 向量维度异常: 期望{dimension}, 实际{len(v_norm)}")
                        if len(v_norm) < dimension:
                            v_norm.extend([0.0] * (dimension - len(v_norm)))
                        else:
                            v_norm = v_norm[:dimension]
                    vecs.append(v_norm)
                except Exception as e:
                    print(f"[RAG] 向量转换失败: {e}，使用零向量替代")
                    vecs.append([0.0] * dimension)

        except Exception as e:
            # ── 批次失败 → 缩小批大小重试 ──
            print(f"[RAG] 批次 {i} 嵌入失败: {e}")
            print(f"[RAG] 尝试缩小批次重试...")

            success = False
            for j in range(0, len(part), 8):
                small_part = part[j:j + 8]
                try:
                    import time
                    time.sleep(2)

                    small_vecs = embedder.encode(small_part)
                    if isinstance(small_vecs, list) and small_vecs and not isinstance(small_vecs[0], list):
                        small_vecs = [small_vecs]

                    for v in small_vecs:
                        if hasattr(v, "tolist"):
                            v = v.tolist()
                        try:
                            v_norm = [float(x) for x in v]
                            if len(v_norm) != dimension:
                                if len(v_norm) < dimension:
                                    v_norm.extend([0.0] * (dimension - len(v_norm)))
                                else:
                                    v_norm = v_norm[:dimension]
                            vecs.append(v_norm)
                            success = True
                        except Exception as e2:
                            print(f"[RAG] 小批次向量转换失败: {e2}")
                            vecs.append([0.0] * dimension)
                except Exception as e2:
                    print(f"[RAG] 小批次 {j // 8} 仍然失败: {e2}")
                    for _ in range(len(small_part)):
                        vecs.append([0.0] * dimension)

            if not success:
                print(f"[RAG] 批次 {i} 完全失败，全部使用零向量")

        print(f"[RAG] 嵌入进度: {min(i + batch_size, len(processed_texts))}/{len(processed_texts)}")

    # ── 步骤 3: 构建元数据并写入向量存储 ──
    metas: List[Dict] = []
    ids: List[str] = []
    for ch in chunks:
        meta = {
            "memory_id": ch["id"],
            "user_id": "rag_user",
            "memory_type": "rag_chunk",
            "content": ch["content"],
            "data_source": "rag_pipeline",
            "rag_namespace": rag_namespace,
            "is_rag_data": "true",
        }
        meta.update(ch.get("metadata", {}))
        metas.append(meta)
        ids.append(ch["id"])

    print(f"[RAG] 向量写入开始: 共{len(vecs)}条向量")
    success = store.add_vectors(vectors=vecs, metadata=metas, ids=ids)
    if success:
        print(f"[RAG] 向量写入完成: {len(vecs)}条向量已入库")
    else:
        print(f"[RAG] 向量写入失败")
        raise RuntimeError("向量索引写入失败，请检查向量存储连接状态")


def embed_query(query: str) -> List[float]:
    """将查询文本编码为向量

    使用统一嵌入模型编码搜索查询。自动处理：
    - numpy 数组 → Python 列表转换
    - 嵌套列表展平（提取第一个向量）
    - 维度对齐（填充或截断至目标维度）

    失败时返回全零向量作为兜底（确保搜索流程不中断）。
    """
    embedder = get_text_embedder_model()
    dimension = get_dimension(384)
    try:
        vec = embedder.encode(query)

        if hasattr(vec, "tolist"):
            vec = vec.tolist()

        # 嵌套列表 → 提取内层向量
        if isinstance(vec, list) and vec and isinstance(vec[0], (list, tuple)):
            vec = vec[0]

        result = [float(x) for x in vec]

        if len(result) != dimension:
            print(f"[RAG] 查询向量维度异常: 期望{dimension}, 实际{len(result)}")
            if len(result) < dimension:
                result.extend([0.0] * (dimension - len(result)))
            else:
                result = result[:dimension]

        return result
    except Exception as e:
        print(f"[RAG] 查询嵌入失败: {e}，使用零向量兜底")
        return [0.0] * dimension


def search_vectors(
    store = None,
    query: str = "",
    top_k: int = 8,
    rag_namespace: Optional[str] = None,
    only_rag_data: bool = True,
    modality: Optional[str] = None,
    score_threshold: Optional[float] = None
) -> List[Dict]:
    """向量检索：将查询编码后在向量存储中搜索最相似的 RAG 分块

    自动构建过滤条件只命中 RAG 数据（memory_type=rag_chunk），
    并通过 rag_namespace 实现多项目命名空间隔离。

    参数:
        store: 向量存储实例，None 时自动创建
        query: 搜索查询文本
        top_k: 返回结果数（默认 8）
        rag_namespace: 命名空间过滤，None 时不按命名空间过滤
        only_rag_data: 是否仅搜索 RAG 数据（默认 True）
        modality: 模态过滤（"text"/"image"/"audio"），None 时不限制
        score_threshold: 最低相似度阈值，None 时不限制

    返回:
        [{"id": "...", "score": 0.85, "metadata": {...}}, ...]
    """
    if not query:
        return []

    if store is None:
        store = _create_default_vector_store()

    qv = embed_query(query)

    # ── 构建 RAG 数据过滤条件 ──
    where = {"memory_type": "rag_chunk"}
    if only_rag_data:
        where["is_rag_data"] = "true"
        where["data_source"] = "rag_pipeline"
    if rag_namespace:
        where["rag_namespace"] = rag_namespace
    if modality:
        where["modality"] = modality

    try:
        return store.search_similar(
            query_vector=qv,
            limit=top_k,
            score_threshold=score_threshold,
            where=where
        )
    except Exception as e:
        print(f"[RAG] 向量搜索失败: {e}")
        return []


def _prompt_mqe(query: str, n: int) -> List[str]:
    """MQE（多查询扩展）：用 LLM 生成原查询的多个语义等价表述

    将原查询 + 扩展查询一起检索，提高召回覆盖率。
    例如 "Python 性能优化" 可能扩展为 "如何加速 Python 代码"、"Python 程序 profiling 方法" 等。
    """
    try:
        from ...core.llm import HelloAgentsLLM
        llm = HelloAgentsLLM()
        prompt = [
            {"role": "system", "content": "你是检索查询扩展助手。生成语义等价或互补的多样化查询。使用中文，简短，避免标点。"},
            {"role": "user", "content": f"原始查询：{query}\n请给出{n}个不同表述的查询，每行一个。"}
        ]
        text = llm.invoke(prompt)
        lines = [ln.strip("- \t") for ln in (text or "").splitlines()]
        outs = [ln for ln in lines if ln]
        return outs[:n] or [query]
    except Exception:
        return [query]


def _prompt_hyde(query: str) -> Optional[str]:
    """HyDE（假设文档嵌入）：让 LLM 先写一段假设的答案，用这段答案做向量检索

    原理：答案性段落的向量分布更接近知识库中的文档，比直接检索问题效果更好。
    适用于用户问题较短、缺乏上下文的情况。
    """
    try:
        from ...core.llm import HelloAgentsLLM
        llm = HelloAgentsLLM()
        prompt = [
            {"role": "system", "content": "根据用户问题，先写一段可能的答案性段落，用于向量检索的查询文档（不要分析过程）。"},
            {"role": "user", "content": f"问题：{query}\n请直接写一段中等长度、客观、包含关键术语的段落。"}
        ]
        return llm.invoke(prompt)
    except Exception:
        return None


def search_vectors_expanded(
    store = None,
    query: str = "",
    top_k: int = 8,
    rag_namespace: Optional[str] = None,
    only_rag_data: bool = True,
    score_threshold: Optional[float] = None,
    enable_mqe: bool = False,
    mqe_expansions: int = 2,
    enable_hyde: bool = False,
    candidate_pool_multiplier: int = 4,
) -> List[Dict]:
    """增强搜索：多查询扩展 + HyDE 假设文档

    在基础向量搜索之上支持两种召回增强策略：
    1. MQE（多查询扩展）：用 LLM 生成多个语义等价查询，分别检索后合并去重
    2. HyDE（假设文档嵌入）：用 LLM 先生成"可能的答案段落"，再以此为查询进行检索

    候选池 = top_k * candidate_pool_multiplier，按扩展查询数量均分。
    命中结果按记忆 ID 合并，取每个 ID 的最高分。

    返回:
        按分数降序排列的 top_k 条结果列表
    """
    if not query:
        return []

    if store is None:
        store = _create_default_vector_store()

    # ── 构建扩展查询列表 ──
    expansions: List[str] = [query]

    if enable_mqe and mqe_expansions > 0:
        expansions.extend(_prompt_mqe(query, mqe_expansions))
    if enable_hyde:
        hyde_text = _prompt_hyde(query)
        if hyde_text:
            expansions.append(hyde_text)

    # 去重
    uniq: List[str] = []
    for e in expansions:
        if e and e not in uniq:
            uniq.append(e)
    expansions = uniq[: max(1, len(uniq))]

    # ── 候选池分配 ──
    pool = max(top_k * candidate_pool_multiplier, 20)
    per = max(1, pool // max(1, len(expansions)))

    # ── RAG 数据过滤条件 ──
    where = {"memory_type": "rag_chunk"}
    if only_rag_data:
        where["is_rag_data"] = "true"
        where["data_source"] = "rag_pipeline"
    if rag_namespace:
        where["rag_namespace"] = rag_namespace

    # ── 每个扩展查询独立检索 → 合并去重（保留每个 ID 的最高分） ──
    agg: Dict[str, Dict] = {}
    for q in expansions:
        qv = embed_query(q)
        hits = store.search_similar(query_vector=qv, limit=per, score_threshold=score_threshold, where=where)
        for h in hits:
            mid = h.get("metadata", {}).get("memory_id", h.get("id"))
            s = float(h.get("score", 0.0))
            # 同一 ID 保留最高分数
            if mid not in agg or s > float(agg[mid].get("score", 0.0)):
                agg[mid] = h

    merged = list(agg.values())
    merged.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
    return merged[:top_k]


def _try_load_cross_encoder(model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
    """尝试加载 Cross-Encoder 重排序模型

    Cross-Encoder 同时对 (query, document) 打分，精度远高于双塔向量检索，
    但速度较慢，通常用于对 top-k 候选做精细重排序。
    """
    try:
        from sentence_transformers import CrossEncoder
        return CrossEncoder(model_name)
    except Exception:
        return None


def rerank_with_cross_encoder(query: str, items: List[Dict], model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2", top_k: int = 10) -> List[Dict]:
    """Cross-Encoder 重排序：对向量检索的 top-k 候选做精细相关度打分

    原理：拼接 (query + 每个候选文本) 为一个 pair，Cross-Encoder 直接输出相关度分数。
    这比向量的余弦距离更准确，但速度慢 N 倍，所以只在候选集较小 (top-k) 上使用。
    """
    ce = _try_load_cross_encoder(model_name)
    if ce is None or not items:
        return items[:top_k]
    pairs = [[query, it.get("content", "")] for it in items]
    try:
        scores = ce.predict(pairs)
        for it, s in zip(items, scores):
            it["rerank_score"] = float(s)  # Cross-Encoder 重排序分数
        items.sort(key=lambda x: x.get("rerank_score", x.get("score", 0.0)), reverse=True)
        return items[:top_k]
    except Exception:
        return items[:top_k]


def compute_graph_signals_from_pool(vector_hits: List[Dict], same_doc_weight: float = 1.0, proximity_weight: float = 1.0, proximity_window_chars: int = 1600) -> Dict[str, float]:
    """计算基于文档结构的图信号分数

    利用文档内分块的位置关系产生两个信号：
    1. 同文档密度：同一文档中被命中的分块越多，密度分数越高
    2. 邻近性：文档内位置越靠近的分块互相增强（在 proximity_window_chars 窗口内）

    最终信号 = same_doc_weight * 密度 + proximity_weight * 邻近得分，归一化到 [0, 1]。

    返回:
        {memory_id: 图信号分数, ...}
    """
    # ── 按文档 ID 分组 ──
    by_doc: Dict[str, List[Dict]] = {}
    for h in vector_hits:
        meta = h.get("metadata", {})
        did = meta.get("doc_id")
        if not did:
            did = meta.get("memory_id") or h.get("id")
        by_doc.setdefault(did, []).append(h)

    # ── 同文档密度：命中分块数 / 最大命中数 ──
    doc_counts = {d: len(arr) for d, arr in by_doc.items()}
    max_count = max(doc_counts.values()) if doc_counts else 1

    # ── 计算每个命中分块的邻近性得分 ──
    graph_signal: Dict[str, float] = {}
    for did, arr in by_doc.items():
        arr.sort(key=lambda x: x.get("metadata", {}).get("start", 0))
        density = doc_counts.get(did, 1) / max_count

        for i, h in enumerate(arr):
            mid = h.get("metadata", {}).get("memory_id", h.get("id"))
            pos_i = h.get("metadata", {}).get("start", 0)
            prox_acc = 0.0

            # ── 向左扫描邻近分块 ──
            j = i - 1
            while j >= 0:
                pos_j = arr[j].get("metadata", {}).get("start", 0)
                dist = abs(pos_i - pos_j)
                if dist > proximity_window_chars:
                    break
                prox_acc += max(0.0, 1.0 - (dist / max(1.0, float(proximity_window_chars))))
                j -= 1
            # ── 向右扫描邻近分块 ──
            j = i + 1
            while j < len(arr):
                pos_j = arr[j].get("metadata", {}).get("start", 0)
                dist = abs(pos_i - pos_j)
                if dist > proximity_window_chars:
                    break
                prox_acc += max(0.0, 1.0 - (dist / max(1.0, float(proximity_window_chars))))
                j += 1

            score = same_doc_weight * density + proximity_weight * prox_acc
            graph_signal[mid] = graph_signal.get(mid, 0.0) + score

    # ── 归一化到 [0, 1] ──
    if graph_signal:
        max_v = max(graph_signal.values())
        if max_v > 0:
            for k in list(graph_signal.keys()):
                graph_signal[k] = graph_signal[k] / max_v
    return graph_signal


def rank(vector_hits: List[Dict], graph_signals: Optional[Dict[str, float]] = None, w_vector: float = 0.7, w_graph: float = 0.3) -> List[Dict]:
    """融合排序：向量检索分数 + 图结构信号

    最终分数 = w_vector * 向量相似度 + w_graph * 图信号分数
    默认权重 7:3，偏向向量语义匹配，图信号作为结构性补充。
    """
    items: List[Dict] = []
    graph_signals = graph_signals or {}
    for h in vector_hits:
        mid = h.get("metadata", {}).get("memory_id", h.get("id"))
        g = float(graph_signals.get(mid, 0.0))
        v = float(h.get("score", 0.0))
        score = w_vector * v + w_graph * g
        items.append({
            "memory_id": mid,
            "score": score,
            "vector_score": v,
            "graph_score": g,
            "content": h.get("metadata", {}).get("content", ""),
            "metadata": h.get("metadata", {}),
        })
    items.sort(key=lambda x: x["score"], reverse=True)
    return items


def merge_snippets(ranked_items: List[Dict], max_chars: int = 1200) -> str:
    """将排序后的分块合并为上下文文本（按字符数截断）

    按排序顺序逐个拼接分块内容，达到 max_chars 上限时停止。
    最后一个分块允许部分截断以确保不超限。
    """
    out: List[str] = []
    total = 0
    for it in ranked_items:
        text = it.get("content", "").strip()
        if not text:
            continue
        if total + len(text) > max_chars:
            remain = max_chars - total
            if remain <= 0:
                break
            out.append(text[:remain])
            total += remain
            break
        out.append(text)
        total += len(text)
    return "\n\n".join(out)


def expand_neighbors_from_pool(selected: List[Dict], pool: List[Dict], neighbors: int = 1, max_additions: int = 5) -> List[Dict]:
    """扩展邻近分块：将选中分块在同一文档中的前后邻居加入结果

    原理：向量检索可能漏掉被选分块前后的相关段落。这里的策略是：
    1. 将候选池按文档 ID 分组，内部按原始位置排序
    2. 对每个选中的分块，向前后各取 neighbors 个相邻分块
    3. 最多追加 max_additions 个分块
    4. 扩展后按分数降序排列

    这能提升上下文完整性，尤其对需要前后文理解的问答场景有帮助。
    """
    if not selected or not pool or neighbors <= 0:
        return selected

    # ── 候选池按文档分组并按位置排序 ──
    by_doc: Dict[str, List[Dict]] = {}
    for it in pool:
        meta = it.get("metadata", {})
        did = meta.get("doc_id")
        if not did:
            continue
        by_doc.setdefault(did, []).append(it)
    for did, arr in by_doc.items():
        arr.sort(key=lambda x: (x.get("metadata", {}).get("start", 0)))

    selected_ids = set(it.get("memory_id") for it in selected)
    additions: List[Dict] = []

    for it in selected:
        meta = it.get("metadata", {})
        did = meta.get("doc_id")
        if not did or did not in by_doc:
            continue
        arr = by_doc[did]

        # ── 找到该分块在文档中的索引 ──
        try:
            idx = next(i for i, x in enumerate(arr) if x.get("memory_id") == it.get("memory_id"))
        except StopIteration:
            continue

        # ── 向前后取邻居 ──
        for offset in range(1, neighbors + 1):
            for j in (idx - offset, idx + offset):
                if 0 <= j < len(arr):
                    cand = arr[j]
                    mid = cand.get("memory_id")
                    if mid not in selected_ids:
                        additions.append(cand)
                        selected_ids.add(mid)
                        if len(additions) >= max_additions:
                            break
            if len(additions) >= max_additions:
                break
        if len(additions) >= max_additions:
            break

    # ── 合并后按分数排序 ──
    extended = list(selected) + additions
    extended.sort(key=lambda x: (x.get("rerank_score", x.get("score", 0.0))), reverse=True)
    return extended


def merge_snippets_grouped(ranked_items: List[Dict], max_chars: int = 1200, include_citations: bool = True) -> str:
    """按文档分组合并上下文（带引用标注）

    与 merge_snippets 的区别：
    1. 按文档 ID 分组，文档之间按累计分数排序
    2. 文档内部按原始位置排序（保持上下文连贯性）
    3. 每个分块附加引用编号 [N]
    4. 末尾生成参考文献列表（来源路径 + 位置区间 + 标题路径）

    这样 LLM 能看到结构化的上下文，并知道每段信息的来源。
    """
    # ── 按文档分组并计算各文档累计分数 ──
    by_doc: Dict[str, List[Dict]] = {}
    doc_score: Dict[str, float] = {}
    for it in ranked_items:
        meta = it.get("metadata", {})
        did = meta.get("doc_id") or meta.get("source_path") or "unknown"
        by_doc.setdefault(did, []).append(it)
        doc_score[did] = doc_score.get(did, 0.0) + float(it.get("score", 0.0))

    ordered_docs = sorted(by_doc.keys(), key=lambda d: doc_score.get(d, 0.0), reverse=True)
    # 文档内按原文位置排序
    for d in ordered_docs:
        by_doc[d].sort(key=lambda x: (x.get("metadata", {}).get("start", 0)))

    out: List[str] = []
    citations: List[Dict] = []
    total = 0
    cite_index = 1

    for did in ordered_docs:
        parts = by_doc[did]
        for it in parts:
            text = (it.get("content", "") or "").strip()
            if not text:
                continue

            suffix = f" [{cite_index}]" if include_citations else ""
            need = len(text) + (len(suffix) if suffix else 0)

            if total + need > max_chars:
                remain = max_chars - total
                if remain <= 0:
                    break
                clipped = text[: max(0, remain - len(suffix))]
                if clipped:
                    out.append(clipped + suffix)
                    total += len(clipped) + len(suffix)
                    if include_citations:
                        m = it.get("metadata", {})
                        citations.append({
                            "index": cite_index,
                            "source_path": m.get("source_path"),
                            "doc_id": m.get("doc_id"),
                            "start": m.get("start"),
                            "end": m.get("end"),
                            "heading_path": m.get("heading_path"),
                        })
                        cite_index += 1
                break

            out.append(text + suffix)
            total += need
            if include_citations:
                m = it.get("metadata", {})
                citations.append({
                    "index": cite_index,
                    "source_path": m.get("source_path"),
                    "doc_id": m.get("doc_id"),
                    "start": m.get("start"),
                    "end": m.get("end"),
                    "heading_path": m.get("heading_path"),
                })
                cite_index += 1

        if total >= max_chars:
            break

    merged = "\n\n".join(out)
    if include_citations and citations:
        lines: List[str] = [merged, "", "参考文献:"]
        for c in citations:
            loc = ""
            if c.get("start") is not None and c.get("end") is not None:
                loc = f" (字符 {c['start']}-{c['end']})"
            hp = f" - {c['heading_path']}" if c.get("heading_path") else ""
            sp = c.get("source_path") or c.get("doc_id") or "未知来源"
            lines.append(f"[{c['index']}] {sp}{loc}{hp}")
        return "\n".join(lines)
    return merged


def compress_ranked_items(ranked_items: List[Dict], enable_compression: bool = True, max_per_doc: int = 2, join_gap: int = 200) -> List[Dict]:
    """压缩排序结果：合并相邻分块 + 限制每文档分段数

    两项策略：
    1. 相邻合并：同一文档中位置间隔 < join_gap 字符的分块合并为一个（减少冗余）
    2. 每文档上限：每个文档最多保留 max_per_doc 个分段（避免单一文档占据全部上下文）

    关闭压缩 (enable_compression=False) 时原样返回。
    """
    if not enable_compression:
        return ranked_items

    by_doc_count: Dict[str, int] = {}   # 每个文档已保留的分段数
    last_by_doc: Dict[str, Dict] = {}   # 每个文档最后加入的分段（用于相邻检测）
    new_items: List[Dict] = []

    for it in ranked_items:
        meta = it.get("metadata", {})
        did = meta.get("doc_id") or meta.get("source_path") or "unknown"
        start = int(meta.get("start") or 0)
        end = int(meta.get("end") or (start + len(it.get("content", "") or "")))

        if did not in last_by_doc:
            # ── 该文档的第一个分块，直接加入 ──
            last_by_doc[did] = it
            by_doc_count[did] = 1
            new_items.append(it)
            continue

        last = last_by_doc[did]
        lmeta = last.get("metadata", {})
        lstart = int(lmeta.get("start") or 0)
        lend = int(lmeta.get("end") or (lstart + len(last.get("content", "") or "")))

        # ── 相邻合并条件：间距在 join_gap 字符内且位置连续 ──
        if start - lend <= join_gap and start >= lstart:
            merged_text = (last.get("content", "") or "").strip()
            add_text = (it.get("content", "") or "").strip()
            if add_text:
                merged_text = f"{merged_text}\n\n{add_text}" if merged_text else add_text
                last["content"] = merged_text
                lmeta["end"] = max(lend, end)
                # 合并后取较高分数
                try:
                    last["score"] = max(float(last.get("score", 0.0)), float(it.get("score", 0.0)))
                except Exception:
                    pass
            last_by_doc[did] = last
        else:
            # ── 间距较大，按是否超过每文档上限决定是否加入 ──
            cnt = by_doc_count.get(did, 0)
            if cnt >= max_per_doc:
                continue  # 超过上限，丢弃
            new_items.append(it)
            last_by_doc[did] = it
            by_doc_count[did] = cnt + 1

    return new_items


def tldr_summarize(text: str, bullets: int = 3) -> Optional[str]:
    try:
        if not text or len(text.strip()) == 0:
            return None
        from ...core.llm import HelloAgentsLLM
        llm = HelloAgentsLLM()
        prompt = [
            {"role": "system", "content": "请将以下内容概括为简洁的要点列表（最多3-5条），用中文，避免重复，突出关键信息。"},
            {"role": "user", "content": f"请用 {max(1, min(5, int(bullets)))} 条要点总结：\n\n{text}"},
        ]
        out = llm.invoke(prompt)
        return out
    except Exception:
        return None


# ==================
# High-level RAG Pipeline API
# ==================

def create_rag_pipeline(
    qdrant_url: Optional[str] = None,
    qdrant_api_key: Optional[str] = None,
    collection_name: str = "hello_agents_rag_vectors",
    rag_namespace: str = "default"
) -> Dict[str, Any]:
    """通过 VectorStoreManager 创建 RAG 管线，自动适配 Zvec/Qdrant 等后端。"""
    if not is_full_memory_enabled():
        raise RuntimeError(full_memory_disabled_message())
    dimension = get_dimension(384)

    store = VectorStoreManager.get_instance(
        url=qdrant_url,
        api_key=qdrant_api_key,
        collection_name=collection_name,
        vector_size=dimension,
        distance="cosine"
    )
    
    def add_documents(file_paths: List[str], chunk_size: int = 800, chunk_overlap: int = 100):
        """向 RAG 管线添加文档：加载 → 分块 → 嵌入 → 入库"""
        chunks = load_and_chunk_texts(
            paths=file_paths,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            namespace=rag_namespace,
            source_label="rag"
        )
        index_chunks(
            store=store,
            chunks=chunks,
            rag_namespace=rag_namespace
        )
        return len(chunks)

    def search(query: str, top_k: int = 8, score_threshold: Optional[float] = None):
        """在 RAG 知识库中搜索"""
        return search_vectors(
            store=store,
            query=query,
            top_k=top_k,
            rag_namespace=rag_namespace,
            score_threshold=score_threshold
        )

    def search_advanced(
        query: str,
        top_k: int = 8,
        enable_mqe: bool = False,
        enable_hyde: bool = False,
        score_threshold: Optional[float] = None
    ):
        """增强搜索：支持多查询扩展 (MQE) 和假设文档嵌入 (HyDE)"""
        return search_vectors_expanded(
            store=store,
            query=query,
            top_k=top_k,
            rag_namespace=rag_namespace,
            enable_mqe=enable_mqe,
            enable_hyde=enable_hyde,
            score_threshold=score_threshold
        )

    def get_stats():
        """获取 RAG 管线统计信息"""
        return store.get_collection_stats()
    
    return {
        "store": store,
        "namespace": rag_namespace,
        "add_documents": add_documents,
        "search": search,
        "search_advanced": search_advanced,
        "get_stats": get_stats
    }
