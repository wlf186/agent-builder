import unittest
import json
from datetime import datetime, timedelta

from src.stream_logger import StreamLogger
from src.structured_logger import StructuredLogger


class StreamLoggerTest(unittest.TestCase):
    def setUp(self) -> None:
        with StreamLogger._log_lock:
            StreamLogger._log_store.clear()

    def tearDown(self) -> None:
        with StreamLogger._log_lock:
            StreamLogger._log_store.clear()

    def test_events_are_redacted_truncated_and_bounded(self) -> None:
        request_logger = StreamLogger.get_logger("request-a")
        for number in range(StreamLogger._max_events + 10):
            request_logger.log_event(
                "test",
                {"number": number, "api_key": "super-secret-value", "text": "x" * 5000},
            )
        logs = request_logger.get_logs()
        self.assertEqual(logs["event_count"], StreamLogger._max_events)
        self.assertEqual(logs["dropped_event_count"], 10)
        self.assertEqual(logs["events"][-1]["data"]["api_key"], "<redacted>")
        self.assertTrue(logs["events"][-1]["data"]["text"].endswith("<truncated>"))

    def test_cleanup_and_global_limit(self) -> None:
        old_max = StreamLogger._max_loggers
        try:
            StreamLogger._max_loggers = 3
            expired = StreamLogger.get_logger("expired")
            expired.start_time = datetime.now() - timedelta(hours=2)
            StreamLogger.get_logger("current-1")
            StreamLogger.get_logger("current-2")
            StreamLogger.get_logger("current-3")
            self.assertNotIn("expired", StreamLogger.get_all_request_ids())
            self.assertLessEqual(len(StreamLogger.get_all_request_ids()), 3)
        finally:
            StreamLogger._max_loggers = old_max

    def test_tool_error_and_generic_private_fields_never_store_values(self) -> None:
        request_logger = StreamLogger.get_logger("request-private")
        secrets = {
            "message": "private user question",
            "argument": "private tool argument",
            "token": "secret bearer token",
            "result": "private tool result",
            "error": "private provider error",
            "stack": "private stack value",
        }

        request_logger.log_event(
            "generic",
            {
                "message": secrets["message"],
                "result": secrets["result"],
                "api_token": secrets["token"],
            },
        )
        request_logger.log_tool_call(
            "demo",
            {"query": secrets["argument"], "api_token": secrets["token"]},
        )
        request_logger.log_error(
            "ProviderError",
            secrets["error"],
            secrets["stack"],
        )

        rendered = json.dumps(request_logger.get_logs(), ensure_ascii=False)
        for secret in secrets.values():
            self.assertNotIn(secret, rendered)
        self.assertIn("message_length", rendered)
        self.assertIn("result_length", rendered)
        self.assertIn("argument_length", rendered)
        self.assertIn("traceback_length", rendered)


class StructuredLoggerPrivacyTest(unittest.TestCase):
    def test_export_contains_only_lengths_and_argument_keys(self) -> None:
        logger = StructuredLogger()
        trace_id = "trace-private"
        secrets = (
            "private user request",
            "private system prompt",
            "private tool argument",
            "short-secret",
            "private tool result",
            "private reasoning",
            "private exception message",
            "private context value",
        )

        logger.log_request(trace_id, "POST", "/api/chat", "agent", secrets[0])
        logger.log_prompt(trace_id, secrets[1], secrets[0], 2, "local", "model")
        logger.log_tool_call(
            trace_id,
            "demo",
            {"query": secrets[2], "api_key": secrets[3]},
            tool_result=secrets[4],
            error=secrets[6],
        )
        logger.log_reasoning(trace_id, secrets[5])
        try:
            raise RuntimeError(secrets[6])
        except RuntimeError as exc:
            logger.log_error(trace_id, exc, {"note": secrets[7]})

        rendered = json.dumps(logger.get_full_log_package(trace_id), ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, rendered)
        self.assertEqual(logger._mask_value("abcd1234"), "<redacted>")
        self.assertNotIn("abcd", rendered)
        self.assertIn("user_input_length", rendered)
        self.assertIn("reasoning_length", rendered)
        self.assertIn("argument_length", rendered)


if __name__ == "__main__":
    unittest.main()
