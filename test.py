from tools.tool import Tool
from tools.toolRegistry import ToolRegistry
from tools.toolParameter import ToolParameter
from tools.tools.search import SearchTool
from tools.mcp_tools.mcptool import MCPTool
import json
from agent.cb_agents import CbAgentsLLM
# toolRegistry = ToolRegistry()
# toolRegistry.register_tool(SearchTool())
# print(toolRegistry.get_tools_description())
# print(json.dumps(toolRegistry.get_tools_description_openai_schema(), ensure_ascii=False, indent=2))

# from tools.mcp_tools.mcptools_add import load_mcp_tools
# tools = load_mcp_tools()
# print(tools)


# import tiktoken

# def count_tokens(text: str, model_name: str = "gpt-4o") -> int:
#     # 1. 根据模型获取对应的编码器
#     # 如果找不到对应模型，tiktoken 会自动回退到默认编码
#     try:
#         encoding = tiktoken.encoding_for_model(model_name)
#     except KeyError:
#         encoding = tiktoken.get_encoding("cl100k_base")
        
#     # 2. 编码并计算长度
#     tokens = encoding.encode(text)
#     return len(tokens)

# # 测试
# text = "你好，Gemini！计算 Token 是 NLP 的基础步骤。"
# num_tokens = count_tokens(text)
# print(f"Token 数量: {num_tokens}")


# import tiktoken
# import time
# import random
# import string

# def benchmark_tiktoken(model_name="gpt-4o", text_size_mb=1):
#     """
#     测试 tiktoken 的性能表现
#     """
#     # 1. 准备数据：生成指定大小的随机文本
#     print(f"--- 正在准备约 {text_size_mb}MB 的测试文本 ---")
#     chars_per_mb = 1024 * 1024
#     # 生成随机英文单词和空格的组合
#     raw_text = ''.join(random.choices(string.ascii_letters + " ", k=int(chars_per_mb * text_size_mb)))
    
#     # 2. 初始化编码器（第一次运行会较慢，因为要加载词表）
#     enc = tiktoken.encoding_for_model(model_name)
    
#     # 3. 开始压力测试
#     print(f"开始测试模型: {model_name} ({enc.name})")
    
#     start_time = time.perf_counter()
#     tokens = enc.encode(raw_text)
#     end_time = time.perf_counter()
    
#     # 4. 计算指标
#     duration_ms = (end_time - start_time) * 1000
#     token_count = len(tokens)
#     throughput = token_count / (end_time - start_time)
    
#     print("-" * 40)
#     print(f"处理文本长度: {len(raw_text) / 1024:.2f} KB")
#     print(f"生成 Token 数: {token_count}")
#     print(f"消耗总时间: {duration_ms:.2f} 毫秒 (ms)")
#     print(f"平均吞吐量: {throughput:,.0f} tokens/sec")
#     print("-" * 40)

# if __name__ == "__main__":
#     # 运行测试
#     benchmark_tiktoken()



# from sklearn.feature_extraction.text import TfidfVectorizer
# from sklearn.metrics.pairwise import cosine_similarity
# import numpy as np

# # 准备文档
# documents = ["手机"] + ["手机是一个电子设备", "手机可以用于打电话", "手机可以用于发送短信","华为手机是一个品牌","小米手机"]

# # TF-IDF向量化
# vectorizer = TfidfVectorizer(stop_words=None, lowercase=True)
# tfidf_matrix = vectorizer.fit_transform(documents)
# print(tfidf_matrix)
# # 计算相似度
# query_vector = tfidf_matrix[0:1]
# doc_vectors = tfidf_matrix[1:]
# print(query_vector)
# print(doc_vectors)

# similarities = cosine_similarity(query_vector, doc_vectors).flatten()
# print(similarities)



# from agent.cb_agent_basic import CbAgentsLLMBasic
# agent = CbAgentsLLMBasic()
# print(agent.ask(prompt="你好,你是谁？"))


# from markitdown import MarkItDown
# md = MarkItDown()
# result = md.convert(r"C:\Users\cb135\Desktop\cbAgent\source\屏幕截图 2026-05-26 164742.png")
# print(result.text_content)

from skills import SkillManager
manager = SkillManager()
print(manager.build_skills_overview())
