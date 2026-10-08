"""設定 — `~/.meeting-cue/config.toml`(無ければ既定)+ CLI の上書き。雛形は repo の config.example.toml。

個人パス・鍵はここに置かない(公開前提)。鍵は ~/.secrets/meeting-cue.env(meetcue.secrets)。
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .segmenter import SegmenterConfig

HOME = Path.home()
APP_DIR = Path(os.environ.get("MEETCUE_HOME") or (HOME / ".meeting-cue"))
REPO_ROOT = Path(__file__).resolve().parents[1]
PORT_BASE = 8765


def default_port() -> int:
    """画面(Web UI)のポート。同じ Mac の利用者ごとに分ける: 8765 + (uid − 501) を 100 で回す(最初の利用者 501 は 8765 のまま)。
    MEETCUE_PORT で上書きできる。uid の無い Windows は 8765。同じ決まりが packaging/launch.sh・overlay_helper(Swift)・
    更新係(update.toml の {port})にもある(2026-09-28: 固定の 8765 だと、もう 1 人の利用者の本体につながり記録が混ざった)。"""
    env = os.environ.get("MEETCUE_PORT", "")
    if env.isdigit() and 1024 <= int(env) <= 65535:
        return int(env)
    uid = getattr(os, "getuid", lambda: None)()
    return PORT_BASE if uid is None else PORT_BASE + (uid - 501) % 100


@dataclass
class LLMConfig:
    # 2026-10-08 keigoly様: 既定を Haiku 5.5 に(回答・質問タブ・用語・サマリ)。Sonnet / Opus は押したときの深掘り([planner] の mid / deep)。
    # 実測(OpenRouter・同じ回答プロンプト): Sonnet 5 初トークン 1.3〜2.5 s・総 8〜9 s・$0.0099 → Haiku 5.5 1.0 s・総 3.8 s・$0.0005
    model: str = "anthropic/claude-haiku-5.5"
    fast_model: str = "anthropic/claude-haiku-5.5"
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
    2026-06 時点の公開価格。Anthropic の応答には料金が入らないので、トークン数からここで計算する)。
    2026-10-08: 既定を Haiku 5.5 に。Haiku 5.5 は adaptive 思考で、Claude Code からは思考を切れない(実測 初トークン 6〜11 s)。
    回答・サマリの役は effort を fast_effort に下げ、依頼文に「すぐ書き始める」一文を足す(実測 0.53〜0.63 s・cues/llm.py)。
    Haiku 5.5 の価格はプロンプト 10 万トークンまでの値(会議の依頼はこの範囲)。"""
    model: str = "claude-haiku-5-5"       # 回答・質問タブ・用語(effort を下げて初トークンを速く)
    fast_model: str = "claude-haiku-5-5"  # 会議後のサマリ・字幕の翻訳は main と同じ
    mid_model: str = "claude-sonnet-5-5"  # 「少し深く」(思考は {type: disabled} を受け付けない・effort で量を決める)
    deep_model: str = "claude-opus-5-5"   # 「深く考える」(思考は切れない・effort で量を決める)
    fast_effort: str = "low"              # main / fast の役の effort(adaptive 思考のモデルだけに付ける)
    prices: dict = field(default_factory=lambda: {"claude-haiku-5-5": [0.10, 0.50], "claude-sonnet-5-5": [2.0, 10.0],
                                                  "claude-opus-5-5": [4.0, 20.0], "claude-sonnet-5": [2.0, 10.0],
                                                  "claude-haiku-4-5": [1.0, 5.0]})


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
    auto: bool = True               # 話が進んだら自動で(Haiku 5.5)。画面の「自動」で切り替えられる
    interval_s: float = 60.0        # 自動は最大 1 分に 1 回(Q11)
    min_new_chars: int = 120        # 前回から会話がこれだけ進んだら
    cooldown_s: float = 10.0        # 回答の生成が終わってからこの秒数は自動で考えない(U4)
    model: str = ""                 # 空 = llm.model(Haiku 5.5)
    # 「少し深く」(2026-10-08 keigoly様: Haiku → Sonnet → Opus の 3 段。回答タブの深掘り FR-6d も同じモデル)
    mid_model: str = "anthropic/claude-sonnet-5.5"
    mid_reasoning: str = "low"      # Sonnet 5.5 は思考を {type: disabled} で切れない。思考の量(low / medium / high)
    mid_max_tokens: int = 3000      # 思考の分も含む上限
    mid_context_chars: int = 6000   # 少し深くに渡す会話
    deep_model: str = "anthropic/claude-opus-5.5"   # 「深く考える」(2026-09-26 OpenRouter: 入力 $4 / 出力 $20 per M)
    deep_reasoning: str = "medium"  # Opus 5.5 は思考を切れない(400)。思考の量(low / medium / high)
    deep_max_tokens: int = 4000     # 思考の分も含む上限(小さいと本文が空になる)
    n: int = 5                      # 候補の本数(Jev の選ぶ係で上位 display 件に ★)
    display: int = 3
    context_chars: int = 3000       # 自動・手動に渡す直近の会話
    deep_context_chars: int = 9000  # 深く考えるに渡す会話
    max_tokens: int = 700


