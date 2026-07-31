from typing import Optional, List, Dict, Any, Union
from datetime import datetime
from pydantic import BaseModel, Field
from enum import Enum


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class Message(BaseModel):
    """支持工具调用的多模态消息类"""
    
    role: MessageRole
    # content 可以是字符串，也可以是多模态数组（仅 user 角色使用数组）
    content: Optional[Union[str, List[Dict[str, Any]]]] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None  # 仅 assistant
    reasoning_content: Optional[str] = None  # 仅 assistant，部分 thinking 模型要求跨轮回传
    tool_name: Optional[str] = None  # 仅 tool
    tool_call_id: Optional[str] = None  # 仅 tool
    is_error: bool = False  # 仅供本地恢复/UI 区分工具成功与失败，不进入模型协议
    timestamp: datetime = Field(default_factory=datetime.now)
    metadata: Optional[Dict[str, Any]] = None

    # ---------- 工厂方法 ----------
    
    @classmethod
    def create_user_message(
        cls,
        input_text: str = "",
        input_image: Optional[str] = None,
        input_audio: Optional[str] = None
    ) -> "Message":
        """创建用户消息，支持文本、图片、音频"""
        content = []
        if input_text:
            content.append({"type": "text", "text": input_text})
        if input_image:
            content.append({"type": "image_url", "image_url": {"url": input_image}})
        if input_audio:
            content.append({"type": "audio_url", "audio_url": {"url": input_audio}})
        return cls(role=MessageRole.USER, content=content)
    
    @classmethod
    def create_system_message(cls, input_text: str) -> "Message":
        """系统消息，content 必须是纯字符串"""
        return cls(role=MessageRole.SYSTEM, content=input_text)
    
    @classmethod
    def create_assistant_message(
        cls,
        input_text: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        reasoning_content: Optional[str] = None,
    ) -> "Message":
        """助手消息，支持文本和工具调用"""
        return cls(
            role=MessageRole.ASSISTANT,
            content=input_text,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
        )
    
    @classmethod
    def create_tool_message(
        cls,
        tool_call_id: str,
        tool_name: str,
        tool_output: str,
        is_error: bool = False,
        model_content: Optional[List[Dict[str, Any]]] = None,
    ) -> "Message":
        """工具结果消息，content 必须是纯字符串"""
        metadata = (
            {"model_content": model_content}
            if model_content
            else None
        )
        return cls(
            role=MessageRole.TOOL,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            content=tool_output,
            is_error=is_error,
            metadata=metadata,
        )
    
    # ---------- 序列化 ----------
    
    def to_dict(self) -> Dict[str, Any]:
        """转为逻辑消息字典；其中 ``image_ref`` 不会被展开。"""
        if self.role == MessageRole.SYSTEM:
            return {"role": "system", "content": self.content}
        
        if self.role == MessageRole.USER:
            return {"role": "user", "content": self.content}
        
        if self.role == MessageRole.ASSISTANT:
            result: Dict[str, Any] = {"role": "assistant", "content": self.content}
            if self.tool_calls:
                result["tool_calls"] = self.tool_calls
            if self.reasoning_content:
                result["reasoning_content"] = self.reasoning_content
            return result
        
        if self.role == MessageRole.TOOL:
            return {
                "role": "tool",
                "content": self.content,
                "tool_call_id": self.tool_call_id,
                "name": self.tool_name
            }
        
        raise ValueError(f"Unknown role: {self.role}")

    def to_provider_dict(self, media_store: Any = None) -> Dict[str, Any]:
        """在 provider 边界展开不可变图片引用，不回写逻辑 history。"""

        payload = self.to_dict()

        def expand(value: Any) -> Any:
            if isinstance(value, list):
                return [expand(item) for item in value]
            if not isinstance(value, dict):
                return value
            if value.get("type") == "image_ref":
                if media_store is None:
                    raise ValueError("provider 序列化 ImageRef 时缺少 MediaBlobStore")
                ref_payload = value.get("image_ref")
                from core.media import ImageRef

                ref = (
                    ref_payload
                    if isinstance(ref_payload, ImageRef)
                    else ImageRef.from_dict(ref_payload or {})
                )
                image_url: Dict[str, Any] = {
                    "url": media_store.to_data_uri(ref),
                }
                if ref.detail:
                    image_url["detail"] = ref.detail
                return {
                    "type": "image_url",
                    "image_url": image_url,
                }
            return {key: expand(item) for key, item in value.items()}

        return expand(payload)
