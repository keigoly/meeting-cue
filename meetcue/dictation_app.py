"""音声入力をアプリ(meetcue app)で動かす(REQUIREMENTS FR-14b・2026-10-19)。

アプリが起動している間、設定の「音声入力を使う」(既定オン)なら裏のスレッドで meetcue.dictation を待ち受けさせる
(Aqua Voice のように常駐。ログイン時に起動はメニューバーの既存の項目)。設定を変えたらすぐ効かせる(キーは登録し直す)。
画面(app.html の「音声入力」)へは GET/POST /api/dictation/… と、SSE の {"type": "dictation", …}(状態・音の大きさ)。
- GET  /api/dictation                          設定・表示名・待ち受けの状態・許可・キーの登録の結果
- POST /api/dictation/settings {…}            一部を変える(誤りは 400 と理由)/ POST /api/dictation/reset {section}
- POST /api/dictation/suspend {on}            キーを決める間、すべてのキーの登録を外す
- POST /api/dictation/capture {on}            キーを決める間、次に押したキーをヘルパーが拾い SSE の captured で返す(Fn も)
- GET|POST /api/dictation/terms               辞書(Soniox に渡す語)/ GET|POST /api/dictation/replacements(音声入力だけの置換)
- GET  /api/dictation/history?q=&offset=      履歴 / POST /api/dictation/history/<rid>/delete / POST …/history/clear
- GET  /api/dictation/audio/<rid>.wav         1 回分の録音(file_route)
- GET  /api/dictation/stats                   使った量と速さ / POST /api/dictation/network(Soniox までの速さと鍵の確認)
- GET  /api/dictation/devices|apps            マイク・起動中のアプリ / POST /api/dictation/meter {on}(マイクのテスト)
- POST /api/dictation/prompt {what: ax|listen}  許可を求める / POST /api/dictation/restart
文(話した中身)は端末のログに出さない(履歴の置き場 ~/.meeting-cue/dictation/ だけ)。
"""
from __future__ import annotations

import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import dictation_history as dhist
from . import dictation_settings as dset
from . import providers, secrets, soniox
from .dictation import Dictation, lock_holder
from .replacements import Replacer

MAX_TERMS_TEXT = 50_000


def rate(ms: float | None) -> str:
    """Soniox までの接続の速さの目安(Aqua Voice のネットワーク状態と同じ 4 段)。"""
    if ms is None:
        return "接続なし"
    return "最高" if ms < 120 else "良好" if ms < 250 else "普通" if ms < 500 else "不良"


def network_check(region: str, key: str | None, timeout: float = 5.0) -> dict:
    """TCP + TLS の接続にかかる ms(鍵も音も送らない)と、鍵の確認(無料の /v1/models)。"""
    host = urlparse(soniox.URLS.get(region, soniox.URLS["us"])).hostname or ""
    out: dict = {"host": host}
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            t1 = time.monotonic()
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host):
                t2 = time.monotonic()
        out.update(ok=True, tcp_ms=round((t1 - t0) * 1000), tls_ms=round((t2 - t0) * 1000))
    except OSError as e:
        out.update(ok=False, error=f"{type(e).__name__}: {e}"[:160], tls_ms=None)
    out["rating"] = rate(out.get("tls_ms"))
    out["key"] = providers.check_key("soniox", key) if key else {"ok": False, "status": None, "error": "no_key"}
    out["at"] = int(time.time() * 1000)
    return out


