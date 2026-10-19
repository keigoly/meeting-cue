"""音声入力(REQUIREMENTS FR-14・2026-10-17 keigoly様・まず Mac / FR-14b・2026-10-19 Aqua Voice の機能をそろえる)。

キーを押している間に話した言葉を Soniox で文字にし、決めたフィラーと置き換え辞書で仕上げて、今使っているアプリに貼り付ける。
OS に触る所(キー・マイク・貼り付け・画面の文脈・音・フローティングバー・消音)は helpers/macos/dictation_helper
(stdout JSONL / stdin のコマンド)。設定は ~/.meeting-cue/dictation/settings.json(meetcue.dictation_settings)。
- 押し方: 有効化のキー(既定 ⌃⌥V・Fn などの修飾キーだけも可)を hold_ms 以上押してから離したら、そこで終わり(押している間
  だけ録る)。短く 2 回押したら(Aqua Voice と同じ・2026-10-18 keigoly様)、もう一度押すまで録る(ハンズフリー)。押したまま
  ハンズフリーに切り替えるキー(既定 Space)・ハンズフリーで始めるキー・フローティングバーのクリックでも同じ。1 回だけ短く
  押したときは録らずに取り消す(double_ms 待っても 2 回目が来ない)。録っている間の Esc(既定)は取り消し: 文字にはして履歴に
  残すが貼らない(「最後の文字起こしを貼り付け」で戻せる)。押した瞬間から録り始めるので、話し始めは欠けない。
- Soniox へは押した瞬間からつなぐ。つながるまで約 1.3 s かかる(2026-10-17 実測)ので、その間の音は手元にためて先に送る。
- 仕上げは決めた語のフィラー除去・空白と読点の整え・置き換え辞書(FR-7)・音声入力だけの置換(定型文)。AI による書き直しは
  既定でしない(Aqua Voice は普段の口述 35 本中 6 本で意味を作り替えた)。「AI で整える」「編集モード」を選んだときだけ
  meetcue.dictation_ai を通す。
- 有効化の方式: 常にインスタント(既定・最後にまとめて貼る)/ ハンズフリー時のみリアルタイム / 常にリアルタイム(確定した語を
  文の区切りごとに、話しているそばから貼る)。最後に「送信して」と言うと、その語を除いて貼り、送信キーを押す。
- LOCAL(privacy = local)では Soniox に送らない(端末内の認識への切り替えは次の段)。
- 1 回ごとに録音(WAV)と文と各段の時間を ~/.meeting-cue/dictation/<日付>/ に残す(プライバシーモードでは残さない)。段ごとの
  ms は ~/.meeting-cue/dictation/metrics.jsonl(phase = dict_*)。`meetcue report dictation` で p50 / p90 を出す。
- 待ち受けは 1 か所だけ(dictation/listener.lock)。アプリ(meetcue app)と `meetcue dictate` が同時にキーを取り合わない。
"""
from __future__ import annotations

import array
import base64
import collections
import json
import math
import os
import queue
import re
import subprocess
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import dictation_settings as dset
from . import dictation_history as dhist
from . import secrets, soniox
from .config import Config
from .metrics import Metrics, new_rid, percentiles
from .replacements import Replacer

PET_DIR = Path(__file__).resolve().parent / "ui" / "static" / "nagi"   # ナギのペット(FR-14c)の絵。v0 は 6 表情

# 決めた語だけを消す(言ったとおりに書く Soniox の文から、話し言葉の詰まりを抜く)。テンプレートのコードは写していない
HARD_FILLERS = ("えーっと", "えーと", "えっと", "ええと", "えー", "えぇ", "あのー", "あのう", "あのぉ", "うーん", "うーむ",
                "あー", "あぁ", "んー", "そのー", "そのう")
SOFT_FILLERS = ("まあ", "まぁ", "あの", "その")   # ふつうの語でもあるので、読点が続くときだけ消す
_COMMA = "[、,，]"
_HARD_RE = re.compile("(?:" + "|".join(map(re.escape, sorted(HARD_FILLERS, key=len, reverse=True))) + ")"
                      + "(?:" + _COMMA + "[ 　]*|(?=[。.!?！？\\s]|$))")
_SOFT_RE = re.compile("(?:" + "|".join(map(re.escape, SOFT_FILLERS)) + ")" + _COMMA + "[ 　]*")
_EN_RE = re.compile(r"(?i)\b(?:um+|uh+|erm|hmm+)\b[,.]?\s*")
_QUOTED_TERM = re.compile(r"「([A-Za-z0-9][A-Za-z0-9 .\-_&+!/]*)」")   # Soniox は英字の語を「」で囲むことがある

# 送信コマンド(FR-14b): 文の区切り(。！？)の後、または口述の頭に、文の終わりとして言ったときだけ。読点の後や文の途中
# (「確認したら、送ってください。」「このメールを送信して。」)は本文のまま(途中までの文を送らない・読み直しの指摘 10-19)。
# 英語も前の文と区切ったときだけ(「Please send it.」は本文のまま)
_SEND_JA = re.compile(r"(?:^|(?<=[。．.!?！？]))[ 　]*(?:送信して(?:ください)?|送信します|送信|送って(?:ください)?)[。．.!！]*\s*$")
_SEND_EN = re.compile(r"(?i)(?:^|(?<=[.!?。]))\s*(?:send it|send)[.!]*\s*$")
_SENTENCE_END = re.compile(r"[。！？!?\n]")
SEND_TERMS = ("送信して",)   # 送信コマンドを使うとき Soniox の語彙に足す


def finish_text(text: str, replacer: Replacer | None = None, extra: Replacer | None = None) -> tuple[str, int]:
    """Soniox の文を仕上げる。(仕上げた文, 消したフィラーの数)。extra = 音声入力だけの置換(定型文)。"""
    s = (text or "").strip()
    n = 0
    for rx in (_HARD_RE, _SOFT_RE, _EN_RE):
        s, k = rx.subn("", s)
        n += k
    s = _QUOTED_TERM.sub(r"\1", s)
    s = re.sub(r"[ 　]+(?=[、。，．,.!?！？」』）])", "", s)        # 句読点の前の空白
    s = re.sub(r"(?<=[、。，．「『（！？])[ 　]+", "", s)          # 句読点の後の空白(Soniox は「。」の後に空白を入れる)
    s = re.sub(r"([。．.!?！？])[、，,。．]+", r"\1", s)
    s = re.sub(r"[、，,]{2,}", "、", s)
    s = re.sub(r"[、，,]+(?=[。．.!?！？])", "", s)
    s = re.sub(r"^[、，,。．.\s]+", "", s)
    s = re.sub(r"[ \t]{2,}", " ", s).strip()
    if replacer is not None and s:
        s = replacer.apply(s)
    if extra is not None and s:
        s = extra.apply(s)
    return s, n


def strip_send(text: str) -> tuple[str, bool]:
    """文の終わりの送信コマンドを除く。(残りの文, 言ったか)。"""
    for rx in (_SEND_JA, _SEND_EN):
        m = rx.search(text)
        if m:
            return text[:m.start()].rstrip(" 　"), True
    return text, False


def casualize(text: str) -> str:
    """カジュアルなメッセージ(FR-14b): 文末の「。」を付けない。英語だけの文は文頭を小文字にし、文末の「.」を付けない。"""
    s = text.rstrip()
    if re.fullmatch(r"[\x00-\x7f]+", s):
        if re.match(r"[A-Z][a-z]", s):   # 「OK」「API」のような略語は変えない
            s = s[:1].lower() + s[1:]
        return s[:-1] if s.endswith(".") and not s.endswith("..") else s
    return s[:-1] if s.endswith("。") else s


