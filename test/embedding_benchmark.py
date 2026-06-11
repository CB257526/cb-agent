"""嵌入模型性能基准测试"""
import os
import time
from memory.embedding import get_text_embedder_model


def benchmark():
    os.environ.setdefault("CBAGENT_ENABLE_FULL_MEMORY", "1")
    model = get_text_embedder_model()
    print(f"模型: {model.model_name}")
    print(f"维度: {model.dimension}")
    print("-" * 50)

    # 1. 预热（消除首次调用冷启动偏差）
    print("预热中...")
    model.encode("warmup")
    print("预热完成\n")

    # 2. 单文本延迟
    texts_single = ["你好世界"]
    start = time.perf_counter()
    model.encode(texts_single[0])
    elapsed = (time.perf_counter() - start) * 1000
    print(f"单条编码延迟: {elapsed:.1f} ms")

    # 3. 单文本多次调用平均
    n = 10
    start = time.perf_counter()
    for _ in range(n):
        model.encode("测试文本")
    total = (time.perf_counter() - start) * 1000
    print(f"单条编码 x{n} 次: 总耗时 {total:.0f} ms, 平均 {total/n:.1f} ms/次")

    # 4. 批量编码
    batch_sizes = [1, 5, 10, 20, 50]
    print(f"\n批量编码测试:")
    for size in batch_sizes:
        texts = [f"这是一条测试文本，用来测试嵌入模型的编码速度。_{i}" for i in range(size)]
        start = time.perf_counter()
        vecs = model.encode(texts)
        elapsed = (time.perf_counter() - start) * 1000
        per_text = elapsed / size
        print(f"  批量 {size:3d} 条: {elapsed:7.1f} ms ({per_text:.1f} ms/条)")

    # 5. 不同文本长度
    print(f"\n不同文本长度测试:")
    lengths = {
        "短 (5字)": "你好世界，AI",
        "中 (50字)": "人工智能是计算机科学的一个分支，它企图了解智能的实质，并生产出一种新的能以人类智能相似的方式做出反应的智能机器。" * 1,
        "长 (200字)": "人工智能是计算机科学的一个分支。" * 10,
    }
    for name, text in lengths.items():
        start = time.perf_counter()
        model.encode(text)
        elapsed = (time.perf_counter() - start) * 1000
        print(f"  {name}: {elapsed:.1f} ms (长度={len(text)})")


if __name__ == "__main__":
    benchmark()
