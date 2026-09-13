"""Dossier:探索段的唯一产出(B-AG3,#151;spec §3.2/D7)。

十类取材槽(C1-C10,spec §4 同源)+ 元信息。探索循环把工具观察写进槽位;
出题段只吃 dossier 渲染文本,不再接触 GitHub(两段解耦,可单测可 eval)。

预算(D7):dossier 总量 ≤40K 字符——add() 是唯一写入口,超限丢弃并标
dossier_capped;四闸的另外两闸(轮数/墙钟)由 explore 循环持有。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import get_args

from official_agent.evaluation.schema import CATEGORY

_MAX_DOSSIER_CHARS = 40_000  # D7:dossier 总量上限

#: 十类取材槽 = 十类题类(SPEC §4),单源自 schema.CATEGORY——真机实测:
#: 槽名与题类名漂移会让模型把槽名当 category 填,信封校验直接拒
SLOT_NAMES: tuple[str, ...] = tuple(get_args(CATEGORY))


@dataclass
class Dossier:
    """探索产出容器。slots 值为多段观察文本('\n\n' 连接,追加不改写)。"""

    slots: dict[str, str] = field(default_factory=lambda: {k: "" for k in SLOT_NAMES})
    attribution: str = ""  # 归属级别+证据(ADR-0008),出题/展示用
    turns_used: int = 0  # 实际轮数(LLM+工具累计)
    degraded: bool = False  # 预算触顶=用已有材料出题,不判失败(D7)
    degrade_reason: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit_tokens: int | None = None  # D9/#154:prompt cache 命中
    cache_miss_tokens: int | None = None
    paths: list[str] = field(default_factory=list)  # list_files 结构化清单(路径白名单校验)
    paths_truncated: bool = False

    @property
    def total_chars(self) -> int:
        return sum(len(v) for v in self.slots.values())

    def add(self, slot: str, observation: str) -> bool:
        """追加观察到槽位(追加不改写,D8)。超总量上限整条丢弃并标 capped。

        返回是否写入(供循环把「写不进」反馈给模型/预算判定)。"""
        if slot not in self.slots:
            return False
        if not observation:
            return False
        if self.total_chars + len(observation) > _MAX_DOSSIER_CHARS:
            self.degraded = True
            self.degrade_reason = self.degrade_reason or "dossier 40K 上限触顶"
            return False
        self.slots[slot] = (
            f"{self.slots[slot]}\n\n{observation}" if self.slots[slot] else observation
        )
        return True

    def render(self) -> str:
        """出题段的材料视图:非空槽位按 C1-C10 序渲染(只读,不改状态)。"""
        parts: list[str] = []
        for name in SLOT_NAMES:
            body = self.slots.get(name, "")
            if body:
                parts.append(f"### {name}\n{body}")
        meta = [
            f"- 归属: {self.attribution}" if self.attribution else None,
            f"- 探索轮数: {self.turns_used}",
            "- 降级: " + self.degrade_reason if self.degraded else None,
        ]
        parts.append("### 探索元信息\n" + "\n".join(m for m in meta if m))
        return "\n\n".join(parts)

    def is_empty(self) -> bool:
        return self.total_chars == 0
