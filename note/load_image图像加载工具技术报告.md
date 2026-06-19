# load_image 图像加载工具技术报告

## 目标

给 agent 增加一个 `load_image` 工具，让模型在执行任务过程中按需读取图片。需求里模型分两类，工具据此分两条路：

1. **支持多模态输入的模型**：本身有视觉能力，工具把图片作为视觉输入直接送给模型，让它"看"原图。
2. **不支持多模态输入的模型**：工具调用系统中现成的 OCR 把图片转成文字，返回最基本的文本 tool result。

工具还要适配多种图片类型（png/jpg/jpeg/webp/gif/bmp/tiff）。

## 关键约束：tool result 进的是 role="tool" 消息，塞不进图片

cb-agent 走的是 **OpenAI Chat Completions** 协议。工具执行后，结果在 [`agent/session.py`](../agent/session.py) 的工具循环里按协议回灌：

```python
messages.append({
    "role": "tool",
    "tool_call_id": call.get("id", ""),
    "name": exec_result.name,
    "content": (exec_result.result if isinstance(exec_result.result, str) else str(exec_result.result)),
})
```

`Tool.run()` 的签名是 `-> str`，content 这里还会 `str()` 兜底。问题在于：

- 即便工具返回 `[{"type":"text"...}, {"type":"image_url"...}]` 这种数组，也会被 `str()` 成 Python repr，模型读到的是一段乱码而非图片。
- 就算改循环放行 list content，**多数 OpenAI 兼容中转站不接受 `role="tool"` 消息里携带 `image_url`**——图片只能出现在 `role="user"` / `role="system"` 消息里。

所以多模态分支**不能靠工具返回值把图片带给模型**。

### 参考 codex 的 view_image 怎么做

codex 有一个语义完全对应的工具 `view_image`（[`外部代码/codex-main/codex-rs/core/src/tools/handlers/view_image.rs`](../../外部代码/codex-main/codex-rs/core/src/tools/handlers/view_image.rs)）：

- 只接受**本地图片路径**（`"Local filesystem path to an image file."`）。
- 先判模型能力，不支持视觉就直接拒绝（codex 没有 OCR 兜底，那是 cb-agent 的增强）。
- 把图片塞进 **function_call_output** 的 `ContentItems([InputImage{...}])` 返回。

但最后这步用的是 **OpenAI Responses API**（`ResponseInputItem::FunctionCallOutput` + `InputImage`），那是 Responses 协议独有的能力。cb-agent 在 Chat Completions 上没有这个口子。**Chat Completions 协议下的等价做法，就是把图片作为一条 `role="user"` 消息注入。**

## 设计：工具排队 + 循环注入

引入一个进程内缓冲，把"工具产出图片"和"循环注入图片"解耦：

```
load_image.run()  ──排队──►  pending_images 缓冲  ──drain──►  工具循环注入 role=user 消息
   (worker 线程)                 (Lock 保护)                      (主线程，下一轮 think 前)
```

### 三个分支的产出

`load_image` 用 `ConstantLLM.resolve_image_ability(LLM_MODEL_ID)` 判断当前模型是否视觉（与用户附件图片同一套能力判定，可被 `IMAGE_ABILITY` env 覆盖）：

| 输入 | 视觉模型 | 非视觉模型 |
|---|---|---|
| 本地路径 | 读字节 → data URI → 排队；返回文本确认 | 调 OCR → 文本 tool result |
| http(s) URL | URL 透传排队（不下载）；返回文本确认 | **拒绝**（不下载，省 OCR 调用成本） |

非视觉 + URL 直接拒绝，是按用户要求"减少代码量也减少 OCR 调用免得费钱"——不下载网络图片，也避免后端发起任意外联请求。

### 循环注入

工具循环在**本轮全部 `role=tool` 消息回灌之后**调 `_inject_pending_images`：drain 缓冲，把图片拼成

```python
{"role": "user", "content": [
    {"type": "text", "text": "图片加载成功：xxx.png"},
    {"type": "image_url", "image_url": {"url": "<data uri 或 http url>"}},
    ...
]}
```

追加到当轮 `messages`。下一轮 `think` 时模型就看到了原图。注入点在 tool 消息之后，不破坏 `assistant.tool_calls` ↔ `role=tool` 的协议配对。

## base64 不落 history 的安全边界

这是复用了项目既有的安全设计。`_extract_protocol_messages` 只把 `assistant`（含 tool_calls）和 `role=tool` 消息 commit 进 `history`，**`role=user` 消息不进 history**。所以注入的 data URI：

- 只活在**当轮 `messages`**，发完即弃；
- 不进 `history` / transcript / state，不落盘；
- token 估算与自动 compact 走 `sanitize_multimodal_payload`，本就把 data URI 换成占位符再计数。

这和用户附件图片（`agent/multimodal_input.py`）是同一条边界：当前轮请求可带 base64，跨轮上下文绝不保存 base64。

## 图片类型适配

直接复用 `MultimodalProcessor.IMAGE_MIME_MAP`（[`utils/multimodal.py`](../utils/multimodal.py)）作为支持类型表与 MIME 来源：png/jpg/jpeg/webp/gif/bmp/tiff。扩展名不在表里时报错并列出支持的格式。大小上限沿用附件同款 `CBAGENT_ATTACHMENT_MAX_MB`（默认 20MB）。

## 影响文件

- `tools/tools/pending_images.py`（新增）：`queue_image` / `drain_images` 进程内 Lock 保护缓冲。drain 每轮工具后必调，图片不跨轮残留，故无需额外清理钩子。
- `tools/tools/load_image_tool.py`（新增）：`LoadImageTool`，三分支路由 + OCR 处理器进程内单例。
- `run_agent.py`：import 并在工具列表注册 `LoadImageTool()`（紧挨 `FileReadTool`）。
- `agent/session.py`：工具循环回灌 tool 消息后调 `_inject_pending_images(messages)`；新增该辅助方法。

## 验证

用项目 venv 实测：

- schema 正常：`name=load_image`，`required=['path']`。
- 视觉模型分支：本地缺失文件报错且不排队；http URL 透传并排队为 `image_url` 块。
- 非视觉分支：URL 被拒绝（不排队）；不支持的扩展名报错并列出支持格式。
- 非视觉 + 真实本地 `image.png`：走 OCR，`routed_as=ocr`，返回识别文本，缓冲保持空。
- 循环注入：排队图片 drain 后拼成 `role=user` 消息，content 形状为 `[text, image_url]`。
- `agent.session` 带新方法 import 正常。
