"""技术栈抽取:简历文本 → 结构化技术栈条目(带原文证据闸门)。

仓深挖那条「证据必须在仓里存在」的白名单,在简历没有仓库地址时整条失效;
无仓场景下**简历原文就是唯一的真实性锚**——每个技术名词都必须能在简历里
找到出处,否则拒收。这样面试官不会问出候选人根本没写过的东西。

等价机制:仓深挖校验 evidence.path 在仓内,这里校验 name/raw_text 在原文内。

分层(纯函数零 IO,可单测、可进 CI;模型只出现在编排入口):
- locate_tech_section / locate_project_section:简历切分
- verify_evidence:证据闸门
- build_items:闸门 → 去重 → 上限截断
- extract_tech_stack:唯一触网处(模型负责名词识别与档位判定)

简历文本的脱敏是调用方职责:与本线其余模型入口一致,进模型前由 evaluation
runner 在 job 入口统一 mask_pii_deep,此处不重复处理。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from langchain_core.messages import HumanMessage

from official_agent.config import get_effective_settings
from official_agent.evaluation.graph import _extract_json
from official_agent.graphs.assistant import build_model
from official_agent.prompt_loader import load_prompt

PROMPT_FILE = "evaluation/tech_stack.md"
TEMPERATURE = 0.2

#: 技术名词上限:简历可能列 9 个以上,取前几档供面试官挑;这一档也为
#: 出题留出预算(每题 1-2 问,总量不越出题硬顶)。
MAX_ITEMS = 6

#: 自述档位:与出题难度标尺一一对应,序自高而低。
ClaimedLevel = Literal["精通", "熟练", "掌握", "了解", "初学", "listed"]

#: 档位措辞 → 档位。**弱措辞排在前面**:「初步了解」既含「了解」也含「初步」,
#: 先命中「初步」得初学,比判成「了解」更保守。「擅长」只算熟练——它表达的
#: 是相对偏好而非登峰造极,抬到精通会把面试官推向候选人答不上的深水区。
_LEVEL_PATTERNS: tuple[tuple[str, ClaimedLevel], ...] = (
    ("精通", "精通"),
    ("擅长", "熟练"),
    ("熟练", "熟练"),
    ("掌握", "掌握"),
    ("初步", "初学"),
    ("粗浅", "初学"),
    ("正在学", "初学"),
    ("用过", "初学"),
    ("初学", "初学"),
    ("了解", "了解"),
)

#: 技术栈栏标题候选(简历措辞不一:「技术栈:」「技术能力」「专业技能」…)
_TECH_HEADERS = ("技术栈", "技术能力", "专业技能", "技能栈", "技能")
#: 技术栈栏之后通常紧跟的板块标题,命中即技术栈栏结束
_TECH_BREAKS = (
    "项目",
    "实习",
    "工作经历",
    "教育",
    "自我介绍",
    "自我评价",
    "加入理由",
    "获奖",
    "荣誉",
)

#: 项目经验栏标题候选
_PROJECT_HEADERS = ("项目经验", "项目经历", "项目实践", "项目")
#: 项目经验栏之后的板块标题
_PROJECT_BREAKS = ("自我介绍", "自我评价", "加入理由", "获奖", "荣誉", "教育")


@dataclass(frozen=True)
class TechStackItem:
    """一条技术栈条目。

    name 与 raw_text 都经证据闸门校验(必须出自简历原文),因此可以直接
    展示给面试官;used_in 已过滤为简历里真实出现过的项目名。
    """

    name: str
    raw_text: str
    claimed_level: ClaimedLevel
    used_in: tuple[str, ...] = ()


def _normalize(text: str) -> str:
    """证据比对归一:忽略大小写与全部空白。

    简历里中英混排的空格极不稳定(「VS Code」/「VSCode」、CJK 与拉丁字母
    之间常无空格),逐字比对会把同一名词判成两个;归一后仍要求是原文子串,
    编造的名词照样过不了闸门。
    """
    return "".join((text or "").split()).casefold()


def _match_header(line: str, headers: Sequence[str]) -> str | None:
    """行首命中标题则返回标题后的同行正文(无则空串);未命中返回 None。

    同行正文用于「技术栈:Python、Java」这类单行写法——真实简历里常见,
    丢掉它等于丢掉整个技术栈栏。
    """
    for header in headers:
        if line.startswith(header):
            return line[len(header) :].lstrip(":： \t\u3000")
    return None


def _locate_section(text: str, headers: Sequence[str], breaks: Sequence[str]) -> str:
    """按标题前缀定位一个板块的正文;未定位到返回空串。

    先判结束再判开始:板块标题之间互为边界(「项目经验」既结束技术栈栏、
    又开启项目经验栏),顺序反了会把边界行当成本栏内容。
    """
    collected: list[str] = []
    inside = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if inside and line.startswith(tuple(breaks)):
            break
        tail = _match_header(line, headers)
        if tail is not None:
            inside = True
            if tail:
                collected.append(tail)
            continue
        if inside:
            collected.append(line)
    return "\n".join(collected).strip()


def locate_tech_section(text: str) -> str:
    """定位技术栈栏正文;未定位到返回空串。"""
    return _locate_section(text, _TECH_HEADERS, _TECH_BREAKS)


def locate_project_section(text: str) -> str:
    """定位项目经验栏正文;未定位到返回空串。"""
    return _locate_section(text, _PROJECT_HEADERS, _PROJECT_BREAKS)


def verify_evidence(name: str, raw_text: str, source_text: str) -> bool:
    """证据闸门:名词须出现在自述出处句里,且出处句须是简历原文。

    两个条件都要求归一后为原文子串。比只查原文更严一档——只查原文时
    「真句子配假名词」能蒙混过关,那样 raw_text 就失去了出处含义。
    """
    normalized_name = _normalize(name)
    normalized_raw = _normalize(raw_text)
    normalized_source = _normalize(source_text)
    if not normalized_name or not normalized_raw or not normalized_source:
        return False
    return normalized_raw in normalized_source and normalized_name in normalized_raw


def normalize_level(value: Any) -> ClaimedLevel:
    """自述措辞 → 固定档位;没写措辞或认不出的取 listed。

    否定措辞不算自述(「不了解 Redis」不是把 Redis 列为技能),故用先行
    否定断言排除;认不出的措辞同样落到 listed,不抬档。
    """
    text = str(value or "").strip()
    for word, level in _LEVEL_PATTERNS:
        if re.search(rf"(?<!不){re.escape(word)}", text):
            return level
    return "listed"


def _clean_used_in(value: Any, source_text: str) -> tuple[str, ...]:
    """项目归属清洗:只留简历里真实出现过的项目名,保序去重。

    编造的项目名与编造的技术名词同性质(都会让面试官问到不存在的东西),
    按同一闸门处理。
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    normalized_source = _normalize(source_text)
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        project = str(item or "").strip()
        key = _normalize(project)
        if not key or key in seen or key not in normalized_source:
            continue
        seen.add(key)
        out.append(project)
    return tuple(out)


