"""音声入力の設定(REQUIREMENTS FR-14b・2026-10-19 keigoly様「キーバインドの設定を追加」「それ以外の機能も全て取り込む」)。

置き場は ~/.meeting-cue/dictation/settings.json(アプリの「音声入力」の画面で変える・`meetcue dictate` も同じものを読む)。
無い項目は既定値で埋める。既定は 10-18 までの動き(⌃⌥V・最後にまとめて貼る・AI なし・画面の文脈なし)のまま。
細かい数値(長押しの ms・2 回押しの間隔・録る上限・Soniox の地域)は従来どおり config.toml の [dictation]。

キーの書き方(ヘルパー dictation-helper と同じ): `+` でつなぐ。修飾は ctrl / opt / shift / cmd、キーは A〜Z・0〜9・F1〜F20・
SPACE・ESCAPE・RETURN・TAB・DELETE・矢印(LEFT など)・記号(MINUS など)。修飾キーだけのキーは fn / lctrl / rctrl / lopt / ropt /
lcmd / rcmd / lshift / rshift(1 語で書く・OS のキーの見張り = 入力監視の許可が要る)。
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

MODS = ("ctrl", "opt", "shift", "cmd")                      # 並びは macOS の表記の順(⌃⌥⇧⌘)
MOD_ALIASES = {"control": "ctrl", "alt": "opt", "option": "opt", "command": "cmd", "meta": "cmd"}
MOD_SYMBOLS = {"ctrl": "⌃", "opt": "⌥", "shift": "⇧", "cmd": "⌘"}
MOD_ONLY = {"fn": "Fn", "lctrl": "左⌃", "rctrl": "右⌃", "lopt": "左⌥", "ropt": "右⌥", "lcmd": "左⌘", "rcmd": "右⌘",
            "lshift": "左⇧", "rshift": "右⇧"}
SPECIAL = {"SPACE": "Space", "ESCAPE": "Esc", "RETURN": "Return", "TAB": "Tab", "DELETE": "Delete", "LEFT": "←",
           "RIGHT": "→", "UP": "↑", "DOWN": "↓", "MINUS": "-", "EQUAL": "=", "LBRACKET": "[", "RBRACKET": "]",
           "BACKSLASH": "\\", "SEMICOLON": ";", "QUOTE": "'", "COMMA": ",", "PERIOD": ".", "SLASH": "/", "GRAVE": "`"}
KEY_NAMES = ({chr(c) for c in range(ord("A"), ord("Z") + 1)} | {str(d) for d in range(10)}
             | {f"F{n}" for n in range(1, 21)} | set(SPECIAL))
FREE_KEYS = {f"F{n}" for n in range(1, 21)}                # 修飾なしでも奪ってよいキー(ふだんの入力に使わない・10-19 keigoly様「1 つのキーで」)
RESERVED = {"cmd+V", "cmd+RETURN"}   # ヘルパーが貼り付けと送信で自分で押すキー(登録すると、貼るたびに音声入力が動く)

ACTIONS = ("activate", "handsfree", "lock", "paste_last", "cancel")
MAX_KEYS = 3                                                # 有効化・ハンズフリーで始めるキーの数の上限
REALTIME = ("never", "handsfree", "always")                # 常にインスタント / ハンズフリー時のみリアルタイム / 常にリアルタイム
SEND_KEYS = ("off", "enter", "cmd_enter", "paste_only")    # 送信コマンド: オフ(語を残す)/ Enter / ⌘+Enter / 貼り付けのみ
LANGUAGES = {"ja_en": ["ja", "en"], "ja": ["ja"], "en": ["en"], "auto": []}
SOUND_SETS = ("default", "soft", "classic")
BAR_MODES = ("recording", "always", "off")
PET_MODES = ("always", "recording", "off")                 # ナギのペット(FR-14c): 常に出す / 録っている間だけ / 出さない
PET_SIZES = ("s", "m", "l")
MAX_INSTRUCTIONS = 4000

DEFAULTS: dict = {
    "enabled": True,                 # アプリで待ち受ける(`meetcue dictate` はこの値に関わらず動く)
    "keys": {"activate": ["ctrl+opt+V"], "handsfree": [], "lock": "SPACE", "paste_last": "ctrl+opt+shift+V",
             "cancel": "ESCAPE", "cancel_enabled": True},
    "realtime": "never",
    "send_key": "enter",
    "auto_enter_apps": [],           # [{"bundle": "com.tinyspeck.slackmacgap", "name": "Slack"}]
    "language": "ja_en",
    "sounds": True,
    "sound_set": "default",
    "floating_bar": "recording",
    "live_text": True,               # バーの上に、話している言葉をその場で出す(10-19 keigoly様「字幕が出ていなかった」)
    "pet": "always",                 # ナギのペット(FR-14c・10-19 keigoly様「常時表示させて」)
    "pet_size": "m",
    "mute_audio": False,
    "avoid_clipboard_history": True,
    "mic": "",                       # 空 = システムの既定のマイク / それ以外はデバイスの UID
    "privacy_mode": False,           # 録音と文を保存しない(履歴に出ない)
    "deep_context": False,           # 前面のアプリ名・題名・カーソルの前の文を Soniox の context に渡す
    "ai_cleanup": False,             # カスタム指示で AI が整える
    "custom_instructions": "",
    "edit_mode": False,              # 文を選んで話すと、話した内容を指示として AI が直す
    "casual": False,                 # チャットのアプリでは文末の「。」を付けない
}
SECTIONS = {"keys": ("keys",),
            "settings": ("send_key", "language", "realtime"),
            "system": ("sounds", "sound_set", "floating_bar", "live_text", "pet", "pet_size", "mute_audio", "avoid_clipboard_history", "mic",
                       "privacy_mode", "deep_context", "edit_mode", "casual", "auto_enter_apps")}
# カジュアルなメッセージのアプリ(Aqua Voice の「Slack、iMessage、Discord など」)
CASUAL_APPS = {"com.tinyspeck.slackmacgap": "Slack", "com.hnc.Discord": "Discord", "jp.naver.line.mac": "LINE",
               "com.apple.MobileSMS": "メッセージ", "com.microsoft.teams2": "Teams", "com.microsoft.teams": "Teams",
               "com.facebook.archon": "Messenger", "net.whatsapp.WhatsApp": "WhatsApp", "ru.keepcoder.Telegram": "Telegram",
               "jp.co.chatwork.mac": "Chatwork"}


def path(app_dir: Path) -> Path:
    return Path(app_dir) / "dictation" / "settings.json"


def parse_key(spec: str) -> tuple[tuple[str, ...], str | None, str | None] | None:
    """'ctrl+opt+V' → (('ctrl', 'opt'), 'V', None)。'fn' → ((), None, 'fn')。読めなければ None。"""
    s = str(spec or "").strip()
    if not s:
        return None
    if s.lower() in MOD_ONLY:
        return (), None, s.lower()
    parts = [p.strip() for p in s.split("+")]
    if any(not p for p in parts):
        return None
    mods: set[str] = set()
    for p in parts[:-1]:
        m = MOD_ALIASES.get(p.lower(), p.lower())
        if m not in MODS:
            return None
        mods.add(m)
    key = parts[-1].upper()
    if key not in KEY_NAMES:
        return None
    return tuple(m for m in MODS if m in mods), key, None


def normalize(spec: str) -> str:
    """書き方をそろえる(修飾の順・大文字)。読めなければ空。"""
    p = parse_key(spec)
    if p is None:
        return ""
    mods, key, only = p
    return only if only else "+".join([*mods, key or ""])


def label(spec: str) -> str:
    """画面に出す名前: ⌃⌥V / Fn / 右⌥ / Esc。"""
    p = parse_key(spec)
    if p is None:
        return ""
    mods, key, only = p
    if only:
        return MOD_ONLY[only]
    return "".join(MOD_SYMBOLS[m] for m in mods) + SPECIAL.get(key or "", key or "")


def lock_label(activate: str, lock: str) -> str:
    """ハンズフリーに切り替え(有効化を押したまま押す)の名前: ⌃⌥V+Space。"""
    if not lock:
        return ""
    return f"{label(activate)}+{label(lock)}" if activate else label(lock)


def _check_trigger(spec, what: str) -> str | None:
    """有効化・ハンズフリーで始める・最後の文字起こしを貼り付け: ふだんの入力を奪わないキーだけ。"""
    p = parse_key(spec) if isinstance(spec, str) else None
    if p is None:
        return f"{what}: キーを読めません({spec})"
    mods, key, only = p
    if only:
        return None
    if normalize(spec) in RESERVED:
        return f"{what}: ⌘V と ⌘Return は貼り付けと送信に使うので選べません"
    if not (set(mods) & {"ctrl", "opt", "cmd"}) and key not in FREE_KEYS:
        return (f"{what}: 1 つのキーなら Fn・右 ⌘・右 ⌥ などの修飾キーか F キーにしてください。組み合わせなら ⌃・⌥・⌘ のどれかと"
                "(文字のキーだけは、ふだんの入力を奪うので使えません)")
    return None


def validate(s: dict) -> list[str]:
    """設定の誤り(画面に出す文)。空なら保存してよい。"""
    errs: list[str] = []
    k = s.get("keys")
    if not isinstance(k, dict):
        return ["keys の形が正しくありません"]
    act = k.get("activate")
    if not isinstance(act, list) or not 1 <= len(act) <= MAX_KEYS:
        errs.append(f"有効化のキーは 1〜{MAX_KEYS} 個です")
        act = []
    hf = k.get("handsfree")
    if not isinstance(hf, list) or len(hf) > MAX_KEYS:
        errs.append(f"ハンズフリーで始めるキーは {MAX_KEYS} 個までです")
        hf = []
    for spec in act:
        if e := _check_trigger(spec, "有効化"):
            errs.append(e)
    for spec in hf:
        if e := _check_trigger(spec, "ハンズフリーで始める"):
            errs.append(e)
    if k.get("paste_last") and (e := _check_trigger(k["paste_last"], "最後の文字起こしを貼り付け")):
        errs.append(e)
    for one in ("lock", "paste_last", "cancel"):
        if not isinstance(k.get(one, ""), str):
            errs.append(f"{one} のキーは文字で書きます")
    lock = k.get("lock") if isinstance(k.get("lock"), str) else ""
    if lock:
        p = parse_key(lock)
        if p is None or p[2] or p[0]:
            errs.append("ハンズフリーに切り替えるキーは、修飾なしの 1 つのキーです(既定 Space)")
    cancel = k.get("cancel") if isinstance(k.get("cancel"), str) else ""
    if cancel:
        p = parse_key(cancel)
        if p is None or p[2]:
            errs.append(f"キャンセルのキーを読めません({cancel})")
        elif not (p[1] == "ESCAPE" or (p[1] or "").startswith("F") and p[1] not in ("F",) and p[1][1:].isdigit()
                  or set(p[0]) & {"ctrl", "opt", "cmd"}):
            errs.append("キャンセルのキーは Esc・F キー、または ⌃・⌥・⌘ と組んだキーにしてください(録っている間、そのキーを奪うため)")
    if not isinstance(k.get("cancel_enabled"), bool):
        errs.append("キャンセルのオン・オフが正しくありません")
    used = [normalize(x) for x in [*act, *hf, k.get("paste_last") if isinstance(k.get("paste_last"), str) else ""]
            if isinstance(x, str) and x]
    if len(used) != len(set(used)):
        errs.append("同じキーを 2 つの役に使っています")
    if s.get("realtime") not in REALTIME:
        errs.append("有効化の方式が正しくありません")
    if s.get("send_key") not in SEND_KEYS:
        errs.append("送信キーが正しくありません")
    if s.get("language") not in LANGUAGES:
        errs.append("言語が正しくありません")
    if s.get("sound_set") not in SOUND_SETS:
        errs.append("音の組が正しくありません")
    if s.get("floating_bar") not in BAR_MODES:
        errs.append("フローティングバーの出し方が正しくありません")
    if s.get("pet") not in PET_MODES:
        errs.append("ナギ(ペット)の出し方が正しくありません")
    if s.get("pet_size") not in PET_SIZES:
        errs.append("ナギ(ペット)の大きさが正しくありません")
    for b in ("enabled", "sounds", "live_text", "mute_audio", "avoid_clipboard_history", "privacy_mode", "deep_context",
              "ai_cleanup", "edit_mode", "casual"):
        if not isinstance(s.get(b), bool):
            errs.append(f"{b} は true / false です")
    if not isinstance(s.get("mic"), str):
        errs.append("マイクが正しくありません")
    ci = s.get("custom_instructions")
    if not isinstance(ci, str) or len(ci) > MAX_INSTRUCTIONS:
        errs.append(f"カスタム指示は {MAX_INSTRUCTIONS} 字までです")
    apps = s.get("auto_enter_apps")
    if not isinstance(apps, list) or len(apps) > 50 or not all(
            isinstance(a, dict) and isinstance(a.get("bundle"), str) and a["bundle"] for a in apps):
        errs.append("自動 Enter のアプリの書き方が正しくありません")
    return errs


def _merge(base: dict, patch: dict) -> dict:
    """既定 + 変更(知らない項目は捨てる)。入れ子の項目(keys)は、変更が辞書のときだけ重ねる(誤った形は検めで落とす)。"""
    out = copy.deepcopy(base)
    for key, v in (patch if isinstance(patch, dict) else {}).items():
        if key not in base:
            continue
        if isinstance(base[key], dict):
            out[key] = {**base[key], **{kk: vv for kk, vv in v.items() if kk in base[key]}} if isinstance(v, dict) else v
        else:
            out[key] = v
    return out


def _normalized(s: dict) -> dict:
    k = s["keys"]
    if not isinstance(k, dict):
        return s   # 検めで「keys の形が正しくありません」になる
    for many in ("activate", "handsfree"):
        if isinstance(k.get(many), list):
            k[many] = [normalize(x) or x if isinstance(x, str) else x for x in k[many]]
    for one in ("lock", "paste_last", "cancel"):
        if isinstance(k.get(one), str):
            k[one] = normalize(k[one]) or k[one]
    s["auto_enter_apps"] = [{"bundle": str(a.get("bundle")), "name": str(a.get("name") or a.get("bundle"))}
                            for a in s.get("auto_enter_apps") or [] if isinstance(a, dict)]
    return s


def defaults(cfg=None) -> dict:
    """既定値。config.toml の [dictation] key / mods / language_hints を変えていれば、それを有効化と言語の既定にする。"""
    d = copy.deepcopy(DEFAULTS)
    dc = getattr(cfg, "dictation", None)
    if dc is not None:
        spec = normalize("+".join([*[m.strip() for m in str(dc.mods).split(",") if m.strip()], str(dc.key)]))
        if spec:
            d["keys"]["activate"] = [spec]
        hints = list(dc.language_hints or [])
        for name, h in LANGUAGES.items():
            if h == hints:
                d["language"] = name
    return d


def load(cfg) -> dict:
    """保存した設定 + 既定値。壊れたファイル・誤った値は既定に戻す(読み込みで止めない)。"""
    base = defaults(cfg)
    try:
        saved = json.loads(path(cfg.app_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    try:
        s = _normalized(_merge(base, saved if isinstance(saved, dict) else {}))
        if validate(s):   # 手で壊した値があれば、項目ごとに既定へ戻す
            for key in list(DEFAULTS):
                trial = _normalized(_merge(base, {key: s[key]}))
                if validate(trial):
                    s[key] = copy.deepcopy(base[key])
            if validate(s):
                s = base
    except Exception:  # noqa: BLE001 — 思わぬ形のファイルでも、音声入力とアプリを止めない
        s = base
    return s


def save(cfg, patch: dict) -> tuple[dict, list[str]]:
    """今の設定に patch を重ねて検め、よければ書く。(設定, 誤り)。"""
    cur = load(cfg)
    if not isinstance(patch, dict):
        return cur, ["設定の形が正しくありません"]
    new = _normalized(_merge(cur, patch))
    errs = validate(new)
    if errs:
        return cur, errs
    p = path(cfg.app_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return new, []


def reset(cfg, section: str) -> tuple[dict, list[str]]:
    """「デフォルトにリセット」: 節(keys / settings / system)の項目だけ既定に戻す。"""
    base = defaults(cfg)
    return save(cfg, {k: copy.deepcopy(base[k]) for k in SECTIONS.get(section, ())})


def language_hints(s: dict) -> list[str]:
    return list(LANGUAGES.get(s.get("language", "ja_en"), ["ja", "en"]))


def helper_bindings(s: dict) -> dict:
    """ヘルパーへ渡すキー(キャンセルがオフなら空)。"""
    k = s["keys"]
    return {"activate": list(k["activate"]), "handsfree": list(k["handsfree"]), "lock": k["lock"] or "",
            "paste_last": k["paste_last"] or "", "cancel": k["cancel"] if k["cancel_enabled"] else ""}


def labels(s: dict) -> dict:
    """画面の表示名(キーの役ごと)。"""
    k = s["keys"]
    first = k["activate"][0] if k["activate"] else ""
    return {"activate": [label(x) for x in k["activate"]], "handsfree": [label(x) for x in k["handsfree"]],
            "lock": lock_label(first, k["lock"]), "paste_last": label(k["paste_last"]), "cancel": label(k["cancel"])}


def uses_modifier_only(s: dict) -> bool:
    """修飾キーだけのキー(入力監視の許可が要る)を使っているか。"""
    k = s["keys"]
    return any((parse_key(x) or ((), None, None))[2] for x in [*k["activate"], *k["handsfree"]])


def idle_hint(s: dict) -> str:
    """フローティングバーを常に出すときの待ち受けの一言。"""
    first = s["keys"]["activate"][0] if s["keys"]["activate"] else ""
    return f"{label(first)} で話す" if first else "音声入力"


_SAFE_BUNDLE = re.compile(r"^[A-Za-z0-9.\-_]{1,200}$")


def safe_bundle(b: str) -> bool:
    return bool(_SAFE_BUNDLE.match(b or ""))
