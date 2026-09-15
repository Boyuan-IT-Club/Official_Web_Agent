"""调查子图路由:纯函数决策层。

- 路由四出口:有仓可读→deep_dive;无仓但简历有料(技术栈或项目内容)
  →cv_dive;无仓且项目栏为空→skip;其余→guided(通用引导题兜底)
- 全部零 IO(IO 在 github_client/LLM 节点),决策可单测
"""

from __future__ import annotations

import re
from typing import Literal

# 项目维字段键的语义匹配(周期配置驱动,键名不稳定,按 label/键名猜)
_PROJECT_HINTS = ("project", "项目")
# 仓库 URL:github.com/owner/repo。左断言防 "mygithub.com" 伪站;
# repo 段含 . 但捕获后剥尾随标点("repo." 句点收尾是常见书写)
_REPO_URL = re.compile(
    r"(?<![A-Za-z0-9-])github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)

#: 占位式表述与无信息量虚词:整栏只有这些 = 没有实质信息。
#: 真实简历里出现过「目前没有做过什么项目」这类写法。
#:
#: 用**单条正则**匹配而非逐条 str.replace:逐条替换有顺序依赖(先剥「没有」
#: 会把「没有做过什么项目」截成「做过什么项目」而漏判),且被长词吞掉的短词
#: 会变成永不命中的死条目。长词优先保证前缀词不会遮蔽更长的表述。
_PLACEHOLDER_VOCAB = (
    "没有做过什么项目",
    "没做过什么项目",
    "还没有项目经验",
    "暂无项目经验",
    "没有做过项目",
    "没做过项目",
    "暂未参加项目",
    "没有项目经验",
    "未参与项目",
    "没有参加过",
    "没什么项目",
    "尚未参加",
    "项目经验",
    "暂无项目",
    "参加",
    "项目",
    "暂无",
    "没有",
    "还没",
    "尚未",
    "暂未",
    "未参与",
    "没什么",
    "暂时",
    "目前",
    "同上",
    "略",
    "无",
)
_PLACEHOLDER_RE = re.compile(
    "|".join(re.escape(w) for w in sorted(_PLACEHOLDER_VOCAB, key=len, reverse=True))
)
#: 标点与空白:判定剩余是否为「字」时一并剔除
_FILLER_CHARS = str.maketrans("", "", " \t\r\n,.;:!?、。，；：！？…—~·-()()[]【】\"'\"/\\")

# 值得度信号
_SIGNAL_DIRS = ("src/", "app/", "tests/", "test/", "docs/", "server/", "client/")


def _strip_repo(owner: str, repo: str) -> tuple[str, str] | None:
    """清洗单个 owner/repo:剥 .git 与尾随标点;空段返回 None。"""
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    repo = repo.rstrip(".,;:)!?}]")  # 尾随标点是书写标点,不属于仓名
    if not owner or not repo:
        return None
    return owner, repo


def has_substantive(text: str) -> bool:
    """该栏是否有实质内容(非空,且不只是占位式表述)。

    公开给路由编排层:它决定要不要为「项目栏没料」的简历额外调一次技术栈
    抽取——已有实质内容时技术栈不影响结论,那次调用可以省掉。
    """
    return bool((text or "").strip()) and not _is_placeholder_only(text)


def extract_repos(text: str) -> list[tuple[str, str]]:
    """提取文本里**全部** github.com/owner/repo,保序去重。

    旧 extract_repo 只返回首个匹配,第二个仓(如候选简历里的
    myloop-meta)被静默丢弃。调用方据此逐仓深挖。
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for m in _REPO_URL.finditer(text or ""):
        cleaned = _strip_repo(m.group(1), m.group(2))
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def extract_repo(text: str) -> tuple[str, str] | None:
    """从项目文本提取首个 (owner, repo);无 GitHub 链接返回 None。"""
    repos = extract_repos(text)
    return repos[0] if repos else None


def _is_placeholder_only(text: str) -> bool:
    """整栏是否只有占位式表述(「暂无」「目前没有做过什么项目」这类)。

    判法:剥掉虚词与无信息量的填充词后什么都不剩。**宁可判成有料**——
    误判为占位会把候选人打回通用引导题,正是本模块要消灭的退化;
    反过来的代价小得多(顶多多出一组质量平庸的题)。
    """
    remainder = _PLACEHOLDER_RE.sub("", text or "")
    return not remainder.translate(_FILLER_CHARS).strip()


def route_project(
    project_text: str, repo_readable: bool | None, *, tech_count: int = 0
) -> Literal["deep_dive", "cv_dive", "skip", "guided"]:
    """路由判据。repo_readable:None=无仓位置;True/False=有仓且探测结果。

    有仓时行为不变:
    - 有仓 + 可读 → deep_dive
    - 有仓 + 不可读 → guided

    无仓时看简历本身有没有料(tech_count 由调用方用技术栈抽取器算出并注入,
    本函数保持纯函数、零 IO):
    - 有技术栈 或 项目经验有实质内容 → cv_dive(简历深挖,产出带追问链的题组)
    - 项目经验整栏为空 → skip(此维不浪费题,交给兜底组)
    - 项目经验有字但全是占位表述 → guided(通用引导题,不硬凑)

    刻意不用字数阈值:那会误杀「短但具体」的简历。
    """
    if repo_readable is not None:
        return "deep_dive" if repo_readable else "guided"
    if tech_count > 0 or has_substantive(project_text):
        return "cv_dive"
    if not (project_text or "").strip():
        return "skip"
    return "guided"


#: 出题深度档。两档就够:分界就是既有的用户规则(大一很基础,大二基础+稍深入),
#: 再细分没有依据。``GradeBand`` 是这一路取值的单一事实源,避免裸字面量散落。
GradeBand = Literal["freshman", "standard"]

#: 缺年级 / 认不出 / 大二以上 → 这一档。也是引入档位之前的既有行为,
#: 故不涉及年级的调用方行为不变。
DEFAULT_GRADE_BAND: GradeBand = "standard"

#: 大一档的识别标记。周期配置里年级是 select(取值形如「大一」),但学制写法
#: 五花八门,英文/带空格的写法一并收——识别不到会退回标准档,那是更深的一档,
#: 对大一候选人偏难。
_FRESHMAN_MARKERS = ("大一", "freshman", "year1", "1styear")

#: 链层数区间按档位:**大一短链只问基础层**,标准档沿用既有的 3-5。
#: 上/下限的最终判定在 validate_qbank_v2_group(schema 只留形状上界)。
LAYER_BOUNDS: dict[str, tuple[int, int]] = {"freshman": (1, 2), "standard": (3, 5)}

#: 档位 → 材料里给模型看的话(进 prompt 的**只有档位**,不带年级原文)
GRADE_BAND_LABELS: dict[str, str] = {"freshman": "大一", "standard": "标准"}


def grade_band(grade: str) -> GradeBand:
    """年级 → 出题深度档。缺失 / 无法识别 / 大二以上 → ``standard``。

    只分两档是刻意的:用户规则的分界就是「大一很基础,大二基础+稍深入」,
    再细分没有依据。**缺字段必须有安全默认**——这里返回 standard 而非报错,
    且 standard 正是加档位之前的既有行为,故不涉及年级的调用方行为不变。

    传入的年级原文**不外流**:调用方只把档位标签写进材料,原文既不进 prompt
    也不落日志(后端记录过「年级」栏里存着姓名,这一栏并不干净)。
    """
    compact = "".join((grade or "").split()).casefold()
    return "freshman" if any(m in compact for m in _FRESHMAN_MARKERS) else DEFAULT_GRADE_BAND
