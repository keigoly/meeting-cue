"""stdlib だけの小さな WebSocket クライアント(RFC 6455 のうち、クライアントとして要る分だけ・2026-10-17)。

Soniox のリアルタイム認識(REQUIREMENTS FR-2b・FR-14)のために書いた。公式 SDK・websockets は入れない(§7 依存の方針)。
- wss:// と ws://(試験用)。圧縮などの拡張は申し込まない。送るフレームは分割しない(受けた分割はつなぐ)。
- 受信は `poll(timeout)` で、届いている分だけを返す。ping には pong を返し、close を受けたら close を返す。
- **1 本の接続は 1 つのスレッドからだけ触る**(SSL の接続を送信と受信の 2 つのスレッドから同時に使わない)。
  送受信を混ぜたいときは、呼び出し側が 1 本のスレッドで「送る分を送る → poll」を回す(meetcue.soniox)。
- キー(Authorization など)は送るだけで、例外の文やログに出さない。
"""
from __future__ import annotations

import base64
import hashlib
import os
import select
import socket
import ssl
import struct
from urllib.parse import urlsplit

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HEADER = 64 * 1024


class WebSocketError(Exception):
    status: int | None = None   # 切り替えを断られたときの HTTP の状態(401 など)


def _mask(data: bytes, key: bytes) -> bytes:
    """4 バイトの鍵で XOR する(整数にまとめて 1 回で。音声 100 ms = 3,200 バイトを 1 バイトずつ回さない)。"""
    if not data:
        return data
    k = (key * (len(data) // 4 + 1))[: len(data)]
    return (int.from_bytes(data, "little") ^ int.from_bytes(k, "little")).to_bytes(len(data), "little")


def encode_frame(opcode: int, payload: bytes, mask_key: bytes | None = None) -> bytes:
    """クライアントから送るフレーム(FIN 付き・必ずマスクする)。"""
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack("!H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack("!Q", n)
    key = mask_key if mask_key is not None else os.urandom(4)
    return head + key + _mask(payload, key)


def parse_frame(buf: bytes) -> tuple[tuple[bool, int, bytes], int] | None:
    """buf の頭に 1 つ分そろっていれば ((fin, opcode, payload), 使ったバイト数)。足りなければ None。"""
    if len(buf) < 2:
        return None
    b1, b2 = buf[0], buf[1]
    n, pos = b2 & 0x7F, 2
    if n == 126:
        if len(buf) < 4:
            return None
        n, pos = struct.unpack("!H", buf[2:4])[0], 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n, pos = struct.unpack("!Q", buf[2:10])[0], 10
    key = None
    if b2 & 0x80:
        if len(buf) < pos + 4:
            return None
        key, pos = buf[pos:pos + 4], pos + 4
    if len(buf) < pos + n:
        return None
    payload = buf[pos:pos + n]
    if key:
        payload = _mask(payload, key)
    return (bool(b1 & 0x80), b1 & 0x0F, payload), pos + n


class WebSocket:
    def __init__(self, sock: socket.socket, buf: bytes = b""):
        self.sock = sock
        self._buf = buf
        self._frag_op: int | None = None
        self._frags: list[bytes] = []
        self.closed = False

    @classmethod
    def connect(cls, url: str, headers: dict[str, str] | None = None, timeout: float = 10.0) -> "WebSocket":
        u = urlsplit(url)
        if u.scheme not in ("wss", "ws") or not u.hostname:
            raise WebSocketError(f"wss:// か ws:// の URL が要る: {u.scheme}://{u.hostname}")
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        try:
            sock: socket.socket = raw
            if u.scheme == "wss":
                sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname)
            key = base64.b64encode(os.urandom(16)).decode()
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            host = u.hostname if u.port is None else f"{u.hostname}:{u.port}"
            lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
                     f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
            lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(4096)
                if not chunk:
                    raise WebSocketError("接続の途中で切れた(応答の見出しが来ない)")
                resp += chunk
                if len(resp) > MAX_HEADER:
                    raise WebSocketError("応答の見出しが長すぎる")
            head, rest = resp.split(b"\r\n\r\n", 1)
            status, *fields = head.decode("latin-1").split("\r\n")
            if " 101 " not in f"{status} ":
                err = WebSocketError(f"WebSocket に切り替わらない: {status.strip()}")
                code = status.split(" ")[1] if len(status.split(" ")) > 1 else ""
                err.status = int(code) if code.isdigit() else None
                raise err
            got = {f.split(":", 1)[0].strip().lower(): f.split(":", 1)[1].strip() for f in fields if ":" in f}
            want = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
            if got.get("sec-websocket-accept") != want:
                raise WebSocketError("Sec-WebSocket-Accept が合わない")
            # 送る側が詰まっても数秒は待つ(0.5 s では回線の一時的な詰まりで送信が時間切れになった・2026-10-17)。
            # 受ける側は poll が select で届いたのを確かめてから読むので、この長さで待たされることはほぼ無い
            sock.settimeout(10.0)
            return cls(sock, rest)
        except BaseException:
            raw.close()
            raise

    def send(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise WebSocketError("閉じた接続には送れない")
        self.sock.sendall(encode_frame(opcode, payload))

    def send_text(self, text: str) -> None:
        self.send(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self.send(OP_BINARY, data)

    def poll(self, timeout: float = 0.0) -> list[tuple[int, bytes]]:
        """届いているメッセージ(text / binary / close)を返す。何も無ければ最大 timeout 秒待つ。切れたら close を返す。"""
        out = self._drain()
        if out or self.closed:
            return out
        pending = isinstance(self.sock, ssl.SSLSocket) and self.sock.pending()
        if not pending and not select.select([self.sock], [], [], timeout)[0]:
            return out
        try:
            chunk = self.sock.recv(65536)
        except (socket.timeout, ssl.SSLWantReadError):
            return out
        if not chunk:
            self.closed = True
            return [(OP_CLOSE, b"")]
        self._buf += chunk
        return self._drain()

    def _drain(self) -> list[tuple[int, bytes]]:
        out: list[tuple[int, bytes]] = []
        while (got := parse_frame(self._buf)) is not None:
            (fin, op, payload), used = got
            self._buf = self._buf[used:]
            if op == OP_PING:
                self.send(OP_PONG, payload)
            elif op == OP_PONG:
                continue
            elif op == OP_CLOSE:
                if not self.closed:
                    try:
                        self.sock.sendall(encode_frame(OP_CLOSE, payload[:2]))
                    except OSError:
                        pass
                self.closed = True
                out.append((OP_CLOSE, payload))
                break
            elif op in (OP_TEXT, OP_BINARY):
                if fin:
                    out.append((op, payload))
                else:
                    self._frag_op, self._frags = op, [payload]
            elif op == OP_CONT and self._frag_op is not None:
                self._frags.append(payload)
                if fin:
                    out.append((self._frag_op, b"".join(self._frags)))
                    self._frag_op, self._frags = None, []
        return out

    def close(self, code: int = 1000) -> None:
        if not self.closed:
            try:
                self.sock.sendall(encode_frame(OP_CLOSE, struct.pack("!H", code)))
            except OSError:
                pass
            self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
