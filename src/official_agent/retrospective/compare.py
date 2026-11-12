"""跨周期对比:给一份快照的每个指标附上基线周期的值与差值。纯函数,零 IO。

差值由代码算好再交出去——「比上一届多了 18%」这种话,读的人(以及之后读快照的
模型)拿到的是现成的数,不是两个数自己去减。

差值不落库,读取时现算:快照只记本周期的事实,同一份快照可以对着不同的基线读,
基线周期重新采集后也不会留下过期的差值。
"""

from __future__ import annotations

import copy
from typing import Any

from official_agent.retrospective.guard import UNIT_RATIO


class SchemaMismatchError(ValueError):
    """两份快照的指标定义版本不同,不能对比。

    口径变了之后同一个 id 下的数含义不同,硬比出来的差值是错的,所以拒绝而不是照算。
    """


def with_baseline(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """返回 current 的副本,每个指标多出 prev / delta / delta_ratio 三项。

    - prev:基线快照里同 id 指标的值;基线里没有,或那边被抑制,则为 None
    - delta:本期值 − prev;任一侧没有值(含被抑制)则为 None
    - delta_ratio:delta ÷ prev,保留 4 位小数;比率类指标不给(两个百分比相除
      容易读错,看 delta 的百分点差即可),prev 为 0 时也不给

    Raises:
        SchemaMismatchError: 两份快照的 schema 不同。
    """
    if current.get("schema") != baseline.get("schema"):
        raise SchemaMismatchError(
            f"指标定义版本不同({current.get('schema')} 对 {baseline.get('schema')}),不能对比"
        )
    baseline_values = {m["id"]: m.get("value") for m in baseline.get("metrics", [])}
    result = copy.deepcopy(current)
    for metric in result.get("metrics", []):
        prev = baseline_values.get(metric["id"])
        value = metric.get("value")
        delta = None if value is None or prev is None else _tidy(value - prev)
        metric["prev"] = prev
        metric["delta"] = delta
        metric["delta_ratio"] = (
            round(delta / prev, 4)
            if delta is not None and prev and metric.get("unit") != UNIT_RATIO
            else None
        )
    result["baseline"] = {"cycle_id": baseline.get("cycle_id"), "schema": baseline.get("schema")}
    return result


def _tidy(number: float) -> float | int:
    """去掉浮点减法的尾噪(0.30000000000000004 这类);整数原样返回。"""
    return number if isinstance(number, int) else round(number, 4)
