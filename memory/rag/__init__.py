"""RAG（检索增强生成）模块

整合 GraphRAG 能力，通过统一管理器适配多后端：

- 文档加载/分块：PDF、Office、图片、音频等多格式 → Markdown → 智能分段
- 向量存储：VectorStoreManager 统一管理，默认 Zvec，可选 Qdrant
- 图存储：GraphStoreManager 统一管理，默认 SQLite，可选 Neo4j
- 检索/排序/合并：多路召回、图信号增强、Cross-Encoder 重排序
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
    embed_query,
    search_vectors,
    rank,
    merge_snippets,
    rerank_with_cross_encoder,
    expand_neighbors_from_pool,
    compute_graph_signals_from_pool,
    merge_snippets_grouped,
    search_vectors_expanded,
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
    "embed_query",
    "search_vectors",
    "rank",
    "merge_snippets",
    "rerank_with_cross_encoder",
    "expand_neighbors_from_pool",
    "compute_graph_signals_from_pool",
    "merge_snippets_grouped",
    "search_vectors_expanded",
    "compress_ranked_items",
    "tldr_summarize",
]
