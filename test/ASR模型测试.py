import base64
from openai import OpenAI
import os
import pathlib
from dotenv import load_dotenv
load_dotenv()

try:
    # 请替换为实际的音频文件路径
    file_path = r"source\西格莉卡Lght.mp3"
    # 请替换为实际的音频文件MIME类型
    audio_mime_type = "audio/mpeg"

    file_path_obj = pathlib.Path(file_path)
    if not file_path_obj.exists():
        raise FileNotFoundError(f"音频文件不存在: {file_path}")

    base64_str = base64.b64encode(file_path_obj.read_bytes()).decode()
    data_uri = f"data:{audio_mime_type};base64,{base64_str}"

    client = OpenAI(
        # 新加坡和北京地域的API Key不同。获取API Key：https://help.aliyun.com/zh/model-studio/get-api-key
        # 若没有配置环境变量，请用百炼API Key将下行替换为：api_key = "sk-xxx",
        api_key=os.getenv("ASR_API_KEY"),
        # 以下为北京地域url，若使用新加坡地域的模型，需将url替换为：https://dashscope-intl.aliyuncs.com/compatible-mode/v1
        base_url=os.getenv("ASR_BASE_URL"),
    )
    

    stream_enabled = True  # 是否开启流式输出
    completion = client.chat.completions.create(
        model=os.getenv("ASR_MODEL_NAME"),
        messages=[
            {
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": data_uri
                        }
                    }
                ],
                "role": "user"
            }
        ],
        stream=stream_enabled,
        # stream设为False时，不能设置stream_options参数
        # stream_options={"include_usage": True},
        extra_body={
            "asr_options": {
                # "language": "zh",
                "enable_itn": False
            }
        }
    )
    if stream_enabled:
        full_content = ""
        print("流式输出内容为：")
        for chunk in completion:
            # 如果stream_options.include_usage为True，则最后一个chunk的choices字段为空列表，需要跳过（可以通过chunk.usage获取 Token 使用量）
            print(chunk)
            if chunk.choices and chunk.choices[0].delta.content:
                full_content += chunk.choices[0].delta.content
        print(f"完整内容为：{full_content}")
    else:
        print(f"非流式输出内容为：{completion.choices[0].message.content}")
except Exception as e:
    print(f"错误信息：{e}")