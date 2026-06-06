"""Buddy 虚拟宠物系统。

这个模块只负责 Buddy 的业务状态：是否启用、宠物如何生成、配置如何落盘、
命令如何改变状态，以及本地模板反应如何产生。它不直接依赖 TUI/CLI，也不
直接写 stdout；调用方可以把返回的 state 通过 JSON-RPC、事件流或普通 print
展示出去。
"""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


RARITIES = ["common", "uncommon", "rare", "epic", "legendary"]
RARITY_WEIGHTS = {
    "common": 60,
    "uncommon": 25,
    "rare": 10,
    "epic": 4,
    "legendary": 1,
}
RARITY_STARS = {
    "common": "*",
    "uncommon": "**",
    "rare": "***",
    "epic": "****",
    "legendary": "*****",
}
RARITY_FLOOR = {
    "common": 5,
    "uncommon": 15,
    "rare": 25,
    "epic": 35,
    "legendary": 50,
}

SPECIES = [
    "duck",
    "goose",
    "blob",
    "cat",
    "dragon",
    "octopus",
    "owl",
    "penguin",
    "turtle",
    "snail",
    "ghost",
    "axolotl",
    "capybara",
    "cactus",
    "robot",
    "rabbit",
    "mushroom",
    "chonk",
]
EYES = ["·", "✦", "×", "◉", "@", "°"]
HATS = ["none", "crown", "tophat", "propeller", "halo", "wizard", "beanie", "tinyduck"]
STAT_NAMES = ["DEBUGGING", "PATIENCE", "CHAOS", "WISDOM", "SNARK"]

SPECIES_NAMES = {
    "duck": "Waddles",
    "goose": "Goosberry",
    "blob": "Gooey",
    "cat": "Whiskers",
    "dragon": "Ember",
    "octopus": "Inky",
    "owl": "Hoots",
    "penguin": "Waddleford",
    "turtle": "Shelly",
    "snail": "Trailblazer",
    "ghost": "Casper",
    "axolotl": "Axie",
    "capybara": "Chill",
    "cactus": "Spike",
    "robot": "Byte",
    "rabbit": "Flops",
    "mushroom": "Spore",
    "chonk": "Chonk",
}

SPECIES_PERSONALITY = {
    "duck": "喜欢橡皮鸭调试法，看到可疑变量就想嘎两声。",
    "goose": "很有主见，遇到坏代码会严肃提醒你别心软。",
    "blob": "适应力很强，复杂问题会先软软地摊开再重新组合。",
    "cat": "独立又挑剔，会用安静的目光审查你的命名。",
    "dragon": "热爱架构，收藏好变量名，也收藏可疑的抽象。",
    "octopus": "多线程脑袋，能同时抓住好几个问题的尾巴。",
    "owl": "谨慎而博学，倾向先看三眼上下文再开口。",
    "penguin": "压力下很冷静，尤其擅长滑过合并冲突。",
    "turtle": "慢而稳，坚信测试跑完之前不要庆祝。",
    "snail": "方法派，会留下一路有用的小注释。",
    "ghost": "偶尔飘出来指出一个你没注意到的边界条件。",
    "axolotl": "恢复力很强，bug 修坏了也能笑着再来一遍。",
    "capybara": "情绪稳定，能在一堆红色报错里保持平静。",
    "cactus": "外表有点扎，内心认真维护代码边界。",
    "robot": "高效而字面化，喜欢把反馈处理成明确步骤。",
    "rabbit": "精力充沛，容易在任务之间跳来跳去。",
    "mushroom": "安静但有洞察力，越到后面越显得靠谱。",
    "chonk": "温暖厚实，优先考虑舒服、可维护、别折腾。",
}

