"""RAG（检索增强生成）模块 — 多模态支持

通过统一管理器适配多后端，支持文本/图片/音频三模态：

文本:
  - load_and_chunk_texts / index_chunks: 文档加载 → 分块 → 嵌入 → 入库
  - search_vectors: 向量检索

图片:
  - index_image / load_and_index_images: OCR 识别 → 嵌入 → 入库
  - search_images: 图片知识库检索

音频:
  - index_audio / load_and_index_audio: ASR 转录 → 嵌入 → 入库
  - search_audio: 音频知识库检索

存储后端:
  - 向量: VectorStoreManager (默认 Zvec, 可选 Qdrant)
  - 图: GraphStoreManager (默认 SQLite, 可选 Neo4j)
"""

from ..embedding import (
    EmbeddingModel,
    LocalTransformerEmbedding,
    TFIDFEmbedding,
    create_embedding_model,
    create_embedding_model_with_fallback,
)
from ..storage.vector_store_manager import VectorStoreManager
from ..storage.graph_store_manager import GraphStoreManager
from .document import Document, DocumentProcessor
from .pipeline import (
    create_rag_pipeline,
    load_and_chunk_texts,
    build_graph_from_chunks,
    index_chunks,
    index_image,
    index_audio,
    load_and_index_images,
    load_and_index_audio,
    embed_query,
    search_vectors,
    search_images,
    search_audio,
    search_vectors_expanded,
    rank,
    merge_snippets,
    rerank_with_cross_encoder,
    expand_neighbors_from_pool,
    compute_graph_signals_from_pool,
    merge_snippets_grouped,
    compress_ranked_items,
    tldr_summarize,
)

SentenceTransformerEmbedding = LocalTransformerEmbedding
HuggingFaceEmbedding = LocalTransformerEmbedding

__all__ = [
    "EmbeddingModel",
    "LocalTransformerEmbedding",
    "SentenceTransformerEmbedding",
    "HuggingFaceEmbedding",
    "TFIDFEmbedding",
    "create_embedding_model",
    "create_embedding_model_with_fallback",
    "VectorStoreManager",
    "GraphStoreManager",
    "Document",
    "DocumentProcessor",
    "create_rag_pipeline",
    "load_and_chunk_texts",
    "build_graph_from_chunks",
    "index_chunks",
    "index_image",
    "index_audio",
    "load_and_index_images",
    "load_and_index_audio",
    "embed_query",
    "search_vectors",
    "search_images",
    "search_audio",
    "search_vectors_expanded",
    "rank",
    "merge_snippets",
    "rerank_with_cross_encoder",
    "expand_neighbors_from_pool",
    "compute_graph_signals_from_pool",
    "merge_snippets_grouped",
    "compress_ranked_items",
    "tldr_summarize",
]
