"""官网对话的逐轮意图提示:按固定规则判断本轮想办什么,给 ReAct 循环一条软提示。

ADR-0003 不设意图分类节点,「意图」由模型选工具表达;本模块是该 ADR 留下的「软路由
提示」位置,不是路由:
- 只输出预定义的标签、标记、证据来源类型和建议工具;不装配工具、不生成确认指纹、
  不触发写操作——能调什么由装配层按身份决定,写操作照旧经确认卡片(ADR-0005);
- 「想办什么」与「能不能办」分开:标签只看本轮文本与会话里的结构化选项,与身份无关;
  身份只影响提示文本——提示里只点名本会话已装配的工具;
- 信号冲突(同一分句既问做法又要执行)判为不确定,不出提示,由原循环自行推理;
- 分数可见性、权限码、确认状态仍由原有业务逻辑判断,这里不复判。

模板在 prompts/assistant_intent_hint.md(ADR-0004),本模块只填工具名与选项。
是否计算、是否把提示交给模型由 config.assistant_intent_mode 决定(见 web/routes.py)。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

from official_agent.prompt_loader import load_prompt, load_prompt_meta

#: 规则版本:改任何规则或词表都要 bump,随记录落库,对照报告按它区分两次运行
INTENT_VERSION = "intent_rules/v1"
HINT_PROMPT_FILE = "assistant_intent_hint.md"

IntentMode = Literal["off", "shadow", "hint"]

LABEL_RULES = "rules_inquiry"
LABEL_SELF = "self_lookup"
LABEL_ADMIN = "admin_lookup"
LABEL_SCHEDULE = "schedule_interview"
LABEL_RESCHEDULE = "handle_reschedule"
LABEL_OTHER = "other"
LABELS = (LABEL_RULES, LABEL_SELF, LABEL_ADMIN, LABEL_SCHEDULE, LABEL_RESCHEDULE, LABEL_OTHER)
#: 多个分句标签不同时,主标签取最需要谨慎的一项:写操作 > 本人数据 > 管理数据 > 公开规则
_LABEL_PRIORITY = (LABEL_RESCHEDULE, LABEL_SCHEDULE, LABEL_SELF, LABEL_ADMIN, LABEL_RULES)
_WRITE_LABELS = frozenset({LABEL_SCHEDULE, LABEL_RESCHEDULE})

FLAG_NEGATION = "negation"
FLAG_MISSING_TARGET = "missing_target"
FLAG_MULTIPLE_TARGETS = "multiple_targets"
FLAG_COMPOUND = "compound"
FLAG_PRIOR_REFERENCE = "prior_reference"
FLAG_RESTRICTED_SCORE = "restricted_score"
#: 标记在记录与提示里的固定顺序
_FLAG_ORDER = (
    FLAG_RESTRICTED_SCORE,
    FLAG_NEGATION,
    FLAG_PRIOR_REFERENCE,
    FLAG_MISSING_TARGET,
    FLAG_MULTIPLE_TARGETS,
    FLAG_COMPOUND,
)

EVIDENCE_KEYWORD = "keyword"
EVIDENCE_PRIOR_OPTIONS = "prior_options"
EVIDENCE_PENDING_CANCELLED = "pending_cancelled"
_EVIDENCE_ORDER = (EVIDENCE_KEYWORD, EVIDENCE_PRIOR_OPTIONS, EVIDENCE_PENDING_CANCELLED)

OPTION_SESSION = "session"
OPTION_RESCHEDULE_REQUEST = "reschedule_request"
#: 上一轮哪些工具的结果算「列出了可选项」,以及选中后承接成什么标签
_OPTION_TOOLS = {
    "find_available_sessions": OPTION_SESSION,
    "list_reschedule_requests": OPTION_RESCHEDULE_REQUEST,
}
_OPTION_LABEL = {OPTION_SESSION: LABEL_SCHEDULE, OPTION_RESCHEDULE_REQUEST: LABEL_RESCHEDULE}

_WRITE_TOOL_NAMES = frozenset({"assign_interview", "handle_reschedule"})
#: 两类写请求的完整工具链:先取周期、再查候选、最后写
_SCHEDULE_TOOLS = frozenset({"get_open_cycle", "find_available_sessions", "assign_interview"})
_RESCHEDULE_TOOLS = frozenset({"get_open_cycle", "list_reschedule_requests", "handle_reschedule"})
#: 建议工具的固定输出顺序(也是全部可能被提示点名的工具)
_TOOL_ORDER = (
    "search_knowledge",
    "get_open_cycle",
    "get_my_interview",
    "find_available_sessions",
    "list_reschedule_requests",
    "list_unassigned",
    "search_resumes",
    "get_recruit_statistics",
    "get_candidate_card",
    "assign_interview",
    "handle_reschedule",
)


@dataclass(frozen=True)
class PriorOption:
    """上一轮工具结果里列出的一个可选项(场次或改期申请)。"""

    kind: str
    option_id: int
    display: str


@dataclass(frozen=True)
class TurnIntent:
    """一轮的判断结果。uncertain 为真时 label 恒为 other,且不出提示。"""

    label: str
    flags: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    suggested_tools: tuple[str, ...] = ()
    uncertain: bool = False
    #: 承接上轮选择时选中的项与它是第几项(1 起);没有承接为 None
    chosen_option: PriorOption | None = None
    chosen_ordinal: int | None = None
    version: str = INTENT_VERSION

    def as_record(self) -> dict[str, Any]:
        """落对话日志与 trace 的形状:只含枚举值与 ID,不含用户原文。"""
        return {
            "version": self.version,
            "label": self.label,
            "flags": list(self.flags),
            "evidence": list(self.evidence),
            "tools": list(self.suggested_tools),
            "uncertain": self.uncertain,
            "chosen_option": self.chosen_option.option_id if self.chosen_option else None,
        }


@dataclass
class _Clause:
    label: str | None = None
    tools: set[str] = field(default_factory=set)
    restricted: bool = False
    uncertain: bool = False


# ── 词表 ────────────────────────────────────────────────────────────────
# 文本先做 NFKC(全角转半角)、转小写、去空白,下面的正则都按这个形状写。

_CLAUSE_SPLIT = re.compile(r"[,;。!?]|然后|顺便|另外|并且|同时|再帮我")
_RESTRICTED = re.compile(r"评分|分数|打分|等级|多少分|几分")
# 分句开头的放弃/改口;裸「不」「别」必须跟着动作动词,否则「不知道怎么报名」也会中
_NEGATION_HEAD = re.compile(
    r"^(算了|不用了|先不用了?|等等|等一下|"
    r"取消(刚才|上一个|这个|那个)的?(操作|安排)?|"
    r"(先|暂时|还是|就)?(别|不要|不用|不)再?(改|排|分配|安排|调|执行|动|同意|批准|处理|弄|操作)了?)"
)
# 「待处理」是申请状态不是动作:有人问「有哪些待处理的改期申请」是在查列表
_APPROVE_VERB = re.compile(r"同意|批准|通过|驳回|拒绝|批了|(?<!待)处理")
_RESCHEDULE = re.compile(r"改期")
# 带去向的排期动词;「改到几点了」「排到哪了」这类问句另由 _WRITE_AS_QUESTION 排除
_SCHEDULE_VERB = re.compile(
    r"(排|安排|分配|分|调|调剂|挪|换|改)(到|进|在|成)|排一下|安排一下|重排|排上"
)
_WRITE_AS_QUESTION = re.compile(
    r"(排|安排|分配|分|调|调剂|挪|换|改)(到|进|在|成)了?(几点|哪|什么时候|啥时候|几号)"
)
_HOW_TO = re.compile(r"怎么|如何|流程|规则|步骤")
_SELF = re.compile(
    r"我的(面试|安排|简历|申请|进度|状态|结果|场次)|我(什么时候|几点|哪天)面试|我被(排|安排|分配)"
)
_SELF_TARGET = re.compile(r"我的面试|(我|帮我)(想|要)?把(我的)?面试")
_RULES = re.compile(
    r"流程|规则|怎么|如何|要求|注意事项|准备什么|需要准备|是什么|做什么|干什么|介绍|部门|报名|截止|多久|社团"
)
_LOOKUP_VERB = re.compile(r"看看|看一下|查查|查一下|瞧瞧")
_DATE = re.compile(r"周[一二三四五六日天]|星期|\d+月\d+[日号]|今天|明天|后天|上午|下午|晚上|下周")
_ADMIN_TOPICS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"空位|空的|场次|余量|剩余|名额|空档"), "find_available_sessions"),
    (re.compile(r"改期申请"), "list_reschedule_requests"),
    (re.compile(r"未分配|没分配|没有分配|没排|未排|待调剂"), "list_unassigned"),
    (re.compile(r"统计|一共|总共|投递|人数|多少份"), "get_recruit_statistics"),
    (re.compile(r"简历"), "search_resumes"),
    (re.compile(r"候选人(卡片|概况|情况)"), "get_candidate_card"),
)

_CN_DIGITS = dict(zip("一二三四五六七八九十", range(1, 11), strict=True)) | {"两": 2}
_ORDINAL = re.compile(r"第([一二两三四五六七八九十]|\d+)(个|场|条|项)?")
_LAST = re.compile(r"最后(一个|那个|一场|一条)")
_FILLER = re.compile(r"那就|就|选|看看|看一下|要|用|吧|呢|啊|呀|了|帮我|好的|好|行|场次|可以")
_DEMONSTRATIVES = frozenset(
    {"这个", "那个", "这一个", "那一个", "这场", "那场", "上面那个", "这条", "那条"}
)

# 目标 ID:先去掉日期与时刻,免得「9 月 11 日」被当成 11 号简历
_DATE_OR_TIME = re.compile(r"\d+月\d+[日号]|\d+[:点]\d*")
_RESUME_IDS = re.compile(r"(?:简历|候选人|同学)(?:编号|id)?#?(\d+)|#(\d+)|(\d+)号(?!申请|改期)")
_ID_PAIR = re.compile(r"(\d+)(?:和|与|跟|、)(\d+)号?")
_REQUEST_IDS = re.compile(r"(\d+)号的?(?:改期)?申请|申请#?(\d+)|#(\d+)")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).lower()


def _strip_negation(clause: str) -> tuple[str, bool]:
    """剥掉分句开头的放弃/改口(可叠加,如「算了不排了」);返回 (剩余文本, 是否改口)。"""
    negated = False
    while True:
        m = _NEGATION_HEAD.match(clause)
        if not m or not m.group(0):
            return clause, negated
        negated = True
        clause = clause[m.end() :]


def _classify_clause(clause: str) -> _Clause:
    """单个分句 → 标签与建议工具;无法归类时 label 为 None。"""
    if _RESTRICTED.search(clause):
        return _Clause(restricted=True)
    if _RESCHEDULE.search(clause) and _APPROVE_VERB.search(clause):
        return _Clause(LABEL_RESCHEDULE, set(_RESCHEDULE_TOOLS))
    if _SCHEDULE_VERB.search(clause) and not _WRITE_AS_QUESTION.search(clause):
        if _HOW_TO.search(clause):
            return _Clause(uncertain=True)
        return _Clause(LABEL_SCHEDULE, set(_SCHEDULE_TOOLS))
    if _SELF.search(clause):
        return _Clause(LABEL_SELF, {"get_open_cycle", "get_my_interview"})
    tools = {tool for pattern, tool in _ADMIN_TOPICS if pattern.search(clause)}
    if "get_recruit_statistics" in tools:
        tools.discard("search_resumes")  # 「一共多少份简历」问的是汇总数,不是检索清单
    if not tools and _LOOKUP_VERB.search(clause) and _DATE.search(clause):
        tools.add("find_available_sessions")  # 排期语境里「看看周六」看的是场次
    if tools:
        return _Clause(LABEL_ADMIN, tools | {"get_open_cycle"})
    if _RULES.search(clause):
        return _Clause(LABEL_RULES, {"search_knowledge"})
    return _Clause()


def _reference(text: str) -> tuple[str, int | None] | None:
    """本轮是否在指代上一轮的选项:("ordinal", n) / ("last", None) / ("demonstrative", None)。"""
    m = _ORDINAL.search(text)
    if m:
        raw = m.group(1)
        return "ordinal", int(raw) if raw.isdigit() else _CN_DIGITS[raw]
    if _LAST.search(text):
        return "last", None
    if _FILLER.sub("", re.sub(r"[^\w]", "", text)) in _DEMONSTRATIVES:
        return "demonstrative", None
    return None


def _target_ids(text: str, label: str) -> set[str]:
    bare = _DATE_OR_TIME.sub("", text)
    pattern = _REQUEST_IDS if label == LABEL_RESCHEDULE else _RESUME_IDS
    ids = {g for m in pattern.finditer(bare) for g in m.groups() if g}
    if label == LABEL_SCHEDULE:
        ids |= {g for m in _ID_PAIR.finditer(bare) for g in m.groups()}
    return ids


def _ordered(items: Iterable[str], order: Sequence[str]) -> tuple[str, ...]:
    present = set(items)
    return tuple(x for x in order if x in present)


def classify_turn(
    message: str,
    *,
    prior_options: Sequence[PriorOption] = (),
    cancelled_pending: bool = False,
) -> TurnIntent:
    """判断本轮想办什么。纯函数,不调模型、不读库。

    Args:
        message: 用户本轮原文。
        prior_options: 上一轮工具结果里列出的可选项(见 prior_options_from_messages);
            只有它非空时,「第二个」「那个」才会被解析成具体对象。
        cancelled_pending: 本轮开始前是否刚因新消息取消了一张待确认卡片。
    """
    text = _normalize(message)
    flags: set[str] = set()
    evidence: set[str] = set()
    if cancelled_pending:
        evidence.add(EVIDENCE_PENDING_CANCELLED)

    clauses: list[_Clause] = []
    for raw in _CLAUSE_SPLIT.split(text):
        rest, negated = _strip_negation(raw)
        if negated:
            flags.add(FLAG_NEGATION)
        if rest:
            clauses.append(_classify_clause(rest))

    if any(c.restricted for c in clauses):
        flags.add(FLAG_RESTRICTED_SCORE)
    if any(c.uncertain for c in clauses):
        return TurnIntent(
            LABEL_OTHER,
            flags=_ordered(flags & {FLAG_NEGATION, FLAG_RESTRICTED_SCORE}, _FLAG_ORDER),
            evidence=_ordered(evidence | {EVIDENCE_KEYWORD}, _EVIDENCE_ORDER),
            uncertain=True,
        )

    labelled = [c for c in clauses if c.label]
    if labelled:
        evidence.add(EVIDENCE_KEYWORD)
    distinct = {(c.label, frozenset(c.tools)) for c in labelled}
    if len(distinct) > 1:
        flags.add(FLAG_COMPOUND)
    present = {c.label for c in labelled}
    label = next((lb for lb in _LABEL_PRIORITY if lb in present), LABEL_OTHER)
    tools: set[str] = set().union(*(c.tools for c in labelled)) if labelled else set()

    chosen: PriorOption | None = None
    chosen_ordinal: int | None = None
    reference = _reference(text)
    if reference is not None:
        flags.add(FLAG_PRIOR_REFERENCE)
        chosen, chosen_ordinal, ref_flag = _resolve(reference, prior_options)
        if ref_flag:
            flags.add(ref_flag)
        if prior_options:
            evidence.add(EVIDENCE_PRIOR_OPTIONS)
            if label == LABEL_OTHER:
                label = _OPTION_LABEL[prior_options[0].kind]
                tools |= _label_tools(label)
    elif label in _WRITE_LABELS:
        ids = _target_ids(text, label)
        if len(ids) > 1:
            flags.add(FLAG_MULTIPLE_TARGETS)
        elif not ids and not (label == LABEL_SCHEDULE and _SELF_TARGET.search(text)):
            flags.add(FLAG_MISSING_TARGET)

    return TurnIntent(
        label,
        flags=_ordered(flags, _FLAG_ORDER),
        evidence=_ordered(evidence, _EVIDENCE_ORDER),
        suggested_tools=_ordered(tools, _TOOL_ORDER),
        chosen_option=chosen,
        chosen_ordinal=chosen_ordinal,
    )


def _label_tools(label: str) -> set[str]:
    return set(_SCHEDULE_TOOLS if label == LABEL_SCHEDULE else _RESCHEDULE_TOOLS)


def _resolve(
    reference: tuple[str, int | None], options: Sequence[PriorOption]
) -> tuple[PriorOption | None, int | None, str | None]:
    """指代 → (选中项, 第几项, 需要追加的标记)。选项缺失或越界一律当作对象不明。"""
    kind, n = reference
    if not options:
        return None, None, FLAG_MISSING_TARGET
    if kind == "last":
        return options[-1], len(options), None
    if kind == "demonstrative":
        return (options[0], 1, None) if len(options) == 1 else (None, None, FLAG_MULTIPLE_TARGETS)
    if n is not None and 1 <= n <= len(options):
        return options[n - 1], n, None
    return None, None, FLAG_MISSING_TARGET


# ── 上一轮的结构化选项 ───────────────────────────────────────────────────

_DATA_ZONE = re.compile(r'^<data source="[^"]*">\n(.*)\n</data>', re.DOTALL)
_SAFE_DISPLAY = re.compile(r"[^0-9:\-]")


def _tool_payload(content: Any) -> Any:
    """工具消息内容 → 信封里的 data;不是预期形状就返回 None。

    工具返回在装配层被包成 `<data source=…>{信封 JSON}</data>`(security/injection_guard.py),
    信封形如 {"status": "ok", "data": …}(tools/envelope.py)。
    """
    if not isinstance(content, str):
        return None
    m = _DATA_ZONE.match(content)
    try:
        envelope = json.loads(m.group(1) if m else content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(envelope, dict) or envelope.get("status") != "ok":
        return None
    return envelope.get("data")


def _options_of(kind: str, data: Any) -> tuple[PriorOption, ...]:
    if not isinstance(data, list):
        return ()
    options: list[PriorOption] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if kind == OPTION_SESSION and isinstance(item.get("sessionId"), int):
            # 只取日期与时刻,且滤成数字与分隔符:地点等自由文本不进 system prompt
            when = " ".join(
                _SAFE_DISPLAY.sub("", str(item.get(k) or "")) for k in ("date", "start")
            ).strip()
            end = _SAFE_DISPLAY.sub("", str(item.get("end") or ""))
            span = f"{when}-{end}" if when and end else when
            display = f"场次 #{item['sessionId']}" + (f"({span})" if span else "")
            options.append(PriorOption(kind, item["sessionId"], display))
        elif kind == OPTION_RESCHEDULE_REQUEST and isinstance(item.get("requestId"), int):
            options.append(PriorOption(kind, item["requestId"], f"改期申请 #{item['requestId']}"))
    return tuple(options)


def prior_options_from_messages(messages: Sequence[Any]) -> tuple[PriorOption, ...]:
    """会话状态里「上一轮」最后一次列出的选项;没有则空。

    上一轮 = 最后一条用户消息之后的消息(调用时本轮消息还没进状态)。只认场次与改期申请
    两类工具结果;更早轮次列过的选项不算——选项可能已经变了,应该重新查。
    """
    last_human = max(
        (i for i, m in enumerate(messages) if getattr(m, "type", None) == "human"), default=-1
    )
    for m in reversed(messages[last_human + 1 :]):
        if getattr(m, "type", None) != "tool":
            continue
        kind = _OPTION_TOOLS.get(getattr(m, "name", "") or "")
        if kind:
            return _options_of(kind, _tool_payload(m.content))
    return ()


# ── 提示文本 ────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _hint_sections() -> dict[str, str]:
    """模板文件按「## 键」切段。"""
    sections: dict[str, str] = {}
    key: str | None = None
    lines: list[str] = []
    for line in load_prompt(HINT_PROMPT_FILE).splitlines():
        if line.startswith("## "):
            if key:
                sections[key] = "\n".join(lines).strip()
            key, lines = line[3:].strip(), []
        else:
            lines.append(line)
    if key:
        sections[key] = "\n".join(lines).strip()
    return sections


