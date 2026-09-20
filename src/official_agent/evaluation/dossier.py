"""Dossier:探索段的唯一产出。

十类取材槽(C1-C10)+ 元信息。探索循环把工具观察写进槽位;
出题段只吃 dossier 渲染文本,不再接触 GitHub(两段解耦,可单测可 eval)。

预算:dossier 总量 ≤40K 字符——add() 是唯一写入口,超限丢弃并标
dossier_capped;四闸的另外两闸(轮数/墙钟)由 explore 循环持有。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import get_args

from official_agent.evaluation.schema import CATEGORY

_MAX_DOSSIER_CHARS = 40_000  # dossier 总量上限

#: 十类取材槽 = 十类题类,单源自 schema.CATEGORY——真机实测:
#: 槽名与题类名漂移会让模型把槽名当 category 填,信封校验直接拒
SLOT_NAMES: tuple[str, ...] = tuple(get_args(CATEGORY))


@dataclass
class Dossier:
    """探索产出容器。slots 值为多段观察文本('\n\n' 连接,追加不改写)。"""

    slots: dict[str, str] = field(default_factory=lambda: {k: "" for k in SLOT_NAMES})
    attribution: str = ""  # 归属级别+证据(ADR-0008),出题/展示用
    turns_used: int = 0  # 实际轮数(LLM+工具累计)
    degraded: bool = False  # 预算触顶=用已有材料出题,不判失败
    degrade_reason: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit_tokens: int | None = None  # prompt cache 命中
    cache_miss_tokens: int | None = None
    paths: list[str] = field(default_factory=list)  # list_files 结构化清单(路径白名单校验)
    paths_truncated: bool = False

    @property
    def total_chars(self) -> int:
        return sum(len(v) for v in self.slots.values())

    def add(self, slot: str, observation: str) -> bool:
        """追加观察到槽位(追加不改写)。超总量上限整条丢弃并标 capped。

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


def dossier_from_resume(resume_text: str, *, attribution: str = "") -> Dossier:
    """简历文本 → 取材档案(无仓路径的取材来源)。

    仓深挖的档案由探索段从代码仓取材;无仓简历没有仓可探,简历文本本身
    就是全部材料。此前这条路径只产出一句「探索段未取得材料」,出题段
    等于拿空档案出题——现在把简历按板块灌进对应取材槽。

    槽位映射按「该板块能回答哪类问题」:
    - 技术栈栏 → C2 技术选型与权衡(候选人主动声明的技术面)
    - 项目经验栏 → C4 实现细节拷打(能追问做法与细节的唯一来源)
    - 其余正文(自我介绍等)→ C1 背景与动机

    定位不到板块时把全文落 C1:宁可粗一点,也不要空档案。
    """
    from official_agent.evaluation.tech_stack import (
        locate_project_section,
        locate_tech_section,
    )

    text = (resume_text or "").strip()
    dossier = Dossier(attribution=attribution)
    if not text:
        return dossier

    tech = locate_tech_section(text)
    project = locate_project_section(text)
    if tech:
        dossier.add("C2_技术选型与权衡", f"技术栈栏:\n{tech}")
    if project:
        dossier.add("C4_实现细节拷打", f"项目经验栏:\n{project}")
    if not tech and not project:
        dossier.add("C1_背景与动机", text)
    else:
        # 除已分派的板块外,其余正文仍是背景材料的来源
        remainder = _resume_remainder(text, tech, project)
        if remainder:
            dossier.add("C1_背景与动机", remainder)
    return dossier


def _resume_remainder(text: str, tech: str, project: str) -> str:
    """扣掉已分派给其它槽的正文,返回剩余部分(保序,丢弃纯标题行)。

    纯标题行(「技术栈:」「项目经验:」)本身不含信息,留在背景槽只会给模型
    灌噪声——正文已经分派到各自的槽里了。
    """
    dispatched = {line.strip() for line in f"{tech}\n{project}".splitlines() if line.strip()}
    kept = [
        line
        for line in text.splitlines()
        if line.strip()
        and line.strip() not in dispatched
        and not _is_label_only(line.strip())
    ]
    return "\n".join(kept)


def _is_label_only(line: str) -> bool:
    """整行是否只是个板块标题(末尾冒号、剥掉冒号后很短)。"""
    stripped = line.rstrip(":：").strip()
    return line.endswith((":", "：")) and len(stripped) <= 6
