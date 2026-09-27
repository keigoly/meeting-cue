"""設定 — `~/.meeting-cue/config.toml`(無ければ既定)+ CLI の上書き。雛形は repo の config.example.toml。

個人パス・鍵はここに置かない(公開前提)。鍵は ~/.secrets/meeting-cue.env(meetcue.secrets)。
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .segmenter import SegmenterConfig

HOME = Path.home()
APP_DIR = Path(os.environ.get("MEETCUE_HOME") or (HOME / ".meeting-cue"))
REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class LLMConfig:
    model: str = "anthropic/claude-sonnet-5"
    fast_model: str = "anthropic/claude-haiku-4.5"
    provider_order: list[str] = field(default_factory=lambda: ["Anthropic"])
    max_tokens: int = 1200
    timeout_s: int = 60
    monthly_budget_usd: float = 10.0
    # 開発者向け(既定オフ・自己責任・2026-09-26): 本人の Mac でログイン済みの Claude Code(claude -p)をサブスクで呼ぶ。
    # true にすると設定画面の接続先に「Claude サブスク(Claude Code 経由)」が出る。モデルは [anthropic] と同じ ID。
    subscription_cli: bool = False
    claude_cli_path: str = ""       # 空 = PATH と ~/.local/bin/claude から探す
    claude_cli_warm: int = 2        # 録音中に温めておくプロセスの数(回答と逆質問が並行するので 2)


@dataclass
class AnthropicConfig:
    """Anthropic 直接接続(設定画面 第 2 段・2026-09-26)。モデル ID と 1M トークンあたりの価格 [入力, 出力](月予算の台帳用。
    2026-06 時点の公開価格。Anthropic の応答には料金が入らないので、トークン数からここで計算する)。"""
    model: str = "claude-sonnet-5"        # 回答・質問タブ(思考を切って初トークンを速く)
    fast_model: str = "claude-haiku-4-5"  # 会議後のサマリ
    deep_model: str = "claude-opus-5-5"   # 「深く考える」(思考は切れない・effort で量を決める)
    prices: dict = field(default_factory=lambda: {"claude-sonnet-5": [2.0, 10.0], "claude-haiku-4-5": [1.0, 5.0],
                                                  "claude-opus-5-5": [4.0, 20.0]})


@dataclass
class JevConfig:
    model: str = "typesafe/jev-1.13"
    timeout_s: int = 6
    act_min: float = 0.6
    to_me_min: float = 0.5   # 2026-09-25 spike: 自分宛の質問は 0.55〜0.64、無関係は ≤ 0.11(較正は Phase 2)


@dataclass
class SelectorConfig:
    """選ぶ係(Jev セレクター)。候補を多めに生成し、Jev の基準別採点で上位 display 件に ★ を付ける。"""
    enabled: bool = True
    n_answers: int = 5
    n_counters: int = 5
    display: int = 3
    timeout_s: int = 6
    weights: dict = field(default_factory=dict)   # {"answer": {...}, "counter": {...}} で既定を上書き


@dataclass
class PlannerConfig:
    """質問タブ(FR-6c・U3/U4)。こちらから聞くべき質問を先回りで出す。相手からの質問(Jev の起動)が常に優先。"""
    enabled: bool = True
    auto: bool = True               # 話が進んだら自動で(Sonnet)。画面の「自動」で切り替えられる
    interval_s: float = 60.0        # 自動は最大 1 分に 1 回(Q11)
    min_new_chars: int = 120        # 前回から会話がこれだけ進んだら
    cooldown_s: float = 10.0        # 回答の生成が終わってからこの秒数は自動で考えない(U4)
    model: str = ""                 # 空 = llm.model(Sonnet 5)
    deep_model: str = "anthropic/claude-opus-5.5"   # 「深く考える」(2026-09-26 OpenRouter: 入力 $4 / 出力 $20 per M)
    deep_reasoning: str = "medium"  # Opus 5.5 は思考を切れない(400)。思考の量(low / medium / high)
    deep_max_tokens: int = 4000     # 思考の分も含む上限(小さいと本文が空になる)
    n: int = 5                      # 候補の本数(Jev の選ぶ係で上位 display 件に ★)
    display: int = 3
    context_chars: int = 3000       # 自動・手動に渡す直近の会話
    deep_context_chars: int = 9000  # 深く考えるに渡す会話
    max_tokens: int = 700


@dataclass
class IndexConfig:
    include: list[str] = field(default_factory=lambda: ["01_Projects", "02_Ideas", "03_Resources", "04_Context"])
    exclude: list[str] = field(default_factory=lambda: [
        "04_Context/Session_Log", "04_Context/Discord_Log", "04_Context/Compact_Snapshots",
        "04_Context/Memo_Private", "00_Inbox/_processed", "_Archive",
        "03_Resources/YouTube"])  # 1,877 本・索引 1.2 GB・コメント単位の雑音 → 既定除外(Q5)
    chunk_chars: int = 800
    top_k: int = 5


@dataclass
class Config:
    vault_root: Path = HOME / "ObsidianVault" / "vault"
    app_dir: Path = APP_DIR
    mode: str = "participant"
    privacy: str = "private"        # private | local
    locale: str = "ja-JP"
    profile: str = ""
    helpers_dir: Path = REPO_ROOT / "helpers" / "macos"
    file_pace: float = 1.0
    record_audio: bool = True       # 2026-09-26 Q10: 音声は常に保存(audio/<channel>.m4a・録音後の再生用)
    llm: LLMConfig = field(default_factory=LLMConfig)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    jev: JevConfig = field(default_factory=JevConfig)
    selector: SelectorConfig = field(default_factory=SelectorConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    index: IndexConfig = field(default_factory=IndexConfig)
    segmenter: SegmenterConfig = field(default_factory=SegmenterConfig)
    # file / tap 音源用(partial が約 1 秒周期のバースト → 1 秒超で待つ)
    segmenter_tap: SegmenterConfig = field(default_factory=lambda: SegmenterConfig(
        pause_ms=1200, pause_incomplete_ms=1600, pause_sentence_ms=1100, safety_ms=3000))
    # 自分の声(channel=mic)用。短く切ると文の途中で切れて文脈を失い、誤りが増える(2026-09-27 読み上げ 91 s を 6 回ずつ再現:
    # マイク用の 500/900/300 ms で CER 平均 28.6%・固有名詞以外 14.9% → 相手側と同じ値で 26.4%・13.2%、揺れも小さい)。
    # 自分の発言は判定に使わないので、確定が 1 秒ほど遅れても困らない。
    # 対面の相手(room)はマイク音源でも判定を急ぐので [segmenter] のまま
    segmenter_self: SegmenterConfig = field(default_factory=lambda: SegmenterConfig(
        pause_ms=1200, pause_incomplete_ms=1600, pause_sentence_ms=1100, safety_ms=3000))

    @property
    def index_db(self) -> Path:
        return self.app_dir / "index" / "vault.sqlite"

    @property
    def sessions_root(self) -> Path:
        return self.app_dir / "sessions"

    @property
    def ledger_path(self) -> Path:
        return self.app_dir / "ledger.json"


def _apply(obj, data: dict) -> None:
    for k, v in (data or {}).items():
        if not hasattr(obj, k):
            continue
        cur = getattr(obj, k)
        if isinstance(cur, Path):
            setattr(obj, k, Path(str(v)).expanduser())
        elif isinstance(cur, tuple):
            setattr(obj, k, tuple(v))
        else:
            setattr(obj, k, v)


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    p = path or (APP_DIR / "config.toml")
    if p.exists():
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        _apply(cfg, {k: v for k, v in data.items() if k not in ("llm", "jev", "selector", "planner", "index", "segmenter", "segmenter_tap",
                                                          "segmenter_self")})
        _apply(cfg.llm, data.get("llm", {}))
        _apply(cfg.jev, data.get("jev", {}))
        _apply(cfg.selector, data.get("selector", {}))
        _apply(cfg.planner, data.get("planner", {}))
        _apply(cfg.index, data.get("index", {}))
        _apply(cfg.segmenter, data.get("segmenter", {}))
        _apply(cfg.segmenter_tap, data.get("segmenter_tap", {}))
        _apply(cfg.segmenter_self, data.get("segmenter_self", {}))
    return cfg
