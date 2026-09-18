# 加载失败时写进提示行的那句原因：要能区分不同故障，且不带出凭据。
import sys
import unittest
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tchmaterial_parser import app


SAMPLE_SECRET = "SAMPLE-SECRET-ABCDEFGHIJ"
POOL = "HTTPSConnectionPool(host='s-file-1.ykt.cbern.com.cn', port=443): Max retries exceeded with url: /zxx/ndrs/x.json"


class FailureReasonTest(unittest.TestCase):
    def test_the_exception_type_leads_and_survives_truncation(self) -> None:
        # 两类故障的消息开头完全一样，只留消息的话在界面上分不出来
        connection = app.failure_reason(ConnectionError(POOL))
        timeout = app.failure_reason(TimeoutError(POOL))

        self.assertTrue(connection.startswith("ConnectionError："), connection)
        self.assertTrue(timeout.startswith("TimeoutError："), timeout)
        self.assertNotEqual(connection, timeout)

    def test_long_messages_are_truncated_but_keep_the_type(self) -> None:
        reason = app.failure_reason(ConnectionError(POOL))

        self.assertLessEqual(len(reason), len("ConnectionError：") + app.FAILURE_MESSAGE_MAX_LENGTH + 1)
        self.assertTrue(reason.endswith("…"))

    def test_an_empty_message_falls_back_to_the_type(self) -> None:
        self.assertEqual(app.failure_reason(ValueError()), "ValueError")

    def test_whitespace_is_collapsed_into_one_line(self) -> None: # 提示行只有一行，换行会把后半句挤没
        reason = app.failure_reason(ValueError("第一行\n第二行\t第三行"))

        self.assertEqual(reason, "ValueError：第一行 第二行 第三行")

    def test_credentials_in_the_message_are_redacted(self) -> None:
        reason = app.failure_reason(ConnectionError(f"请求 https://a.com/x.pdf?accessToken={SAMPLE_SECRET} 失败"))

        self.assertNotIn(SAMPLE_SECRET, reason)
        self.assertIn("ConnectionError", reason)


if __name__ == "__main__":
    unittest.main()
