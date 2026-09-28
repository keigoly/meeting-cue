"""更新係(packaging/updater/updater.py)と、アプリの「入れる版」(/api/version)の試験(2026-09-27 段 2)。

一時フォルダに origin(bare)・開発用(main)・実行用(stable)の 3 つの git を作る。本物のアプリ・launchd・通知には
触れない(アプリが動いているか・合図・通知は差し替える。作り直しと点検は python の短いコマンド)。
"""
import importlib.util
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from pathlib import Path

import pytest

from meetcue import app as app_mod
from meetcue.app import App
from meetcue.config import Config

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("updater", ROOT / "packaging" / "updater" / "updater.py")
updater = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(updater)

TOML = """
[app]
name = "t"
branch = "stable"
data_dir = "~/.unused"
data_env = "MEETCUE_HOME"
version_url = "http://127.0.0.1:9/api/version"
nudge_url = "http://127.0.0.1:9/api/action"

[mac]
bundle_id = "x.test"
process = "no-such-process"
updater_label = "x.test.updater"
build = [["{python}", "-c", "import os; open(os.path.join(os.environ['MEETCUE_HOME'], 'built'), 'a').write('x')"]]
rebuild_paths = ["helpers/"]
smoke = [["{python}", "-c", "import mod"]]
exit_log = "logs/host-{date}.jsonl"
exit_phase = "host_child_exit"
quit_phase = "host_quit"
spawn_phase = "host_spawn"

[notify]
discord_env_file = "~/.unused-test.env"
discord_key = "T_MEETCUE_WEBHOOK"
"""


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MEETCUE_HOME", str(home))
    monkeypatch.delenv("MEETCUE_UPDATE_RID", raising=False)
    monkeypatch.delenv("T_MEETCUE_WEBHOOK", raising=False)
    monkeypatch.setattr(updater.Updater, "notify", lambda self, text: None)   # 本物の通知(osascript)を出さない
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = t\n\temail = t@example.com\n[commit]\n\tgpgsign = false\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    dev = tmp_path / "dev"
    dev.mkdir()
    git(dev, "init", "-q", "-b", "main")
    (dev / "update.toml").write_text(TOML, encoding="utf-8")
    (dev / "mod.py").write_text("X = 1\n", encoding="utf-8")
    git(dev, "add", "-A")
    git(dev, "commit", "-q", "-m", "c1")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(dev), str(origin)], check=True, capture_output=True)
    git(dev, "remote", "add", "origin", str(origin))
    git(dev, "push", "-q", "origin", "main:stable")
    run = tmp_path / "run"
    subprocess.run(["git", "clone", "-q", "-b", "stable", str(origin), str(run)], check=True, capture_output=True)
    return dev, run, home


def advance(dev, files: dict, msg="next") -> str:
    """開発用で 1 コミット作り、stable へ進める(試験に合格して promote された、の代わり)。"""
    for name, text in files.items():
        p = dev / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(dev, "add", "-A")
    git(dev, "commit", "-q", "-m", msg)
    git(dev, "push", "-q", "origin", "main:stable")
    return git(dev, "rev-parse", "HEAD")


def records(home) -> list[dict]:
    return [json.loads(line) for f in sorted((home / "logs").glob("updater-*.jsonl"))
            for line in f.read_text(encoding="utf-8").splitlines()]


def built(home) -> int:
    p = home / "built"
    return len(p.read_text()) if p.exists() else 0


def test_check_without_change_only_records(repos):
    _, run, home = repos
    head = git(run, "rev-parse", "HEAD")
    assert updater.Updater(run).check() == 0
    rec = records(home)
    assert [r["phase"] for r in rec] == ["check"]
    assert rec[0]["ok"] and rec[0]["changed"] is False and rec[0]["head"] == head[:7] and "ms" in rec[0] and rec[0]["rid"]
    assert git(run, "rev-parse", "HEAD") == head


