import os

from memory.embedding import get_text_embedder_model,create_embedding_model

if __name__ == "__main__":
    os.environ.setdefault("CBAGENT_ENABLE_FULL_MEMORY", "1")
    model = get_text_embedder_model()
    print(model)
    print(f"模型维度: {model.dimension}")
    # 测试encode方法
    vec = model.encode("你好")
    print(vec)
    print(f"向量维度: {vec.shape}")
