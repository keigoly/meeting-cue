"""Soniox のクライアント(FR-2b・FR-14・2026-10-17): WebSocket の枠の作り方と、Soniox を真似た手元のサーバーでの一通りの流れ。"""
import base64
import hashlib
import json
import socket
import struct
import threading

from meetcue import soniox, wsclient


def test_frame_round_trip_and_mask():
    for n in (0, 5, 125, 126, 4000, 70000):
        payload = bytes(range(256)) * (n // 256) + bytes(range(n % 256))
        frame = wsclient.encode_frame(wsclient.OP_BINARY, payload, mask_key=b"\x01\x02\x03\x04")
        got = wsclient.parse_frame(frame)
        assert got is not None
        (fin, op, body), used = got
        assert (fin, op, body, used) == (True, wsclient.OP_BINARY, payload, len(frame))
    assert wsclient.parse_frame(frame[:-1]) is None          # 足りなければ待つ(読みかけを捨てない)


def test_config_hints_terms_and_no_key():
    cfg = soniox.config(["Claude Code", " ", "RunPod"])
    assert cfg["model"] == "stt-rt-v5" and cfg["sample_rate"] == 16000 and cfg["num_channels"] == 1
    assert cfg["language_hints"] == ["ja", "en"]
    assert cfg["context"] == {"terms": ["Claude Code", "RunPod"]}
    assert "api_key" not in cfg                               # 鍵は Authorization ヘッダだけ
    assert "context" not in soniox.config([])


def test_handle_tokens_final_partial_markers_and_error():
    s = soniox.Session("k")
    s.t_open = 0.0
    s._handle({"tokens": [{"text": "こん", "is_final": True, "language": "ja"},
                          {"text": "にち", "is_final": False}]})
    assert s.text == "こん" and s.partial == "にち"
    s._handle({"tokens": [{"text": "にちは", "is_final": True, "language": "ja"}, {"text": "<end>", "is_final": True},
                          {"text": "<fin>", "is_final": True}]})
    assert s.text == "こんにちは" and s.partial == "" and s.ends == 1 and s._fin.is_set()
    assert s.languages() == {"ja": 2}
    assert s._handle({"error_code": 402, "error_message": "Insufficient balance"}) is True
    assert s.error_code == 402 and "402" in s.error


class FakeSoniox(threading.Thread):
    """1 回だけ受ける Soniox もどき: 設定 → 音声 → finalize で語と <fin> → 空のテキストで finished。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.headers: dict[str, str] = {}
        self.config: dict = {}
        self.audio = 0

    def send(self, conn, obj):
        body = json.dumps(obj).encode()
        conn.sendall(bytes([0x81, len(body)]) + body if len(body) < 126
                     else bytes([0x81, 126]) + struct.pack("!H", len(body)) + body)

    def run(self):
        conn, _ = self.srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        lines = req.decode().split("\r\n")
        self.headers = {ln.split(":", 1)[0].lower(): ln.split(":", 1)[1].strip() for ln in lines[1:] if ":" in ln}
        accept = base64.b64encode(hashlib.sha1((self.headers["sec-websocket-key"] + wsclient._GUID).encode()).digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        buf = b""
        while True:
            got = wsclient.parse_frame(buf)
            if got is None:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
                continue
            (_, op, payload), used = got
            buf = buf[used:]
            if op == wsclient.OP_BINARY:
                self.audio += len(payload)
                self.send(conn, {"tokens": [{"text": "途中", "is_final": False}]})
            elif op == wsclient.OP_TEXT and not self.config:
                self.config = json.loads(payload)
            elif op == wsclient.OP_TEXT and payload and json.loads(payload).get("type") == "finalize":
                self.send(conn, {"tokens": [{"text": "テスト", "is_final": True, "language": "ja", "start_ms": 0,
                                             "end_ms": 500}, {"text": "<fin>", "is_final": True}]})
            elif op == wsclient.OP_TEXT and payload == b"":
                self.send(conn, {"tokens": [], "finished": True})
            elif op == wsclient.OP_CLOSE:
                conn.close()
                return


def test_session_end_to_end_with_fake_server():
    fake = FakeSoniox()
    fake.start()
    seen = []
    s = soniox.Session("secret-key", terms=["Meeting Cue"], url=f"ws://127.0.0.1:{fake.port}/transcribe-websocket",
                       on_update=lambda sess: seen.append(sess.partial))
    assert s.start(), s.error
    for _ in range(3):
        assert s.send_audio(b"\x00\x00" * 1600)          # 100 ms ずつ
    s.finalize()
    assert s.wait_fin(5.0)
    s.close()
    fake.join(5.0)
    assert fake.headers["authorization"] == "Bearer secret-key"
    assert fake.config["context"] == {"terms": ["Meeting Cue"]} and "api_key" not in fake.config
    assert fake.audio == 9600 and s.audio_s == 0.3
    assert s.text == "テスト" and s.error is None and s.finalize_s is not None and "途中" in seen


def test_marks_partial_tokens_and_proc_ms():
    """会議(2026-10-18): 確定("<fin>" / "<end>")のたびにその時点の語の数を marks に積む。処理済みの長さと応答の時刻も持つ。"""
    s = soniox.Session("k")
    s.t_open = 0.0
    s._handle({"tokens": [{"text": "一", "is_final": True, "start_ms": 0, "end_ms": 100}, {"text": "<fin>", "is_final": True},
                          {"text": "二", "is_final": False, "start_ms": 120, "end_ms": 300}], "total_audio_proc_ms": 480})
    assert s.marks == [("fin", 1)] and s.proc_ms == 480 and s.t_last_msg is not None
    assert s.partial_tokens == [{"text": "二", "start_ms": 120, "end_ms": 300}] and s.partial == "二"
    s._handle({"tokens": [{"text": "二", "is_final": True}, {"text": "<end>", "is_final": True}]})
    assert s.marks == [("fin", 1), ("end", 2)] and s.partial_tokens == [] and s.text == "一二"


def test_refused_handshake_reports_http_status():
    """鍵が違うと、WebSocket への切り替えの時点で 401 が返る。端末内へ切り替えるかを決めるため、状態の番号を残す。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
        conn.close()

    threading.Thread(target=run, daemon=True).start()
    s = soniox.Session("bad", url=f"ws://127.0.0.1:{port}/transcribe-websocket")
    assert s.start() is False and s.error_code == 401 and "401" in s.error
    srv.close()


def test_connect_failure_is_reported_not_raised():
    s = soniox.Session("k", url="ws://127.0.0.1:9/none", connect_timeout=1.0)
    assert s.start() is False and s.error.startswith("connect:")
    assert s.send_audio(b"\x00\x00") is False and s.wait_fin(0.1) is False
    s.close()
