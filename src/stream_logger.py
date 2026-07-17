"""
流式请求日志记录器 - AC130-202603150000

与前端 DebugLogger 配合，提供结构化的后端日志系统
"""
import logging
import re
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional

from .log_safety import content_length, summarize_arguments


logger = logging.getLogger(__name__)

_SENSITIVE_KEY = re.compile(
    r"(^|[_-])(authorization|cookie|token|secret|password|api[_-]?key)([_-]|$)",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
_PRIVATE_CONTENT_KEY = re.compile(
    r"^(?:args|arguments|content|details|error|history|input|message|messages|"
    r"output|prompt|reasoning|result|stack|traceback|url)$",
    re.IGNORECASE,
)


def _redact_text(value: str) -> str:
    value = _BEARER_VALUE.sub("Bearer <redacted>", value)
    return _URL_CREDENTIALS.sub(r"\1<redacted>@", value)


class StreamLogger:
    """流式请求日志记录器

    为每个请求记录结构化日志，支持线程安全的并发访问
    """

    # 类级别的日志存储
    _log_store: Dict[str, 'StreamLogger'] = {}
    _log_lock = threading.RLock()
    _max_loggers = 500
    _max_events = 500
    _max_collection_items = 50
    _max_value_chars = 4000
    _retention_hours = 1  # 日志保留时间（小时）

    def __init__(self, request_id: str):
        """初始化日志记录器

        Args:
            request_id: 请求唯一标识符
        """
        self.request_id = request_id
        self.start_time = datetime.now()
        self.events: Deque[Dict[str, Any]] = deque(maxlen=self._max_events)
        self.dropped_event_count = 0
        self._lock = threading.Lock()  # 实例级别的锁，保护 events 列表

    @classmethod
    def _sanitize(cls, value: Any, depth: int = 0) -> Any:
        """Bound debug payloads and redact common credential fields."""
        if depth >= 4:
            return "<max-depth>"
        if isinstance(value, dict):
            result: Dict[str, Any] = {}
            items = list(value.items())
            for key, nested in items[: cls._max_collection_items]:
                key_text = str(key)
                if _SENSITIVE_KEY.search(key_text):
                    result[key_text] = "<redacted>"
                elif _PRIVATE_CONTENT_KEY.fullmatch(key_text):
                    result[f"{key_text}_length"] = content_length(nested)
                else:
                    result[key_text] = cls._sanitize(nested, depth + 1)
            if len(items) > cls._max_collection_items:
                result["_truncated_items"] = len(items) - cls._max_collection_items
            return result
        if isinstance(value, (list, tuple, set)):
            items = list(value)
            result = [cls._sanitize(item, depth + 1) for item in items[: cls._max_collection_items]]
            if len(items) > cls._max_collection_items:
                result.append(f"<truncated {len(items) - cls._max_collection_items} items>")
            return result
        if isinstance(value, str):
            value = _redact_text(value)
            if len(value) > cls._max_value_chars:
                return value[: cls._max_value_chars] + "<truncated>"
            return value
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return cls._sanitize(str(value), depth + 1)

    def log_event(self, category: str, data: Dict[str, Any]) -> None:
        """记录日志事件

        Args:
            category: 事件类别（如 request_start, llm_call, tool_call 等）
            data: 事件数据（字典格式）
        """
        with self._lock:
            if len(self.events) == self._max_events:
                self.dropped_event_count += 1
            self.events.append({
                "timestamp": datetime.now().isoformat(),
                "category": category,
                "data": self._sanitize(data),
            })

    def log_error(self, error_type: str, message: str, traceback: str = None) -> None:
        """记录错误事件

        Args:
            error_type: 错误类型（如 TimeoutError, ValueError 等）
            message: 错误消息
            traceback: 错误堆栈（可选）
        """
        error_data = {
            "type": error_type,
            "message_length": content_length(message),
        }
        if traceback:
            error_data["traceback_length"] = content_length(traceback)

        self.log_event("error", error_data)

    def log_llm_call(self, model: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """记录 LLM 调用事件

        Args:
            model: 模型名称
            input_tokens: 输入 token 数
            output_tokens: 输出 token 数
        """
        self.log_event("llm_call", {
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens
        })

    def log_tool_call(self, tool_name: str, args: Dict[str, Any]) -> None:
        """记录工具调用事件

        Args:
            tool_name: 工具名称
            args: 工具参数
        """
        self.log_event("tool_call", {
            "name": tool_name,
            **summarize_arguments(args),
        })

    def log_sse_event(self, event_type: str) -> None:
        """记录 SSE 事件

        Args:
            event_type: SSE 事件类型（thinking, content, tool_call 等）
        """
        self.log_event("sse_event", {"type": event_type})

    def get_logs(self) -> Dict[str, Any]:
        """获取完整日志

        Returns:
            包含 request_id 和 events 的字典
        """
        with self._lock:
            return {
                "request_id": self.request_id,
                "start_time": self.start_time.isoformat(),
                "end_time": datetime.now().isoformat(),
                "event_count": len(self.events),
                "dropped_event_count": self.dropped_event_count,
                "events": list(self.events)  # 返回副本
            }

    @classmethod
    def get_logger(cls, request_id: Optional[str] = None) -> 'StreamLogger':
        """获取或创建日志记录器

        Args:
            request_id: 请求 ID，如果为 None 则自动生成

        Returns:
            StreamLogger 实例
        """
        if request_id is None:
            request_id = f"auto-{uuid.uuid4().hex[:8]}"

        with cls._log_lock:
            if request_id not in cls._log_store:
                cls._cleanup_old_logs()
                while len(cls._log_store) >= cls._max_loggers:
                    oldest_request_id = min(
                        cls._log_store,
                        key=lambda key: cls._log_store[key].start_time,
                    )
                    del cls._log_store[oldest_request_id]
                cls._log_store[request_id] = StreamLogger(request_id)
            return cls._log_store[request_id]

    @classmethod
    def _cleanup_old_logs(cls) -> None:
        """清理超过保留时间的日志

        可由任意线程安全调用。
        """
        with cls._log_lock:
            cutoff = datetime.now() - timedelta(hours=cls._retention_hours)
            expired = [
                request_id
                for request_id, request_logger in cls._log_store.items()
                if request_logger.start_time < cutoff
            ]
            for request_id in expired:
                del cls._log_store[request_id]
        if expired:
            logger.debug("Removed %d expired request loggers", len(expired))

    @classmethod
    def get_all_request_ids(cls) -> List[str]:
        """获取所有活跃的请求 ID

        Returns:
            请求 ID 列表
        """
        with cls._log_lock:
            return list(cls._log_store.keys())

    @classmethod
    def find_logger(cls, request_id: str) -> Optional['StreamLogger']:
        """Return an existing logger without allocating attacker-controlled state."""
        with cls._log_lock:
            return cls._log_store.get(request_id)

    @classmethod
    def remove_logger(cls, request_id: str) -> bool:
        """手动移除指定请求的日志记录器

        Args:
            request_id: 请求 ID

        Returns:
            是否成功移除
        """
        with cls._log_lock:
            if request_id in cls._log_store:
                del cls._log_store[request_id]
                return True
            return False


# 便捷函数
def get_logger(request_id: Optional[str] = None) -> StreamLogger:
    """获取或创建日志记录器（便捷函数）

    Args:
        request_id: 请求 ID

    Returns:
        StreamLogger 实例
    """
    return StreamLogger.get_logger(request_id)


def cleanup_old_logs() -> None:
    """清理超过保留时间的日志（便捷函数）"""
    StreamLogger._cleanup_old_logs()