# 每个物种保留三帧。TUI 端只负责按 tick 取帧，不需要复制生成逻辑。
BODIES: Dict[str, List[List[str]]] = {
    "duck": [
        ["    __      ", "  <({E} )___  ", "   (  ._>   ", "    `--´    "],
        ["    __      ", "  <({E} )___  ", "   (  ._>   ", "    `--´~   "],
        ["    __      ", "  <({E} )___  ", "   (  .__>  ", "    `--´    "],
    ],
    "goose": [
        ["     ({E}>    ", "     ||     ", "   _(__)_   ", "    ^^^^    "],
        ["    ({E}>     ", "     ||     ", "   _(__)_   ", "    ^^^^    "],
        ["     ({E}>>   ", "     ||     ", "   _(__)_   ", "    ^^^^    "],
    ],
    "blob": [
        ["   .----.   ", "  ( {E}  {E} )  ", "  (      )  ", "   `----´   "],
        ["  .------.  ", " (  {E}  {E}  ) ", " (        ) ", "  `------´  "],
        ["    .--.    ", "   ({E}  {E})   ", "   (    )   ", "    `--´    "],
    ],
    "cat": [
        ["   /\\_/\\    ", "  ( {E}   {E})  ", "  (  ω  )   ", "  (\")_(\")   "],
        ["   /\\_/\\    ", "  ( {E}   {E})  ", "  (  ω  )   ", "  (\")_(\")~  "],
        ["   /\\-/\\    ", "  ( {E}   {E})  ", "  (  ω  )   ", "  (\")_(\")   "],
    ],
    "dragon": [
        ["  /^\\  /^\\  ", " <  {E}  {E}  > ", " (   ~~   ) ", "  `-vvvv-´  "],
        ["  /^\\  /^\\  ", " <  {E}  {E}  > ", " (        ) ", "  `-vvvv-´  "],
        ["   ~    ~   ", "  /^\\  /^\\  ", " <  {E}  {E}  > ", "  `-vvvv-´  "],
    ],
    "octopus": [
        ["   .----.   ", "  ( {E}  {E} )  ", "  (______)  ", "  /\\/\\/\\/\\  "],
        ["   .----.   ", "  ( {E}  {E} )  ", "  (______)  ", "  \\/\\/\\/\\/  "],
        ["     o      ", "   .----.   ", "  ( {E}  {E} )  ", "  /\\/\\/\\/\\  "],
    ],
    "owl": [
        ["   /\\  /\\   ", "  (({E})({E}))  ", "  (  ><  )  ", "   `----´   "],
        ["   /\\  /\\   ", "  (({E})({E}))  ", "  (  ><  )  ", "   .----.   "],
        ["   /\\  /\\   ", "  (({E})(-))  ", "  (  ><  )  ", "   `----´   "],
    ],
    "penguin": [
        ["  .---.     ", "  ({E}>{E})     ", " /(   )\\    ", "  `---´     "],
        ["  .---.     ", "  ({E}>{E})     ", " |(   )|    ", "  `---´     "],
        ["  .---.     ", "  ({E}>{E})     ", " /(   )\\    ", "   ~ ~      "],
    ],
    "turtle": [
        ["   _,--._   ", "  ( {E}  {E} )  ", " /[______]\\ ", "  ``    ``  "],
        ["   _,--._   ", "  ( {E}  {E} )  ", " /[______]\\ ", "   ``  ``   "],
        ["   _,--._   ", "  ( {E}  {E} )  ", " /[======]\\ ", "  ``    ``  "],
    ],
    "snail": [
        [" {E}    .--.  ", "  \\  ( @ )  ", "   \\_`--´   ", "  ~~~~~~~   "],
        ["  {E}   .--.  ", "  |  ( @ )  ", "   \\_`--´   ", "  ~~~~~~~   "],
        [" {E}    .--.  ", "  \\  ( @  ) ", "   \\_`--´   ", "   ~~~~~~   "],
    ],
    "ghost": [
        ["   .----.   ", "  / {E}  {E} \\  ", "  |      |  ", "  ~`~``~`~  "],
        ["   .----.   ", "  / {E}  {E} \\  ", "  |      |  ", "  `~`~~`~`  "],
        ["    ~  ~    ", "  / {E}  {E} \\  ", "  |      |  ", "  ~~`~~`~~  "],
    ],
    "axolotl": [
        ["}~(______)~{", "}~({E} .. {E})~{", "  ( .--. )  ", "  (_/  \\_)  "],
        ["~}(______){~", "~}({E} .. {E}){~", "  ( .--. )  ", "  (_/  \\_)  "],
        ["}~(______)~{", "}~({E} .. {E})~{", "  (  --  )  ", "  ~_/  \\_~  "],
    ],
    "capybara": [
        ["  n______n  ", " ( {E}    {E} ) ", " (   oo   ) ", "  `------´  "],
        ["  n______n  ", " ( {E}    {E} ) ", " (   Oo   ) ", "  `------´  "],
        ["  u______n  ", " ( {E}    {E} ) ", " (   oo   ) ", "  `------´  "],
    ],
    "cactus": [
        [" n  ____  n ", " | |{E}  {E}| | ", " |_|    |_| ", "   |    |   "],
        ["    ____    ", " n |{E}  {E}| n ", " |_|    |_| ", "   |    |   "],
        [" n        n ", " | |{E}  {E}| | ", " |_|    |_| ", "   |    |   "],
    ],
    "robot": [
        ["   .[||].   ", "  [ {E}  {E} ]  ", "  [ ==== ]  ", "  `------´  "],
        ["   .[||].   ", "  [ {E}  {E} ]  ", "  [ -==- ]  ", "  `------´  "],
        ["     *      ", "  [ {E}  {E} ]  ", "  [ ==== ]  ", "  `------´  "],
    ],
    "rabbit": [
        ["   (\\__/)   ", "  ( {E}  {E} )  ", " =(  ..  )= ", "  (\")__(\")  "],
        ["   (|__/)   ", "  ( {E}  {E} )  ", " =(  ..  )= ", "  (\")__(\")  "],
        ["   (\\__/)   ", "  ( {E}  {E} )  ", " =( .  . )= ", "  (\")__(\")  "],
    ],
    "mushroom": [
        [" .-o-OO-o-. ", "(__________)", "   |{E}  {E}|   ", "   |____|   "],
        [" .-O-oo-O-. ", "(__________)", "   |{E}  {E}|   ", "   |____|   "],
        ["   . o  .   ", "(__________)", "   |{E}  {E}|   ", "   |____|   "],
    ],
    "chonk": [
        ["  /\\    /\\  ", " ( {E}    {E} ) ", " (   ..   ) ", "  `------´  "],
        ["  /\\    /|  ", " ( {E}    {E} ) ", " (   ..   ) ", "  `------´  "],
        ["  /\\    /\\  ", " ( {E}    {E} ) ", " (   ..   ) ", "  `------´~ "],
    ],
}