def hint_prompt_version() -> str:
    return load_prompt_meta(HINT_PROMPT_FILE).get("version", "")


def _names(tools: Iterable[str]) -> str:
    return "、".join(_ordered(tools, _TOOL_ORDER))


def _label_line(intent: TurnIntent, assembled: set[str]) -> str | None:
    s = _hint_sections()
    suggested = set(intent.suggested_tools) & assembled
    lookups = suggested - _WRITE_TOOL_NAMES
    writes = suggested & _WRITE_TOOL_NAMES
    if intent.label in (LABEL_RULES, LABEL_SELF, LABEL_ADMIN):
        return s[intent.label].format(tools=_names(lookups)) if lookups else s["no_tool"]
    if intent.label == LABEL_SCHEDULE:
        if not writes:
            fallback = assembled & {"search_knowledge", "get_my_interview"}
            if not fallback:
                return s["no_tool"]
            return s["schedule_interview_no_write"].format(tools=_names(fallback))
        if intent.chosen_option is not None:
            return s["schedule_interview_chosen"].format(write_tools=_names(writes))
        return s["schedule_interview"].format(
            lookup_tools=_names(lookups) or _names(writes), write_tools=_names(writes)
        )
    if intent.label == LABEL_RESCHEDULE:
        if not writes:
            return s["handle_reschedule_no_write"]
        return s["handle_reschedule"].format(
            lookup_tools=_names(lookups) or _names(writes), write_tools=_names(writes)
        )
    return None


