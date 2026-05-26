import os
import base64
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
client = OpenAI(
    api_key=os.getenv("OCR_API_KEY"),
    base_url=os.getenv("OCR_BASE_URL"),
)

# 读取本地图片并转为 base64
filename = r'source\屏幕截图 2026-05-26 164742.png'
with open(filename, "rb") as f:
    image_data = base64.b64encode(f.read()).decode("utf-8")

# 根据文件扩展名确定 MIME 类型
ext = os.path.splitext(filename)[1].lower()
mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
mime_type = mime_map.get(ext, "image/png")

completion = client.chat.completions.create(
    model=os.getenv("OCR_MODEL_NAME"),
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{image_data}"
                    },
                },
                {"type": "text", "text": "请仅输出图像中的文本内容。"},
            ],
        },
    ],
)
print(completion.choices[0].message.content)