def _merge_used_in(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    """合并同一名词的两处项目归属(去重保序)。"""
    out = list(left)
    seen = {_normalize(x) for x in left}
    for item in right:
        key = _normalize(item)
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return tuple(out)


def build_items(
    raw_items: Sequence[Any], source_text: str, *, limit: int = MAX_ITEMS
) -> list[TechStackItem]:
    """模型原始条目 → 受闸门约束的技术栈条目(纯函数,零 IO)。

    闸门顺序:证据校验(弃编造)→ 同名词合并 → 上限截断。保序:模型按
    技术栈栏顺序返回,顺序即醒目度,截断取前 limit 条。
    """
    out: list[TechStackItem] = []
    index: dict[str, int] = {}
    rejected: list[str] = []

    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        raw_text = str(raw.get("raw_text") or "").strip()
        if not verify_evidence(name, raw_text, source_text):
            rejected.append(name or "<无名>")
            continue

        key = _normalize(name)
        used_in = _clean_used_in(raw.get("used_in"), source_text)
        if key in index:
            # 同名重复出现:并入项目归属,不新增条目(一个名词一个条目)
            current = out[index[key]]
            out[index[key]] = replace(
                current, used_in=_merge_used_in(current.used_in, used_in)
            )
            continue
        if len(out) >= limit:
            continue
        index[key] = len(out)
        out.append(
            TechStackItem(
                name=name,
                raw_text=raw_text,
                claimed_level=normalize_level(raw.get("claimed_level")),
                used_in=used_in,
            )
        )

    if rejected:
        logging.getLogger(__name__).warning(
            "guard_event guard_name=tech_stack_evidence verdict=rejected count=%d head=%r",
            len(rejected),
            rejected[:3],
        )
    return out


def _content_text(response: Any) -> str:
    """模型回复 → 文本(多模态 content 取文本块拼接)。"""
    raw = getattr(response, "content", "")
    if isinstance(raw, list):
        raw = "".join(block.get("text", "") for block in raw if isinstance(block, dict))
    return raw if isinstance(raw, str) else str(raw)


def _build_material(resume_text: str) -> str:
    """构造模型材料:定位到的技术栈栏 + 简历全文。

    技术栈栏单列一份是给模型的显式焦点——真实简历里技术栈栏常无分隔符连写
    (「PythonC语言C++Git」),单列能显著降低切分时的漏词。
    """
    tech = locate_tech_section(resume_text)
    return (
        "技术栈栏(自简历正文定位;未定位到则为空):\n"
        + (tech or "(未定位到)")
        + "\n\n简历正文:\n"
        + resume_text
    )


async def extract_tech_stack(
    resume_text: str, *, limit: int = MAX_ITEMS
) -> list[TechStackItem]:
    """简历文本 → 技术栈条目;抽不出返回空列表,由调用方决定是否降级。

    不触网的短路:文本为空、或技术栈栏与项目经验栏都定位不到——这两种情况
    没有任何可锚定的原文,调用模型必然被证据闸门全否,白花一次调用。

    模型调用本身失败(网络/供应商)照常抛出:那是基础设施故障,不是
    「这份简历抽不出技术栈」,不该被静默当成降级信号。
    """
    text = (resume_text or "").strip()
    if not text:
        return []
    if not locate_tech_section(text) and not locate_project_section(text):
        return []

    settings = get_effective_settings()
    model = build_model(settings, temperature=TEMPERATURE)
    response = await model.ainvoke(
        [HumanMessage(content=load_prompt(PROMPT_FILE) + "\n\n---\n\n" + _build_material(text))]
    )
    try:
        payload = json.loads(_extract_json(_content_text(response)))
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "guard_event guard_name=tech_stack_parse verdict=unparsable"
        )
        return []
    raw_items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        logging.getLogger(__name__).warning(
            "guard_event guard_name=tech_stack_parse verdict=no_items"
        )
        return []
    return build_items(raw_items, text, limit=limit)