def render_hint(intent: TurnIntent, tool_names: Iterable[str]) -> str:
    """意图 → 拼在 system prompt 末尾的提示段;不确定或没什么可提示时为空串。

    tool_names 是本会话已装配的工具;提示只点名其中的工具,没有对应工具时改用
    「不能办」的说法,绝不暗示存在没装配的能力。
    """
    if intent.uncertain:
        return ""
    s = _hint_sections()
    lines: list[str] = []
    label_line = _label_line(intent, set(tool_names))
    if label_line and FLAG_RESTRICTED_SCORE not in intent.flags:
        lines.append(label_line)
    for flag in intent.flags:
        if flag == FLAG_PRIOR_REFERENCE:
            if intent.chosen_option is not None:
                lines.append(
                    s["flag_prior_reference_resolved"].format(
                        ordinal=intent.chosen_ordinal, option=intent.chosen_option.display
                    )
                )
            continue
        lines.append(s[f"flag_{flag}"])
    if not lines:
        return ""
    return "\n\n" + s["header"] + "\n" + "\n".join(f"- {line}" for line in lines)


def mentioned_tools(hint: str) -> set[str]:
    """提示文本里点名了哪些工具(供「只点名已装配工具」的不变量检查)。"""
    return {name for name in _TOOL_ORDER if name in hint}
