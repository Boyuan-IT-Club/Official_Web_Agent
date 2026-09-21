# 可读性规范（命名 / 函数）

> **何时读**：写或改任何代码时（与 [comments.md](comments.md) 搭配）。
> **取向**：可读性 > 简洁性，维护性 > 聪明代码。机械性规则（行数、格式）交 linter，人只管原则。

## 命名

- **揭示意图**：名字表达「做什么」，`check_password()` 只做检查，别顺便 `init_session()`。
- **长度与作用域成正比**：局部可短（循环 `i/j/k` 可接受），跨模块必须描述充分（Linux kernel 原则）。
- **可读可念可搜索**：`customer` 不用 `cust`；魔法数字→命名常量，名字让它可被 grep。
- **禁假区分**：`a1/a2`、`data1/data2`、`info/data`；禁噪音词（`Info/Data/Manager` 无信息量）。
- **一致动词**：同一类操作全库用一个词（要 `get` 都 `get`，要 `fetch` 都 `fetch`）。
- **布尔**：`is/has/can/should` 开头，正面语义（禁 `is_not_ready` 双重否定）。
- **禁类型编码**（匈牙利命名）；语言惯例定大小写风格；命名用英文。

## 函数

- **单一职责**：一个函数只做一件事，名字说做什么就只做什么。
- **快路径扁平**：guard clause（早返回）走主流程，缩进 ≤3 层；嵌套超了就是拆分信号。**不用硬行数当目标**——长到要滚动、或需要分段注释才能读，就拆（那几段注释本身就该是函数名）。
- **参数少**：多了用 keyword-only 参数/options object；避免魔法布尔参数（`render_as_admin()` 优于 `render(True)`）。
- **副作用显式**：纯函数优先；有副作用在名字或接口注释里声明。
- **局部变量克制**：一个函数里 5-10 个以上就考虑拆（kernel 参考值）。

## 错误处理

已独立成文：[error-handling.md](error-handling.md)——错误三类、分层传播、外部调用（超时/重试三问）、禁兜底红线。

## 日志

已独立成文：[observability.md](observability.md)——日志四律、各层记什么、request_id 串联、三支柱起步最小集。

## 检验问句

- 资深工程师会觉得这段过度设计或太绕吗？会 → 简化（Karpathy）。
- 一年后回到这段代码，能靠名字+注释读懂吗？不能 → 现在补。

## 来源

- [Linux kernel coding-style](https://www.kernel.org/doc/html/latest/process/coding-style.html)（作用域定长度、局部变量数、注释密度）
- [Google TS Style Guide](https://google.github.io/styleguide/tsguide.html) / [Google PyGuide](https://google.github.io/styleguide/pyguide.html)（命名与注释的机械规则交给 linter 的立场）
- Robert C. Martin《Clean Code》命名/函数章 + [qntm 的平衡批评](https://qntm.org/clean)
