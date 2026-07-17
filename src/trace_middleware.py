"""
Trace ID 中间件 - 实现全链路追踪

职责：
1. 从请求头提取 X-Request-ID，或生成新的 Trace ID
2. 注入请求上下文 (request.state.trace_id)
3. 设置响应头，返回 Trace ID

文件版本: 1.0
创建日期: 2026-03-15
"""

import re
import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from typing import Callable


_TRACE_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,100}")


def normalise_trace_id(candidate: str | None) -> str:
    """Accept only a bounded header-safe identifier, otherwise generate one."""
    if candidate and _TRACE_ID_PATTERN.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())


class TraceMiddleware(BaseHTTPMiddleware):
    """
    Trace ID 中间件

    实现全链路追踪：
    - 从请求头提取 X-Request-ID
    - 如无则生成 UUID v4
    - 注入 request.state.trace_id
    - 在响应头返回 X-Request-ID
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable
    ) -> Response:
        # 1. 从请求头提取 Trace ID，或生成新的
        trace_id = normalise_trace_id(request.headers.get('X-Request-ID'))

        # 2. 注入请求上下文
        request.state.trace_id = trace_id

        # 3. 执行请求
        try:
            response = await call_next(request)

            # 4. 设置响应头，返回 Trace ID
            response.headers['X-Request-ID'] = trace_id

            return response
        except Exception as exc:
            # 记录错误（但不中断请求处理）
            # 错误会被全局异常处理器捕获
            request.state.trace_error = type(exc).__name__
            raise


def get_trace_id(request: Request) -> str | None:
    """
    从请求中获取 Trace ID

    Args:
        request: Starlette 请求对象

    Returns:
        Trace ID 或 None
    """
    return getattr(request.state, 'trace_id', None)
