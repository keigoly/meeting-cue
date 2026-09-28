#!/usr/bin/env python3
"""更新係(Mac / Windows 同時アップデートの段 2・2026-09-27)。依存は stdlib のみ。

実行用ツリー(~/Apps/meeting-cue・branch stable)を、両方の OS の試験に合格した origin/stable へ fast-forward する。
アプリの中身は知らず、ツリーの根元の update.toml(更新の約束)だけを読む。

  updater.py [--tree <dir>] check
      launchd が 1 分おき(update.toml の check_interval_s)。stable が進んでいれば fetch だけする(動いているアプリの足元の
      ファイルは変えない)。同じ結果の確認は 1 時間に 1 行だけ記録する。
      アプリが動いていれば「今すぐ確認して」を送り、いつ入れるかはアプリが決める(終了を待って apply が呼ばれる)。
      アプリが止まっていれば、その場で apply(開き直しなし)まで行う。
  updater.py [--tree <dir>] apply [--pid <pid>] [--rebuild] [--relaunch front|bg|hidden|none]
      アプリ(pid)の終了を待って、取り込み → 作り直し(要るときだけ)→ 点検 → 開き直し → 版の確認。
      どこかで失敗したら 1 つ前へ戻して開き直し、その版を <data>/updater/bad に記録して二度と入れない。
  updater.py [--tree <dir>] verify-pending --pid <本体の pid>
      起動で確かめていない版(アプリが止まっている間・終了時に入れた版、開き直した直後に終了された版 =
      <data>/updater/unverified)を、次の起動で確かめる。packaging/launch.sh serve が印を見つけたら裏で呼ぶ。
      動けば印を消し、落ちたら確かめ済みの最後の版へ戻して開き直す。
  updater.py [--tree <dir>] install [--interval <秒>] | uninstall | status | notify-test(Discord に試験の 1 通)

記録: <data>/logs/updater-<日付>.jsonl(1 行 1 段: rid・phase・ok・ms)。phase = check / fetch / nudge / wait_quit /
merge / build / smoke / relaunch / verify / quit / rollback / apply / notify / install / pending / pending_clear /
verify_pending。
開発用ツリー(branch が stable 以外)では取り込みも巻き戻しもしない(作り直しと開き直しだけ)。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import plistlib
import socket
import subprocess
import sys
import time
import tomllib
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:   # Windows(段 3 で作る)
    fcntl = None

KEEP_LOGS = 14
QUIET_S = 3600   # 同じ結果の確認(変化なし・同じエラー)は 1 時間に 1 行だけ記録する(1 分おきに確かめるため)
RELAUNCH = ("front", "bg", "hidden", "none")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


class Updater:
    def __init__(self, tree: Path, rid: str | None = None):
        self.tree = Path(tree).resolve()
        conf = tomllib.loads((self.tree / "update.toml").read_text(encoding="utf-8"))
        self.app: dict = conf["app"]
        port = str(self.port())
        for k in ("version_url", "nudge_url"):   # {port} = アプリの画面のポート(利用者ごと・2026-09-28)
            if k in self.app:
                self.app[k] = self.app[k].replace("{port}", port)
        self.mac: dict = conf.get("mac", {})
        self.notify_conf: dict = conf.get("notify", {})
        self.branch: str = self.app["branch"]
        home = os.environ.get(self.app.get("data_env") or "") or os.path.expanduser(self.app["data_dir"])
        self.data = Path(home)
        self.rid = rid or os.environ.get("MEETCUE_UPDATE_RID") or uuid.uuid4().hex[:8]
        self.t0 = time.monotonic()
        self._foreign_logged = False

    def port(self) -> int:
        """アプリの画面のポート: port_env の値か、port_base + (uid − 501) を 100 で回した値(同じ Mac の利用者ごとに分ける)。
        アプリ側(meetcue/config.py の default_port・launch.sh・overlay_helper)と同じ決まり。uid の無い OS は port_base。"""
        base = int(self.app.get("port_base") or 8765)
        env = os.environ.get(self.app.get("port_env") or "", "")
        if env.isdigit() and 1024 <= int(env) <= 65535:
            return int(env)
        return base + (os.getuid() - 501) % 100 if hasattr(os, "getuid") else base

    # ---- 記録 --------------------------------------------------------------------------------
    def log(self, phase: str, ok: bool = True, since: float | None = None, **kw) -> dict:
        rec: dict = {"t_ms": int(time.time() * 1000), "rid": self.rid, "phase": phase, "ok": ok}
        if since is not None:
            rec["ms"] = round((time.monotonic() - since) * 1000)
        rec.update({k: v for k, v in kw.items() if v is not None and v != ""})
        d = self.data / "logs"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"updater-{datetime.now():%Y%m%d}.jsonl"
        fresh = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if fresh:
            for old in sorted(d.glob("updater-*.jsonl"))[:-KEEP_LOGS]:
                old.unlink(missing_ok=True)
        if sys.stdout.isatty():   # launchd の記録(updater-launchd.log)には重ねない(作り直しの出力とエラーだけ)
            print(json.dumps(rec, ensure_ascii=False), flush=True)
        return rec

    # ---- git ---------------------------------------------------------------------------------
    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.tree), *args], capture_output=True, text=True, timeout=120)

    def rev(self, ref: str) -> str:
        r = self.git("rev-parse", "--verify", "-q", ref + "^{commit}")
        return r.stdout.strip() if r.returncode == 0 else ""

    def on_branch(self) -> bool:
        return self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == self.branch

    def dirty(self) -> bool:
        return bool(self.git("status", "--porcelain", "--untracked-files=no").stdout.strip())

    def touches(self, a: str, b: str) -> bool:
        """a..b で作り直しが要るファイル(update.toml の rebuild_paths)が変わったか。"""
        if not a or not b or a == b:
            return False
        names = self.git("diff", "--name-only", f"{a}..{b}").stdout.splitlines()
        return any(n.startswith(tuple(self.mac.get("rebuild_paths", []))) for n in names)

    # ---- 巻き戻した版 / 1 つずつ -------------------------------------------------------------
    @property
    def bad_file(self) -> Path:
        return self.data / "updater" / "bad"

    def bad(self) -> set[str]:
        try:
            return set(self.bad_file.read_text(encoding="utf-8").split())
        except OSError:
            return set()

    def mark_bad(self, sha: str) -> None:
        self.bad_file.parent.mkdir(parents=True, exist_ok=True)
        with self.bad_file.open("a", encoding="utf-8") as f:
            f.write(sha + "\n")

    # ---- 起動で確かめていない版(2026-09-27): 次の起動で確かめる ------------------------------
    @property
    def pending_file(self) -> Path:
        return self.data / "updater" / "unverified"

    def pending(self) -> dict | None:
        """起動で確かめていない版の印 {"sha", "good"(確かめ済みの最後の版 = 戻す先), "t_ms"}。"""
        try:
            p = json.loads(self.pending_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return p if isinstance(p, dict) and p.get("sha") and p.get("good") else None

    def set_pending(self, sha: str, good: str) -> None:
        self.pending_file.parent.mkdir(parents=True, exist_ok=True)
        self.pending_file.write_text(json.dumps({"sha": sha, "good": good, "t_ms": int(time.time() * 1000)}), encoding="utf-8")
        self.log("pending", True, sha=sha[:7], good=good[:7])

    def clear_pending(self, why: str) -> None:
        if self.pending_file.exists():
            self.pending_file.unlink(missing_ok=True)
            self.log("pending_clear", True, why=why)

    @contextlib.contextmanager
    def lock(self, wait_s: float):
        """launchd の check とアプリからの apply を重ねない。wait_s 待っても取れなければ False。"""
        p = self.data / "updater" / "lock"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            if fcntl is None:
                yield True
                return
            deadline = time.monotonic() + wait_s
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        yield False
                        return
                    time.sleep(0.5)
            try:
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    # ---- アプリ(update.toml の約束だけを使う) ----------------------------------------------
    def cmds(self, key: str) -> list[list[str]]:
        out = []
        for c in self.mac.get(key, []):
            c = [sys.executable if a == "{python}" else a for a in c]
            if "/" in c[0] and not os.path.isabs(c[0]):
                c[0] = str(self.tree / c[0])
            out.append(c)
        return out

    def run_cmds(self, key: str) -> bool:
        t = time.monotonic()
        for c in self.cmds(key):
            try:
                r = subprocess.run(c, cwd=self.tree, timeout=900)   # 出力はそのまま(update-*.log / launchd の記録へ)
            except (OSError, subprocess.SubprocessError) as e:
                self.log(key, False, t, cmd=Path(c[0]).name, error=str(e)[:300])
                return False
            if r.returncode != 0:
                self.log(key, False, t, cmd=" ".join(Path(c[0]).name if i == 0 else a for i, a in enumerate(c))[:200],
                         rc=r.returncode)
                return False
        self.log(key, True, t)
        return True

    def app_running(self) -> bool:
        proc = self.mac.get("process")
        if not proc or sys.platform != "darwin":
            return False
        return subprocess.run(["pgrep", "-x", proc], capture_output=True).returncode == 0

    def running(self) -> str:
        try:
            with urllib.request.urlopen(self.app["version_url"], timeout=2) as r:
                v = json.loads(r.read())
        except (OSError, ValueError):
            return ""
        uid = v.get("uid")
        if uid is not None and hasattr(os, "getuid") and uid != os.getuid():
            # 同じ Mac の別の利用者の本体が答えた(2026-09-28)。その版はこの利用者のアプリの版ではないので確かめに使わない。
            # 記録は 1 回の実行で 1 行だけ
            if not self._foreign_logged:
                self._foreign_logged = True
                self.log("version_foreign", False, url=self.app["version_url"], server_uid=uid, own_uid=os.getuid(),
                         running=str(v.get("running") or ""))
            return ""
        return str(v.get("running") or "")

    def nudge(self) -> bool:
        t = time.monotonic()
        req = urllib.request.Request(self.app["nudge_url"], data=json.dumps({"action": "update_check"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                ok = r.status == 200
        except (OSError, ValueError) as e:
            self.log("nudge", False, t, error=str(e)[:200])
            return False
        self.log("nudge", ok, t)
        return ok

    def relaunch(self, mode: str) -> int:
        """開き直す。戻り値 = 開き直す直前の時刻(ms・verify がこれより後の本体の終了だけを見る)。"""
        since = int(time.time() * 1000)
        t = time.monotonic()
        args = ["open"] + (["-g"] if mode in ("bg", "hidden") else []) + (["-j"] if mode == "hidden" else [])
        args += ["-b", self.mac["bundle_id"]]
        if mode in ("bg", "hidden"):   # フォーカスを奪わない(hidden = 閉じていたウィンドウは開かない)
            args += ["--args", "--relaunch", mode]
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.log("relaunch", r.returncode == 0, t, mode=mode, error=r.stderr.strip()[:200])
        return since

    def host_events(self, since_ms: int) -> list[dict]:
        """開き直した後のアプリ(ホスト)の記録(update.toml の exit_log)。日付をまたいでも見落とさない。"""
        pat = self.mac.get("exit_log")
        if not pat:
            return []
        out = []
        for day in sorted({datetime.fromtimestamp(since_ms / 1000).strftime("%Y%m%d"), datetime.now().strftime("%Y%m%d")}):
            try:
                lines = (self.data / pat.replace("{date}", day)).read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if int(r.get("t_ms") or 0) >= since_ms:
                    out.append(r)
        return out

    def exited(self, events: list[dict]) -> int:
        """本体が落ちたか(exit_phase の 0 以外の status)。終了コード 0(前の版の正規の終了など)と、終了の操作で
        止めた(quitting)ものは数えない。"""
        phase = self.mac.get("exit_phase")
        return next((int(r["status"]) for r in events
                     if phase and r.get("phase") == phase and r.get("status") and not r.get("quitting")), 0)

    def quitted(self, events: list[dict]) -> bool:
        """利用者がアプリを終了したか(quit_phase)。"""
        phase = self.mac.get("quit_phase")
        return bool(phase) and any(r.get("phase") == phase for r in events)

    def spawn_time(self, pid: int | None, wait_s: float = 5) -> int:
        """本体(pid)を起動した記録の時刻(update.toml の spawn_phase)。次の起動で確かめるときの起点。無ければ 0。"""
        phase = self.mac.get("spawn_phase")
        t = time.monotonic()
        while pid and phase and time.monotonic() - t < wait_s:   # ホストが記録を書くのは起動の直後なので少し待つ
            for r in self.host_events(int(time.time() * 1000) - 120_000):
                if r.get("phase") == phase and r.get("pid") == pid:
                    return int(r["t_ms"])
            time.sleep(0.25)
        return 0

    def verify(self, want: str, since_ms: int = 0, timeout: float = 90) -> str:
        """開き直した版が動いたか(/api/version の running)。戻り値 = "ok" / "failed" / "quit"。
        本体が落ちた記録が出たら待たずに "failed"(2026-09-27・以前は落ちても 90 s 待っていた)。利用者が終了した記録が
        出たら "quit"(確かめられないが失敗ではない・同日)。timeout は固まって応答しないときの上限。"""
        t = time.monotonic()
        got, code, quit_ = "", 0, False
        while time.monotonic() - t < timeout:
            got = self.running()
            if got and want.startswith(got):
                break
            events = self.host_events(since_ms) if since_ms else []
            code = self.exited(events)   # 落ちた後に終了されたなら、落ちたほうを取る
            quit_ = not code and self.quitted(events)
            if code or quit_:
                break
            time.sleep(0.5)
        res = "ok" if got and want.startswith(got) else "quit" if quit_ else "failed"
        self.log("verify", res == "ok", t, want=want[:7], running=got, exited=code or None, quit=quit_ or None)
        return res

    def quit_app(self) -> None:
        t = time.monotonic()
        subprocess.run(["osascript", "-e", f'tell application id "{self.mac["bundle_id"]}" to quit'],
                       capture_output=True, timeout=150)
        while time.monotonic() - t < 150 and self.app_running():
            time.sleep(0.5)
        self.log("quit", not self.app_running(), t)

    def notify(self, text: str) -> None:
        """失敗・巻き戻しの知らせ: この機械の通知 + Discord(update.toml の [notify]・鍵があるときだけ)。"""
        title = f'{self.app.get("display_name") or self.app["name"]} の更新'
        if sys.platform == "darwin":
            script = f"display notification {json.dumps(text, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}"
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        self.log("notify", True, text=text, discord=self.discord(f"[{title}・{socket.gethostname().split('.')[0]}] {text}"))

    def webhook(self) -> str:
        """Discord の Webhook の URL(環境変数 → 鍵のファイル <key>=<URL>)。無ければ空。"""
        key = self.notify_conf.get("discord_key")
        if not key:
            return ""
        if os.environ.get(key):
            return os.environ[key]
        try:
            text = Path(os.path.expanduser(self.notify_conf.get("discord_env_file", ""))).read_text(encoding="utf-8")
        except OSError:
            return ""
        for line in text.splitlines():
            k, sep, v = line.strip().removeprefix("export ").partition("=")
            if sep and k.strip() == key:
                return v.strip().strip("'\"")
        return ""

    def discord(self, content: str) -> str:
        """Discord に 1 通(メンションは出さない)。戻り値 = "sent" / "skipped"(鍵なし)/ "error: …"。失敗しても更新は止めない。"""
        url = self.webhook()
        if not url:
            return "skipped"
        body = json.dumps({"content": content[:1900], "allowed_mentions": {"parse": []}}, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "User-Agent": f"{self.app['name']}-updater/1.0",   # 既定の Python-urllib は Discord に弾かれることがある
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return "sent" if 200 <= r.status < 300 else f"error: HTTP {r.status}"
        except (OSError, ValueError) as e:
            return f"error: {str(e)[:120]}"

    # ---- 同じことを何度も書かない・送らない(1 分おきに確かめるため) --------------------------
    def _state(self) -> dict:
        try:
            return json.loads((self.data / "updater" / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, st: dict) -> None:
        p = self.data / "updater" / "state.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

    def quiet(self, key: str) -> bool:
        """同じ結果(key)を直近 QUIET_S 秒以内に記録していれば True(記録しない)。違えば記録する側に回して覚える。"""
        st = self._state()
        last = st.get("check") or {}
        now = time.time()
        if last.get("key") == key and now - float(last.get("t") or 0) < QUIET_S:
            return True
        st["check"] = {"key": key, "t": now}
        self._save_state(st)
        return False

    def notify_once(self, key: str, text: str) -> None:
        """同じ出来事(key = 種類と版)は 1 回だけ知らせる(1 分おきの確認で同じ通知を繰り返さない)。"""
        st = self._state()
        sent = st.get("notified") or []
        if key in sent:
            return
        st["notified"] = (sent + [key])[-50:]
        self._save_state(st)
        self.notify(text)

    # ---- 本体 --------------------------------------------------------------------------------
    def check(self) -> int:
        t = time.monotonic()
        with self.lock(0) as got:
            if not got:
                self.log("check", True, t, skipped="locked")
                return 0
            if not self.on_branch():
                self.log("check", False, t, error=f"branch が {self.branch} ではない(開発用ツリーには取り込まない)")
                return 2
            head = self.rev("HEAD")
            r = self.git("ls-remote", "origin", f"refs/heads/{self.branch}")
            remote = r.stdout.split()[0] if r.returncode == 0 and r.stdout.strip() else ""
            if not remote:
                err = (r.stderr.strip() or f"origin に {self.branch} が無い")[:300]
                if not self.quiet("error:" + err[:80]):   # 寝ている・つながらない間は 1 時間に 1 行
                    self.log("check", False, t, error=err)
                return 1
            blocked = remote in self.bad()
            changed = remote != head and not blocked
            if changed or not self.quiet(f"same:{head}:{remote}:{blocked}"):   # 変化なしは 1 時間に 1 行
                self.log("check", True, t, head=head[:7], remote=remote[:7], changed=changed, blocked=blocked or None)
            if remote == head or blocked:
                return 0
            if self.rev(f"origin/{self.branch}") != remote:
                t = time.monotonic()
                f = self.git("fetch", "-q", "origin", self.branch)
                remote = self.rev(f"origin/{self.branch}")
                self.log("fetch", f.returncode == 0, t, to=remote[:7], error=f.stderr.strip()[:300])
                if f.returncode != 0 or not remote or remote in self.bad():
                    return 1 if f.returncode != 0 else 0
            if self.git("merge-base", "--is-ancestor", head, remote).returncode != 0:
                self.log("check", False, error="fast-forward できない(stable が書き換えられた?)", head=head[:7], remote=remote[:7])
                self.notify_once(f"ff:{remote}", f"stable({remote[:7]})が今の版 {head[:7]} の続きではないため取り込みません(stable が書き換えられた?)")
                return 1
            if self.app_running():   # いつ入れるかはアプリが決める(録音中は待つ・自動なら空いたときに裏で再起動)
                self.nudge()
                return 0
            return self._apply_locked("none", False)

    def apply(self, pid: int | None, rebuild: bool, relaunch: str) -> int:
        if pid:
            t = time.monotonic()
            while time.monotonic() - t < 150 and _alive(pid):   # 録音の保存とサマリを待つ(アプリの終了は最大 120 s)
                time.sleep(0.5)
            self.log("wait_quit", not _alive(pid), t, pid=pid)
        with self.lock(600) as got:
            if not got:
                self.log("apply", False, error="別の更新が終わらない(lock)")
                if relaunch != "none":
                    self.relaunch(relaunch)
                return 1
            return self._apply_locked(relaunch, rebuild)

    def _apply_locked(self, relaunch: str, rebuild: bool) -> int:
        prev = new = self.rev("HEAD")
        old = self.pending()
        old = old if old and old["sha"] == prev else None   # 今の版がまだ起動で確かめていない版か
        good = old["good"] if old else prev                 # 失敗したときに戻す先 = 確かめ済みの最後の版
        if self.on_branch():
            target = self.rev(f"origin/{self.branch}")
            if target and target != prev and target not in self.bad():
                t = time.monotonic()
                if self.dirty():
                    self.log("merge", False, t, error="実行用ツリーに手で入れた変更がある(取り込まない)")
                    self.notify_once(f"dirty:{target}", f"実行用ツリー {self.tree} に手で入れた変更があるため、{target[:7]} を取り込みません")
                else:
                    m = self.git("merge", "--ff-only", "-q", target)
                    new = self.rev("HEAD")
                    self.log("merge", m.returncode == 0 and new == target, t, **{"from": prev[:7], "to": target[:7]},
                             error=m.stderr.strip()[:300])
        built = rebuild or self.touches(prev, new)
        ok = self.run_cmds("build") if built else True
        ok = ok and self.run_cmds("smoke")
        launched = False
        unverified = self.on_branch() and (new != prev or old is not None)   # 起動で確かめる版があるか
        if ok and relaunch != "none":
            self.clear_pending("relaunch")   # ここで開き直して確かめる(次の起動の確認と二重にしない)
            since = self.relaunch(relaunch)
            launched = True
            res = self.verify(new, since)
            if res == "quit":   # 開き直した直後に利用者が終了した: 確かめられないが失敗ではない(巻き戻さない・開き直さない)
                if unverified:
                    self.set_pending(new, good)   # 次の起動で確かめる
                self.log("apply", True, self.t0, **{"from": prev[:7], "to": new[:7]}, built=built, relaunch=relaunch,
                         verified=False)
                return 0
            ok = res == "ok"
        if ok:
            if not launched and unverified:
                self.set_pending(new, good)       # 開き直していない(アプリが止まっている / 終了時)→ 次の起動で確かめる
            self.log("apply", True, self.t0, **{"from": prev[:7], "to": new[:7]}, built=built, relaunch=relaunch,
                     verified=launched or not unverified)
            return 0
        if new != good and self.on_branch():
            self.rollback(good, new, relaunch, built)
        else:
            self.notify(f"更新の作り直しか起動の確認に失敗しました({new[:7]})。記録: {self.data / 'logs'}")
            if relaunch != "none" and not launched:
                self.relaunch(relaunch)
        return 1

    def rollback(self, prev: str, bad: str, relaunch: str, built: bool) -> None:
        t = time.monotonic()
        self.mark_bad(bad)
        if self.app_running():   # 開き直した壊れた版を終わらせてから戻す
            self.quit_app()
        r = self.git("reset", "--hard", "-q", prev)
        ok = r.returncode == 0 and self.rev("HEAD") == prev
        self.clear_pending("rollback")   # 戻した版はここで確かめる
        if ok and (built or self.touches(prev, bad)):
            ok = self.run_cmds("build")
        if ok and relaunch != "none":
            since = self.relaunch(relaunch)
            ok = self.verify(prev, since) in ("ok", "quit")   # 戻した版を開いた直後に終了されても、戻すこと自体はできた
        self.log("rollback", ok, t, **{"from": bad[:7], "to": prev[:7]}, error=r.stderr.strip()[:300])
        self.notify(f"更新 {bad[:7]} を入れられなかったため、{prev[:7]} に戻しました(この版は二度と入れません)" if ok
                    else f"更新 {bad[:7]} の巻き戻しに失敗しました。記録: {self.data / 'logs'}")

    def verify_pending(self, pid: int | None) -> int:
        """起動で確かめていない版を、今の起動で確かめる(packaging/launch.sh serve が印を見つけたら裏で呼ぶ・pid = 本体)。
        動けば印を消す。利用者が終了したら印を残す(次の起動でもう一度)。落ちたら確かめ済みの最後の版へ戻して裏で開き直す。"""
        p = self.pending()
        if not p:
            return 0
        head = self.rev("HEAD")
        if p["sha"] != head or not self.on_branch():   # 版が動いた(巻き戻し・手の操作)→ 古い印
            self.clear_pending("stale")
            return 0
        since = self.spawn_time(pid) or int(time.time() * 1000)
        res = self.verify(head, since)
        if res == "ok":
            self.clear_pending("verified")
            return 0
        if res == "quit":
            self.log("pending", True, sha=head[:7], good=p["good"][:7], quit=True)
            return 0
        with self.lock(120) as got:
            p = self.pending()
            if not got or not p or p["sha"] != self.rev("HEAD"):   # 待つ間に別の更新が片付けた
                self.log("verify_pending", False, error="lock" if not got else "印が変わった")
                return 1
            # 裏で開き直す(2026-09-27 keigoly様): ウィンドウは出すがフォーカスを奪わない(前の版で開いたことは通知で知らせる)
            self.rollback(p["good"], p["sha"], "bg", False)
        return 1

    # ---- launchd -----------------------------------------------------------------------------
    def _plist(self) -> tuple[str, Path]:
        label = self.mac["updater_label"]
        return label, Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"

    def install(self, interval: int) -> int:
        label, plist = self._plist()
        logs = self.data / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        doc = {"Label": label, "ProgramArguments": ["/bin/bash", str(self.tree / "packaging" / "updater" / "run.sh"), "check"],
               "StartInterval": interval, "RunAtLoad": True,
               "StandardOutPath": str(logs / "updater-launchd.log"), "StandardErrorPath": str(logs / "updater-launchd.log")}
        if os.environ.get(self.app.get("data_env") or ""):
            doc["EnvironmentVariables"] = {self.app["data_env"]: str(self.data)}
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps(doc))
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True, text=True)
        self.log("install", r.returncode == 0, label=label, interval=interval, tree=str(self.tree), error=r.stderr.strip()[:300])
        return 0 if r.returncode == 0 else 1

    def uninstall(self) -> int:
        label, plist = self._plist()
        r = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True)
        plist.unlink(missing_ok=True)
        self.log("uninstall", True, label=label, error=r.stderr.strip()[:300])
        return 0

    def status(self) -> int:
        head, up = self.rev("HEAD"), self.rev(f"origin/{self.branch}")
        print(f"tree    {self.tree}(branch {self.git('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()})")
        print(f"HEAD    {head[:7]}  origin/{self.branch} {up[:7]}  bad {sorted(b[:7] for b in self.bad()) or '-'}")
        p = self.pending()
        print(f"running {self.running() or '-'}  unverified {p['sha'][:7] + '(戻す先 ' + p['good'][:7] + ')' if p else '-'}")
        logs = sorted((self.data / "logs").glob("updater-*.jsonl"))
        if logs:
            for line in logs[-1].read_text(encoding="utf-8").splitlines()[-12:]:
                print("  " + line)
        return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="更新係(stable を実行用ツリーへ)")
    p.add_argument("--tree", default=str(Path(__file__).resolve().parents[2]))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    a = sub.add_parser("apply")
    a.add_argument("--pid", type=int)
    a.add_argument("--rebuild", action="store_true")
    a.add_argument("--relaunch", choices=RELAUNCH, default="front")
    i = sub.add_parser("install")
    i.add_argument("--interval", type=int, help="確かめる間隔(秒)。既定は update.toml の check_interval_s")
    sub.add_parser("uninstall")
    sub.add_parser("status")
    sub.add_parser("notify-test")
    vp = sub.add_parser("verify-pending")
    vp.add_argument("--pid", type=int)
    ns = p.parse_args(argv)
    u = Updater(Path(ns.tree))
    if ns.cmd == "check":
        return u.check()
    if ns.cmd == "apply":
        return u.apply(ns.pid, ns.rebuild, ns.relaunch)
    if ns.cmd == "install":
        return u.install(ns.interval or int(u.app.get("check_interval_s") or 300))
    if ns.cmd == "notify-test":
        res = u.discord(f"[{u.app.get('display_name') or u.app['name']} の更新・{socket.gethostname().split('.')[0]}] 通知の試験です(更新係から Discord へ届くかの確認)")
        u.log("notify_test", res == "sent", discord=res)
        print(res)
        return 0 if res == "sent" else 1
    if ns.cmd == "uninstall":
        return u.uninstall()
    if ns.cmd == "verify-pending":
        return u.verify_pending(ns.pid)
    return u.status()


if __name__ == "__main__":
    sys.exit(main())
