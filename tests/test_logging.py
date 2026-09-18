# 日志落盘的位置、体量与脱敏。
# 这里出现的 Token 全是显眼的假值：未被忽略的文件里不得留下任何真实凭据。
import logging
import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

from src.tchmaterial_parser import config, logging_utils, platform_utils


FAKE_TOKEN = "FAKE-ACCESS-TOKEN-0123456789"
FAKE_MAC_KEY = "FAKE-MAC-KEY-9876543210"


class LogDirPathTest(unittest.TestCase):
    def test_log_dir_sits_next_to_the_config_file(self) -> None:
        for os_name, config_path in {
            "Windows": Path("C:/Users/tester/AppData/Local/tchMaterial-parser/data.json"),
            "Linux": Path("/home/tester/.config/tchMaterial-parser/data.json"),
            "Darwin": Path("/Users/tester/Library/Application Support/tchMaterial-parser/data.json"),
        }.items():
            with self.subTest(os_name=os_name):
                with patch.object(config, "config_file_path", lambda path=config_path: path):
                    self.assertEqual(config.log_dir_path(), config_path.parent / "logs")

    def test_log_dir_is_none_on_an_unsupported_system(self) -> None:
        with patch.object(config, "config_file_path", lambda: None):
            self.assertIsNone(config.log_dir_path())


class SetupLoggingTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.log_dir = Path(temp_dir.name) / "tchMaterial-parser" / "logs"
        self.addCleanup(self.restore_logger)
        self.previous_handlers = list(logging_utils.logger.handlers)
        self.previous_level = logging_utils.logger.level
        logging_utils.logger.handlers.clear()
        self.context = patch.object(logging_utils, "_configured", False)
        self.context.start()
        self.addCleanup(self.context.stop)

    def restore_logger(self) -> None: # 用例之间不能互相污染同一个全局 logger
        for handler in list(logging_utils.logger.handlers):
            handler.close()
        logging_utils.logger.handlers[:] = self.previous_handlers
        logging_utils.logger.setLevel(self.previous_level)

    def file_handlers(self) -> list[logging.Handler]:
        return [handler for handler in logging_utils.logger.handlers if isinstance(handler, logging_utils.RotatingFileHandler)]

    def test_rotation_limits_are_applied(self) -> None:
        with patch.object(logging_utils.config, "log_dir_path", lambda: self.log_dir):
            logging_utils.setup_logging()

        handler = self.file_handlers()[0]
        self.assertEqual(handler.maxBytes, logging_utils.LOG_MAX_BYTES)
        self.assertEqual(handler.backupCount, logging_utils.LOG_BACKUP_COUNT)
        self.assertTrue(self.log_dir.is_dir())

    def test_repeated_setup_does_not_stack_handlers(self) -> None:
        with patch.object(logging_utils.config, "log_dir_path", lambda: self.log_dir):
            logging_utils.setup_logging()
            first = list(logging_utils.logger.handlers)
            logging_utils.setup_logging()

        self.assertEqual(logging_utils.logger.handlers, first)

    def test_missing_log_dir_leaves_only_the_console_handler(self) -> None:
        with patch.object(logging_utils.config, "log_dir_path", lambda: None):
            logging_utils.setup_logging() # 不应抛异常，否则 main() 第一行就会把程序拦住

        self.assertEqual(self.file_handlers(), [])

    def test_unwritable_log_dir_leaves_only_the_console_handler(self) -> None:
        with patch.object(logging_utils.config, "log_dir_path", lambda: self.log_dir):
            with patch.object(Path, "mkdir", side_effect=PermissionError("Operation not permitted")):
                logging_utils.setup_logging()

        self.assertEqual(self.file_handlers(), [])


class RedactionTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.log_file = Path(temp_dir.name) / "test.log"
        self.logger = logging.getLogger("tchmaterial_parser.tests.redaction")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        handler = logging.FileHandler(self.log_file, encoding="utf-8")
        handler.setFormatter(logging_utils.RedactingFormatter(logging_utils.LOG_FORMAT))
        self.logger.addHandler(handler)
        self.addCleanup(self.logger.handlers.clear)
        self.addCleanup(handler.close)
        credentials = patch.multiple(logging_utils.config, access_token=FAKE_TOKEN, mac_key=FAKE_MAC_KEY)
        credentials.start()
        self.addCleanup(credentials.stop)

    def written(self) -> str:
        for handler in self.logger.handlers:
            handler.flush()
        return self.log_file.read_text(encoding="utf-8")

    def assert_no_plaintext(self, text: str) -> None:
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn(FAKE_MAC_KEY, text)
        self.assertIn(logging_utils.REDACTED, text)

    def test_message_and_args_are_redacted(self) -> None:
        self.logger.info("请求 %s", f"https://example.com/file.pdf?accessToken={FAKE_TOKEN}&x=1")
        self.logger.info("Authorization: Bearer %s", FAKE_TOKEN)
        self.logger.info('X-ND-AUTH: MAC id="%s",nonce="0",mac="0"', FAKE_TOKEN)
        self.logger.info("Cookie: session=%s", FAKE_TOKEN)
        self.logger.info("裸串形式的凭据 %s 与 %s", FAKE_TOKEN, FAKE_MAC_KEY)

        self.assert_no_plaintext(self.written())

    def test_exception_traceback_is_redacted(self) -> None:
        # requests 的连接异常会把完整 URL 写进消息里，而这段文本是 Formatter 事后才拼出来的
        try:
            raise ConnectionError(f"HTTPSConnectionPool: /a.pdf?accessToken={FAKE_TOKEN} 连接失败")
        except ConnectionError as error:
            self.logger.error("下载失败", exc_info=error)

        written = self.written()
        self.assert_no_plaintext(written)
        self.assertIn("ConnectionError", written) # 脱敏不该把堆栈本身一起抹掉

    def test_print_error_goes_through_the_logger(self) -> None:
        with self.assertLogs(platform_utils.logger, level=logging.ERROR) as captured:
            platform_utils.print_error(ValueError("配置项 theme 必须是字符串"))

        self.assertIn("配置项 theme 必须是字符串", "\n".join(captured.output))

    def test_redact_access_token_keeps_the_rest_of_the_url(self) -> None:
        redacted = logging_utils.redact_access_token(f"https://example.com/a.pdf?accessToken={FAKE_TOKEN}&page=3")

        self.assertEqual(redacted, f"https://example.com/a.pdf?accessToken={logging_utils.REDACTED}&page=3")


class EnvironmentAndDurationTest(unittest.TestCase):
    def test_dependency_version_falls_back_when_metadata_is_missing(self) -> None:
        # 打包产物里没有依赖的 dist-info，这一条不能把启动带崩
        with patch.object(metadata, "version", side_effect=metadata.PackageNotFoundError("pypdf")):
            self.assertEqual(logging_utils.dependency_version("pypdf", "no_such_module_for_tests"), logging_utils.UNKNOWN_VERSION)
            self.assertEqual(logging_utils.dependency_version("requests", "requests"), __import__("requests").__version__)

    def test_log_environment_survives_missing_metadata_and_tk(self) -> None:
        broken_root = object() # 没有 getvar / tk，模拟读不到 Tcl/Tk 信息
        with patch.object(metadata, "version", side_effect=metadata.PackageNotFoundError("pillow")):
            with self.assertLogs(logging_utils.logger, level=logging.INFO) as captured:
                logging_utils.log_environment(broken_root)

        output = "\n".join(captured.output)
        self.assertIn("应用版本", output)
        self.assertIn("依赖版本", output)

    def test_log_duration_warns_only_beyond_the_threshold(self) -> None:
        action_logger = logging.getLogger("tchmaterial_parser.tests.duration")

        with self.assertLogs(action_logger, level=logging.DEBUG) as fast:
            with logging_utils.log_duration(action_logger, "快操作", warn_ms=60000):
                pass
        self.assertEqual(fast.records[0].levelno, logging.DEBUG)

        with self.assertLogs(action_logger, level=logging.DEBUG) as slow:
            with logging_utils.log_duration(action_logger, "慢操作", warn_ms=0.0001):
                pass
        self.assertEqual(slow.records[0].levelno, logging.WARNING)


if __name__ == "__main__":
    unittest.main()