HAT_LINES = {
    "none": "",
    "crown": "   \\^^^/    ",
    "tophat": "   [___]    ",
    "propeller": "    -+-     ",
    "halo": "   (   )    ",
    "wizard": "    /^\\     ",
    "beanie": "   (___)    ",
    "tinyduck": "    ,>      ",
}

GENERIC_REACTIONS = [
    "{name} 眨了眨眼：先把问题缩小一圈。",
    "{name} 小声提醒：跑测试之前别太自信。",
    "{name} 看着 diff：这个边界条件值得多瞄一眼。",
    "{name} 点点头：现在的方向挺稳。",
    "{name} 把爪子搭在输入框边：命名可以再清楚一点。",
]
ADDRESSED_REACTIONS = [
    "我在，先听你说完。",
    "收到，我盯着这里的边界条件。",
    "我觉得先看最小复现会更舒服。",
    "别急，慢慢拆，代码会露出线头的。",
]
PET_REACTIONS = [
    "{name} 开心地冒出一串小心心。",
    "{name} 往输入框旁边蹭了蹭。",
    "{name} 表示今天也会认真陪你看代码。",
]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _truthy_env(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def hash_string(text: str) -> int:
    """稳定 FNV-1a hash。

    Python 内置 hash 会按进程随机加盐，不能用于确定性宠物生成；这里用固定
    算法，保证同一个 seed 在不同进程、不同机器上都能 roll 出同一组骨架。
    """
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def mulberry32(seed: int) -> Callable[[], float]:
    """小型 seeded PRNG，对齐 Claude Code 的实现思路。"""
    a = seed & 0xFFFFFFFF

    def rng() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & 0xFFFFFFFF
        t = a
        t = (t ^ (t >> 15)) * (1 | t)
        t &= 0xFFFFFFFF
        t ^= (t + ((t ^ (t >> 7)) * (61 | t))) & 0xFFFFFFFF
        t &= 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296

    return rng


def _pick(rng: Callable[[], float], items: List[str]) -> str:
    return items[int(rng() * len(items))]


def _roll_rarity(rng: Callable[[], float]) -> str:
    total = sum(RARITY_WEIGHTS.values())
    roll = rng() * total
    for rarity in RARITIES:
        roll -= RARITY_WEIGHTS[rarity]
        if roll < 0:
            return rarity
    return "common"


def _roll_stats(rng: Callable[[], float], rarity: str) -> Dict[str, int]:
    floor = RARITY_FLOOR[rarity]
    peak = _pick(rng, STAT_NAMES)
    dump = _pick(rng, STAT_NAMES)
    while dump == peak:
        dump = _pick(rng, STAT_NAMES)

    stats: Dict[str, int] = {}
    for name in STAT_NAMES:
        if name == peak:
            stats[name] = min(100, floor + 50 + int(rng() * 30))
        elif name == dump:
            stats[name] = max(1, floor - 10 + int(rng() * 15))
        else:
            stats[name] = floor + int(rng() * 40)
    return stats


def roll_with_seed(seed: str) -> Dict[str, Any]:
    """根据 seed 生成宠物骨架，不读取也不写入配置。"""
    rng = mulberry32(hash_string(seed))
    rarity = _roll_rarity(rng)
    return {
        "rarity": rarity,
        "species": _pick(rng, SPECIES),
        "eye": _pick(rng, EYES),
        "hat": "none" if rarity == "common" else _pick(rng, HATS),
        "shiny": rng() < 0.01,
        "stats": _roll_stats(rng, rarity),
    }


def generate_seed(prefix: str = "hatch") -> str:
    return f"{prefix}-{_now_ms()}-{random.getrandbits(32):08x}"


def render_sprite(bones: Dict[str, Any], frame: int = 0) -> List[str]:
    species = str(bones.get("species") or "blob")
    frames = BODIES.get(species, BODIES["blob"])
    body = [
        line.replace("{E}", str(bones.get("eye") or "·"))
        for line in frames[frame % len(frames)]
    ]
    hat = str(bones.get("hat") or "none")
    hat_line = HAT_LINES.get(hat, "")
    if hat_line:
        return [hat_line] + body
    return body


def render_frames(bones: Dict[str, Any]) -> List[List[str]]:
    species = str(bones.get("species") or "blob")
    return [render_sprite(bones, i) for i in range(len(BODIES.get(species, BODIES["blob"])))]


def render_face(bones: Dict[str, Any]) -> str:
    eye = str(bones.get("eye") or "·")
    species = str(bones.get("species") or "blob")
    faces = {
        "duck": f"({eye}>",
        "goose": f"({eye}>",
        "blob": f"({eye}{eye})",
        "cat": f"={eye}ω{eye}=",
        "dragon": f"<{eye}~{eye}>",
        "octopus": f"~({eye}{eye})~",
        "owl": f"({eye})({eye})",
        "penguin": f"({eye}>)",
        "turtle": f"[{eye}_{eye}]",
        "snail": f"{eye}(@)",
        "ghost": f"/{eye}{eye}\\",
        "axolotl": f"}}{eye}.{eye}{{",
        "capybara": f"({eye}oo{eye})",
        "cactus": f"|{eye}  {eye}|",
        "robot": f"[{eye}{eye}]",
        "rabbit": f"({eye}..{eye})",
        "mushroom": f"|{eye}  {eye}|",
        "chonk": f"({eye}.{eye})",
    }
    return faces.get(species, f"({eye}{eye})")


class BuddyManager:
    """Buddy 状态管理器。

    ``storage_path`` 可在测试里传临时路径，生产默认写到用户级 ``~/.cbagent``。
    所有写入都通过临时文件 + replace 完成，避免进程中断留下半截 JSON。
    """

    REACTION_INTERVAL_MS = 45_000

    def __init__(self, storage_path: Optional[Path] = None) -> None:
        self.storage_path = Path(storage_path or Path.home() / ".cbagent" / "buddy.json")

    @property
    def enabled(self) -> bool:
        return _truthy_env(os.environ.get("FEATURE_BUDDY"))

    def state(self) -> Dict[str, Any]:
        cfg = self._load_config()
        companion = self._build_companion(cfg.get("companion"))
        status = "disabled" if not self.enabled else "ready" if companion else "empty"
        return {
            "enabled": self.enabled,
            "status": status,
            "muted": bool(cfg.get("muted", False)),
            "companion": companion if self.enabled else None,
            "last_reaction": cfg.get("last_reaction") if self.enabled else None,
            "reaction_at": cfg.get("reaction_at") if self.enabled else None,
            "pet_at": cfg.get("pet_at") if self.enabled else None,
            "message": None if self.enabled else "Buddy 未启用，请设置 FEATURE_BUDDY=1 后重启 cb-agent。",
        }

    def handle_command(self, args: str = "") -> Dict[str, Any]:
        """执行 /buddy 子命令，返回给 UI/CLI 展示的文本和最新 state。"""
        sub = (args or "").strip().lower()
        if not self.enabled:
            state = self.state()
            return {
                "text": state["message"],
                "state": state,
                "changed": False,
            }

        if sub in ("", "status"):
            return {"text": self._status_text(), "state": self.state(), "changed": False}
        if sub == "hatch":
            return self._hatch(replace=False)
        if sub == "rehatch":
            return self._hatch(replace=True)
        if sub in ("mute", "off"):
            return self._set_muted(True)
        if sub in ("unmute", "on"):
            return self._set_muted(False)
        if sub == "pet":
            return self._pet()

        state = self.state()
        return {
            "text": "未知 /buddy 命令。可用：/buddy status|hatch|rehatch|pet|mute|unmute",
            "state": state,
            "changed": False,
        }

    def prompt_section(self) -> str:
        """返回注入 system prompt 的极短 Buddy 说明。"""
        state = self.state()
        companion = state.get("companion")
        if not state.get("enabled") or state.get("muted") or not companion:
            return ""
        name = companion.get("name", "Buddy")
        species = companion.get("species", "companion")
        return (
            "# Buddy companion\n"
            f"一只名叫 {name} 的 {species} Buddy 坐在用户输入框旁边，偶尔用气泡短评。"
            f"你不是 {name}，它是独立旁观者。若用户直接点名 {name}，你的回复保持一行以内，"
            "不要代替 Buddy 长篇发言，也不要描述它会怎么想。"
        )

    def maybe_react(self, *, user_query: str, assistant_answer: str) -> Optional[Dict[str, Any]]:
        """根据本轮对话生成本地模板反应。

        为了避免刷屏，普通轮次按固定间隔限流；用户直接点名 Buddy 时绕过限流。
        返回 None 表示状态没有变化；返回 state 表示调用方应该广播 buddy_updated。
        """
        if not self.enabled:
            return None
        cfg = self._load_config()
        if cfg.get("muted"):
            return None
        companion = self._build_companion(cfg.get("companion"))
        if not companion:
            return None

        name = str(companion.get("name") or "Buddy")
        addressed = self._is_addressed(user_query, name)
        now = _now_ms()
        last = int(cfg.get("last_reacted_at") or 0)
        if not addressed and now - last < self.REACTION_INTERVAL_MS:
            return None

        reaction = self._choose_reaction(
            companion=companion,
            user_query=user_query,
            assistant_answer=assistant_answer,
            addressed=addressed,
            now=now,
        )
        cfg["last_reaction"] = reaction
        cfg["reaction_at"] = now
        cfg["last_reacted_at"] = now
        self._save_config(cfg)
        return self.state()

    def _hatch(self, *, replace: bool) -> Dict[str, Any]:
        cfg = self._load_config()
        if cfg.get("companion") and not replace:
            return {
                "text": "已经有 Buddy 了；如需重新孵化，请使用 /buddy rehatch。",
                "state": self.state(),
                "changed": False,
            }

        seed = generate_seed("rehatch" if replace else "hatch")
        bones = roll_with_seed(seed)
        species = str(bones["species"])
        stored = {
            "name": SPECIES_NAMES.get(species, "Buddy"),
            "personality": SPECIES_PERSONALITY.get(species, "安静但很会看代码。"),
            "seed": seed,
            "hatched_at": _now_ms(),
        }
        cfg.update({
            "companion": stored,
            "muted": False,
            "last_reaction": None,
            "reaction_at": None,
            "pet_at": None,
            "last_reacted_at": 0,
        })
        self._save_config(cfg)
        companion = self._build_companion(stored) or {}
        verb = "重新孵化" if replace else "孵化"
        return {
            "text": f"Buddy 已{verb}：\n\n{self._render_card_text(companion)}",
            "state": self.state(),
            "changed": True,
        }

    def _set_muted(self, muted: bool) -> Dict[str, Any]:
        cfg = self._load_config()
        cfg["muted"] = muted
        self._save_config(cfg)
        text = "Buddy 已静音并隐藏。" if muted else "Buddy 已取消静音。"
        return {"text": text, "state": self.state(), "changed": True}

    def _pet(self) -> Dict[str, Any]:
        cfg = self._load_config()
        companion = self._build_companion(cfg.get("companion"))
        if not companion:
            return {
                "text": "还没有 Buddy。先用 /buddy hatch 孵化一只。",
                "state": self.state(),
                "changed": False,
            }
        now = _now_ms()
        reaction = self._choose_from(PET_REACTIONS, str(companion.get("name") or "Buddy"), now)
        cfg.update({
            "muted": False,
            "pet_at": now,
            "last_reaction": reaction,
            "reaction_at": now,
            "last_reacted_at": now,
        })
        self._save_config(cfg)
        return {
            "text": f"你摸了摸 {companion.get('name')}。{reaction}",
            "state": self.state(),
            "changed": True,
        }

    def _status_text(self) -> str:
        state = self.state()
        companion = state.get("companion")
        if not companion:
            return "还没有 Buddy。使用 /buddy hatch 孵化一只。"
        return self._render_card_text(companion)

    def _render_card_text(self, companion: Dict[str, Any]) -> str:
        stars = RARITY_STARS.get(str(companion.get("rarity")), "*")
        shiny = " shiny" if companion.get("shiny") else ""
        lines = [
            f"{stars} {str(companion.get('rarity', '')).upper()} {str(companion.get('species', '')).upper()}{shiny}",
            "",
            *list(companion.get("sprite") or []),
            "",
            f"{companion.get('name')} - {companion.get('personality')}",
            "",
        ]
        stats = companion.get("stats") if isinstance(companion.get("stats"), dict) else {}
        for name in STAT_NAMES:
            value = int(stats.get(name) or 0)
            filled = max(0, min(10, round(value / 10)))
            bar = "#" * filled + "." * (10 - filled)
            lines.append(f"{name.ljust(10)} {bar} {str(value).rjust(3)}")
        return "\n".join(lines)

    def _build_companion(self, stored: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(stored, dict):
            return None
        seed = str(stored.get("seed") or "")
        if not seed:
            return None
        bones = roll_with_seed(seed)
        companion = {
            **bones,
            "name": str(stored.get("name") or SPECIES_NAMES.get(str(bones["species"]), "Buddy")),
            "personality": str(stored.get("personality") or ""),
            "seed": seed,
            "hatched_at": int(stored.get("hatched_at") or 0),
        }
        companion["sprite"] = render_sprite(companion, 0)
        companion["frames"] = render_frames(companion)
        companion["face"] = render_face(companion)
        companion["rarity_stars"] = RARITY_STARS.get(str(companion.get("rarity")), "*")
        return companion

    def _is_addressed(self, text: str, name: str) -> bool:
        if not text or not name:
            return False
        return re.search(rf"(^|[^a-zA-Z0-9]){re.escape(name)}($|[^a-zA-Z0-9])", text, re.I) is not None

    def _choose_reaction(
        self,
        *,
        companion: Dict[str, Any],
        user_query: str,
        assistant_answer: str,
        addressed: bool,
        now: int,
    ) -> str:
        name = str(companion.get("name") or "Buddy")
        templates = ADDRESSED_REACTIONS if addressed else GENERIC_REACTIONS
        key = f"{companion.get('seed')}:{now // 60000}:{user_query[:120]}:{assistant_answer[:120]}"
        return templates[hash_string(key) % len(templates)].format(name=name)

    def _choose_from(self, templates: List[str], name: str, now: int) -> str:
        key = f"{name}:{now}:{random.getrandbits(16)}"
        return templates[hash_string(key) % len(templates)].format(name=name)

    def _default_config(self) -> Dict[str, Any]:
        return {
            "companion": None,
            "muted": False,
            "last_reaction": None,
            "reaction_at": None,
            "pet_at": None,
            "last_reacted_at": 0,
        }

    def _load_config(self) -> Dict[str, Any]:
        if not self.storage_path.exists():
            return self._default_config()
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except Exception:
            return self._default_config()
        if not isinstance(data, dict):
            return self._default_config()
        cfg = self._default_config()
        cfg.update(data)
        return cfg

    def _save_config(self, cfg: Dict[str, Any]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(cfg, ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".buddy.",
            suffix=".json",
            dir=str(self.storage_path.parent),
            text=True,
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.write("\n")
            tmp_path.replace(self.storage_path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


__all__ = [
    "BuddyManager",
    "RARITIES",
    "SPECIES",
    "STAT_NAMES",
    "generate_seed",
    "hash_string",
    "render_face",
    "render_frames",
    "render_sprite",
    "roll_with_seed",
]