class DictationService:
    def __init__(self, cfg, web, term, guard=None):
        self.cfg, self.web, self.term = cfg, web, term
        self.guard = guard   # 押した時に断る理由(アプリ: LOCAL の会議を録っている間)
        self.engine: Dictation | None = None
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.error = ""
        self.live: dict = {"state": "idle"}
        self.net: dict | None = None
        self.meter: dict = {"on": False}
        self._exits: list[float] = []   # ヘルパーが自分で止まった時刻(自動で起動し直す回数を数える・REQUIREMENTS §5.4)
        self.dict_replacer = Replacer(self.root / "replacements.txt", escapes=True)
        self.pet_meeting = "idle"       # 会議の状態(ナギのペットの表情・FR-14c)
        listeners = getattr(web, "listeners", None)
        if isinstance(listeners, list):
            listeners.append(self._on_web)

    @property
    def root(self) -> Path:
        return self.cfg.app_dir / "dictation"

    def settings(self) -> dict:
        return dset.load(self.cfg)

    # ---- 待ち受けの開始・停止 ----
    def start(self) -> None:
        if sys.platform != "darwin":
            self.error = "音声入力はまだ Mac だけです(Windows は次の段)"
            return
        s = self.settings()
        if not s["enabled"]:
            self.error = ""
            return
        with self._lock:
            if self.engine and self.thread and self.thread.is_alive():
                return
            eng = Dictation(self.cfg, settings=s, out=self._log, on_change=self._changed, guard=self.guard)
            code, why = eng.check()
            if code:
                self.error = why
                self._push()
                return
            holder = lock_holder(self.root / "listener.lock")
            if holder:
                self.error = f"音声入力は別の所(meetcue dictate・pid {holder})で待ち受けています。そちらを止めると、ここで待ち受けます"
                self._push()
                return
            self.error = ""
            eng.pet_meeting = self.pet_meeting
            self.engine = eng
            self.thread = threading.Thread(target=self._run, args=(eng,), name="dictation", daemon=True)
            self.thread.start()

    def _run(self, eng: Dictation) -> None:
        t0 = time.monotonic()
        rc = None
        try:
            rc = eng.run()
            if rc:
                self.error = self.error or f"音声入力を始められませんでした(コード {rc})"
        except Exception as e:  # noqa: BLE001 — 音声入力の失敗でアプリを止めない
            self.error = f"音声入力が止まりました: {type(e).__name__}: {e}"[:200]
        self.term.status(f"dictation_stop ms={round((time.monotonic() - t0) * 1000)} rc={rc} error={self.error or None}")
        self.live = {"state": "idle"}
        with self._lock:
            mine = self.engine is eng
            if mine:
                self.engine = self.thread = None
        # 止めていないのにヘルパーが終わった(落ちた): 10 分に 3 回まで起動し直す(REQUIREMENTS §5.4)
        if mine and rc == 0 and not eng.stopped():
            now = time.monotonic()
            self._exits = [t for t in self._exits if now - t < 600] + [now]
            if len(self._exits) <= 3:
                self.term.status(f"dictation_restart n={len(self._exits)}")
                time.sleep(1.0)
                self.start()
                return
            self.error = "音声入力のヘルパーが 10 分に 3 回止まったので、待ち受けをやめました(ステータスの「待ち受けをやり直す」)"
        self._push()

    def stop(self, wait: float = 6.0) -> None:
        with self._lock:
            eng, th = self.engine, self.thread
            self.engine = self.thread = None
        if eng:
            eng.stop()
        if th:
            th.join(wait)

    def restart(self) -> None:
        self._exits = []
        self.stop()
        self.start()

    def running(self) -> bool:
        return bool(self.engine and self.engine.running and self.thread and self.thread.is_alive())

    def _log(self, msg: str) -> None:
        if msg.startswith("  "):   # 話した文の写し(端末のログには出さない)
            return
        self.term.status(f"dictation: {msg}")

    def _on_web(self, ev: dict) -> None:
        """会議の出来事を、ナギのペット(FR-14c)の表情へ: 録音中 = listen・保存とサマリ = think・自分への質問の検知 = cue・
        保存した = done。画面へ送るすべての知らせで呼ばれるので軽く済ませる(ヘルパーへは本体のループに積むだけ)。"""
        t = ev.get("type")
        eng = self.engine
        if t == "state":
            st = "think" if ev.get("saving") else "listen" if ev.get("recording") and not ev.get("paused") else "idle"
            if st != self.pet_meeting:
                self.pet_meeting = st
                if eng:
                    eng.set_meeting(st)
        elif t == "judgment" and ev.get("trigger") and eng:
            eng.meeting_event("cue")
        elif t == "session_done" and eng:
            eng.meeting_event("done")

    def _pet_action(self, ev: dict) -> None:
        """ペットの右クリックの操作: 隠す・録っている間だけ・大きさは設定に保存する(画面にも出す)。画面を開くは既定のブラウザで。"""
        if ev.get("action") == "open":
            try:
                subprocess.Popen(["open", f"{self.web.url}#dictation"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                self.term.status(f"dictation_pet_open error={type(e).__name__}")
            return
        patch = {k: ev[k] for k in ("pet", "pet_size") if k in ev}
        if ev.get("action") != "set" or not patch:
            return
        s, errs = dset.save(self.cfg, patch)
        self.term.status(f"dictation_pet {patch} errors={errs or None}")
        if not errs and self.engine:
            self.engine.apply_settings(s)
        self._push()

    def _changed(self, ev: dict) -> None:
        kind = ev.get("kind")
        if kind == "pet":
            self._pet_action(ev)
            return
        if kind == "state":
            self.live = {k: v for k, v in ev.items() if k != "kind"}
        elif kind in ("level", "meter", "captured", "capture"):
            if kind == "meter":
                self.meter = {"on": bool(ev.get("on")), "error": ev.get("error")}
            self.web.push({"type": "dictation", **ev}, keep=False)
            return
        self._push(ev)

    def _push(self, ev: dict | None = None) -> None:
        self.web.push({"type": "dictation", "kind": (ev or {}).get("kind", "status"), "running": self.running(),
                       "live": self.live, "error": self.error,
                       **({"take": {k: ev.get(k) for k in ("rid", "canceled", "app", "sent")}} if ev and ev.get("kind") == "take" else {})},
                      keep=False)

    # ---- 画面へ渡す状態 ----
    def view(self) -> dict:
        s = self.settings()
        eng = self.engine
        ready = (eng.ready if eng else {}) or {}
        target_why = ""
        if s["ai_cleanup"] or s["edit_mode"]:
            from . import dictation_ai
            target_why = dictation_ai.app_target(self.cfg)[1]
        return {"settings": s, "labels": dset.labels(s), "defaults": dset.defaults(self.cfg),
                "platform": sys.platform, "running": self.running(), "error": self.error, "live": self.live,
                "ready": {k: ready.get(k) for k in ("mic", "ax", "listen", "source", "paste")},
                "bindings": (eng.bindings if eng else {}) or {},
                "helper": (self.cfg.helpers_dir / "dictation_helper" / "dictation-helper").exists(),
                "soniox_key": bool(secrets.api_key("soniox")[0]), "local": self.cfg.privacy == "local",
                "modifier_only": dset.uses_modifier_only(s), "casual_apps": sorted(set(dset.CASUAL_APPS.values())),
                "ai_why": target_why, "last": bool(eng and eng.last), "net": self.net, "meter": self.meter,
                "resource": eng.resource_view() if eng else None}

    def _apply(self, s: dict) -> None:
        """保存した設定を効かせる: 使う / 使わないの切り替えは起動と停止、それ以外は待ち受けたまま。"""
        if not s["enabled"]:
            self.stop()
            self.error = ""
        elif self.running():
            self.engine.apply_settings(s)
        else:
            self.start()

    # ---- API ----
    def api(self, method: str, sub: list[str], body: dict | None, path: str) -> tuple[int, object] | None:
        body = body or {}
        if method == "GET" and not sub:
            return 200, self.view()
        if method == "POST" and sub == ["settings"]:
            s, errs = dset.save(self.cfg, body)
            if errs:
                return 400, {"error": "・".join(errs), "errors": errs}
            self.term.status(f"dictation_settings keys={sorted(body)}")
            self._apply(s)
            return 200, self.view()
        if method == "POST" and sub == ["reset"]:
            s, errs = dset.reset(self.cfg, str(body.get("section") or ""))
            if errs:
                return 400, {"error": "・".join(errs)}
            self._apply(s)
            return 200, self.view()
        if method == "POST" and sub == ["suspend"]:
            if self.running():
                self.engine.post("suspend", on=bool(body.get("on")))
            return 200, {"ok": True}
        if method == "POST" and sub == ["capture"]:   # キーを決める間、次に押したキーをヘルパーが拾う(Fn などブラウザが知らせないキーも)
            if not self.running():
                return 409, {"error": "音声入力が待ち受けていません"}
            self.engine.post("capture", on=bool(body.get("on")))
            return 200, {"ok": True}
        if sub == ["terms"]:
            f = self.cfg.dictation.terms_file
            if method == "GET":
                text = f.read_text(encoding="utf-8") if f.exists() else ""
                return 200, {"text": text, "count": len([w for w in text.splitlines() if w.strip() and not w.strip().startswith("#")])}
            text = str(body.get("text") or "")
            if len(text) > MAX_TERMS_TEXT:
                return 400, {"error": "辞書が大きすぎます"}
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text if text.endswith("\n") or not text else text + "\n", encoding="utf-8")
            n = len([w for w in text.splitlines() if w.strip() and not w.strip().startswith("#")])
            self.term.status(f"dictation_terms count={n}")
            return 200, {"text": text, "count": n, "over": n > 300}
        if sub == ["replacements"]:
            if method == "GET":
                return 200, self.dict_replacer.view()
            text = str(body.get("text") or "")
            if len(text) > 200_000:
                return 400, {"error": "置換が大きすぎます"}
            self.dict_replacer.save(text)
            v = self.dict_replacer.view()
            self.term.status(f"dictation_replacements rules={v['rules']} ignored={len(v['ignored'])}")
            return 200, v
        if method == "GET" and sub == ["history"]:
            q = parse_qs(urlparse(path).query)
            off = int((q.get("offset") or ["0"])[0] or 0)
            return 200, dhist.list_items(self.root, (q.get("q") or [""])[0], limit=50, offset=max(0, off))
        if method == "POST" and len(sub) == 3 and sub[0] == "history" and sub[2] == "delete":
            ok = dhist.delete(self.root, sub[1])
            return (200, {"ok": True}) if ok else (404, {"error": "no such record"})
        if method == "POST" and sub == ["history", "clear"]:
            n = dhist.clear(self.root)
            if self.engine:
                self.engine.last = None
            self.term.status(f"dictation_history_clear n={n}")
            return 200, {"ok": True, "n": n}
        if method == "GET" and sub == ["stats"]:
            return 200, dhist.stats(self.root / "metrics.jsonl")
        if method == "POST" and sub == ["network"]:
            self.net = network_check(self.cfg.dictation.region, secrets.api_key("soniox")[0])
            self.term.status(f"dictation_network tls_ms={self.net.get('tls_ms')} rating={self.net['rating']} "
                             f"key={self.net['key'].get('ok')}")
            return 200, self.net
        if method == "GET" and sub in (["devices"], ["apps"]):
            if not self.running():
                return 409, {"error": "音声入力が待ち受けていません(「音声入力を使う」をオンに)"}
            r = self.engine.request(sub[0])
            return (200, r) if r else (504, {"error": "ヘルパーから返事がありません"})
        if method == "POST" and sub == ["meter"]:
            if not self.running():
                return 409, {"error": "音声入力が待ち受けていません"}
            if self.engine.take is not None:
                return 409, {"error": "録っている間はテストできません"}
            self.engine.post("meter", on=bool(body.get("on")), seconds=8)
            return 200, {"ok": True}
        if method == "POST" and sub == ["prompt"]:
            what = body.get("what")
            if what not in ("ax", "listen") or not self.running():
                return 409, {"error": "音声入力が待ち受けていません"}
            self.engine.send("ax_prompt" if what == "ax" else "listen_prompt")
            return 200, {"ok": True}
        if method == "POST" and sub == ["restart"]:
            self.restart()
            return 200, self.view()
        return None

    def audio(self, rid: str) -> Path | None:
        return dhist.audio_path(self.root, re.sub(r"\.wav$", "", rid))
