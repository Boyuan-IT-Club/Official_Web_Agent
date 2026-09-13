"""B4:奖项 brief(可注入搜索)+ 无证据兜底基础三维/部门技能题组(#131/#133)。

- SearchProvider 协议:web_search 通道(检查点⑤)未接入前用 NullProvider
  ——查不到即标「不可考,仅以候选人陈述为准」,追问收窄纯过程,绝不编含金量
- 无证据候选:基础三维每维 3-4 深题 + 按部门技能题组(2-4 浅-中) +
  约 15 分钟建议组合(#133)
"""

from __future__ import annotations

from typing import Any, Protocol

from official_agent.evaluation.scoring import FieldText

_AWARD_HINTS = ("award", "reward", "奖")


class SearchProvider(Protocol):
    async def search(self, query: str) -> list[dict[str, Any]]: ...


class NullSearchProvider:
    """无 web_search 通道(检查点⑤):一切奖项都走「不可考」路径。"""

    async def search(self, query: str) -> list[dict[str, Any]]:
        return []


def extract_awards(fields: list[FieldText]) -> list[str]:
    """从简历字段抽奖项条目(字段键/名含「奖」;值按行拆,去掉空行)。"""
    awards: list[str] = []
    for f in fields:
        key = f.field_key.lower()
        if not any(h in key or h in f.title for h in _AWARD_HINTS):
            continue
        for line in f.value.splitlines():
            line = line.strip().lstrip("-*• ")
            if line:
                awards.append(line)
    return awards


async def build_award_brief(provider: SearchProvider, award_title: str) -> dict[str, Any]:
    """奖项背景卡(#131):查得到给背景摘要,查不到标「不可考」。

    两条纪律:#131 明文——查不到绝不编含金量;追问永远收窄到候选人
    本人的作品/角色/复盘(纯过程),不考奖项本身。
    """
    results = await provider.search(f"{award_title} 比赛 主办方 规模")
    if not results:
        return {
            "award": award_title,
            "status": "unverifiable",
            "background": "信息不可考,仅以候选人陈述为准",
            "questions": [
                {
                    "anchor": "guided",
                    "question": f"这个「{award_title}」奖项,你的作品/角色具体是什么?",
                    "sub_prompts": ["评审最看重作品的哪一点?"],
                    "answer_reference": {
                        "strong": "讲清自己的贡献与作品本身,细节可回溯",
                        "acceptable": "能说清角色与过程",
                        "weak": "只谈奖项含金量,说不清自己做了什么",
                    },
                    "evidence": {"path": "", "note": f"奖项:{award_title}(不可考,纯过程追问)"},
                    "time_minutes": 3,
                }
            ],
        }
    background = ";".join(
        str(r.get("snippet") or r.get("background") or "")[:120] for r in results[:3]
    )
    return {
        "award": award_title,
        "status": "verified",
        "background": background,
        "questions": [],
    }


BASE_THREE = [
    {
        "anchor": "guided",
        "dimension": "自我介绍",
        "question": "用两分钟讲一个简历上没写、但最能代表你的经历。",
        "sub_prompts": ["为什么选它?"],
        "time_minutes": 3,
    },
    {
        "anchor": "guided",
        "dimension": "个人简介",
        "question": "你简历里的简介,自己最不满意的是哪一句?为什么?",
        "sub_prompts": ["现在重写你会怎么写?"],
        "time_minutes": 3,
    },
    {
        "anchor": "guided",
        "dimension": "加入理由",
        "question": "除了简历上写的加入理由,你预期在社团最可能受挫的是什么?",
        "sub_prompts": ["打算怎么应对?"],
        "time_minutes": 3,
    },
]


def base_three_questions() -> list[dict[str, Any]]:
    """无证据候选的基础三维兜底(每维一题深挖,#133:非占位)。"""
    return [
        {
            "anchor": "guided",
            "question": q["question"],
            "sub_prompts": q["sub_prompts"],
            "answer_reference": {
                "strong": "有具体事例与反思",
                "acceptable": "能自圆其说",
                "weak": "空泛套话",
            },
            "evidence": {"path": "", "note": f"基础三维:{q['dimension']}"},
            "time_minutes": q["time_minutes"],
        }
        for q in BASE_THREE
    ]


def suggest_plan(questions: list[dict[str, Any]], budget_minutes: int = 15) -> list[int]:
    """约 15 分钟建议组合:按题均分预算,超出预算的题标记 0(建议跳过)。"""
    if not questions:
        return []
    per = max(2, budget_minutes // len(questions))
    plan = []
    remaining = budget_minutes
    for q in questions:
        t = min(q.get("time_minutes") or per, remaining)
        plan.append(t)
        remaining -= t
    return plan
