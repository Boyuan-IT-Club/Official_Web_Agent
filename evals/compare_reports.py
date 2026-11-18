"""两组 eval 报告的逐条对照(意图提示 off 对 hint 等)。

用法:
  uv run python evals/compare_reports.py --before off.json --after hint.json
  uv run python evals/compare_reports.py --before off1.json off2.json off3.json \
      --after hint1.json hint2.json hint3.json --out evals/reports/intent_compare.md

每侧可给多份同条件的重复运行,逐条按多数定结论。输出 Markdown(缺省打印到终端)。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from official_agent.evals.compare import compare_reports, load_reports, render


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="两组 eval 报告的逐条对照")
    parser.add_argument("--before", type=Path, nargs="+", required=True)
    parser.add_argument("--after", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, help="Markdown 输出路径")
    args = parser.parse_args(argv)
    text = render(compare_reports(load_reports(args.before), load_reports(args.after)))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"对照报告 → {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