@dataclass
class DigConfig:
    """深掘りの段(FR-6d・2026-10-08 keigoly様)。回答タブの質問を「少し深く」(mid = Sonnet 5.5)/「深く」(deep = Opus 5.5)で
    掘り下げる。モデルは接続先の mid / deep(cues/llm.py の Target)。Vault から多く・長めに引き、会話も長めに渡す。"""
    top_k_mid: int = 8
    top_k_deep: int = 12
    body_chars: int = 1200          # ナレッジ 1 件あたりの本文(回答の 600 字より長く)
    mid_context_chars: int = 4000
    deep_context_chars: int = 9000
    mid_reasoning: str = "low"      # Sonnet 5.5 / Opus 5.5 は思考を切れない。思考の量(low / medium / high)
    deep_reasoning: str = "medium"
    mid_max_tokens: int = 4000      # 思考の分も含む上限(小さいと本文が空になる)
    deep_max_tokens: int = 8000
    n_answers: int = 3
    n_evidence: int = 5
    timeout_s: int = 120            # Opus は長く書く(押してから待つ前提)
    # Obsidian の MCP 検索(FR-5b)を待つ上限と、引く語の数。Obsidian の簡易検索は 1 語 2.4〜3.5 s(2026-10-08 実測・
    # 4,816 ノート)で、2 s では毎回時間切れだった。押してから待つ深掘りなので 1 語・4.5 s まで待つ。過ぎたら索引だけで続ける
    mcp_timeout_s: float = 4.5
    mcp_terms: int = 1


@dataclass
class GlossaryConfig:
    """用語タブ(FR-6e・2026-10-08 keigoly様)。会話(相手と自分)に出た専門用語・業界用語に、短い説明と自分の Obsidian の
    ノートを付けて次々に出す(main の役 = Haiku 5.5)。相手からの質問への回答が先(回答の生成中は始めない)。"""
    enabled: bool = True
    auto: bool = True               # 画面の「自動で拾う」で切り替えられる
    interval_s: float = 8.0         # 自動は最短この間隔
    min_chars: int = 40             # 前回から会話がこれだけ進んだら
    max_terms: int = 4              # 1 回に書かせる語の上限
    context_chars: int = 1200       # 1 回に渡す新しい発言
    candidates: int = 8             # 先に索引を引く候補語(発言から取り出した語)
    # 候補語のノートの抜粋の長さ。2026-10-08 実測(Claude サブスク・Haiku 5.5): 300 字だと 3 回中 2 回で思考が入り初トークン
    # 4.5〜4.9 s → 120 字で 3 回とも 0.51〜0.59 s(抜粋なしは 0.51〜0.83 s)。ノートそのものは語ごとの札で別に付ける
    snippet_chars: int = 120
    notes_per_term: int = 2         # 語ごとの「あなたのノート」
    max_tokens: int = 700
    level: str = "高校以上〜大学で習う語・業界の専門用語・技術用語・略語"
    # 索引に無い語を Obsidian の MCP で探す(1 語 2.4〜3.5 s・2026-10-08 実測)。1 回の用語で 1 語まで・4.5 s まで待つ
    mcp_per_run: int = 1
    mcp_timeout_s: float = 4.5


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
    helpers_dir: Path = REPO_ROOT / "helpers" / ("windows" if sys.platform == "win32" else "macos")
    # Windows の STT ヘルパーを動かす python(専用の仮想環境・packaging/windows/setup.ps1 が作る。REQUIREMENTS FR-12)
    stt_python: Path = APP_DIR / "stt-venv" / "Scripts" / "python.exe"
    # Windows のウィンドウ(pywebview)と本体を動かす python(アプリ用の仮想環境・setup.ps1 が作る。本体は import しない)
    window_python: Path = APP_DIR / "app-venv" / "Scripts" / "python.exe"
    # Windows の取り込み機器(名前の一部・空 = 既定の機器)。loopback は相手の声を聞く出力機器(ヘッドセットなど)
    loopback_device: str = ""
    mic_device: str = ""
    file_pace: float = 1.0
    record_audio: bool = True       # 2026-09-26 Q10: 音声は常に保存(audio/<channel>.m4a・録音後の再生用)
    llm: LLMConfig = field(default_factory=LLMConfig)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    jev: JevConfig = field(default_factory=JevConfig)
    selector: SelectorConfig = field(default_factory=SelectorConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    dig: DigConfig = field(default_factory=DigConfig)
    glossary: GlossaryConfig = field(default_factory=GlossaryConfig)
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
        _apply(cfg, {k: v for k, v in data.items() if k not in ("llm", "anthropic", "jev", "selector", "planner", "dig", "glossary", "index",
                                                          "segmenter", "segmenter_tap", "segmenter_self")})
        _apply(cfg.llm, data.get("llm", {}))
        _apply(cfg.anthropic, data.get("anthropic", {}))
        _apply(cfg.jev, data.get("jev", {}))
        _apply(cfg.selector, data.get("selector", {}))
        _apply(cfg.planner, data.get("planner", {}))
        _apply(cfg.dig, data.get("dig", {}))
        _apply(cfg.glossary, data.get("glossary", {}))
        _apply(cfg.index, data.get("index", {}))
        _apply(cfg.segmenter, data.get("segmenter", {}))
        _apply(cfg.segmenter_tap, data.get("segmenter_tap", {}))
        _apply(cfg.segmenter_self, data.get("segmenter_self", {}))
    return cfg