def test_app_stopped_check_takes_new_stable_and_builds_only_when_needed(repos):
    dev, run, home = repos
    new = advance(dev, {"mod.py": "X = 2\n"})
    assert updater.Updater(run).check() == 0
    assert git(run, "rev-parse", "HEAD") == new and built(home) == 0          # 本体(Python)だけ → 作り直さない
    phases = [r["phase"] for r in records(home)]
    assert phases == ["check", "fetch", "merge", "smoke", "pending", "apply"]   # 起動では確かめていない → 印
    newer = advance(dev, {"helpers/a.swift": "// v2\n"})
    assert updater.Updater(run).check() == 0
    assert git(run, "rev-parse", "HEAD") == newer and built(home) == 1      # 作り直しの場所が変わった → 1 回


def test_app_running_check_only_fetches_and_nudges(repos, monkeypatch):
    dev, run, home = repos
    head = git(run, "rev-parse", "HEAD")
    new = advance(dev, {"mod.py": "X = 2\n"})
    nudged = []
    monkeypatch.setattr(updater.Updater, "app_running", lambda self: True)
    monkeypatch.setattr(updater.Updater, "nudge", lambda self: nudged.append(1) or True)
    assert updater.Updater(run).check() == 0
    assert git(run, "rev-parse", "HEAD") == head                              # 動いているアプリの足元は変えない
    assert git(run, "rev-parse", "origin/stable") == new and nudged == [1]
    assert updater.Updater(run).check() == 0 and nudged == [1, 1]            # 入れるまで 5 分ごとに合図し直す


def test_broken_version_rolls_back_and_is_never_taken_again(repos, monkeypatch):
    dev, run, home = repos
    prev = git(run, "rev-parse", "HEAD")
    bad = advance(dev, {"mod.py": "raise SystemExit(3)\n", "helpers/a.swift": "// v2\n"})
    notes = []
    monkeypatch.setattr(updater.Updater, "notify", lambda self, text: notes.append(text))
    monkeypatch.setattr(updater.Updater, "app_running", lambda self: True)
    monkeypatch.setattr(updater.Updater, "nudge", lambda self: True)
    assert updater.Updater(run).check() == 0                                  # 動いている → fetch だけ
    monkeypatch.setattr(updater.Updater, "app_running", lambda self: False)
    assert updater.Updater(run).apply(None, False, "none") == 1               # アプリの終了の後(終了時にインストール)
    assert git(run, "rev-parse", "HEAD") == prev                              # 点検で読み込めない → 1 つ前へ
    assert (home / "updater" / "bad").read_text().split() == [bad]
    assert built(home) == 2                                                   # 新しい版で 1 回・戻した版で 1 回
    assert notes and bad[:7] in notes[0] and prev[:7] in notes[0]
    rb = [r for r in records(home) if r["phase"] == "rollback"]
    assert rb and rb[0]["ok"] and rb[0]["from"] == bad[:7] and rb[0]["to"] == prev[:7]

    assert updater.Updater(run).check() == 0                                  # 同じ版は二度と取り込まない
    assert git(run, "rev-parse", "HEAD") == prev and records(home)[-1].get("blocked") is True
    fixed = advance(dev, {"mod.py": "X = 3\n"})                               # 直した版が stable に来たら入る
    assert updater.Updater(run).check() == 0 and git(run, "rev-parse", "HEAD") == fixed


