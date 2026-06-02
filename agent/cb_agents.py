import logging
import os
import queue
import threading
import time
from openai import OpenAI
from dotenv import load_dotenv
from typing import List, Dict, Optional, Any
import json
from constant.llm.constant_llm import ConstantLLM
from agent.event_bus import EventBus
from agent.events import (
    Cancelled, ReasoningDelta, TextDelta, TokenUsage, ToolCallPlanned,
)
# 加载 .env 文件中的环境变量
load_dotenv()
logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    """读取 float 环境变量；写错时退回默认值，避免一次配置错误阻断 agent 启动。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("invalid float env %s=%r, fallback to %s", name, raw, default)
        return default


def _usage_to_dict(usage: Any) -> Optional[Dict[str, int]]:
    """OpenAI usage 对象 → dict。None 透传 None。"""
    if usage is None:
        return None
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "total_tokens": getattr(usage, "total_tokens", 0) or 0,
    }

class CbAgentsLLM:
    """
    通过OpenAI API调用大语言模型。
    它用于调用任何兼容OpenAI接口的服务，并默认使用流式响应。
    """
    def __init__(self, model: str = None, apiKey: str = None, baseUrl: str = None, timeout: int = None, provider: Optional[str] = "auto"):
        """
        初始化客户端。优先使用传入参数，如果未提供，则从环境变量加载。
        """
        self.model = model or os.getenv("LLM_MODEL_ID")
        apiKey = apiKey or os.getenv("LLM_API_KEY")
        baseUrl = baseUrl or os.getenv("LLM_BASE_URL")
        timeout = timeout or int(os.getenv("LLM_TIMEOUT", 60))
        self.provider = provider
        self.is_Function_Calling = self._is_able_Function_Calling()
        self.timeout = timeout
        # 当前正在读取的 OpenAI stream 句柄表。取消请求来自 Gateway/TUI 或 CLI signal
        # 线程，而真正读 chunk 的代码在 chat worker 里；保存句柄后，取消方就能主动
        # close stream，而不是被动等“下一个 chunk 到来”才检查 cancel_event。
        self._stream_lock = threading.Lock()
        self._stream_seq = 0
        self._active_streams: Dict[int, Any] = {}
        # 轮询间隔越短，Ctrl-C 越灵敏；过短会让空转更频繁。0.2s 对 TUI 来说足够跟手。
        self._stream_poll_seconds = max(0.05, _env_float("LLM_STREAM_POLL_SECONDS", 0.2))
        # 模型长时间无 chunk 时只写诊断日志，不自动取消。这样既能定位“卡在哪”，
        # 又不会误杀确实在长思考但仍可能返回的 provider。
        self._stream_idle_log_seconds = max(1.0, _env_float("LLM_STREAM_IDLE_LOG_SECONDS", 20.0))
        # 取消后最多等读流线程收尾多久。超过就放它作为 daemon 线程自然结束，
        # 保证 UI/busy 状态先释放，避免用户只能关终端。
        self._stream_join_seconds = max(0.0, _env_float("LLM_STREAM_JOIN_SECONDS", 1.0))

        
        if not all([self.model, apiKey, baseUrl]):
            raise ValueError("模型ID、API密钥和服务地址必须被提供或在.env文件中定义。")

        self.client = OpenAI(api_key=apiKey, base_url=baseUrl, timeout=timeout)

    def _next_stream_id(self) -> int:
        """生成本进程内递增 stream id，便于日志把一次 LLM 请求串起来。"""
        with self._stream_lock:
            self._stream_seq += 1
            return self._stream_seq

    def _register_stream(self, stream_id: int, stream: Any) -> None:
        """登记当前活跃 stream；取消方会从这里拿到句柄并调用 close()。"""
        with self._stream_lock:
            self._active_streams[stream_id] = stream

    def _unregister_stream(self, stream_id: int) -> None:
        """stream 正常结束或异常结束后移除登记，避免后续取消误关旧句柄。"""
        with self._stream_lock:
            self._active_streams.pop(stream_id, None)

    def _close_stream(self, stream_id: int, stream: Any, reason: str) -> bool:
        """尝试关闭一个 OpenAI streaming response。

        不同 OpenAI-compatible SDK/provider 返回的 stream 对象实现不完全一致：
        有的提供 close()，有的没有。这里做能力检测，能关就关，不能关也只记录诊断，
        因为真正的兜底是“主逻辑不再阻塞等待该线程”，读流线程会交给 SDK timeout
        或 provider 自己的连接关闭来收尾。
        """
        close = getattr(stream, "close", None)
        if not callable(close):
            logger.warning("LLM stream %s has no close(); reason=%s", stream_id, reason)
            return False
        try:
            close()
            logger.warning("requested close for LLM stream %s; reason=%s", stream_id, reason)
            return True
        except Exception:
            logger.exception("failed to close LLM stream %s; reason=%s", stream_id, reason)
            return False

    def cancel_active_streams(self, reason: str = "cancel") -> int:
        """主动关闭所有活跃 LLM stream，返回成功调用 close() 的数量。

        Gateway 的 session.cancel 和 CLI 的 Ctrl-C 都会走到这里。这样即使 SDK 正在
        阻塞等待下一个 stream chunk，取消请求也有机会从另一个线程打断底层连接。
        """
        with self._stream_lock:
            streams = list(self._active_streams.items())
        closed = 0
        for stream_id, stream in streams:
            if self._close_stream(stream_id, stream, reason):
                closed += 1
        return closed

    def _iter_chat_stream(
        self,
        request_kwargs: Dict[str, Any],
        *,
        cancel_event: Optional[threading.Event],
        round_idx: int,
    ):
        """以可取消的方式读取 OpenAI streaming response。

        旧实现直接在当前线程里执行：
            response = client.chat.completions.create(..., stream=True)
            for chunk in response: ...

        问题是如果 create() 或 ``for chunk in response`` 阻塞在网络读上，Ctrl-C
        只是设置 cancel_event，代码却回不到 Python 层检查这个 event，TUI 就会一直
        停在 working/busy。这里把“创建请求 + 读 chunk”放入一个 daemon 读流线程，
        当前线程只从 Queue 里取 chunk，并按固定间隔检查 cancel_event：
        - 正常时：读流线程把 chunk 放进队列，当前线程继续按原逻辑处理；
        - 用户取消时：当前线程立即 close 活跃 stream 并返回，busy 状态可以释放；
        - provider 长时间不吐 chunk：当前线程周期性写诊断日志，方便定位卡点。
        """
        stream_id = self._next_stream_id()
        events: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=1)
        stop_event = threading.Event()
        next_permit = threading.Semaphore(1)
        started_at = time.monotonic()

        def _put_event(kind: str, payload: Any) -> bool:
            """向主线程投递读流事件；取消后不再无限期阻塞在满队列上。

            队列容量被限制为 1，是为了让后台读流线程不要抢跑多个 chunk。但这也
            意味着如果主线程已经因为 cancel 退出，后台线程不能再傻等 put() 腾位。
            这里用短 timeout 轮询 stop_event，保证取消路径没有遗留阻塞线程。
            """
            while not stop_event.is_set():
                try:
                    events.put((kind, payload), timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False

        def _reader() -> None:
            response = None
            try:
                response = self.client.chat.completions.create(**request_kwargs)
                self._register_stream(stream_id, response)
                if stop_event.is_set():
                    self._close_stream(stream_id, response, "cancel_before_first_chunk")
                    return
                if not _put_event("created", time.monotonic() - started_at):
                    self._close_stream(stream_id, response, "cancel_before_stream_created_event")
                    return
                if stop_event.is_set():
                    self._close_stream(stream_id, response, "cancel_before_first_chunk")
                    return

                # 只允许后台线程一次读取一个 chunk。主线程 yield 给业务代码并处理完后，
                # 才释放 next_permit 让这里继续 next(iterator)。这样既能把阻塞的
                # next() 放到可关闭的线程里，又不会让后台线程抢跑很多 chunk，保留
                # 原同步 for chunk 循环的顺序语义和“取消时保留已处理正文”的行为。
                iterator = iter(response)
                while not stop_event.is_set():
                    next_permit.acquire()
                    if stop_event.is_set():
                        break
                    try:
                        chunk = next(iterator)
                    except StopIteration:
                        break
                    if stop_event.is_set():
                        break
                    if not _put_event("chunk", chunk):
                        break
            except Exception as exc:
                if not stop_event.is_set():
                    _put_event("error", exc)
            finally:
                self._unregister_stream(stream_id)
                if not stop_event.is_set():
                    _put_event("done", None)

        thread = threading.Thread(
            target=_reader,
            name=f"cb-agent-llm-stream-r{round_idx}-{stream_id}",
            daemon=True,
        )
        thread.start()
        last_idle_log = started_at

        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    stop_event.set()
                    next_permit.release()
                    with self._stream_lock:
                        active_stream = self._active_streams.get(stream_id)
                    if active_stream is not None:
                        self._close_stream(stream_id, active_stream, "cancel_event")
                    logger.warning(
                        "LLM stream %s cancelled while waiting for provider response; round=%s",
                        stream_id,
                        round_idx,
                    )
                    break

                try:
                    kind, payload = events.get(timeout=self._stream_poll_seconds)
                except queue.Empty:
                    now = time.monotonic()
                    if now - last_idle_log >= self._stream_idle_log_seconds:
                        logger.warning(
                            "LLM stream %s still waiting: %.1fs elapsed, round=%s, model=%s",
                            stream_id,
                            now - started_at,
                            round_idx,
                            self.model,
                        )
                        last_idle_log = now
                    continue

                if kind == "created":
                    print(f"✅ 大语言模型响应成功（stream={stream_id}, {payload:.2f}s）:")
                    continue
                if kind == "chunk":
                    yield payload
                    if cancel_event is not None and cancel_event.is_set():
                        stop_event.set()
                        next_permit.release()
                        with self._stream_lock:
                            active_stream = self._active_streams.get(stream_id)
                        if active_stream is not None:
                            self._close_stream(stream_id, active_stream, "cancel_after_chunk")
                        logger.warning(
                            "LLM stream %s cancelled after chunk processing; round=%s",
                            stream_id,
                            round_idx,
                        )
                        break
                    next_permit.release()
                    continue
                if kind == "error":
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    raise payload
                if kind == "done":
                    break
        finally:
            if stop_event.is_set():
                next_permit.release()
                thread.join(timeout=self._stream_join_seconds)

    def _is_able_Function_Calling(self) -> bool:
        """根据模型提供商判断是否支持函数调用"""
        #实现根据模型提供商判断是否支持函数调用的逻辑
        return ConstantLLM.llm_dict[self.model]["is_tool"]

    def think(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0,
        tools: Optional[List[Dict]] = None,
        event_bus: Optional[EventBus] = None,
        cancel_event: Optional[threading.Event] = None,
        round_idx: int = 0,
    ) -> Any:
        """
        调用大语言模型进行思考，并返回其响应。
        tools: OpenAI Function Calling 的标准格式 JSON 字符串 比如：
        {
            "type": "function",
            "function": {
                "name": "calculator",
                "description": "执行数学计算的工具...",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "需要计算的数学表达式..."
                        }
                    },
                    "required": ["expression"]
                }
            }
        }

        return: {'answer': str, 'reason': str, 'tool_calls': List[Dict[str, Any]],
                 'usage': Dict | None, 'cancelled': bool}

        event_bus: 可选事件总线。流式 chunk 会经它发出 TextDelta/ReasoningDelta/
                   TokenUsage/ToolCallPlanned/Cancelled 事件。前端订阅它替代 print。
                   传 None 时维持旧行为（直接 print 到 stdout）。
        cancel_event: 可选 threading.Event。每收一个 chunk 检查一次，set 则中止
                      流式读取并返回已累积内容（带 cancelled=True 标记）。
        round_idx: 工具循环当前轮次，1-based。仅作为事件元信息透传。
        """
        print(f"🧠 正在调用 {self.model} 模型...")
        try:
            if self.is_Function_Calling:
                # 支持函数调用的模型调用
                return self._think_with_Function_Calling(
                    messages, temperature, tools,
                    event_bus=event_bus, cancel_event=cancel_event, round_idx=round_idx,
                )
            else:
                return self._think_no_Function_Calling(
                    messages, temperature,
                    event_bus=event_bus, cancel_event=cancel_event, round_idx=round_idx,
                )

        except Exception as e:
            print(f"❌ 调用LLM API时发生错误: {e}")
            return None
    
    #根据api厂商是否支持Function Calling进行不同的请求
    # 1 不支持Function Calling
    def _think_no_Function_Calling(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0,
        event_bus: Optional[EventBus] = None,
        cancel_event: Optional[threading.Event] = None,
        round_idx: int = 0,
    ) -> List[Any]:
        """不支持函数调用的模型调用 直接返回原始响应 让调用者自己解析"""
        collected_content: List[str] = []
        accumulated = ""
        last_usage = None
        cancelled_emitted = False
        request_kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        # 使用可取消的流式读取器，而不是直接在当前线程阻塞迭代 SDK stream。
        # 这样 Ctrl-C/session.cancel 在 provider 长时间不吐 chunk 时也能及时生效。
        for chunk in self._iter_chat_stream(
            request_kwargs,
            cancel_event=cancel_event,
            round_idx=round_idx,
        ):
            # cancel 检查放最前面，确保下一个 chunk 边界一定能出
            if cancel_event is not None and cancel_event.is_set():
                if event_bus is not None:
                    event_bus.emit(Cancelled(where="llm_stream", round_idx=round_idx))
                cancelled_emitted = True
                break

            # usage 通常在最后一个 chunk（choices 为空）出现
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                last_usage = usage

            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content or ""
            if content:
                # 默认（无 bus）维持旧行为打印到 stdout；有 bus 时不直接 print，
                # 让订阅者自己决定渲染方式
                if event_bus is None:
                    print(content, end="", flush=True)
                accumulated += content
                collected_content.append(content)
                if event_bus is not None:
                    event_bus.emit(TextDelta(
                        delta=content,
                        accumulated=accumulated,
                        round_idx=round_idx,
                    ))
        if event_bus is None:
            print()  # 在流式输出结束后换行（旧行为）
        if (
            cancel_event is not None
            and cancel_event.is_set()
            and event_bus is not None
            and not cancelled_emitted
        ):
            # 取消可能发生在“等待下一个 chunk”的空档。此时 for 循环不会再进入上面的
            # chunk 边界检查，所以这里补发一次事件，让 TUI 能明确显示中断状态。
            event_bus.emit(Cancelled(where="llm_stream", round_idx=round_idx))

        # 推 token usage 事件
        if last_usage is not None and event_bus is not None:
            event_bus.emit(TokenUsage(
                prompt_tokens=getattr(last_usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(last_usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(last_usage, "total_tokens", 0) or 0,
                round_idx=round_idx,
            ))

        full_text = "".join(collected_content)
        # 兼容旧返回结构：[text, None]；额外字段挂在 list 末尾会破坏调用方解构
        # → 改成多带回 usage/cancelled 信息但保持位置 0/1 不变
        return [full_text, None]


    # 2 支持Function Calling
    def _think_with_Function_Calling(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0,
        tools: Optional[List[Dict]] = None,
        event_bus: Optional[EventBus] = None,
        cancel_event: Optional[threading.Event] = None,
        round_idx: int = 0,
    ) -> Dict[str, Any]:
        """支持函数调用的模型调用（流式）。

        OpenAI 协议在 stream=True 下，tool_calls 会按 index 分块下发：
            delta.tool_calls = [
              {"index": 0, "id": "...", "type": "function",
               "function": {"name": "...", "arguments": "..."}},
              ...
            ]
        每个分片可能只带 name 的一部分或 arguments json 的一段，必须按 index 累积。
        content / reasoning_content 同样按 delta 增量拼接。

        event_bus / cancel_event / round_idx 见 think() 文档。
        """
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        content_accumulated = ""
        reasoning_accumulated = ""
        # 按 index 累积 tool_calls 分片
        tool_calls_by_index: Dict[int, Dict[str, Any]] = {}
        last_usage = None
        cancelled_emitted = False
        request_kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "tools": tools,
            "tool_choice": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        # 控制可见正文的打印：只有真正开始吐 content 时才打 "assistant > " 前缀
        printed_prefix = False

        # 使用可取消的流式读取器，避免 SDK 阻塞等待 chunk 时 Ctrl-C 无法释放 session。
        for chunk in self._iter_chat_stream(
            request_kwargs,
            cancel_event=cancel_event,
            round_idx=round_idx,
        ):
            # cancel 检查放最前面：保证下一 chunk 边界能优雅退出
            if cancel_event is not None and cancel_event.is_set():
                if event_bus is not None:
                    event_bus.emit(Cancelled(where="llm_stream", round_idx=round_idx))
                cancelled_emitted = True
                break

            # usage 通常在 stream 末尾的"choices 为空"chunk 上
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                last_usage = usage

            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # 1) 普通 content：直接流式打到终端（无 bus 时）或经 bus 派发
            piece = getattr(delta, "content", None) or ""
            if piece:
                if event_bus is None:
                    if not printed_prefix:
                        print("\nassistant > ", end="", flush=True)
                        printed_prefix = True
                    print(piece, end="", flush=True)
                content_accumulated += piece
                content_parts.append(piece)
                if event_bus is not None:
                    event_bus.emit(TextDelta(
                        delta=piece,
                        accumulated=content_accumulated,
                        round_idx=round_idx,
                    ))

            # 2) reasoning_content（DeepSeek thinking 等）：旧行为只累计不直接打，
            #    交给上层 run_agent 渲染成 "Thought for Xs" 块
            r_piece = getattr(delta, "reasoning_content", None) or ""
            if r_piece:
                reasoning_parts.append(r_piece)
                reasoning_accumulated += r_piece
                if event_bus is not None:
                    event_bus.emit(ReasoningDelta(
                        delta=r_piece,
                        accumulated=reasoning_accumulated,
                        round_idx=round_idx,
                    ))

            # 3) tool_calls 分片：按 index 累积
            tc_chunks = getattr(delta, "tool_calls", None) or []
            for tc in tc_chunks:
                idx = tc.index if tc.index is not None else 0
                slot = tool_calls_by_index.setdefault(
                    idx,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tc.id:
                    slot["id"] = tc.id
                if tc.type:
                    slot["type"] = tc.type
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["function"]["name"] += fn.name
                    if getattr(fn, "arguments", None):
                        slot["function"]["arguments"] += fn.arguments

        if event_bus is None and printed_prefix:
            print()  # 流式正文末尾补换行（旧行为）
        if (
            cancel_event is not None
            and cancel_event.is_set()
            and event_bus is not None
            and not cancelled_emitted
        ):
            # 如果取消发生在空闲等待期，_iter_chat_stream 会直接结束生成器；
            # 补发 Cancelled 可以让 UI 不必猜测“为什么流突然结束”。
            event_bus.emit(Cancelled(where="llm_stream", round_idx=round_idx))

        content = "".join(content_parts)
        reasoning_content = "".join(reasoning_parts) or None
        # 按 index 排序，输出形如 [{id, type, function:{name, arguments}}, ...]
        tool_calls = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]

        # tool_calls 累积完成 → 每个发 ToolCallPlanned 事件（执行还没开始）
        if event_bus is not None:
            for tc in tool_calls:
                event_bus.emit(ToolCallPlanned(
                    call_id=tc.get("id", ""),
                    name=tc["function"]["name"],
                    arguments_json=tc["function"]["arguments"],
                    round_idx=round_idx,
                ))

        # token usage 事件
        if last_usage is not None and event_bus is not None:
            event_bus.emit(TokenUsage(
                prompt_tokens=getattr(last_usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(last_usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(last_usage, "total_tokens", 0) or 0,
                round_idx=round_idx,
            ))

        return {
            "answer": content,
            "reason": reasoning_content if reasoning_content else content,
            "tool_calls": tool_calls,
            "reasoning_content": reasoning_content,
            "usage": _usage_to_dict(last_usage),
        }
    
    def _auto_detect_provider(self, api_key: Optional[str], base_url: Optional[str]) -> str:
        """
        自动检测LLM提供商
        """
        # 1. 检查特定提供商的环境变量 (最高优先级)
        if os.getenv("MODELSCOPE_API_KEY"): return "modelscope"
        if os.getenv("OPENAI_API_KEY"): return "openai"
        if os.getenv("ZHIPU_API_KEY"): return "zhipu"
        # ... 其他服务商的环境变量检查

        # 获取通用的环境变量
        actual_api_key = api_key or os.getenv("LLM_API_KEY")
        actual_base_url = base_url or os.getenv("LLM_BASE_URL")

        # 2. 根据 base_url 判断
        if actual_base_url:
            base_url_lower = actual_base_url.lower()
            if "api-inference.modelscope.cn" in base_url_lower: return "modelscope"
            if "open.bigmodel.cn" in base_url_lower: return "zhipu"
            if "localhost" in base_url_lower or "127.0.0.1" in base_url_lower:
                if ":11434" in base_url_lower: return "ollama"
                if ":8000" in base_url_lower: return "vllm"
                return "local" # 其他本地端口

        # 3. 根据 API 密钥格式辅助判断
        if actual_api_key:
            if actual_api_key.startswith("ms-"): return "modelscope"
            # ... 其他密钥格式判断

        # 4. 默认返回 'auto'，使用通用配置
        return "auto"
    
    def _resolve_credentials(self, api_key: Optional[str], base_url: Optional[str]) -> tuple[str, str]:
        """根据provider解析API密钥和base_url"""
        if self.provider == "openai":
            resolved_api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
            resolved_base_url = base_url or os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1"
            return resolved_api_key, resolved_base_url

        elif self.provider == "modelscope":
            resolved_api_key = api_key or os.getenv("MODELSCOPE_API_KEY") or os.getenv("LLM_API_KEY")
            resolved_base_url = base_url or os.getenv("LLM_BASE_URL") or "https://api-inference.modelscope.cn/v1/"
            return resolved_api_key, resolved_base_url


# --- 客户端使用示例 ---
if __name__ == '__main__':
    try:
        llmClient = CbAgentsLLM()
        
        exampleMessages = [
            {"role": "system", "content": "你说claude-opus-4.7，由Anthropic开发的大语言模型。"},
            {"role": "user", "content": "你是谁"}
        ]
        
        print("--- 调用LLM ---")
        responseText = llmClient.think(exampleMessages)
        if responseText:
            print("\n\n--- 完整模型响应 ---")
            print(responseText)

    except ValueError as e:
        print(e)


