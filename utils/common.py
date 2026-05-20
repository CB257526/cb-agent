import tiktoken

def count_tokens(text: str, model_name: str = "gpt-4o") -> int:
    # 1. 根据模型获取对应的编码器
    # 如果找不到对应模型，tiktoken 会自动回退到默认编码
    try:
        encoding = tiktoken.encoding_for_model(model_name)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
        
    # 2. 编码并计算长度
    tokens = encoding.encode(text)
    return len(tokens)