def test_app_sees_fetched_stable_as_the_update_and_hides_bad(repos, monkeypatch, tmp_path):
    dev, run, home = repos
    monkeypatch.setattr(app_mod, "REPO", run)
    a = App(Config(app_dir=home), sources=[], window=False)
    head = git(run, "rev-parse", "HEAD")
    a._boot_commit = head
    assert a._version("")["behind"] == 0
    new = advance(dev, {"mod.py": "X = 2\n"})
    git(run, "fetch", "-q", "origin", "stable")                               # 更新係の check(アプリが動いている)
    v = a._version("")
    assert (v["head"], v["running"], v["behind"], v["blocked"]) == (new[:7], head[:7], 1, "")
    (home / "updater").mkdir(exist_ok=True)
    (home / "updater" / "bad").write_text(new + "\n", encoding="utf-8")
    v = a._version("")
    assert (v["head"], v["behind"], v["blocked"]) == (head[:7], 0, new[:7])   # 巻き戻した版は出さない
    monkeypatch.setattr(app_mod, "REPO", dev)                                 # 開発用ツリー(main)は HEAD のまま
    a._boot_commit = git(dev, "rev-parse", "HEAD")
    assert a._version("")["behind"] == 0


def test_app_rebuild_flag_follows_update_toml(repos, monkeypatch):
    dev, run, home = repos
    monkeypatch.setattr(app_mod, "REPO", run)
    a = App(Config(app_dir=home), sources=[], window=False)
    a._boot_commit = git(run, "rev-parse", "HEAD")
    advance(dev, {"mod.py": "X = 2\n", "packaging/other.sh": "echo\n"})     # 試験の約束では helpers/ だけが作り直し
    git(run, "fetch", "-q", "origin", "stable")
    assert a._version("")["rebuild"] is False
    advance(dev, {"helpers/a.swift": "// v2\n"})
    git(run, "fetch", "-q", "origin", "stable")
    assert a._version("")["rebuild"] is True


def test_update_check_action_sets_nudge_for_the_host(tmp_path):
    a = App(Config(app_dir=tmp_path), sources=[], window=False)
    assert "update_nudge" not in a.web.state
    a._action("update_check", {})
    assert a.web.state["update_nudge"] > 0


def test_dev_branch_and_dirty_tree_are_not_touched(repos, monkeypatch):
    dev, run, home = repos
    assert updater.Updater(dev).check() == 2                                  # 開発用ツリー(main)には取り込まない
    head = git(run, "rev-parse", "HEAD")
    advance(dev, {"mod.py": "X = 2\n"})
    (run / "mod.py").write_text("X = 99\n", encoding="utf-8")                # 実行用ツリーを手で変えた
    assert updater.Updater(run).check() == 0
    assert git(run, "rev-parse", "HEAD") == head and (run / "mod.py").read_text() == "X = 99\n"
    assert any(r["phase"] == "merge" and not r["ok"] for r in records(home))


def test_rewritten_stable_is_refused(repos):
    dev, run, home = repos
    head = git(run, "rev-parse", "HEAD")
    git(dev, "checkout", "-q", "--orphan", "other")
    (dev / "mod.py").write_text("X = 7\n", encoding="utf-8")
    git(dev, "add", "-A")
    git(dev, "commit", "-q", "-m", "rewritten")
    git(dev, "push", "-q", "-f", "origin", "other:stable")
    assert updater.Updater(run).check() == 1
    assert git(run, "rev-parse", "HEAD") == head
    assert "fast-forward" in records(home)[-1]["error"]


