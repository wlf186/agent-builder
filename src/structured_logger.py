"""
结构化日志记录器 - 实现业务链路追踪

职责：
1. 输出 JSON 格式日志
2. 关联 Trace ID
3. 敏感信息脱敏
4. 记录请求、Prompt、工具调用、错误等事件

文件版本: 1.0
创建日期: 2026-03-15
"""

import json
import re
import threading
import traceback
from datetime import datetime
from typing import Any, Optional, Dict, List
from collections import defaultdict, deque

from .log_safety import content_length, summarize_arguments


class StructuredLogger:
    """
    结构化日志记录器

    支持按 Trace ID 分组记录日志，并提供脱敏功能
    """

    # 敏感字段匹配规则
    SENSITIVE_PATTERNS = [
        (re.compile(r'api[_-]?key', re.IGNORECASE), 'api_key'),
        (re.compile(r'token', re.IGNORECASE), 'token'),
        (re.compile(r'password', re.IGNORECASE), 'password'),
        (re.compile(r'secret', re.IGNORECASE), 'secret'),
        (re.compile(r'private[_-]?key', re.IGNORECASE), 'private_key'),
        (re.compile(r'bearer', re.IGNORECASE), 'bearer'),
    ]
    MAX_TRACES = 500
    MAX_EVENTS_PER_TRACE = 500
    MAX_VALUE_CHARS = 4000
    MAX_COLLECTION_ITEMS = 50

    def __init__(self):
        # 日志存储：按 trace_id 分组
        self._log_store: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self.MAX_EVENTS_PER_TRACE)
        )
        # 性能指标存储
        self._metrics_store: Dict[str, Dict] = {}
        self._lock = threading.RLock()

    def _sanitize(self, data: Any, depth: int = 0) -> Any:
        """
        敏感信息脱敏

        Args:
            data: 待脱敏数据

        Returns:
            脱敏后的数据
        """
        if depth >= 4:
            return '<max-depth>'

        if isinstance(data, str):
            # 检查是否为敏感值
            for pattern, key_type in self.SENSITIVE_PATTERNS:
                if pattern.search(data):
                    return self._mask_value(data, key_type)
            return data[:self.MAX_VALUE_CHARS] + ('<truncated>' if len(data) > self.MAX_VALUE_CHARS else '')

        elif isinstance(data, dict):
            sanitized = {}
            items = list(data.items())
            for key, value in items[:self.MAX_COLLECTION_ITEMS]:
                key = str(key)
                # 检查键名是否为敏感字段
                is_sensitive = any(
                    pattern.search(key)
                    for pattern, _ in self.SENSITIVE_PATTERNS
                )

                if is_sensitive:
                    sanitized[key] = self._mask_value(str(value), key)
                else:
                    sanitized[key] = self._sanitize(value, depth + 1)
            if len(items) > self.MAX_COLLECTION_ITEMS:
                sanitized['_truncated_items'] = len(items) - self.MAX_COLLECTION_ITEMS
            return sanitized

        elif isinstance(data, (list, tuple, set)):
            values = list(data)
            sanitized_values = [
                self._sanitize(item, depth + 1)
                for item in values[:self.MAX_COLLECTION_ITEMS]
            ]
            if len(values) > self.MAX_COLLECTION_ITEMS:
                sanitized_values.append(f'<truncated {len(values) - self.MAX_COLLECTION_ITEMS} items>')
            return sanitized_values

        return data

    def _append(self, trace_id: str, entry: Dict[str, Any]) -> None:
        with self._lock:
            if trace_id not in self._log_store and len(self._log_store) >= self.MAX_TRACES:
                oldest_trace = next(iter(self._log_store))
                self._log_store.pop(oldest_trace, None)
                self._metrics_store.pop(oldest_trace, None)
            self._log_store[trace_id].append(entry)

    def _mask_value(self, value: str, key_type: str = 'default') -> str:
        """
        掩码敏感值

        Args:
            value: 原始值
            key_type: 键类型

        Returns:
            掩码后的值
        """
        # Never retain credential fragments. Prefix/suffix disclosure is enough
        # to identify short-lived tokens when logs are exported together.
        return '<redacted>'

    def log_request(
        self,
        trace_id: str,
        method: str,
        path: str,
        agent_name: str,
        user_input: str,
        conversation_id: Optional[str] = None,
        file_ids: Optional[List[str]] = None
    ) -> None:
        """
        记录请求阶段

        Args:
            trace_id: 追踪 ID
            method: HTTP 方法
            path: 请求路径
            agent_name: Agent 名称
            user_input: 用户输入
            conversation_id: 会话 ID
            file_ids: 文件 ID 列表
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'level': 'INFO',
            'category': 'request',
            'data': self._sanitize({
                'method': method,
                'path': path,
                'agent_name': agent_name,
                'user_input_length': len(user_input),
                'conversation_id': conversation_id,
                'file_ids': file_ids or [],
            })
        }
        self._append(trace_id, entry)

    def log_prompt(
        self,
        trace_id: str,
        system_prompt: str,
        user_message: str,
        message_count: int,
        model_provider: str,
        model_name: str
    ) -> None:
        """
        记录 Prompt 阶段

        Args:
            trace_id: 追踪 ID
            system_prompt: 系统提示词
            user_message: 用户消息
            message_count: 消息数量
            model_provider: 模型提供商
            model_name: 模型名称
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'level': 'DEBUG',
            'category': 'prompt',
            'data': self._sanitize({
                'system_prompt_length': len(system_prompt),
                'user_message_length': len(user_message),
                'message_count': message_count,
                'model_provider': model_provider,
                'model_name': model_name,
            })
        }
        self._append(trace_id, entry)

    def log_tool_call(
        self,
        trace_id: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_result: Optional[str] = None,
        duration_ms: Optional[int] = None,
        error: Optional[str] = None
    ) -> None:
        """
        记录工具调用

        Args:
            trace_id: 追踪 ID
            tool_name: 工具名称
            tool_args: 工具参数
            tool_result: 工具结果
            duration_ms: 执行耗时（毫秒）
            error: 错误信息
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'level': 'ERROR' if error else 'INFO',
            'category': 'tool_call',
            'data': self._sanitize({
                'tool_name': tool_name,
                **summarize_arguments(tool_args),
                'tool_result_length': content_length(tool_result),
                'duration_ms': duration_ms,
                'error_length': content_length(error),
            })
        }
        self._append(trace_id, entry)

    def log_reasoning(
        self,
        trace_id: str,
        reasoning_content: str
    ) -> None:
        """
        记录思维链

        Args:
            trace_id: 追踪 ID
            reasoning_content: 推理内容
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'level': 'DEBUG',
            'category': 'reasoning',
            'data': {
                'reasoning_length': len(reasoning_content),
            }
        }
        self._append(trace_id, entry)

    def log_completion(
        self,
        trace_id: str,
        duration_ms: int,
        chunk_count: int,
        first_token_latency_ms: Optional[int] = None
    ) -> None:
        """
        记录完成阶段

        Args:
            trace_id: 追踪 ID
            duration_ms: 总耗时（毫秒）
            chunk_count: Chunk 数量
            first_token_latency_ms: 首字延迟（毫秒）
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'level': 'INFO',
            'category': 'completion',
            'data': {
                'duration_ms': duration_ms,
                'chunk_count': chunk_count,
                'first_token_latency_ms': first_token_latency_ms,
            }
        }
        self._append(trace_id, entry)

        # 保存性能指标
        with self._lock:
            self._metrics_store[trace_id] = {
                'duration_ms': duration_ms,
                'chunk_count': chunk_count,
                'first_token_latency_ms': first_token_latency_ms,
            }

    def log_error(
        self,
        trace_id: str,
        error: Exception,
        context: Optional[Dict] = None
    ) -> None:
        """
        记录错误

        Args:
            trace_id: 追踪 ID
            error: 异常对象
            context: 上下文信息
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'level': 'ERROR',
            'category': 'error',
            'data': self._sanitize({
                'error_type': type(error).__name__,
                'error_message_length': content_length(str(error)),
                'stack_trace_length': content_length(traceback.format_exc()),
                'context': summarize_arguments(context or {}),
            })
        }
        self._append(trace_id, entry)

    def get_logs(self, trace_id: str) -> List[Dict]:
        """
        获取指定 Trace ID 的日志

        Args:
            trace_id: 追踪 ID

        Returns:
            日志列表
        """
        with self._lock:
            return list(self._log_store.get(trace_id, []))

    def get_metrics(self, trace_id: str) -> Optional[Dict]:
        """
        获取指定 Trace ID 的性能指标

        Args:
            trace_id: 追踪 ID

        Returns:
            性能指标字典
        """
        with self._lock:
            metric = self._metrics_store.get(trace_id)
            return dict(metric) if metric is not None else None

    def get_full_log_package(self, trace_id: str) -> Optional[Dict]:
        """
        获取完整日志包（用于 API 响应）

        Args:
            trace_id: 追踪 ID

        Returns:
            完整日志包
        """
        logs = self.get_logs(trace_id)
        if not logs:
            return None

        return {
            'meta': {
                'version': '1.0',
                'exported_at': datetime.utcnow().isoformat(),
                'trace_id': trace_id,
            },
            'logs': logs,
            'metrics': self.get_metrics(trace_id),
        }

    def clear_logs(self, trace_id: str) -> None:
        """
        清除指定 Trace ID 的日志

        Args:
            trace_id: 追踪 ID
        """
        with self._lock:
            self._log_store.pop(trace_id, None)
            self._metrics_store.pop(trace_id, None)

    def clear_old_logs(self, max_age_seconds: int = 3600) -> int:
        """
        清除旧日志（默认保留 1 小时）

        Args:
            max_age_seconds: 最大保留时间（秒）

        Returns:
            清除的日志数量
        """
        cutoff_time = datetime.utcnow().timestamp() - max_age_seconds
        cleared = 0

        with self._lock:
            for trace_id in list(self._log_store.keys()):
                logs = self._log_store[trace_id]
                if logs:
                    first_timestamp = logs[0]['timestamp']
                    log_time = datetime.fromisoformat(first_timestamp).timestamp()
                    if log_time < cutoff_time:
                        self._log_store.pop(trace_id, None)
                        self._metrics_store.pop(trace_id, None)
                        cleared += 1

        return cleared


# 全局日志记录器实例
_logger_instance: Optional[StructuredLogger] = None


def get_structured_logger() -> StructuredLogger:
    """获取全局结构化日志记录器实例"""
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = StructuredLogger()
    return _logger_instance
