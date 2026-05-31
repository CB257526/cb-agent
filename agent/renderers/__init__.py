"""renderers 子包：把 EventBus 事件流渲染成具体输出。

- cli.py：CLI 终端渲染（ANSI 颜色 + 流式 stdout）
- (后续可加) textual_renderer.py / web_renderer.py 等

每个 renderer 通过 event_bus.subscribe(..., EventType) 订阅自己关心的事件，
彼此独立，互不依赖。
"""
