# -*- coding: utf-8 -*-
# 供 test_download_resume_integration.py 共用的本地测试服务器：
# 真的实现 Range / If-Range / ETag / 416 语义，而不是只回 200——否则证明不了续传，
# 只是个更慢的单元测试。绑 port 0，调用方自己读回内核分配的实际端口。

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import threading
import time

_RANGE_PATTERN = re.compile(r"bytes=(\d+)-")


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True # 测试进程退出时不必等待还在收尾的连接

    def handle_error(self, request, client_address) -> None:
        pass # 暂停/取消会让客户端主动断开连接，这是预期行为，不需要打印堆栈噪音


class RangeAwareHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None: # noqa: N802 （http.server 的既定命名）
        # 每个响应后都主动断开连接：暂停/取消会在响应写到一半时被迫中断，
        # 若允许连接池复用同一条 keep-alive 连接，下一次请求可能读到上一条
        # 响应没写完的尾部字节，把响应解析成乱码——这是测试服务器要避免的问题，
        # 与真实 CDN 处理长连接的方式无关，不影响 Range/If-Range/416 语义本身。
        self.close_connection = True
        content: bytes = self.server.content
        etag: str = self.server.etag
        total = len(content)

        range_header = self.headers.get("Range")
        if_range = self.headers.get("If-Range")

        def record(responded_status: int) -> None:
            # 供测试断言“真的走了 Range 续传”：不能只看最终字节，字节一致也可能是
            # 续传退化成了不带 Range 的全量重下——请求头与服务端实际回的状态码才是证据。
            self.server.requests.append({"range": range_header, "if_range": if_range, "status": responded_status})

        if range_header:
            match = _RANGE_PATTERN.fullmatch(range_header)
            start = int(match.group(1)) if match else 0

            if if_range and if_range != etag: # 校验子不匹配：远端已变化，回整份最新内容
                record(200)
                self._send_full(content, etag)
                return

            if start >= total: # 范围无效：offset 早就超出了当前内容的长度
                record(416)
                body = b""
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                return

            record(206)
            body = content[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", etag)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Connection", "close")
            self.end_headers()
            self._write_slowly(body)
            return

        record(200)
        self._send_full(content, etag)

    def _send_full(self, content: bytes, etag: str) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("ETag", etag)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Connection", "close")
        self.end_headers()
        self._write_slowly(content)

    def _write_slowly(self, body: bytes) -> None:
        # 分成小块慢慢写，给测试一个能在“中途”暂停/取消的真实时间窗口，
        # 而不是整份内容一次系统调用写完，快到测试永远抓不到中途状态。
        chunk_size = 65536
        try:
            for offset in range(0, len(body), chunk_size):
                self.wfile.write(body[offset:offset + chunk_size])
                self.wfile.flush()
                time.sleep(0.005)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass # 客户端暂停/取消时主动断开，属于正常路径，不是服务器故障

    def log_message(self, format: str, *args: object) -> None: # 静默访问日志，避免淹没测试输出
        pass


def start_range_server(content: bytes, etag: str) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    """起一个绑在 127.0.0.1:0（内核分配空闲端口）上的服务，返回 (服务对象, 服务线程, 完整 URL)。"""
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), RangeAwareHandler)
    server.content = content
    server.etag = etag
    server.requests = [] # 每次请求的 Range/If-Range 头与服务端实际回的状态码，供测试断言真的走了续传
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}/book.pdf"


def stop_range_server(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