def _host_log(home, **rec):
    d = home / "logs"
    d.mkdir(exist_ok=True)
    with (d / f"host-{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def test_crash_right_after_relaunch_rolls_back_without_waiting(repos, monkeypatch):
    dev, run, home = repos
    prev = git(run, "rev-parse", "HEAD")
    bad = advance(dev, {"mod.py": "X = 2\n"})                                  # 読み込めるが起動で落ちる版の代わり
    git(run, "fetch", "-q", "origin", "stable")
    now = int(time.time() * 1000)
    _host_log(home, phase="host_child_exit", status=1, t_ms=now - 60000)     # 開き直す前の記録は数えない
    _host_log(home, phase="host_child_exit", status=0, t_ms=now)             # 前の版の正規の終了(0)も数えない
    _host_log(home, phase="host_quit", t_ms=now - 1)                         # 前の版を入れ替えのために終えた記録も数えない
    state = {"running": ""}
    launched = []

    def relaunch(self, mode):
        since = int(time.time() * 1000)
        head = git(run, "rev-parse", "HEAD")
        launched.append(head)
        if head == bad:
            _host_log(home, phase="host_child_exit", status=1, t_ms=since)   # 起動直後に本体が落ちた
            _host_log(home, phase="host_quit", t_ms=since)                   # その後に終了されても、落ちたほうを取る
        else:
            state["running"] = head[:7]
        return since

    monkeypatch.setattr(updater.Updater, "relaunch", relaunch)
    monkeypatch.setattr(updater.Updater, "running", lambda self: state["running"])
    monkeypatch.setattr(updater.Updater, "app_running", lambda self: False)
    monkeypatch.setattr(updater.Updater, "notify", lambda self, text: None)
    t = time.monotonic()
    assert updater.Updater(run).apply(None, False, "bg") == 1
    assert time.monotonic() - t < 15                                          # 以前は 90 s 待っていた
    assert launched == [bad, prev] and git(run, "rev-parse", "HEAD") == prev
    v = [r for r in records(home) if r["phase"] == "verify"]
    assert (v[0]["ok"], v[0]["exited"]) == (False, 1) and v[1]["ok"] is True and "exited" not in v[1]
    assert (home / "updater" / "bad").read_text().split() == [bad]


def test_verify_waits_for_running_when_nothing_exited(repos, monkeypatch):
    _, run, home = repos
    head = git(run, "rev-parse", "HEAD")
    answers = iter(["", "", head[:7]])                                        # 本体の応答が 2 回遅れる
    monkeypatch.setattr(updater.Updater, "running", lambda self: next(answers))
    u = updater.Updater(run)
    assert u.verify(head, int(time.time() * 1000), timeout=10) == "ok"
    assert records(home)[-1]["running"] == head[:7]


def test_user_quit_right_after_relaunch_is_not_a_failure(repos, monkeypatch):
    dev, run, home = repos
    prev = git(run, "rev-parse", "HEAD")
    new = advance(dev, {"mod.py": "X = 2\n"})                                  # 良い版(起動が遅いだけ)
    git(run, "fetch", "-q", "origin", "stable")
    launched, notes = [], []

    def relaunch(self, mode):
        since = int(time.time() * 1000)
        launched.append(git(run, "rev-parse", "HEAD"))
        # 起動の途中で利用者が ⌘Q: 本体は SIGINT で止まり(status 2・quitting)、アプリが終わる
        _host_log(home, phase="host_child_exit", status=2, quitting=True, t_ms=since)
        _host_log(home, phase="host_quit", t_ms=since)
        return since

    monkeypatch.setattr(updater.Updater, "relaunch", relaunch)
    monkeypatch.setattr(updater.Updater, "running", lambda self: "")
    monkeypatch.setattr(updater.Updater, "app_running", lambda self: False)
    monkeypatch.setattr(updater.Updater, "notify", lambda self, text: notes.append(text))
    t = time.monotonic()
    assert updater.Updater(run).apply(None, False, "bg") == 0
    assert time.monotonic() - t < 15
    assert git(run, "rev-parse", "HEAD") == new and new != prev              # 巻き戻さない
    assert launched == [new] and notes == []                                  # 終了したアプリを開き直さない・通知しない
    assert not (home / "updater" / "bad").exists()                            # 悪い版にしない
    assert updater.Updater(run).pending() | {"t_ms": 0} == {"sha": new, "good": prev, "t_ms": 0}   # 次の起動で確かめる
    v = [r for r in records(home) if r["phase"] == "verify"][-1]
    a = [r for r in records(home) if r["phase"] == "apply"][-1]
    assert (v["ok"], v["quit"], "exited" in v) == (False, True, False)
    assert (a["ok"], a["verified"]) == (True, False)


def _fake_launch(monkeypatch, run, home, crash: set, state: dict):
    """開き直しの代わり: 版が crash に入っていれば起動直後に落ちた記録、そうでなければ running を返す。"""
    launched = []

    def relaunch(self, mode):
        since = int(time.time() * 1000)
        head = git(run, "rev-parse", "HEAD")
        launched.append((mode, head))
        if head in crash:
            _host_log(home, phase="host_child_exit", status=1, quitting=False, t_ms=since)
        else:
            state["running"] = head[:7]
        return since

    monkeypatch.setattr(updater.Updater, "relaunch", relaunch)
    monkeypatch.setattr(updater.Updater, "running", lambda self: state["running"])
    monkeypatch.setattr(updater.Updater, "app_running", lambda self: False)
    return launched


def test_pending_version_is_verified_at_next_launch(repos, monkeypatch):
    dev, run, home = repos
    new = advance(dev, {"mod.py": "X = 2\n"})
    assert updater.Updater(run).check() == 0                                  # アプリが止まっている → 取り込んで印
    assert updater.Updater(run).pending()["sha"] == new
    state = {"running": ""}
    _fake_launch(monkeypatch, run, home, set(), state)
    _host_log(home, phase="host_spawn", pid=4242, t_ms=int(time.time() * 1000))   # 利用者がアプリを開いた
    state["running"] = new[:7]
    assert updater.Updater(run).verify_pending(4242) == 0
    assert updater.Updater(run).pending() is None
    assert records(home)[-1] | {"t_ms": 0, "rid": ""} == {"t_ms": 0, "rid": "", "phase": "pending_clear", "ok": True,
                                                           "why": "verified"}


def test_pending_version_that_crashes_at_next_launch_goes_back_to_last_good(repos, monkeypatch):
    dev, run, home = repos
    good = git(run, "rev-parse", "HEAD")
    advance(dev, {"mod.py": "X = 2\n"})
    assert updater.Updater(run).check() == 0                                  # 1 つ目: 起動で確かめないまま
    bad = advance(dev, {"mod.py": "X = 3\n"})
    assert updater.Updater(run).check() == 0                                  # 2 つ目も: 戻す先は確かめ済みの最後の版のまま
    assert updater.Updater(run).pending() | {"t_ms": 0} == {"sha": bad, "good": good, "t_ms": 0}
    notes = []
    monkeypatch.setattr(updater.Updater, "notify", lambda self, text: notes.append(text))
    state = {"running": ""}
    launched = _fake_launch(monkeypatch, run, home, {bad}, state)
    now = int(time.time() * 1000)
    _host_log(home, phase="host_spawn", pid=4242, t_ms=now)
    _host_log(home, phase="host_child_exit", status=1, quitting=False, t_ms=now + 1)   # 開いた直後に落ちた
    t = time.monotonic()
    assert updater.Updater(run).verify_pending(4242) == 1
    assert time.monotonic() - t < 15
    assert git(run, "rev-parse", "HEAD") == good and launched == [("bg", good)]   # 裏で開き直す(フォーカスを奪わない)
    assert (home / "updater" / "bad").read_text().split() == [bad]
    assert updater.Updater(run).pending() is None and notes and good[:7] in notes[0]


def test_pending_is_kept_when_the_user_quits_at_next_launch(repos, monkeypatch):
    dev, run, home = repos
    new = advance(dev, {"mod.py": "X = 2\n"})
    assert updater.Updater(run).check() == 0
    _fake_launch(monkeypatch, run, home, set(), {"running": ""})
    now = int(time.time() * 1000)
    _host_log(home, phase="host_spawn", pid=4242, t_ms=now)
    _host_log(home, phase="host_child_exit", status=2, quitting=True, t_ms=now + 1)
    _host_log(home, phase="host_quit", t_ms=now + 2)
    assert updater.Updater(run).verify_pending(4242) == 0
    assert updater.Updater(run).pending()["sha"] == new and git(run, "rev-parse", "HEAD") == new


def test_stale_pending_is_dropped(repos):
    dev, run, home = repos
    u = updater.Updater(run)
    u.set_pending("0" * 40, git(run, "rev-parse", "HEAD"))                    # 今の版と違う版の印
    assert u.verify_pending(4242) == 0 and u.pending() is None
    assert records(home)[-1]["why"] == "stale"


class _Hook(BaseHTTPRequestHandler):
    got: list = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _Hook.got.append({"path": self.path, "ua": self.headers.get("User-Agent"), "body": json.loads(self.rfile.read(n))})
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_discord_webhook_from_env_file_without_mentions(repos, tmp_path, monkeypatch):
    _, run, home = repos
    srv = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Hook.got = []
    u = updater.Updater(run)
    assert u.discord("x") == "skipped"                                        # 鍵が無ければ送らない
    env = tmp_path / "secret.env"
    env.write_text(f"OTHER=1\nexport T_MEETCUE_WEBHOOK='http://127.0.0.1:{srv.server_port}/hook'\n", encoding="utf-8")
    u.notify_conf["discord_env_file"] = str(env)
    try:
        assert u.discord("巻き戻しました @everyone") == "sent"
    finally:
        srv.shutdown()
    g = _Hook.got[0]
    assert g["path"] == "/hook" and g["ua"].startswith("t-updater/")         # 既定の Python-urllib では送らない
    assert g["body"]["content"] == "巻き戻しました @everyone" and g["body"]["allowed_mentions"] == {"parse": []}
    monkeypatch.setenv("T_MEETCUE_WEBHOOK", "http://127.0.0.1:9/none")       # 環境変数が先・届かなくても例外にしない
    assert u.discord("x").startswith("error:")


def test_unchanged_checks_are_logged_once_an_hour(repos, monkeypatch):
    dev, run, home = repos
    for _ in range(3):
        assert updater.Updater(run).check() == 0
    assert [r["phase"] for r in records(home)] == ["check"]                   # 1 分おきでも変化なしは 1 行
    monkeypatch.setattr(updater, "QUIET_S", 0)
    assert updater.Updater(run).check() == 0 and len(records(home)) == 2      # 1 時間たてば記録する
    monkeypatch.setattr(updater, "QUIET_S", 3600)
    new = advance(dev, {"mod.py": "X = 2\n"})
    assert updater.Updater(run).check() == 0                                  # 変化はすぐ記録する
    assert any(r["phase"] == "check" and r.get("changed") for r in records(home)) and git(run, "rev-parse", "HEAD") == new


def test_same_refusal_is_notified_once(repos, monkeypatch):
    dev, run, home = repos
    notes = []
    monkeypatch.setattr(updater.Updater, "notify", lambda self, text: notes.append(text))
    advance(dev, {"mod.py": "X = 2\n"})
    (run / "mod.py").write_text("X = 99\n", encoding="utf-8")                # 実行用ツリーを手で変えた
    for _ in range(3):
        assert updater.Updater(run).check() == 0
    assert len(notes) == 1 and "手で入れた変更" in notes[0]
    advance(dev, {"mod.py": "X = 3\n"})                                       # 別の版なら改めて知らせる
    assert updater.Updater(run).check() == 0 and len(notes) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="ロックは Mac の更新係だけ(Windows は段 3)")
