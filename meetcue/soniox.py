"""Soniox のリアルタイム認識(stt-rt-v5)のクライアント(REQUIREMENTS FR-2b・FR-14・2026-10-17)— stdlib のみ。

公開仕様どおりの WebSocket(`wss://stt-rt.soniox.com/transcribe-websocket`)。流れ:
  接続(鍵は Authorization: Bearer)→ 設定の JSON を 1 通 → 音声(16 kHz モノラル s16le)をバイナリで流す →
  話し終わりに {"type": "finalize"} → それまでの語がすべて確定すると "<fin>" が届く → 空のテキストで終わりを伝える →
  {"finished": true} が届いて閉じる。
- 応答の tokens には「確定した語(is_final)」と「まだ変わりうる語」が混ざる。確定した語は 1 度しか来ないのでつなぐ。
  まだ変わりうる語は毎回その時点の全部が来る(前の分と置き換える)。
- "<fin>"(finalize の完了)と "<end>"(話の区切り・endpoint_detection のとき)は印で、文字には入れない。
- 語彙は context.terms(使う語だけを正しい表記で。余計な語は取り違えの元になる)。言語のヒントは既定で日本語と英語
  (ヒントなしでは名前の羅列を韓国語と判定した・2026-10-17 の読み上げの計測)。
- 鍵を開始の JSON の api_key で送る形は 2027-01-15 から拒否されるので使わない。
- **送受信は 1 本のスレッド(`_loop`)だけが接続を触る**。send_audio / finalize は列に積むだけ。
- raise しない: 失敗は `error`(と Soniox の `error_code`)に入れる。端末内の認識へ戻すかは呼び出し側が決める。
- 会議では 1 本の接続で finalize を何度も送る(2026-10-18・meetcue.soniox_stt): "<fin>" / "<end>" が届くたびに、その時点の
  確定した語の数を `marks` に積む(どこまでが 1 回の確定かを呼び出し側が分けられる)。`proc_ms` は Soniox が処理し終えた
  音の長さ(total_audio_proc_ms・詰まりの見張りに使う)。接続が終わったら `on_done` を 1 回呼ぶ。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Callable

from . import wsclient

URLS = {"us": "wss://stt-rt.soniox.com/transcribe-websocket",
        "jp": "wss://stt-rt.jp.soniox.com/transcribe-websocket"}   # 日本の地域はプロジェクトごとの申請・鍵も別
MODEL = "stt-rt-v5"
SAMPLE_RATE = 16000
BYTES_PER_S = SAMPLE_RATE * 2
END_OF_AUDIO = ""   # 終わりの合図は空のテキストフレーム(空のバイナリでは終わらない)


def config(terms: list[str] | tuple[str, ...] = (), language_hints: list[str] | tuple[str, ...] = ("ja", "en"),
           model: str = MODEL, endpoint_detection: bool = False, sample_rate: int = SAMPLE_RATE,
           context: dict | None = None) -> dict:
    """開始の JSON(鍵は入れない)。context は音声入力の画面の文脈(FR-14b・選んだときだけ):
    {"general": [(項目, 値), …], "text": カーソルの前の文}。語彙と同じ context に入れる。"""
    cfg: dict = {"model": model, "audio_format": "pcm_s16le", "sample_rate": sample_rate, "num_channels": 1,
                 "enable_language_identification": True}
    if language_hints:
        cfg["language_hints"] = list(language_hints)
    ctx: dict = {}
    general = [{"key": str(k), "value": str(v)} for k, v in (context or {}).get("general", []) if str(v).strip()]
    if general:
        ctx["general"] = general
    if (context or {}).get("text", "").strip():
        ctx["text"] = context["text"]
    words = [t.strip() for t in terms if t and t.strip()]
    if words:
        ctx["terms"] = words
    if ctx:
        cfg["context"] = ctx
    if endpoint_detection:
        cfg["enable_endpoint_detection"] = True
    return cfg


class Session:
    """1 本の音声(1 チャネル)を流すセッション。start → send_audio … → finalize → wait_fin → close。"""

    def __init__(self, api_key: str, *, terms: list[str] | tuple[str, ...] = (),
                 language_hints: list[str] | tuple[str, ...] = ("ja", "en"), region: str = "us", model: str = MODEL,
                 endpoint_detection: bool = False, on_update: Callable[["Session"], None] | None = None,
                 connect_timeout: float = 10.0, url: str | None = None, sample_rate: int = SAMPLE_RATE,
                 on_done: Callable[["Session"], None] | None = None, context: dict | None = None):
        self._key = api_key
        self.url = url or URLS.get(region, URLS["us"])
        self.cfg = config(terms, language_hints, model, endpoint_detection, sample_rate, context)
        self.bytes_per_s = sample_rate * 2
        self.on_update = on_update
        self.on_done = on_done
        self.connect_timeout = connect_timeout
        self.final_tokens: list[dict] = []   # 確定した語。recv_s = 流し始めてから届くまでの秒
        self.partial = ""                    # まだ変わりうる語をつないだもの(最新)
        self.partial_tokens: list[dict] = []  # まだ変わりうる語(時刻つき・最新)
        self.marks: list[tuple[str, int]] = []   # ("fin" | "end", その時点の final_tokens の数)
        self.ends = 0                        # "<end>" の数
        self.proc_ms = 0                     # Soniox が処理し終えた音の長さ(total_audio_proc_ms)
        self.t_last_msg: float | None = None
        self.error: str | None = None
        self.error_code: int | None = None
        self.close_code: int | None = None   # 相手から閉じられたときの WebSocket の番号と理由(誤りの JSON なしで切られたとき)
        self.close_reason = ""
        self.connect_ms: int | None = None
        self.audio_bytes = 0
        self.t_open: float | None = None
        self.t_finalize: float | None = None
        self.t_fin: float | None = None
        self.lock = threading.Lock()         # 語の更新(_handle)と、別のスレッドからの読み取りをそろえる
        self._q: queue.Queue = queue.Queue()
        self._fin = threading.Event()
        self._done = threading.Event()
        self._ws: wsclient.WebSocket | None = None
        self._thread: threading.Thread | None = None

    # --- 呼び出し側 ---
    def start(self) -> bool:
        t0 = time.monotonic()
        try:
            self._ws = wsclient.WebSocket.connect(self.url, {"Authorization": f"Bearer {self._key}"},
                                                  timeout=self.connect_timeout)
            self._ws.send_text(json.dumps(self.cfg, ensure_ascii=False))
        except Exception as err:   # 鍵は例外の文に入らない(ヘッダは送るだけ)
            self.error = f"connect: {type(err).__name__}: {err}"
            self.error_code = getattr(err, "status", None)   # 接続の時点で断られた HTTP の状態(401 など)
            self._done.set()
            return False
        self.t_open = time.monotonic()
        self.connect_ms = int((self.t_open - t0) * 1000)
        self._thread = threading.Thread(target=self._loop, name="soniox", daemon=True)
        self._thread.start()
        return True

    def send_audio(self, pcm: bytes) -> bool:
        if self._done.is_set() or not pcm:
            return False
        self.audio_bytes += len(pcm)
        self._q.put((wsclient.OP_BINARY, pcm))
        return True

    def finalize(self) -> None:
        if self.t_finalize is None:
            self.t_finalize = time.monotonic()
        self._q.put((wsclient.OP_TEXT, json.dumps({"type": "finalize"}).encode()))

    def keepalive(self) -> None:
        """音を送らない間に接続を保つ(音も keepalive も無いと、約 21 s で 408 Request timeout を返して閉じる・2026-10-18 実測)。
        Windows のループバックは何も鳴っていないと音を渡さないので、会議の始まりの待ち時間に要る。"""
        self._q.put((wsclient.OP_TEXT, json.dumps({"type": "keepalive"}).encode()))

    def fin_count(self) -> int:
        """届いた "<fin>" の数(1 本の接続で finalize を何度も送るとき・音声入力のリアルタイム)。"""
        with self.lock:
            return sum(1 for kind, _ in self.marks if kind == "fin")

    def wait_fin_count(self, n: int, timeout: float) -> bool:
        """"<fin>" が n 個そろうのを待つ。接続が先に終わったら待たない。"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.fin_count() >= n:
                return True
            if self._done.is_set():
                break
            time.sleep(0.005)
        return self.fin_count() >= n

    def wait_fin(self, timeout: float) -> bool:
        """finalize の完了("<fin>")を待つ。接続が先に終わったら待たない。"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._fin.is_set():
                return True
            if self._done.is_set():
                return self._fin.is_set()
            time.sleep(0.005)
        return self._fin.is_set()

    def close(self, timeout: float = 5.0) -> None:
        """終わりの合図を送り、{"finished": true} を待って閉じる。"""
        if self._thread and not self._done.is_set():
            self._q.put((wsclient.OP_TEXT, END_OF_AUDIO.encode()))
            self._done.wait(timeout)
        self._q.put(None)
        if self._thread:
            self._thread.join(2.0)
        if self._ws:
            self._ws.close()

    @property
    def text(self) -> str:
        return "".join(t["text"] for t in self.final_tokens)

    @property
    def audio_s(self) -> float:
        return round(self.audio_bytes / self.bytes_per_s, 2)

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def finalize_s(self) -> float | None:
        if self.t_finalize is None or self.t_fin is None:
            return None
        return round(self.t_fin - self.t_finalize, 3)

    def languages(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.final_tokens:
            if t.get("language"):
                out[t["language"]] = out.get(t["language"], 0) + 1
        return out

    # --- 送受信のスレッド ---
    def _loop(self) -> None:
        ws = self._ws
        assert ws is not None
        try:
            while not self._done.is_set():
                while True:
                    try:
                        item = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        return
                    ws.send(*item)
                for op, payload in ws.poll(0.01):
                    if op == wsclient.OP_CLOSE:
                        if len(payload) >= 2:
                            self.close_code = int.from_bytes(payload[:2], "big")
                            self.close_reason = payload[2:].decode("utf-8", "replace")[:200]
                        return
                    if op == wsclient.OP_TEXT and self._handle(json.loads(payload)):
                        return
        except Exception as err:
            self.error = self.error or f"{type(err).__name__}: {err}"
        finally:
            self._done.set()
            if self.on_done:
                try:
                    self.on_done(self)
                except Exception:
                    pass

    def _handle(self, data: dict) -> bool:
        """1 通の応答を取り込む。終わり(finished・エラー)なら True。"""
        now = time.monotonic()
        self.t_last_msg = now
        if data.get("error_code"):
            self.error_code = int(data["error_code"])
            self.error = f"Soniox {data['error_code']}: {data.get('error_message', '')}".strip()
            return True
        if isinstance(data.get("total_audio_proc_ms"), (int, float)):
            self.proc_ms = int(data["total_audio_proc_ms"])
        partial: list[dict] = []
        with self.lock:
            for tok in data.get("tokens", []):
                text = tok.get("text", "")
                if text == "<fin>":
                    if self.t_fin is None:
                        self.t_fin = now
                    self._fin.set()
                    self.marks.append(("fin", len(self.final_tokens)))
                elif text == "<end>":
                    self.ends += 1
                    self.marks.append(("end", len(self.final_tokens)))
                elif tok.get("is_final"):
                    self.final_tokens.append({"text": text, "start_ms": tok.get("start_ms"), "end_ms": tok.get("end_ms"),
                                              "language": tok.get("language"),
                                              "recv_s": round(now - (self.t_open or now), 3)})
                else:
                    partial.append({"text": text, "start_ms": tok.get("start_ms"), "end_ms": tok.get("end_ms")})
            self.partial_tokens = partial
            self.partial = "".join(t["text"] for t in partial)
        if self.on_update:
            try:
                self.on_update(self)
            except Exception:
                pass
        return bool(data.get("finished"))
