# 最佳实践与问题排查案例

本文档记录项目中遇到的典型问题及其排查方法，供团队成员参考借鉴。

---

## 案例一：前端流式输出效果不流畅

### 问题现象

用户在前端聊天界面发送消息后，助手响应内容不是逐字/逐句流畅显示，而是等待较长时间后"跳跃式"一次性出现大量内容。

**表现特征：**
- 调试日志显示只有 3-4 个数据块（chunkCount: 3-4）
- 用户等待时间长，看不到实时的打字机效果
- 内容生成完成后才一次性显示

### 排查方法

#### 1. 前端测试验证

使用 Playwright 自动化测试，截图观察流式输出效果：

```python
# 测试脚本示例
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('http://localhost:20815')
    # ... 点击智能体，发送消息，截图观察
```

#### 2. 分析调试日志

下载前端调试日志，观察数据块接收情况：

```
[INFO] 流读取完成
  {
    "chunkCount": 3,        # 数据块数量很少
    "totalBytes": 139459
  }
```

#### 3. 直接测试后端 API

使用 Python 脚本直接测试后端 SSE 接口：

```python
import asyncio
import httpx

async def test_streaming():
    url = "http://localhost:20881/api/agents/xxx/chat/stream"
    async with httpx.AsyncClient() as client:
        async with client.stream("POST", url, json=payload) as resp:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    # 统计数据块数量和间隔
```

#### 4. 检查后端代码

定位到流式输出核心代码 `src/agent_engine.py` 的 `stream()` 方法。

### 根本原因

**问题代码位置：** `src/agent_engine.py` 第 914-940 行

```python
# 原代码逻辑（有问题）
async for chunk in self.llm.astream(messages):
    if chunk.content:
        # 问题1：默认进入缓冲模式
        buffering = True
        buffer_content += chunk.content

        # 问题2：所有内容都被缓冲，不输出
        if buffering:
            buffer_content += chunk.content
        elif not might_be_tool_call:
            yield {"type": "content", "content": chunk.content}

# 问题3：LLM 响应完成后才输出缓冲内容
if buffering:
    for char in buffer_content:  # 响应完成后才逐字符输出
        yield {"type": "content", "content": char}
```

**问题分析：**

1. **过度缓冲**：为了检测工具调用，代码默认缓冲了所有 LLM 输出
2. **延迟输出**：只有 LLM 完全响应后，才开始逐字符输出到前端
3. **假流式效果**：用户看到的是"假流式"——等待很久后内容快速出现

### 解决措施

修改缓冲逻辑，只在可能包含工具调用时缓冲（检查前 50 个字符）：

```python
# 优化后的代码
BUFFER_THRESHOLD = 50  # 缓冲区阈值
started_streaming = False

async for chunk in self.llm.astream(messages):
    if chunk.content:
        response_content += chunk.content

        if not content_started:
            # 检查是否可能是工具调用（以 { 或 ```json 开头）
            if stripped.startswith('{') or stripped.startswith('```json'):
                might_be_tool_call = True
                buffering = True
            else:
                buffering = True
                buffer_content += chunk.content

        elif buffering and not started_streaming:
            buffer_content += chunk.content

            # 检测到工具调用
            if '"tool"' in buffer_content:
                might_be_tool_call = True

            # 关键改进：超过阈值且没有工具调用，开始流式输出
            if len(buffer_content) > BUFFER_THRESHOLD and not might_be_tool_call:
                started_streaming = True
                for char in buffer_content:
                    yield {"type": "content", "content": char}
                buffer_content = ""

        elif started_streaming:
            # 已经开始流式输出，直接输出新内容
            yield {"type": "content", "content": chunk.content}
```

### 优化效果

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| 数据块数量 | 3-4 个 | **382 个** |
| 内容数据块 | 少量 | **379 个** |
| 平均块间隔 | 很长 | **15ms** |
| 流式效果 | 跳跃式显示 | **逐字符流畅显示** |

### 经验总结

1. **流式输出要尽早开始**：不要等 LLM 完全响应后再输出，应该在确认不是工具调用后立即开始
2. **设置合理的缓冲阈值**：缓冲区大小要适中（本项目设为 50 字符），既能检测工具调用，又不会过度延迟
3. **测试验证很重要**：使用自动化测试脚本直接测试后端 API，可以准确测量流式输出效果
4. **调试日志是关键**：前端的调试日志能快速定位问题所在

---

## 排查工具箱

### 1. Playwright 自动化测试

用于验证前端 UI 效果：

```bash
# 安装
pip install playwright
playwright install chromium

# 运行测试脚本
python test_stream_output.py
```

### 2. 直接测试后端 API

绕过前端，直接测试 SSE 接口：

```python
import asyncio
import httpx
import json

async def test_sse():
    url = "http://localhost:20881/api/agents/智能体名/chat/stream"
    payload = {"message": "测试消息", "history": []}

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, json=payload) as resp:
            chunk_count = 0
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    chunk_count += 1
                    data = json.loads(line[6:])
                    print(f"[{chunk_count}] type: {data.get('type')}")

            print(f"总数据块: {chunk_count}")

asyncio.run(test_sse())
```

### 3. 前端调试日志

前端 AgentChat 组件会记录详细日志，可以通过"下载调试日志"按钮获取：

- 请求 URL 和参数
- 响应状态和 Content-Type
- 数据块接收数量和时间
- 最终内容长度

### 4. 后端日志

查看后端日志：

```bash
tail -f /tmp/backend.log
```

---

## 其他常见问题

（待补充...）

---

*最后更新: 2026-03-04*
