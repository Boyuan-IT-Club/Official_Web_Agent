# Python 惯用法补充

> **何时读**：写本仓 Python 代码时（通用原则见 [readability.md](readability.md) 与 [comments.md](comments.md)）。
> 只放 Python **独有**的惯用法和易踩坑。机械规则交给 Ruff（本仓 `line-length = 100`，已配置）。

## 一、命名
- 函数/变量 `snake_case`；类 `PascalCase`；常量 `UPPER_SNAKE`；模块全小写短名。
- 私有 `_` 前缀（`_internal`）；名称碰撞用单下划线避免（`class_`）；魔法方法 `__dunder__`。
- 布尔 `is/has/can`：`is_active`、`has_data`。

## 二、类型提示
- 公共函数/方法必须加 type hints（PEP 484）——注解是契约，不是摆设。
- 运行时校验用 **Pydantic**（v2 用 `BaseModel`）；FastAPI/LangGraph 都依赖它。
- `Optional[T]` = `T | None`（3.10+）；避免隐式 `None` 返回，显式标注 `-> None`。
- 复杂结构用 `TypedDict`（字典形状）或 `dataclass`，别用裸 dict 传业务数据。

## 三、惯用法
- **推导式**优先于 map/filter（可读），但别嵌套过深——三层以上拆 for 循环。
- **EAFP**（Easier to Ask Forgiveness than Permission）：try/catch 优于 if 判断类型。
- **context manager**（`with`）管资源：文件、锁、连接。
- **`dataclass`** 做数据容器（`@dataclass(frozen=True)` 不可变）。
- **`pathlib.Path`** 代替 `os.path` 字符串拼接。
- **`f-string`** 格式化；模块内用 `logging`，`print()` 不用于调试（面向用户的 CLI 输出用 rich `console.print()`）。

## 四、易踩坑
- **可变默认参数**：`def f(xs=[])` 会跨调用共享！用 `xs=None` + 函数内 `xs = xs or []`。
- **`==` vs `is`**：`==` 比值，`is` 比身份（`is None` / `is not None` 用 `is`）。
- **闭包变量延迟绑定**：循环里创建 lambda 会捕获最后的值——用默认参数固定 `lambda x=x: ...`。
- **异步**：`async def` 内才能 `await`；`asyncio.run()` 是入口；**`async def` 里直接调同步阻塞函数（如同步 DB 驱动）会卡住整个事件循环**——要么用异步客户端，要么 `asyncio.to_thread` 包裹。
- **`asyncio.create_task` 的返回值要保存引用**，否则任务可能被垃圾回收中途取消（官方文档明确警告）。
- **模块级 `assert` 做运行时自检会被 `python -O` 剥离**——防线要靠显式 `if/raise`。

## 五、LangGraph（本仓栈）
- **State 用 `TypedDict`** 显式定义状态结构。
- **Node 是纯函数**：`(state) -> dict`，返回要更新的字段，不做副作用。
- **边**：普通边顺序流转；条件边用函数返回下一节点名。
- **checkpoint**：开发用 `MemorySaver`，生产用持久化（本仓统一 Postgres，见 ADR-0007）。
- 人机协作用 `interrupt`；子图用 `StateGraph` 嵌套。
- 工具定义：`@tool` 装饰器 + docstring（给 LLM 看）+ type hints（给校验器看）。

## 六、权威源
- [PEP 8](https://peps.python.org/pep-0008/) — 风格基础
- [PEP 20](https://peps.python.org/pep-0020/) — The Zen of Python（`import this`）
- [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)
- 总原则：**显式优于隐式，可读性优先，用标准库能解决就不引依赖**。
