import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.message import Message

# 创建系统消息
system_msg = Message.create_system_message(input_text="你是一个专业的天气助手")
print(system_msg.to_dict())

# 创建用户消息
user_msg = Message.create_user_message(input_text="今天天气怎么样？")
print(user_msg.to_dict())

# 创建助手消息
assistant_msg = Message.create_assistant_message(input_text="我将查询今天北京的天气",
                                                 tool_calls=[{'id': 'call_cdda3196091e4ca38a166a4b', 'function': {'arguments': '{"location": "Beijing, CN"}', 'name': 'get_weather'}, 'type': 'function'}])
print(assistant_msg.to_dict())

# 创建工具消息
tool_msg = Message.create_tool_message(tool_call_id="call_cdda3196091e4ca38a166a4b",
                                        tool_name="get_weather",
                                        tool_output="北京今天天气晴，温度25度")
print(tool_msg.to_dict())

