"""PII 确定性脱敏(#68 出口契约;共享层,#115 review P0-3)。

## 出口契约表(#164,SEC-08/#68 拍板:枚举全部出边界出口,缺一即红线)

1. 工具返回层:mask_pii_deep(P0-3)+ 字段键级姓名掩(readonly 工具返回全量)
2. conversation_log:mask_pii(state/conversation.write_conversation,已做)
3. trace 上报(observability):上报前 deep 掩;完整简历原文类 payload **禁入**
   (observability.py 红线注释同步)
4. 审计 action:写入口过 mask_pii_deep(state/audit.write_audit)
5. SSE delta(回复出口):输出守卫 mask_pii_output,检出→掩码替换**照发**
   (cli/web 回复出口;#159 守卫契约「回复出口」同位)
6. checkpointer 挂起载荷:summary 先 mask_pii(require_confirmation)+
   挂起态 24h TTL 清理(state/pg.purge_expired_interrupts)
7. 评分/出题模型入口:evaluation runner 在 _run_job 对简历字段统一
   mask_pii_deep 后才进 run_evaluation/run_bundle(#176;命中打
   eval_pii_exit 安全日志)

## 规则表(#164 扩展)

- 手机号留前 3 后 4;身份证留前 4 后 4;QQ 全掩(5-11 位词边界);
- **邮箱**进文本正则(全掩);
- **姓名不进文本正则**(误杀),按**结构化字段键白名单**(name/real_name 等)
  在 mask_pii_deep 键级掩;
- 负例基线:年份/日期/小数成绩不被误掩(测试钉住);
- **12-17 位纯数字全掩**(#176 评审:学号/准考证号原先漏掩)——纯数字
  长单号随之被掩,出向脱敏宁滥勿漏,有意取舍(测试同步更新)。

## 占位符映射(#159/#160 决议):**不建还原通道**

规则确定性正则 → 影子运行(EVA-09)对原始简历独立重掩后对比,天然对齐;
面试官要真数据回后端原接口,Agent 永不还原;占位符外泄由输出守卫兜底。
"""

from __future__ import annotations

import logging
import re
from typing import Any

GUARD_NAME_PII_OUTPUT = "pii_output"

_MASK_RULES: list[tuple[re.Pattern[str], str]] = [
    # 手机号(11 位,1 开头):留前 3 后 4
    (re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)"), r"\1****\2"),
    # 身份证(18 位,校验位可为 X/x,#176 评审:尾号 X 原先漏掩):留前 4 后 3
    (re.compile(r"(?<!\d)(\d{4})\d{10}(\d{3}[Xx\d])(?!\d)"), r"\1**********\2"),
    # 邮箱:全掩(#164)
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[邮箱]"),
    # 12-17 位纯数字(#176 评审:12 位学号/准考证号原先三条规则全不命中):全掩
    (re.compile(r"(?<!\d)\d{12,17}(?!\d)"), "************"),
    # QQ(5-11 位纯数字,词边界):全掩——放最后,避免吞掉手机/身份证已掩产物
    (re.compile(r"(?<!\d)\d{5,11}(?!\d)"), "*****"),
]

#: 姓名类字段键白名单(#164:姓名不进文本正则,键级掩)
_NAME_KEYS = frozenset({"name", "real_name", "student_name", "candidate_name", "姓名"})


def mask_pii(text: str) -> str:
    """确定性 PII 脱敏:手机号留前 3 后 4、身份证留前 4 后 4、邮箱/QQ 全掩。

    无匹配原样返回。负例基线:纯数字串(年份「2024」、日期、会话 id)不受
    影响——QQ 规则限 5-11 位且词边界;单号含连字符/字母亦不匹配。
    """
    masked = text
    for pattern, repl in _MASK_RULES:
        masked = pattern.sub(repl, masked)
    return masked


def mask_pii_deep(payload: Any) -> Any:
    """递归脱敏 dict/list 结构里的全部字符串叶子(工具返回层/审计写入口)。

    键级姓名掩:值为字符串且键在 _NAME_KEYS(name/real_name 等)→ 整值掩为
    〔姓名〕(姓名不进文本正则,#164)。"""
    if isinstance(payload, str):
        return mask_pii(payload)
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            if isinstance(k, str) and k.lower() in _NAME_KEYS and isinstance(v, str) and v:
                out[k] = "〔姓名〕"
            else:
                out[k] = mask_pii_deep(v)
        return out
    if isinstance(payload, list):
        return [mask_pii_deep(v) for v in payload]
    return payload


def mask_pii_output(text: str) -> tuple[str, dict | None]:
    """回复出口守卫(#159 契约「输出守卫」;#164 §3):检出 PII → 掩码替换照发。

    与确定性拦截(不发送)不同:PII 检出**替换后照发**,不拦截不重试
    (#160 决议 §3)。返回 (最终文本, trace);trace 仅命中时非 None:
    {guard_name: pii_output, verdict: "masked", reason: 命中规则名}。"""
    if not text:
        return text, None
    masked = mask_pii(text)
    if masked == text:
        return text, None
    rule = _matched_rule(text) or "pii_pattern"
    trace = {
        "guard_name": GUARD_NAME_PII_OUTPUT,
        "verdict": "masked",
        "reason": f"命中规则:{rule}",
    }
    logging.getLogger(__name__).warning(
        "guard_event guard_name=%s verdict=masked rule=%s", GUARD_NAME_PII_OUTPUT, rule
    )
    return masked, trace


def _matched_rule(text: str) -> str:
    for name, (pattern, _repl) in {
        "phone": _MASK_RULES[0],
        "id_card": _MASK_RULES[1],
        "email": _MASK_RULES[2],
        "qq": _MASK_RULES[3],
    }.items():
        if pattern.search(text):
            return name
    return ""


class ReplyPiiMasker:
    """流式回复的逐块 PII 掩码器(#164):尾部缓冲抗跨块切分。

    feed(chunk) 返回可安全下发的文本(保留 32 字符尾缓冲,防手机号/邮箱被
    chunk 边界切开漏掩);finish() 冲洗残余。掩码幂等(已掩文本重掩不变)。"""

    _TAIL = 32

    def __init__(self) -> None:
        self.buffer = ""

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self.buffer += chunk
        masked = mask_pii(self.buffer)
        keep = min(len(masked), self._TAIL)
        self.buffer = masked[-keep:]
        return masked[:-keep]

    def finish(self) -> str:
        tail = mask_pii(self.buffer)
        self.buffer = ""
        return tail