def test_check_skips_while_another_update_holds_the_lock(repos):
    dev, run, home = repos
    advance(dev, {"mod.py": "X = 2\n"})
    head = git(run, "rev-parse", "HEAD")
    with updater.Updater(run).lock(0) as got:
        assert got
        assert updater.Updater(run).check() == 0
    assert git(run, "rev-parse", "HEAD") == head and records(home)[-1]["skipped"] == "locked"


class _Version(BaseHTTPRequestHandler):
    body: dict = {}

    def do_GET(self):
        b = json.dumps(_Version.body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


@pytest.mark.skipif(not hasattr(__import__("os"), "getuid"), reason="uid は Mac / Linux だけ")
def test_version_from_another_users_app_is_ignored_and_logged_once(repos):
    """同じ Mac の別の利用者の本体が答えたら、その版は使わず(確かめに使わない)記録に残す(2026-09-28)。"""
    import os
    _, run, home = repos
    srv = HTTPServer(("127.0.0.1", 0), _Version)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        u = updater.Updater(run)
        u.app["version_url"] = f"http://127.0.0.1:{srv.server_port}/api/version"
        _Version.body = {"running": "abc1234", "uid": os.getuid()}
        assert u.running() == "abc1234"
        assert not [r for r in records(home) if r["phase"] == "version_foreign"]   # 自分の本体なら何も残さない
        _Version.body = {"running": "def5678", "uid": os.getuid() + 1}
        assert u.running() == "" and u.running() == ""                          # 別の利用者の本体の版は使わない
        foreign = [r for r in records(home) if r["phase"] == "version_foreign"]
        assert len(foreign) == 1                                                  # 1 回の実行で 1 行だけ
        assert (foreign[0]["server_uid"], foreign[0]["own_uid"], foreign[0]["running"]) == (os.getuid() + 1, os.getuid(), "def5678")
    finally:
        srv.shutdown()


def test_app_version_reports_its_owner(repos, monkeypatch):
    import os
    _, run, home = repos
    monkeypatch.setattr(app_mod, "REPO", run)
    a = App(Config(app_dir=home), sources=[], window=False)
    assert a._version("")["uid"] == getattr(os, "getuid", lambda: None)()


def test_port_is_per_user_and_matches_the_app(repos, monkeypatch):
    """{port} は利用者ごと(8765 + (uid − 501) を 100 で回す)・MEETCUE_PORT で上書き。アプリ側の default_port と同じ値。"""
    import os
    from meetcue import config as config_mod
    _, run, home = repos
    toml = (run / "update.toml").read_text(encoding="utf-8").replace(
        'version_url = "http://127.0.0.1:9/api/version"',
        'version_url = "http://127.0.0.1:{port}/api/version"\nport_base = 8765\nport_env = "MEETCUE_PORT"')
    (run / "update.toml").write_text(toml, encoding="utf-8")
    monkeypatch.delenv("MEETCUE_PORT", raising=False)
    for uid, want in ((501, 8765), (502, 8766), (500, 8864), (601, 8765)):   # 100 ごとに一回りする
        monkeypatch.setattr(os, "getuid", lambda uid=uid: uid, raising=False)
        u = updater.Updater(run)
        assert u.port() == want == config_mod.default_port()
        assert u.app["version_url"] == f"http://127.0.0.1:{want}/api/version"
        assert u.app["nudge_url"] == "http://127.0.0.1:9/api/action"            # {port} の無い URL はそのまま
    monkeypatch.setenv("MEETCUE_PORT", "9123")
    assert updater.Updater(run).port() == 9123 == config_mod.default_port()
    monkeypatch.setenv("MEETCUE_PORT", "80")                                      # 範囲の外は使わない
    assert updater.Updater(run).port() == 8765 + (os.getuid() - 501) % 100 == config_mod.default_port()
    monkeypatch.delenv("MEETCUE_PORT")
    monkeypatch.delattr(os, "getuid", raising=False)                              # uid の無い OS(Windows)
    assert updater.Updater(run).port() == 8765 == config_mod.default_port()


def test_real_update_toml_uses_the_per_user_port():
    import tomllib
    conf = tomllib.loads((ROOT / "update.toml").read_text(encoding="utf-8"))["app"]
    assert "{port}" in conf["version_url"] and "{port}" in conf["nudge_url"]
    assert conf["port_base"] == 8765 and conf["port_env"] == "MEETCUE_PORT"
