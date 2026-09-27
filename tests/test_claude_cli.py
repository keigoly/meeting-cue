"""開発者向けの Claude サブスク(Claude Code 経由・2026-09-26): 偽の claude で、温めたプロセスの使い回し・補充・失敗・打ち切り。

本物の Claude Code は呼ばない(利用枠を使わない)。偽の claude は stream-json の入力を 1 行待ち、決まった応答を返す。
"""
import json
import stat
import sys
import time

import pytest

from meetcue import secrets
from meetcue.app import App
from meetcue.config import Config
from meetcue.cues import claude_cli, llm

WIN_SKIP = "偽の実行ファイルが #! のスクリプト(Mac・Linux の書き方)なので Windows では動かない。Windows 版の開発で .cmd の偽物に直す(Vault Windows_Brief.md)"
FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
log = os.environ["FAKE_CLAUDE_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "t": time.time()}) + "\n")
line = sys.stdin.readline()
if not line:
    sys.exit(0)
msg = json.loads(line)["message"]["content"]
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
model = sys.argv[sys.argv.index("--model") + 1]
def out(o):
    print(json.dumps(o, ensure_ascii=False), flush=True)
out({"type": "system", "subtype": "init", "model": model})
out({"type": "stream_event", "event": {"type": "message_start", "message": {"model": model}}})
if mode == "die":
    sys.exit(3)
for t in ["意図: 費用\n", "回答1: 約 100 万円", "です"]:
    out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": t}}})
    if mode == "slow":
        time.sleep(0.5)
if mode == "error":
    out({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "rate limited"})
else:
    out({"type": "result", "subtype": "success", "is_error": False, "result": "ok", "echo": msg[:20],
         "usage": {"input_tokens": 50, "output_tokens": 12}})
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip(WIN_SKIP)
    cli = tmp_path / "claude"
    cli.write_text(FAKE, encoding="utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "spawn.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")

    def spawns():
        return [json.loads(x)["args"] for x in log.read_text().splitlines()] if log.exists() else []
    return str(cli), tmp_path, spawns


def _wait(cond, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_warm_process_is_reused_and_refilled(fake):
    cli, tmp, spawns = fake
    pool = claude_cli.Pool(cli, tmp / "cwd", warm={"claude-sonnet-5": 1})
    try:
        pool.ensure()
        assert _wait(lambda: len(spawns()) == 1)
        got = []
        msgs = [{"role": "system", "content": "指示です"}, {"role": "user", "content": "費用は?"}]
        r = claude_cli.stream_chat(msgs, pool=pool, model="claude-sonnet-5", on_delta=got.append)
        assert r.ok and r.text == "意図: 費用\n回答1: 約 100 万円です" and got[0] == "意図: 費用\n"
        assert r.extra["billing"] == "subscription" and r.extra["warm"] is True and r.extra["age_ms"] >= 0 and r.cost_usd is None
        assert (r.input_tokens, r.output_tokens, r.provider) == (50, 12, "Claude Code")
        assert _wait(lambda: len(spawns()) == 2)                        # 使った分をすぐ補充
        args = spawns()[0]
        for flag in ("-p", "--safe-mode", "--no-session-persistence", "--input-format", "--include-partial-messages"):
            assert flag in args                                         # 個人の文脈を読まない・会話を残さない
        assert args[args.index("--tools") + 1] == "" and args[args.index("--model") + 1] == "claude-sonnet-5"
        assert (tmp / "cwd").is_dir()
    finally:
        pool.close()


def test_cold_start_when_not_warmed_and_effort(fake):
    cli, tmp, spawns = fake
    pool = claude_cli.Pool(cli, tmp / "cwd")
    try:
        r = claude_cli.stream_chat([{"role": "user", "content": "x"}], pool=pool, model="claude-opus-5-5", effort="medium")
        assert r.ok and r.extra["warm"] is False
        args = spawns()[-1]
        assert args[args.index("--effort") + 1] == "medium"
    finally:
        pool.close()


@pytest.mark.parametrize("mode,error", [("error", "cli_error"), ("die", "bad_response")])
def test_errors(fake, monkeypatch, mode, error):
    cli, tmp, _ = fake
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    pool = claude_cli.Pool(cli, tmp / "cwd")
    try:
        r = claude_cli.stream_chat([{"role": "user", "content": "x"}], pool=pool, model="m")
        assert not r.ok and r.error == error
    finally:
        pool.close()


def test_cancel_kills_process(fake, monkeypatch):
    cli, tmp, _ = fake
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    pool = claude_cli.Pool(cli, tmp / "cwd")
    seen = []
    try:
        r = claude_cli.stream_chat([{"role": "user", "content": "x"}], pool=pool, model="m",
                                   on_delta=seen.append, should_stop=lambda: len(seen) >= 1)
        assert r.error == "cancelled" and r.ms_total < 2000
    finally:
        pool.close()
    assert claude_cli.stream_chat([], pool=None, model="m").error == "no_cli"


def test_set_warm_and_close(fake):
    cli, tmp, spawns = fake
    pool = claude_cli.Pool(cli, tmp / "cwd")
    pool.set_warm({"m": 2})
    assert _wait(lambda: len(spawns()) == 2)
    pool.set_warm({})                                                    # 録音が終わったら待たせているものを片付ける
    assert pool._idle == {}
    pool.close()


def test_prompt_and_find_cli(fake, tmp_path):
    cli, *_ = fake
    assert claude_cli._prompt([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]) == "# 指示\nS\n\n# 入力\nU"
    assert claude_cli._prompt([{"role": "user", "content": "U"}]) == "U"
    assert claude_cli.find_cli(cli) == cli
    assert claude_cli.find_cli(str(tmp_path / "無い")) in (None, claude_cli.find_cli(""))   # 指定が無ければ PATH などを探す


def test_llm_target_claude_cli(fake):
    cli, tmp, spawns = fake
    pool = claude_cli.Pool(cli, tmp / "cwd")
    try:
        t = llm.target_for(Config(), "claude-cli", claude_cli.MARKER, {"_pool": pool})
        assert (t.provider, t.model, t.fast_model, t.deep_model) == ("claude-cli", "claude-sonnet-5", "claude-haiku-4-5",
                                                                     "claude-opus-5-5")
        r = llm.stream(t, [{"role": "user", "content": "x"}], role="fast")
        assert r.ok and r.cost_usd is None                                  # プランの利用枠(API の台帳に数えない)
        assert spawns()[-1][spawns()[-1].index("--model") + 1] == "claude-haiku-4-5"
    finally:
        pool.close()


def test_app_offers_claude_cli_only_when_enabled(fake, tmp_path, monkeypatch):
    cli, *_ = fake
    monkeypatch.setattr(secrets, "key_status", lambda: {p: {"set": False, "source": None} for p in secrets.PROVIDERS})
    off = App(Config(app_dir=tmp_path / "a"), sources=[], window=False)
    off._drive_accounts = lambda: []
    v = off.settings_view()
    assert "claude-cli" not in v["providers_ready"] and v["subscription_cli"] == {"enabled": False, "found": False}
    assert off._api("POST", "/api/settings", {"provider": "claude-cli"})[0] == 400   # 公開の既定では選べない
    cfg = Config(app_dir=tmp_path / "b")
    cfg.llm.subscription_cli, cfg.llm.claude_cli_path = True, cli
    on = App(cfg, sources=[], window=False)
    on._drive_accounts = lambda: []
    v = on.settings_view()
    assert "claude-cli" in v["providers_ready"] and v["subscription_cli"] == {"enabled": True, "found": True}
    assert on._api("POST", "/api/settings", {"provider": "claude-cli"})[1]["provider"] == "claude-cli"
