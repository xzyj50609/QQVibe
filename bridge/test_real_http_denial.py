"""Rejected POST transport on real loopback sockets; no provider or user data."""
import socket
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from real_http import make_handler


class DenialTests(unittest.TestCase):
    def setUp(self):
        self.finished = threading.Event()
        self.errors = []
        parent = make_handler(SimpleNamespace())
        finished = self.finished
        class Handler(parent):
            def do_POST(self):
                try:
                    return super().do_POST()
                finally:
                    finished.set()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.handle_error = lambda *_: self.errors.append("unexpected handler error")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def client(self, length):
        client = socket.create_connection(self.server.server_address, timeout=2)
        self.addCleanup(client.close)
        client.sendall((f"POST /api/model-source/list HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                        f"Origin: https://untrusted.example\r\nContent-Length: {length}\r\n\r\n").encode())
        return client

    def response(self, client):
        response = b""
        while chunk := client.recv(4096):
            response += chunk
        self.assertIn(b"403 Forbidden", response)
        self.assertIn(b'{"error": "forbidden"}', response)
        return response

    def test_split_headers_and_later_body_reliably_return_denial(self):
        for _ in range(20):
            with self.client(16) as client:
                time.sleep(.01)
                client.sendall(b'{"blocked":true}')
                self.assertNotIn(b"blocked", self.response(client))

    def test_body_that_never_arrives_does_not_hold_the_handler(self):
        client = self.client(65536)
        self.assertTrue(self.finished.wait(1))
        self.response(client)

    def test_dripping_body_cannot_extend_absolute_discard_deadline(self):
        client = self.client(65536)
        def drip():
            for _ in range(40):
                try:
                    client.sendall(b"x")
                except OSError:
                    return
                time.sleep(.02)
        sender = threading.Thread(target=drip, daemon=True)
        sender.start()
        try:
            self.assertTrue(self.finished.wait(.6), "drip extended the absolute discard deadline")
        finally:
            client.close()
            sender.join(timeout=1)

    def test_oversized_declaration_is_not_drained_or_dispatched(self):
        client = self.client(1000000000)
        self.assertTrue(self.finished.wait(.6))
        self.response(client)

    def test_empty_and_invalid_declarations_deny_without_handler_errors(self):
        for length in (0, "invalid", -1):
            self.finished.clear()
            with self.client(length) as client:
                self.assertTrue(self.finished.wait(.6))
                self.response(client)


if __name__ == "__main__":
    unittest.main()