def split_ready(text: str) -> tuple[str, str]:
    """リアルタイム: 文の区切り(。！？ と改行)まで = 貼ってよい分、残り。長い文は 40 字を超えたら読点でも区切る。"""
    ends = [m.end() for m in _SENTENCE_END.finditer(text)]
    if ends:
        return text[:ends[-1]], text[ends[-1]:]
    if len(text) > 40:
        i = max(text.rfind("、"), text.rfind(","))
        if i > 0:
            return text[:i + 1], text[i + 1:]
    return "", text


def load_terms(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")   # 手で書いた文字化けで 1 回分を落とさない
    except OSError:
        return []
    seen, out = set(), []
    for line in text.splitlines():
        w = line.strip()
        if w and not w.startswith("#") and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out[:300]


def acquire_lock(path: Path) -> int | None:
    """待ち受けは 1 か所だけ(アプリと `meetcue dictate` がキーを取り合わない)。取れたら fd、ほかが持っていれば None。"""
    try:
        import fcntl
    except ImportError:   # Windows(音声入力は Mac から)
        return -1
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def lock_holder(path: Path) -> int | None:
    """待ち受けている別のプロセスの pid(いなければ None)。"""
    fd = acquire_lock(path)
    if fd is not None:
        release_lock(fd)
        return None
    try:
        return int(path.read_text().strip() or 0) or None
    except (OSError, ValueError):
        return None


def release_lock(fd: int | None) -> None:
    if fd is None or fd < 0:
        return
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass
    os.close(fd)


def rms_db(pcm: bytes) -> float:
    """16 bit の音の大きさ(dBFS)。"""
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if not a:
        return -120.0
    acc = sum(x * x for x in a) / len(a)
    return 10 * math.log10(acc / 32768 ** 2) if acc > 0 else -120.0


class Pauses:
    """リアルタイム(FR-14b): 話した後の間(既定 0.5 s)を音の大きさで見つける。Soniox は確定(is_final)が話から約 5 s 遅れ、
    文末の「。」は次の音か finalize まで確定しない(2026-10-19 実測)ので、間ごとに finalize を送って区切りを確定させる。
    床の雑音は静かな所へ速く・うるさい所へゆっくり寄せ、床 + 10 dB(かつ −50 dBFS)を超えたら話していると見なす。"""

    def __init__(self, gap_ms: int = 500, chunk_ms: int = 100):
        self.gap = max(1, gap_ms // chunk_ms)
        self.floor = -60.0
        self.quiet = 0
        self.spoke = False

    def push(self, pcm: bytes) -> bool:
        """100 ms 分を見て、話の後に間が空いたら True(1 回の間につき 1 度だけ)。"""
        db = rms_db(pcm)
        self.floor = db if db < self.floor else self.floor + (db - self.floor) * 0.02
        if db > max(self.floor + 10, -50):
            self.spoke, self.quiet = True, 0
            return False
        self.quiet += 1
        if self.spoke and self.quiet >= self.gap:
            self.spoke = False
            return True
        return False


class Take:
    """1 回の口述。"""

    def __init__(self, trigger: str = "activate") -> None:
        self.rid = new_rid()
        self.t_down = time.monotonic()
        self.started = datetime.now()
        self.trigger = trigger                # activate | handsfree | bar
        self.mode: str | None = None          # hold | toggle | tap(短く 1 回押して 2 回目を待っている)。離すまで決まらない
        self.locked = False                   # 有効化を押したままハンズフリーに切り替えた
        self.t_tap: float | None = None       # 1 回目を離した時刻(2 回押しの待ち)
        self.canceled: str | None = None      # tap(1 回だけ短く押した)/ abort(修飾キーを組み合わせに使った)= 何も残さない /
                                              # user(Esc など)= 文字にして残すが貼らない
        self.pcm = bytearray()
        self.lock = threading.Lock()
        self.stopped = threading.Event()      # キーで止めた
        self.rec_stopped = threading.Event()  # ヘルパーが残りの音を出し切った
        self.failed: str | None = None
        self.t_stop: float | None = None
        self.t_paste: float | None = None
        self.audio_s = 0.0
        self.record: dict = {}
        self.context: dict = {}               # 押した時の前面のアプリ・画面の文脈・選んだ文
        self.ctx_event = threading.Event()
        self.app: dict = {}                   # 止めた時の前面のアプリ(貼り先)
        self.fed = 0                          # リアルタイム: 貼るよう頼んだ確定の文の長さ(Soniox の文の位置)
        self.inserts: list[dict] = []         # リアルタイムの 1 回分ずつ {pid, start, end, text, ok}(ok は返事まで None)
        self.rt_off = False                   # リアルタイムの貼り付けが失敗した(アプリが変わった・許可が無い)→ 残りは最後にまとめる
        self.edit: bool | None = False        # 編集モード(選んだ文を話した指示で直す)。None = 選んだ文がまだ分からない
        self.live = ""                        # 話している言葉の窓に出した文(同じ文は送り直さない)
        self.grace_until = 0.0                # 修飾キーだけのキーで始めたとき: ここまで音・バー・接続を待つ(ショートカットなら黙って捨てる)
        self.announced = False                # 録り始めの音とバーを出したか
        self.live_frozen = False              # 貼る文を出した後は、途中の文で上書きしない

    def good_inserts(self) -> list[dict]:
        """リアルタイムで、先頭から続けて貼れた分。途中で失敗したら、それより後は貼れていないものとして最後にまとめて貼る。"""
        out = []
        for e in self.inserts:
            if e["ok"] is not True:
                break
            out.append(e)
        return out

    @property
    def inserted(self) -> list[str]:
        """リアルタイムで貼った(貼るよう頼んで、失敗していない)文。"""
        return [e["text"] for e in self.inserts if e["ok"] is not False]


class Dictation:
    def __init__(self, cfg: Config, *, helper_args: list[str] | None = None, out: Callable[[str], None] = print,
                 settings: dict | None = None, on_change: Callable[[dict], None] | None = None,
                 guard: Callable[[], str | None] | None = None):
        self.cfg, self.d = cfg, cfg.dictation
        self.guard = guard   # 押した時に音声入力を断る理由(アプリ: LOCAL の会議を録っている間・読み直しの指摘 10-19)
        self.s = settings if settings is not None else dset.load(cfg)
        self.root = cfg.app_dir / "dictation"
        self.root.mkdir(parents=True, exist_ok=True)
        self.metrics = Metrics(self.root / "metrics.jsonl")
        self.res_metrics = Metrics(self.root / "resource.jsonl")   # ヘルパーの重さ(1 分に 1 行。速さの記録とは分ける)
        self.resource: collections.deque[dict] = collections.deque(maxlen=60)   # 最近 1 時間(ステータスの画面に出す)
        self._res_seen = 0                                   # このヘルパーの重さの行の数(最初の 1 行は起動の分)
        self.replacer = Replacer(cfg.app_dir / "replacements.txt")
        self.dict_replacer = Replacer(self.root / "replacements.txt", escapes=True)
        self.helper_args = helper_args or []
        self.out = out
        self.on_change = on_change
        self.proc: subprocess.Popen | None = None
        self.events: queue.Queue = queue.Queue()
        self._wlock = threading.Lock()
        self._key: str | None = None
        self.take: Take | None = None                       # 録音中(キーで止める対象)
        self.starting: collections.deque[Take] = collections.deque()   # start を送って rec_start を待つもの
        self.ctx_wait: collections.deque[Take] = collections.deque()   # start を送って context を待つもの
        self.rec_target: Take | None = None                  # いま音を受け取っているもの
        self.pastes: dict[int, tuple[str, Take | None]] = {}   # 貼り付けの番号 → (take | insert | last, 1 回)
        self._paste_seq = 0
        self._paste_lock = threading.Lock()
        self.workers: list[threading.Thread] = []
        self.ready: dict = {}
        self.bindings: dict = {}
        self.errors: list[dict] = []
        self.last: dict | None = None if self.s.get("privacy_mode") else dhist.latest(self.root)
        self.replies: dict[str, dict] = {}                   # devices / apps の返事
        self._reply_events: dict[str, threading.Event] = {}
        self._stop = threading.Event()
        self._lockfd: int | None = None
        self.running = False
        self.pet_meeting = "idle"                            # 会議の状態(ペットの表情に使う・アプリが知らせる)

    def helper_path(self) -> Path:
        return self.cfg.helpers_dir / "dictation_helper" / "dictation-helper"

    def helper_argv(self) -> list[str]:
        pet = [] if "--no-bar" in self.helper_args else ["--pet-dir", str(PET_DIR)]   # 画面に何も出さない試験では、ペットも出さない
        return [str(self.helper_path()), "--bindings", json.dumps(dset.helper_bindings(self.s), ensure_ascii=False),
                "--state-dir", str(self.root), *pet, *([] if self.d.paste else ["--no-paste"]), *self.helper_args]

    # --- 起動と終了 ---
    def check(self) -> tuple[int, str]:
        """動けるか。(0, "") / (2, 理由)。"""
        if self.cfg.privacy == "local":
            return 2, "LOCAL(privacy = local)では、音声入力は使えません(Soniox に送らない約束・端末内の認識は次の段)"
        if not secrets.api_key("soniox")[0]:
            return 2, "Soniox の鍵がありません(⚙ の「会議の文字起こし(Soniox)」か、キーチェーン local.meetcue/soniox)"
        if not self.helper_path().exists():
            return 2, f"音声入力のヘルパーがありません: {self.helper_path()}(helpers/macos で make)"
        return 0, ""

    def run(self, seconds: float | None = None) -> int:
        code, why = self.check()
        if code:
            self.out(why)
            return code
        self._key = secrets.api_key("soniox")[0]
        self._lockfd = acquire_lock(self.root / "listener.lock")
        if self._lockfd is None:
            pid = lock_holder(self.root / "listener.lock")
            self.out(f"音声入力はすでに別の所で待ち受けています(pid {pid})。そちらを止めてから始めてください")
            return 3
        argv = self.helper_argv()
        t_end = time.monotonic() + seconds if seconds else None
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         text=True, encoding="utf-8", bufsize=1)
            threading.Thread(target=self._read, name="dictation-helper", daemon=True).start()
            self.running = True
            self._notify("running", running=True)
            while not self._stop.is_set():
                try:
                    ev = self.events.get(timeout=0.1)
                except queue.Empty:
                    ev = None
                if ev is not None:
                    if ev.get("type") in ("bye", "_eof"):
                        break
                    self.on_event(ev)
                self.tick()
                if t_end and time.monotonic() > t_end:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def stop(self) -> None:
        """別のスレッドから止める(アプリ)。"""
        self._stop.set()

    def stopped(self) -> bool:
        """止めるよう頼まれたか(頼まれずに終わった = ヘルパーが落ちた)。"""
        return self._stop.is_set()

    def tick(self) -> None:
        """0.1 s ごと: 2 回目が来なかった短い押しを取り消す・録りっぱなしを max_s で止める。"""
        tk = self.take
        if tk and not tk.announced and tk.canceled is None and time.monotonic() >= tk.grace_until:
            self._announce(tk)
        if tk and tk.mode == "tap" and time.monotonic() - (tk.t_tap or 0) > self.d.double_ms / 1000:
            self.cancel(tk, "tap")
        elif tk and time.monotonic() - tk.t_down > self.d.max_s:
            self.out(f"{self.d.max_s:.0f} 秒を超えたので止めます")
            tk.mode = tk.mode or "hold"
            self.end(tk)

    def shutdown(self) -> None:
        if self.take:
            self.end(self.take)
        for w in self.workers:
            w.join(timeout=10.0)
        if self.proc and self.proc.poll() is None:
            try:
                self.send("quit")
                self.proc.wait(timeout=3.0)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
        self.metrics.close()
        self.res_metrics.close()
        release_lock(self._lockfd)
        self._lockfd = None
        self.running = False
        self._notify("running", running=False)

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            try:
                self.events.put(json.loads(line))
            except ValueError:
                continue
        self.events.put({"type": "_eof"})

    def send(self, line: str) -> None:
        if not self.proc or not self.proc.stdin:
            return
        with self._wlock:
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def cmd(self, which: str, /, **kw) -> None:
        self.send(json.dumps({"cmd": which, **kw}, ensure_ascii=False))

    def _notify(self, kind: str, **data) -> None:
        if self.on_change:
            try:
                self.on_change({"kind": kind, **data})
            except Exception:  # noqa: BLE001 — 画面への知らせの失敗で音声入力を止めない
                pass

    # --- ほかのスレッドから(アプリの画面): 本体のループに積んで、ヘルパーとのやり取りを 1 本にまとめる ---
    def apply_settings(self, s: dict) -> None:
        self.events.put({"type": "_settings", "settings": s})

    def request(self, which: str, /, timeout: float = 1.5, **kw) -> dict | None:
        """devices / apps など返事のあるコマンド。返事(無ければ None)。"""
        ev = threading.Event()
        self._reply_events[which] = ev
        self.events.put({"type": "_cmd", "cmd": which, **kw})
        return self.replies.get(which) if ev.wait(timeout) else None

    def post(self, which: str, /, **kw) -> None:
        """返事を待たないコマンド(suspend・meter・paste_last など)。"""
        self.events.put({"type": "_cmd", "cmd": which, **kw})

    def set_meeting(self, state: str) -> None:
        """会議の状態(idle | listen = 録音中 | think = 保存・サマリ)。ナギのペットの表情に使う。どのスレッドからでもよい。"""
        self.pet_meeting = state
        self.post("pet", meeting=state)

    def meeting_event(self, event: str) -> None:
        """会議の出来事(cue = 自分への質問を検知 / done = 保存した)。ペットが 1 回だけ反応する。"""
        self.post("pet", event=event)

    def _configure_helper(self) -> None:
        s = self.s
        self.cmd("sounds", on=bool(s["sounds"]), set=s["sound_set"])
        self.cmd("bar_mode", mode=s["floating_bar"], hint=dset.idle_hint(s), live=bool(s.get("live_text", True)))
        self.cmd("clipboard", avoid_history=bool(s["avoid_clipboard_history"]))
        self.cmd("mic", uid=s["mic"])
        self.cmd("pet", mode=s["pet"], size=s["pet_size"], meeting=self.pet_meeting)

    # --- ヘルパーからの知らせ ---
    def on_event(self, ev: dict) -> None:
        t = ev.get("type")
        now = time.monotonic()
        if t == "ready":
            self.ready = ev
            self.bindings = ev.get("bindings") or {}
            self._configure_helper()
            ax = "貼り付けできます" if ev.get("ax") else "アクセシビリティの許可が無いので、文はクリップボードに入れます"
            lb = dset.labels(self.s)
            self.out(f"音声入力の準備ができました: {' / '.join(lb['activate'])}(長く押して離す = 離したら終わり / "
                     f"2 回押す = もう一度押すまで)・{ax}")
            self._report_bindings()
            self._notify("ready", ready=ev)
        elif t == "bindings":
            self.bindings = ev.get("bindings") or {}
            self._report_bindings()
            self._notify("bindings", bindings=self.bindings, listen=ev.get("listen"), suspended=ev.get("suspended"))
        elif t == "_settings":
            old, self.s = self.s, ev["settings"]
            if dset.helper_bindings(old) != dset.helper_bindings(self.s):
                self.cmd("bindings", bindings=dset.helper_bindings(self.s))
            self._configure_helper()
            if self.s.get("privacy_mode") and not old.get("privacy_mode"):
                self.last = None
        elif t == "_cmd":
            name = ev["cmd"]
            if name == "paste_last":
                self.paste_last()
            elif name in ("devices", "apps"):
                self.cmd(name)
            else:
                self.cmd(name, **{k: v for k, v in ev.items() if k not in ("type", "cmd")})
        elif t in ("devices", "apps"):
            self.replies[t] = ev
            e = self._reply_events.pop(t, None)
            if e:
                e.set()
        elif t in ("level", "meter", "captured", "capture"):   # captured = キーを決める間に拾ったキー(画面が受け取って保存する)
            self._notify(t, **{k: v for k, v in ev.items() if k != "type"})
        elif t == "key":
            self.on_key(ev.get("state", ""), ev.get("action") or "activate", via=ev.get("via"))
        elif t == "resource":
            self._on_resource(ev)
        elif t == "pet":   # ペットの右クリックの操作(隠す・録っている間だけ・大きさ・画面を開く)。保存はアプリが受け持つ
            self._notify("pet", **{k: v for k, v in ev.items() if k != "type"})
        elif t == "context":
            tk = self.ctx_wait.popleft() if self.ctx_wait else None
            if tk:
                tk.context = ev
                tk.ctx_event.set()
                self.metrics.emit(tk.rid, "dict_context", ms=ev.get("ms"), ok=True, app=ev.get("app"),
                                  title_chars=len(ev.get("title") or ""), before_chars=len(ev.get("before") or ""),
                                  selected_chars=len(ev.get("selected") or ""), secure=bool(ev.get("secure")))
        elif t == "rec_start":
            self.rec_target = self.starting.popleft() if self.starting else None
        elif t == "rec_first":
            tk = self.rec_target
            if tk:
                self.metrics.emit(tk.rid, "dict_first_audio", ms=round((now - tk.t_down) * 1000, 1), ok=True)
        elif t == "audio":
            tk = self.rec_target
            if tk:
                with tk.lock:
                    tk.pcm += base64.b64decode(ev.get("pcm", ""))
        elif t == "rec_stop":
            tk = self.rec_target
            if tk:
                tk.audio_s = float(ev.get("audio_s") or 0.0)
                tk.app = {"app": ev.get("app") or "", "bundle": ev.get("bundle") or ""}
                tk.rec_stopped.set()
            self.rec_target = None
        elif t == "pasted":
            self._on_pasted(ev, now)
        elif t == "mute":
            self.metrics.emit(self.take.rid if self.take else "-", "dict_mute", ok=bool(ev.get("ok")), on=ev.get("on"),
                              how=ev.get("how"), error=ev.get("error"), recovered=ev.get("recovered"))
        elif t == "error":
            phase = ev.get("phase")
            self.errors = (self.errors + [ev])[-20:]
            self.metrics.emit(self.take.rid if self.take else "-", "dict_error", ok=False, error=f"{phase}: {ev.get('error')}")
            self.out(f"ヘルパーの誤り: {phase}: {ev.get('error')}{' ' + ev['spec'] if ev.get('spec') else ''}")
            if phase in ("mic", "file") and ev.get("error") not in ("device_not_found", "set_device_failed") and self.starting:
                tk = self.starting.popleft()
                tk.failed = f"{phase}: {ev.get('error')}"
                tk.stopped.set()
                tk.rec_stopped.set()
                if self.take is tk:
                    self.take = None
                if self.s["mute_audio"]:
                    self.cmd("mute", on=False)
                self._bar("error", "マイクを開けませんでした")
                self.cmd("sound", name="error")

    RESOURCE_KEYS = ("cpu_pct", "footprint_mb", "peak_mb", "wakeups", "idle_wakeups", "interval_s", "pet")

    def _on_resource(self, ev: dict) -> None:
        """ヘルパーの重さ(60 s ごと・ナギのペット v0 の Step 1): resource.jsonl に残し、ステータスの画面に出す。"""
        row = {k: ev[k] for k in self.RESOURCE_KEYS if ev.get(k) is not None}
        if "cpu_pct" not in row:
            return
        self._res_seen += 1
        if self._res_seen == 1:   # 最初の 1 分には起動の分(絵を読む・窓を作る)が入る: 残すが、平均と画面には入れない
            self.res_metrics.emit("-", "pet_resource", ok=True, startup=True, **row)
            return
        self.resource.append({"at": int(time.time() * 1000), **row})
        self.res_metrics.emit("-", "pet_resource", ok=True, **row)

    def resource_view(self) -> dict | None:
        """ステータスの画面: 最新と、最近 1 時間の CPU の平均と最大・メモリの最大。"""
        rows = list(self.resource)
        if not rows:
            return None
        cpu = [r["cpu_pct"] for r in rows]
        mem = [r["footprint_mb"] for r in rows if isinstance(r.get("footprint_mb"), (int, float))]
        return {"last": rows[-1], "minutes": len(rows), "cpu_avg": round(sum(cpu) / len(cpu), 3), "cpu_max": max(cpu),
                "mem_max": max(mem) if mem else None}

    def _report_bindings(self) -> None:
        rows = {a: [{k: r.get(k) for k in ("spec", "ok", "via", "error")} for r in v] for a, v in self.bindings.items()}
        self.metrics.emit("-", "dict_bindings", ok=all(r.get("ok") for v in self.bindings.values() for r in v), bindings=rows)
        for a, v in self.bindings.items():
            for r in v:
                if not r.get("ok"):
                    why = {"in_use": "ほかのアプリが使っています", "listen_permission": "入力監視の許可が要ります",
                           "unsafe": "ふだんの入力を奪うキーです"}.get(r.get("error"), r.get("error") or "登録できません")
                    self.out(f"キーを登録できませんでした: {r.get('label') or r.get('spec')}({why})")

    def paste(self, kind: str, tk: Take | None, text: str, send: str | None = None, expect: str | None = None) -> int:
        """ヘルパーに貼らせる。返事(pasted)は番号で突き合わせる(送信キーの返事は遅れて出るため)。expect = 貼ってよい
        前面のアプリ(bundle id)。ヘルパーは前面が違えば貼らず・送信キーも押さない(離した後に別のアプリへ移ったとき、文と
        Enter がそのアプリに入らないように・読み直しの指摘 10-19)。"""
        with self._paste_lock:
            self._paste_seq += 1
            pid = self._paste_seq
            self.pastes[pid] = (kind, tk, text)
        self.cmd("paste", text=text, send=send, id=pid, expect=expect)
        return pid

    def _target(self, tk: Take, insert: bool = False) -> str | None:
        """貼ってよい前面のアプリ。リアルタイムで貼り始めたら押した時のアプリ、それ以外は離した時のアプリ。"""
        start = (tk.context or {}).get("bundle") or ""
        if insert or tk.good_inserts():
            return start or None
        return (tk.app or {}).get("bundle") or start or None

    def _on_pasted(self, ev: dict, now: float) -> None:
        with self._paste_lock:
            kind, tk, text = self.pastes.pop(ev.get("id"), ("take", None, ""))
        ok = bool(ev.get("ok"))
        if kind == "insert" and tk:
            for e in tk.inserts:
                if e["pid"] == ev.get("id"):
                    e["ok"] = ok
            if not ok:
                tk.rt_off = True   # 残りは最後にまとめて貼る(貼れなかった分も含めて)
            self.metrics.emit(tk.rid, "dict_insert_paste", ms=ev.get("ms"), ok=ok, error=ev.get("reason"))
            return
        if kind == "last":
            self.metrics.emit("-", "dict_paste_last", ok=ok, error=ev.get("reason"), chars=ev.get("chars"))
            self._bar("done" if ok else "error", "貼り付けました" if ok else "貼り付けられませんでした")
            return
        if not tk:
            return
        total = round((now - (tk.t_stop or now)) * 1000, 1)
        self.metrics.emit(tk.rid, "dict_paste", ms=ev.get("ms"), ok=ok, error=ev.get("reason"),
                          clipboard=ev.get("clipboard"), sent=ev.get("sent"), app=ev.get("app"))
        self.metrics.emit(tk.rid, "dict_total", ms=total, ok=ok, from_down_ms=round((now - tk.t_down) * 1000, 1),
                          mode=tk.mode, realtime=bool(tk.good_inserts()))
        if ev.get("sent"):
            self.metrics.emit(tk.rid, "dict_send", ok=ok, key=ev.get("sent"), app=ev.get("app"))
        if ev.get("reason") == "app_changed":   # 離した後に別のアプリへ移った: 貼らない(取り戻し方を出す)
            lb = dset.labels(self.s)["paste_last"]
            if lb:
                where = f"アプリが変わったので貼りませんでした({lb} で貼り付け)"
            else:
                self.cmd("clip", text=text)
                where = "アプリが変わったので、クリップボードに入れました(⌘V で貼れます)"
        else:
            where = ("送信しました" if ev.get("sent") else "貼り付けました") if ok else (
                "クリップボードに入れました(⌘V で貼れます)" if ev.get("clipboard") else "貼り付けていません")
        self._bar("done" if ok else "cancel" if ev.get("clipboard") or ev.get("reason") == "app_changed" else "error", where)
        self.out(f"→ {where}(離してから {total / 1000:.2f} 秒)")
        self._notify("pasted", rid=tk.rid, ok=ok, where=where)

    def _bar(self, state: str, text: str = "", handsfree: bool = False) -> None:
        self.cmd("bar", state=state, text=text, handsfree=handsfree)

    def _handsfree_hint(self) -> str:
        lb = dset.labels(self.s)
        parts = [f"{lb['activate'][0]} で終わる"] if lb["activate"] else []
        if self.s["keys"]["cancel_enabled"] and lb["cancel"]:
            parts.append(f"{lb['cancel']} で取り消し")
        return "・".join(parts)

    # --- キー ---
    def on_key(self, state: str, action: str = "activate", via: str | None = None) -> None:
        tk = self.take
        if action == "activate":
            if state == "down":
                if tk is None:
                    self.begin("activate", grace=self.d.modifier_grace_ms / 1000 if via == "tap" else 0.0)
                elif tk.mode == "tap":   # 2 回目(1 回目を離してから double_ms 以内。過ぎていれば tick が取り消し済み)
                    tk.mode = "toggle"
                    self.metrics.emit(tk.rid, "dict_double", ms=round((time.monotonic() - (tk.t_tap or tk.t_down)) * 1000, 1),
                                      ok=True)
                    self._to_handsfree(tk)
                elif tk.mode == "toggle":
                    self.end(tk)
            elif state == "up" and tk is not None and tk.mode is None and tk.trigger == "activate":
                held = (time.monotonic() - tk.t_down) * 1000
                if held >= self.d.hold_ms:
                    tk.mode = "hold"
                    self.end(tk)
                elif self.d.toggle_taps >= 2:
                    tk.mode, tk.t_tap = "tap", time.monotonic()
                else:
                    tk.mode = "toggle"
                    self._to_handsfree(tk)
            elif state == "abort" and tk is not None and tk.mode is None:
                self.cancel(tk, "abort")
        elif action == "lock":
            if state == "down" and tk is not None and tk.mode is None:
                tk.mode, tk.locked = "toggle", True
                self.metrics.emit(tk.rid, "dict_lock", ms=round((time.monotonic() - tk.t_down) * 1000, 1), ok=True)
                self._to_handsfree(tk)
        elif action == "handsfree":   # 押すと始まり、もう一度で終わり(フローティングバーのクリックも)
            if state != "down":
                return
            if tk is None:
                new = self.begin(via if via == "bar" else "handsfree")
                if new:
                    new.mode = "toggle"
                    self._to_handsfree(new, announce=False)
            elif tk.mode is None:     # 有効化を押したまま押した = ハンズフリーへの切り替え
                self.on_key("down", "lock")
            else:
                tk.mode = "toggle"
                self.end(tk)
        elif action == "cancel":
            if state == "down" and tk is not None:
                self.cancel(tk, "user")
        elif action == "stop":   # 話している言葉の窓の ■: 押し方に関わらず、ここで止めて貼る
            if state == "down" and tk is not None:
                tk.mode = tk.mode if tk.mode in ("hold", "toggle") else "toggle"
                self.end(tk)
        elif action == "paste_last":
            if state == "down":
                self.paste_last()

    def _to_handsfree(self, tk: Take, announce: bool = True) -> None:
        self._announce(tk)
        if announce:
            self.out("(ハンズフリーで録っています。もう一度押すと終わります)")
        self._bar("rec", self._handsfree_hint(), handsfree=True)
        self._notify("state", state="recording", rid=tk.rid, mode="toggle")

    def _announce(self, tk: Take) -> None:
        """録り始めを知らせる(音・バー・話している言葉の窓・消音)。修飾キーだけのキーでは、待ち時間を過ぎてから。"""
        if tk.announced or tk.canceled:
            return
        tk.announced = True
        self.cmd("sound", name="start")
        if self.s["mute_audio"]:
            self.cmd("mute", on=True, delay_ms=250)
        if tk.mode == "toggle":
            self._bar("rec", self._handsfree_hint(), handsfree=True)
        else:
            self._bar("rec", "", handsfree=False)
        self.cmd("live", text="")
        self.out("● 録音中")
        self._notify("state", state="recording", rid=tk.rid, mode=tk.mode)

    def begin(self, trigger: str = "activate", grace: float = 0.0) -> Take | None:
        why = self.guard() if self.guard else None
        if why:   # 例: LOCAL(仕事の会議)の録音中。マイクを開かない・Soniox に送らない
            self._bar("error", why)
            self.cmd("sound", name="error")
            self.out(why)
            self.metrics.emit("-", "dict_refused", ok=False, error=why)
            return None
        tk = Take(trigger)
        tk.grace_until = time.monotonic() + grace
        self.take = tk
        self.starting.append(tk)
        self.ctx_wait.append(tk)
        self.cmd("start", context="full" if self.s["deep_context"] else "app", selection=bool(self.s["edit_mode"]))
        if grace <= 0:
            self._announce(tk)
        self.metrics.emit(tk.rid, "dict_start", ok=True, trigger=trigger, grace_ms=round(grace * 1000))
        w = threading.Thread(target=self.work, args=(tk,), name=f"dictation-{tk.rid}", daemon=True)
        self.workers = [x for x in self.workers if x.is_alive()] + [w]
        w.start()
        return tk

    def end(self, tk: Take, *, quiet: bool = False) -> None:
        if tk.stopped.is_set():
            return
        tk.t_stop = time.monotonic()
        tk.stopped.set()
        if self.take is tk:
            self.take = None
        self.send("stop")
        if self.s["mute_audio"]:
            self.cmd("mute", on=False)
        if not quiet:
            self._announce(tk)   # 待ち時間の内に止めた(押してすぐ離した)ときも、始まりと終わりをそろえる
            self.cmd("sound", name="stop")
            self._bar("busy", "文字にしています…")
            self._notify("state", state="processing", rid=tk.rid, mode=tk.mode)
        self.metrics.emit(tk.rid, "dict_stop", ms=round((tk.t_stop - tk.t_down) * 1000, 1), ok=True, mode=tk.mode)

    def cancel(self, tk: Take, kind: str = "tap") -> None:
        """取り消し。tap / abort = 録らなかったことにする(貼らない・残さない)/ user = 文字にして残すが貼らない。"""
        if tk.canceled:
            return
        tk.canceled = kind
        self.end(tk, quiet=True)
        self.metrics.emit(tk.rid, "dict_cancel", ms=round(((tk.t_stop or 0) - tk.t_down) * 1000, 1), ok=True, kind=kind)
        if kind == "user":
            self.cmd("sound", name="cancel")
            lb = dset.labels(self.s)["paste_last"]
            self._bar("cancel", f"取り消しました({lb} で貼り付け)" if lb else "取り消しました")
            self.out("(取り消しました。文字にして履歴に残します。貼り付けません)")
            self._notify("state", state="processing", rid=tk.rid, mode=tk.mode, canceled=kind)
        elif kind == "tap":
            self._bar("cancel", "長く押して話す・2 回押すとハンズフリー")
            self.out("(1 回だけ押したので取り消しました。続けて録るときは 2 回押してください)")
            self._notify("state", state="idle", rid=tk.rid, canceled=kind)
        else:
            if tk.announced:   # 待ち時間の内に捨てたとき(ショートカットに使った)は、何も出していないので消すものもない
                self._bar("idle")
            self._notify("state", state="idle", rid=tk.rid, canceled=kind)

    def paste_last(self) -> None:
        if self.take is not None:
            return
        if not self.last or not self.last.get("text"):
            self._bar("error", "まだ文字起こしがありません")
            self.metrics.emit("-", "dict_paste_last", ok=False, error="none")
            return
        self.paste("last", None, self.last["text"])

    # --- 1 回分の処理(別スレッド): つなぐ → 流す(リアルタイムなら区切りごとに貼る)→ finalize → 仕上げ → 貼り付け → 記録 ---
    def _realtime(self, tk: Take) -> bool:
        rt = self.s["realtime"]
        if tk.edit is not False or tk.rt_off or self.s["ai_cleanup"] or tk.canceled:
            return False
        return rt == "always" or (rt == "handsfree" and tk.mode == "toggle")

    def _soniox_context(self, tk: Take) -> dict | None:
        """画面の文脈(FR-14b・選んだときだけ): 前面のアプリ名・ウィンドウの題名・カーソルの前の文。"""
        c = tk.context or {}
        if not self.s["deep_context"] or c.get("secure"):
            return None
        general = [(k, v) for k, v in (("アプリ", c.get("app")), ("ウィンドウ", c.get("title"))) if v]
        return {"general": general, "text": (c.get("before") or "")[-1000:]} if general or c.get("before") else None

    def _live(self, tk: Take, sess: soniox.Session) -> None:
        """話している言葉の窓(フローティングバーの上): 確定した語 + まだ変わりうる語を、貼る時と同じ仕上げで出す。
        Soniox の応答ごと(話している間は 0.1〜0.5 s ごと)に呼ばれる。同じ文は送り直さない。文は画面に出すだけで、残さない。"""
        s = self.s
        if tk.live_frozen or tk.canceled or not s.get("live_text", True) or s["floating_bar"] == "off":
            return
        with sess.lock:
            raw = "".join(t["text"] for t in sess.final_tokens) + sess.partial
        text = finish_text(raw, self.replacer, self.dict_replacer)[0]
        if text and text != tk.live:
            tk.live = text
            self.cmd("live", text=text[-600:])

    def _feed(self, tk: Take, sess: soniox.Session, upto_fin: bool = False) -> None:
        """リアルタイム: 確定した語を貼る。間で finalize した直後(upto_fin)は確定した分をすべて、それ以外は文の区切りまで
        (送信コマンドかもしれない文は終わりまで待つ)。"""
        if tk.rt_off:
            return
        with sess.lock:
            final = "".join(t["text"] for t in sess.final_tokens)
        ready = final[tk.fed:] if upto_fin else split_ready(final[tk.fed:])[0]
        if not ready:
            return
        cleaned, _ = finish_text(ready, self.replacer, self.dict_replacer)
        if self.s["send_key"] != "off" and strip_send(cleaned)[1]:
            return
        start, tk.fed = tk.fed, tk.fed + len(ready)
        if not cleaned:
            return
        prev = tk.inserted[-1] if tk.inserted else ""
        if prev and re.match(r"[A-Za-z0-9]", cleaned) and prev[-1:] in ".!?,;:":
            cleaned = " " + cleaned   # 英語の文と文の間の空白(仕上げで先頭の空白は落ちる)
        e = {"pid": None, "start": start, "end": tk.fed, "text": cleaned, "ok": None}
        tk.inserts.append(e)
        e["pid"] = self.paste("insert", tk, cleaned, expect=self._target(tk, insert=True))
        self.metrics.emit(tk.rid, "dict_insert", ok=True, chars=len(cleaned), n=len(tk.inserts),
                          ms=round((time.monotonic() - tk.t_down) * 1000, 1))

    def work(self, tk: Take) -> None:
        try:
            self._work(tk)
        except Exception as e:  # noqa: BLE001 — 1 回分の失敗で、バーを「文字にしています…」のまま残さない
            self.metrics.emit(tk.rid, "dict_error", ok=False, error=f"work: {type(e).__name__}: {e}"[:200])
            self.out(f"→ 文字にできませんでした({type(e).__name__})")
            self._bar("error", "文字にできませんでした")
            self.cmd("sound", name="error")
            self._notify("state", state="idle", rid=tk.rid)

    def _work(self, tk: Take) -> None:
        s = self.s
        while time.monotonic() < tk.grace_until and tk.canceled is None:   # 修飾キーだけのキー: ショートカットなら Soniox につながない
            time.sleep(0.01)
        if tk.canceled in ("tap", "abort"):
            return
        if s["deep_context"] or s["edit_mode"]:
            tk.ctx_event.wait(0.4)
        if s["edit_mode"]:   # 選んだ文が届いていなければ、まだ分からない(None)= リアルタイムで貼らず、終わってから決める
            tk.edit = bool((tk.context.get("selected") or "").strip()) if tk.ctx_event.is_set() else None
        else:
            tk.edit = False
        terms = load_terms(self.d.terms_file)
        if s["send_key"] != "off" and not tk.edit:   # 間で区切ると最後の「送信して」だけが短く残り、聞き違えた(そう信して・10-19)
            terms = [*terms, *[w for w in SEND_TERMS if w not in terms]]
        sess = soniox.Session(self._key or "", terms=terms, language_hints=dset.language_hints(s), region=self.d.region,
                              context=self._soniox_context(tk), on_update=lambda ss: self._live(tk, ss))
        ok = sess.start()
        self.metrics.emit(tk.rid, "dict_connect", ms=sess.connect_ms, ok=ok, error=sess.error,
                          context=bool(sess.cfg.get("context", {}).get("general") or sess.cfg.get("context", {}).get("text")))
        sent = 0
        deadline = None
        pauses = Pauses(self.d.realtime_gap_ms)
        fin_want = 0   # リアルタイム: 間で送った finalize の "<fin>" をいくつ待っているか(その数がそろったら貼る)
        while tk.canceled in (None, "user"):
            done = tk.rec_stopped.is_set()   # 先に見る: 立っていれば、それまでに届いた音はすべて pcm に入っている
            with tk.lock:
                n = len(tk.pcm)
                chunk = bytes(tk.pcm[sent:n]) if n > sent else b""
            if chunk:
                if ok:
                    rt = self._realtime(tk)
                    for i in range(0, len(chunk), 3200):
                        part = chunk[i:i + 3200]
                        sess.send_audio(part)
                        if rt and pauses.push(part):
                            fin_want = max(fin_want, sess.fin_count()) + 1
                            sess.finalize()
                            self.metrics.emit(tk.rid, "dict_pause", ok=True, n=fin_want,
                                              ms=round((time.monotonic() - tk.t_down) * 1000, 1))
                sent = n
                continue
            if ok and self._realtime(tk):
                if fin_want and sess.fin_count() >= fin_want:
                    self._feed(tk, sess, upto_fin=True)
                    fin_want = 0
                else:
                    self._feed(tk, sess)
            if done:   # 最後の音まで送り終えた(見た後に届いた分を取りこぼさない・読み直しの指摘 10-19)
                break
            if tk.stopped.is_set():
                deadline = deadline or time.monotonic() + 2.0   # ヘルパーが rec_stop を返さなくても止まる
                if time.monotonic() > deadline:
                    break
            time.sleep(0.01)
        if tk.canceled in ("tap", "abort"):   # 録らなかったことにする: 確定させず閉じる。貼らない・残さない
            sess.close(1.0)
            return
        raw = ""
        if ok:
            want = max(fin_want, sess.fin_count()) + 1   # 間で送った finalize の分も待ってから、最後の分を待つ
            t_fin = time.monotonic()
            sess.finalize()
            fin = sess.wait_fin_count(want, self.d.fin_timeout_s)
            fin_ms = round((time.monotonic() - t_fin) * 1000, 1)
            sess.close(3.0)
            raw = sess.text
            self.metrics.emit(tk.rid, "dict_final", ms=fin_ms if fin else None, ok=fin and not sess.error, error=sess.error,
                              chars=len(raw), audio_s=sess.audio_s, languages=sess.languages(), fins=sess.fin_count())
        self._deliver(tk, sess, raw, ok)

    def _deliver(self, tk: Take, sess: soniox.Session, raw: str, ok: bool) -> None:
        """仕上げ → (AI)→ 送信コマンド → 貼り付け → 記録。"""
        s = self.s
        if tk.edit is None:   # 選んだ文が遅れて届く(マイクの起動が遅い・アクセシビリティが遅い)。分からないまま貼らない
            got = tk.ctx_event.wait(2.0)
            tk.edit = bool((tk.context.get("selected") or "").strip()) if got else False
            self.metrics.emit(tk.rid, "dict_context_late", ok=got, edit=tk.edit)
        t_wait = time.monotonic() + 1.5   # リアルタイムの返事(貼れたか)を待つ: 貼れた分のあとから最後に貼る
        while any(e["ok"] is None for e in tk.inserts) and time.monotonic() < t_wait:
            time.sleep(0.02)
        good = tk.good_inserts()
        done_text = "".join(e["text"] for e in good)
        t0 = time.perf_counter()
        rest = raw[good[-1]["end"]:] if good else raw
        text, fillers = finish_text(rest, self.replacer, self.dict_replacer)
        if done_text and re.match(r"[A-Za-z0-9]", text) and done_text[-1:] in ".!?,;:":
            text = " " + text
        send = False
        if s["send_key"] != "off" and not tk.edit:
            text, send = strip_send(text)
        self.metrics.emit(tk.rid, "dict_text", ms=round((time.perf_counter() - t0) * 1000, 2), ok=True,
                          chars=len(done_text + text), chars_raw=len(raw), fillers=fillers, send=send,
                          realtime_failed=tk.rt_off)
        ai: dict | None = None
        edit: dict | None = None
        if tk.edit and text and tk.canceled is None:
            edit = self._ai_edit(tk, text)
            text = edit.get("text") or ""
        elif s["ai_cleanup"] and text and tk.canceled is None:
            ai = self._ai_cleanup(tk, text)
            text = ai.get("text") or text
        app = tk.app or {}
        if s["casual"] and app.get("bundle") in dset.CASUAL_APPS and not tk.edit and text:
            text = casualize(text)
        key = None
        if send and s["send_key"] in ("enter", "cmd_enter"):
            key = s["send_key"]
        elif not tk.edit and any(a.get("bundle") == app.get("bundle") for a in s["auto_enter_apps"]) and \
                (text or good):
            key = "enter"   # 自動 Enter のアプリ
        full = done_text + text
        tk.live_frozen = True
        if tk.canceled is None and full and s.get("live_text", True) and s["floating_bar"] != "off":
            self.cmd("live", text=full[-600:])   # 窓には、途中の文ではなく貼る文を出す
        if tk.canceled == "user":
            pass   # 取り消し: 貼らない(バーは取り消した時に出した・文は履歴と「最後の文字起こし」に残す)
        elif edit is not None and not edit.get("ok"):
            self.out(f"→ 編集モード: AI で直せませんでした({edit.get('error')})。選んだ文はそのままです")
        elif text or key:
            tk.t_paste = time.monotonic()
            self.paste("take", tk, text, key, expect=self._target(tk))
            if text:
                self.out(f"  {text}")
        elif good:   # リアルタイムで貼り終えている
            self._bar("done", "貼り付けました")
            self.metrics.emit(tk.rid, "dict_total", ms=round((time.monotonic() - (tk.t_stop or time.monotonic())) * 1000, 1),
                              ok=True, mode=tk.mode, realtime=True)
        else:
            reason = tk.failed or sess.error or ("no_speech" if ok else "connect")
            self.metrics.emit(tk.rid, "dict_paste", ok=False, error=reason)
            self.out(f"→ 文がありません({reason})")
            self._bar("error", {"no_speech": "聞き取れませんでした", "connect": "Soniox につながりません"}.get(
                reason, "文字にできませんでした"))
            if reason != "no_speech":
                self.cmd("sound", name="error")
        if full.strip():
            self.last = {"rid": tk.rid, "text": full, "at": tk.started.isoformat(timespec="seconds")}
        self.save(tk, sess, raw, full, fillers, send=key, ai=ai, edit=edit)
        self._notify("take", rid=tk.rid, text=full, canceled=tk.canceled, app=app.get("app"), sent=key)
        self._notify("state", state="idle", rid=tk.rid)

    def _ai_target(self):
        from . import dictation_ai
        return dictation_ai.app_target(self.cfg)

    def _ai_cleanup(self, tk: Take, text: str) -> dict:
        from . import dictation_ai
        self._bar("busy", "AI で整えています…")
        target, why = self._ai_target()
        if target is None:
            self.metrics.emit(tk.rid, "dict_ai", ok=False, error=why)
            self.out(f"(AI で整えられませんでした: {why})")
            return {"ok": False, "error": why}
        r = dictation_ai.cleanup(self.cfg, target, text, self.s["custom_instructions"])
        self.metrics.emit(tk.rid, "dict_ai", ms=round(r.ms_total, 1), ok=r.ok, error=r.error, model=r.model,
                          cost=r.cost_usd, chars_in=len(text), chars_out=len(r.text or ""))
        return {"ok": r.ok, "text": r.text if r.ok else "", "before": text, "model": r.model, "ms": round(r.ms_total),
                "error": r.error}

    def _ai_edit(self, tk: Take, instruction: str) -> dict:
        from . import dictation_ai
        self._bar("busy", "AI で直しています…")
        selected = tk.context.get("selected") or ""
        target, why = self._ai_target()
        if target is None:
            self.metrics.emit(tk.rid, "dict_edit", ok=False, error=why)
            self._bar("error", why[:40])
            return {"ok": False, "error": why, "instruction": instruction}
        r = dictation_ai.edit(self.cfg, target, selected, instruction)
        self.metrics.emit(tk.rid, "dict_edit", ms=round(r.ms_total, 1), ok=r.ok, error=r.error, model=r.model,
                          cost=r.cost_usd, chars_in=len(selected), chars_out=len(r.text or ""))
        if not r.ok:
            self._bar("error", "AI で直せませんでした")
        return {"ok": r.ok, "text": r.text if r.ok else "", "instruction": instruction, "selected": selected,
                "model": r.model, "ms": round(r.ms_total), "error": r.error}

    def save(self, tk: Take, s: soniox.Session, raw: str, text: str, fillers: int, *, send: str | None = None,
             ai: dict | None = None, edit: dict | None = None) -> None:
        if self.s.get("privacy_mode"):
            return   # プライバシーモード: 録音も文も残さない(数だけ metrics に残る)
        day = self.root / tk.started.strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        stem = day / f"{tk.started.strftime('%H%M%S')}_{tk.rid}"
        if self.d.keep_audio and tk.pcm:
            with wave.open(str(stem.with_suffix(".wav")), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(soniox.SAMPLE_RATE)
                w.writeframes(bytes(tk.pcm))
        rec = {"rid": tk.rid, "started_at": tk.started.isoformat(timespec="seconds"), "mode": tk.mode,
               "trigger": tk.trigger, "locked": tk.locked, "canceled": tk.canceled,
               "audio_s": round(len(tk.pcm) / soniox.BYTES_PER_S, 2), "raw": raw, "text": text, "fillers": fillers,
               "app": (tk.app or {}).get("app") or tk.context.get("app") or "",
               "bundle": (tk.app or {}).get("bundle") or tk.context.get("bundle") or "",
               "realtime": bool(tk.good_inserts()), "realtime_failed": tk.rt_off, "sent": send, "ai": ai, "edit": edit,
               "soniox": {"connect_ms": s.connect_ms, "finalize_s": s.finalize_s, "languages": s.languages(),
                          "error": s.error, "region": self.d.region,
                          "context": bool(s.cfg.get("context", {}).get("general") or s.cfg.get("context", {}).get("text"))},
               "failed": tk.failed}
        stem.with_suffix(".json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")


def report(cfg: Config) -> str:
    """音声入力の各段の ms(p50 / p90)と失敗の数。"""
    path = cfg.app_dir / "dictation" / "metrics.jsonl"
    if not path.exists():
        return "音声入力の記録がまだありません"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not rows:
        return "音声入力の記録がまだありません"
    by: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by[r.get("phase", "")].append(r)
    names = {"dict_first_audio": "押してから最初の音", "dict_context": "画面の文脈", "dict_connect": "Soniox への接続",
             "dict_final": "finalize から確定", "dict_ai": "AI で整える", "dict_edit": "編集モード",
             "dict_total": "離してから貼り終えるまで"}
    lines = [f"音声入力 {len(by.get('dict_start', []))} 回", "| 段 | 回数 | p50 ms | p90 ms | 失敗 |", "|---|---|---|---|---|"]
    for phase, label in names.items():
        rs = by.get(phase, [])
        if not rs and phase in ("dict_context", "dict_ai", "dict_edit"):
            continue
        ms = [r["ms"] for r in rs if isinstance(r.get("ms"), (int, float))]
        p = percentiles(ms) if ms else {}
        lines.append(f"| {label} | {len(rs)} | {p.get('p50', '-')} | {p.get('p90', '-')} | {sum(1 for r in rs if not r.get('ok'))} |")
    kinds = collections.Counter(r.get("kind") for r in by.get("dict_cancel", []))
    extra = [f"取り消し {dict(kinds)}" if kinds else "", f"ハンズフリーへの切り替え {len(by.get('dict_lock', []))}"
             if by.get("dict_lock") else "", f"送信 {len(by.get('dict_send', []))}" if by.get("dict_send") else "",
             f"リアルタイムの貼り付け {len(by.get('dict_insert', []))}" if by.get("dict_insert") else "",
             f"最後の文字起こしを貼り付け {len(by.get('dict_paste_last', []))}" if by.get("dict_paste_last") else ""]
    extra = [x for x in extra if x]
    if extra:
        lines.append("・".join(extra))
    res = resource_summary(cfg.app_dir / "dictation" / "resource.jsonl")
    if res:
        lines.append(res)
    return "\n".join(lines)


def resource_summary(path: Path, last_n: int = 1440) -> str:
    """ヘルパーの重さ(phase pet_resource・最近 last_n 分)を 1 行に。目標: 待っている間の CPU 0.1% 未満・ペットの分のメモリ +10 MB 以内。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-last_n:]
    except OSError:
        return ""
    rows = []
    for x in lines:
        try:
            r = json.loads(x)
        except ValueError:
            continue
        if r.get("phase") == "pet_resource" and isinstance(r.get("cpu_pct"), (int, float)) and not r.get("startup"):
            rows.append(r)
    if not rows:
        return ""
    cpu = sorted(r["cpu_pct"] for r in rows)
    mem = [r["footprint_mb"] for r in rows if isinstance(r.get("footprint_mb"), (int, float))]
    wake = [r["wakeups"] / max(1.0, float(r.get("interval_s") or 60)) for r in rows if isinstance(r.get("wakeups"), int)]
    p90 = cpu[min(int(len(cpu) * 0.9), len(cpu) - 1)]
    out = (f"ヘルパーの重さ({len(rows)} 分): CPU 平均 {sum(cpu) / len(cpu):.3f}%・遅い 1 割 {p90:.3f}%・最大 {cpu[-1]:.3f}%"
           + (f"・メモリ 最新 {mem[-1]:.1f} MB・最大 {max(mem):.1f} MB" if mem else "")
           + (f"・起こされた回数 平均 {sum(wake) / len(wake):.1f} 回/秒" if wake else ""))
    return out
