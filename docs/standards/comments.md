# 注释规范

> **何时读**：写任何代码时；评审他人/AI 生成代码时。
> **基准线**：注释描述**抽象**，不复述实现（Ousterhout）。Code tells you how, comments tell you why。坏注释比没注释更糟。
> **语言政策**：注释用中文，命名用英文。

## 五级注释：必须写 / 可选 / 禁止

| 层级 | 必须写 | 可选 | 禁止 |
|---|---|---|---|
| 模块头 | 一句话职责 + 设计约束/边界（为什么存在） | 使用示例 | 复述文件名/目录 |
| 类 | 公共类：实例代表什么、不变量、并发约定 | 零值/空态语义 | 「这是一个 XX 类」式废话 |
| 方法 | 公共 API；有副作用；会抛异常；行为非显而易见 | trivial getter | 复述签名/类型已说明的信息 |
| 块（函数内） | 魔数与公式来源、workaround、非显而易见算法、外部规范链接 | 分段导航（拆函数优先） | changelog、复述下一段代码 |
| 行内 | 非显而易见行的行尾 why | — | 解释 what（`# 自增 i`） |

## 接口注释优先（写不出注释 = 设计有问题）

公共类/函数**先写注释再写实现**：一句话说清它是什么 + 参数/返回/异常/副作用/前置条件，让调用者不读实现就会用。写不出一句话的抽象，改设计而不是硬写注释。实现细节的注释只补「为什么这么做」，不补「做了什么」。

## 必须写注释的典型场景

- 魔数与公式：写来源或推导（`# Rec.601 亮度系数`，而非「红色权重」）。
- workaround：写触发条件 + 摘除条件。
- 不变量与边界标记：约束在此，改前先想（如「先到者 messageId」语义）。
- 空 catch、类型忽略、非空断言：写为什么安全/为什么吞。
- TODO/FIXME：唯一允许的任务引用格式 `# TODO(zewang): <说明> - <触发条件或日期>`；不许写「以后优化」。

## 泄露红线（AI 生成代码逐条自查）

每条注释必须**自包含**：一年后的人类无需任何会话上下文就能读懂。

1. 禁会话代号与任务编号：issue 号（`#164`）、`ticket 3.2`、`评审第 3 条`。
2. 禁迭代/模块黑话：`M6`、`SEC-07`、`R1/R2/R3` 这类外部读者无从解码的代号——把编号背后的知识直接写出来。仓库内可稳定查到的引用（`ADR-xxxx`、`docs/` 路径）可以留，但要写清它是什么，别只挂编号。
3. 禁 changelog 式：`新增了 X`、`根据评审意见修复`——改动历史归 git commit message。
4. 禁相对叙述：`原本…现在…`、`本次改动后`——注释只描述代码**当下**状态。

**判据**：注释里每个标识符，读者能否**仅凭这个仓库**找到它？能则留，不能则删。

交付前机械自查（出现即处理，不许进库）：

```bash
git diff -U0 | grep -nE "print\(.*debug|TODO: ?$|FIXME: ?$"
git diff | grep -nE "#[0-9]{2,3}|M[0-9]+ |(SEC|GRA|INF|EVA|TOOL|MEM|OBS|COP)-[0-9]+|评审|本次(改动|修复)|原来|改为"  # 泄露候选，人工逐条确认
```

## 模板

Python（Google 风格 docstring）：

```python
"""一句话职责（what，不是 how）。

设计约束 / 不变量 / 并发安全 / 适用场景。
Args/Returns/Raises 各节只写类型签名表达不了的约束与语义。
"""
# TODO(zewang): 说明 - 触发条件
```

TypeScript：

```ts
/** 模块：一句话职责；设计约束：为什么存在/边界在哪。 */

/**
 * 一句话抽象。
 * @remarks 不变量 / 并发安全 / 适用场景。
 * @param arr - 语义与约束（类型签名已说明的不再重复）。
 * @returns 语义。
 * @throws Error 何时抛。
 */
```

## 与 Clean Code 的取舍

《Clean Code》说注释是「失败的表达」，指的是复述型注释；该教条被引申为「不写注释」已被广泛批评（qntm 等）。本规范立场：能靠命名与重构表达的就重构；外部约束、魔数来源、非显而易见的 why 必须注释。复杂逻辑没有注释视为缺陷，评审按不合格处理。

## 来源

- [Ousterhout《A Philosophy of Software Design》书评](https://blog.pragmaticengineer.com/a-philosophy-of-software-design/)（interface vs implementation comments、先写注释）
- [Go doc comments](https://go.dev/doc/comment) / [Rust API Guidelines: Documentation](https://rust-lang.github.io/api-guidelines/documentation.html) / [Google PyGuide 注释节](https://google.github.io/styleguide/pyguide.html) / [PEP 257](https://peps.python.org/pep-0257/) / [TSDoc](https://tsdoc.org)
- [SO Blog: Best Practices for Writing Code Comments](https://stackoverflow.blog/2021/12/23/best-practices-for-writing-code-comments/) / [Atwood: Code Tells You How](https://blog.codinghorror.com/code-tells-you-how-comments-tell-you-why/) / [Linux kernel coding-style 第 8 章](https://www.kernel.org/doc/html/latest/process/coding-style.html)
- 反方平衡：[qntm 对 Clean Code 的批评](https://qntm.org/clean)
