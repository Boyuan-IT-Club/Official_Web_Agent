# 数据对象与映射规范（Mapper / Repo / DTO）

> **何时读**：定义 API 出入参、定义落库对象、写对象转换时。
> **一句话**：对象三分法——entity / request / response；每新增一种对象都要回答「它隔离了什么」，答不上来就不加。

## 三分法（单人/中小项目默认全集）

| 对象 | 是什么 | 定义在哪 | 例 |
|---|---|---|---|
| **entity** | 存储映射（表/文档） | 基础设施层，由 repo 返回 | SQLAlchemy model、Drizzle/Prisma model |
| **request** | 入参校验 schema | 接口层 | Pydantic model / Zod schema（`z.infer` 出类型） |
| **response** | 出参契约 schema | 接口层或 shared 契约包 | Pydantic / Zod / OpenAPI schema |

Java 大厂的 PO/DO/BO/AO/VO/DTO/Query 全家桶（阿里 Java 开发手册）是为「多人分层团队 + 远程调用边界」设计的。单人/中小项目**默认不照搬**，但每个对象都有自己值得引入的信号（见下节）——看情况使用，不一刀切禁止。

## 看情况引入：DO / DTO / BO / VO（阿里规约）

| 对象 | 阿里定义 | 引入信号 |
|---|---|---|
| DO | 数据表映射（dao 层流转） | Spring/Java 项目里就是本表 entity，沿用 DO 命名即可 |
| DTO | service 对外传输 | 真出现跨进程边界：RPC、消息队列、对外开放 API |
| BO | 业务对象（封装业务逻辑） | 同一业务对象被多个 service 组装/消费、且携带行为 |
| VO | 显示层对象 | 对外契约与表/领域结构实质分叉（裁剪/改名/合并字段） |

信号没出现就留在三分法——为分层而复制对象是纯翻译税。DDD 战术对象（聚合根/值对象/领域服务）的引入信号见 [layering.md](layering.md) 的 DDD 节。

## 判定问答

- 这个对象在进程内复制、却不跨任何边界？→ 删掉（LocalDTO，Fowler 认证有害）。
- API 出参字段 = 表字段原样？→ 先复用 entity 当 response（快路径）；出现「契约分叉信号」（对外字段要改名/隐藏/合并）再拆独立 schema。
- 要不要 BO/VO/AO？→ 不要，直到触发 [layering.md](layering.md) 的加层信号。
- 一个对象既当入参校验又当表映射又当出参？→ 三责一体，第一次分叉就会崩；快速原型允许，进主干前拆。

## Repository vs DAO

- **DAO 面向表**：`user_dao.insert(row)`，接口形状跟着存储机制走。
- **Repository 面向领域集合**：`user_repository.find_active_users()`，隐藏存储方式，方法名说业务。
- 默认用 Repository 语义；现代 O/RM 已把两者融合时，不重复叠一层手写 DAO。
- 接口在 **consumer 侧**声明（见 [layering.md](layering.md) 依赖规则）；repo 方法返回 entity 或领域类型，禁返回裸 SQL 行对象穿透到 service 之上。

## 映射（Mapper / Converter）

- 映射是**纯函数**：无 I/O、无副作用，放转换发生的边界上（接口层 request→调用参数；基础设施层 row→entity）。
- 命名 `to_x`：`to_user_response(entity: User) -> UserResponse`。
- 手写优先；字段多（≈10+）且模式重复 3+ 处时再考虑编译期 codegen（Java MapStruct），禁运行时反射映射库（静默丢字段、类型不安全）。
- 映射函数里禁止顺手加工业务（默认值填充、状态推导）——那是业务逻辑，去领域层；映射只做字段搬运与显式转换。

## 禁止

- ORM entity 带着懒加载关联直接序列化出 API（N+1、循环引用、字段裸奔三连）。
- 用 `dict[str, Any]` 当业务数据出入参（查询条件多字段时对象化，命名字段可校验）。
- 映射逻辑散落在 route/service 各处各写一份——一处一个 `to_x`，单一出处。

## 来源

- [Fowler: LocalDTO](https://martinfowler.com/bliki/LocalDTO.html) / [Repository](https://martinfowler.com/eaaCatalog/repository.html) / [Data Mapper](https://martinfowler.com/eaaCatalog/dataMapper.html) / [Active Record](https://martinfowler.com/eaaCatalog/activeRecord.html)
- [阿里巴巴 Java 开发手册（alibaba/p3c）](https://github.com/alibaba/p3c)——分层领域模型规约（PO/DO/BO/DTO/VO）的出处，本文件有意精简
- [Uncle Bob: The Clean Architecture](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html)——跨边界传简单结构
- [MapStruct](https://mapstruct.org/)——编译期映射工具定位
