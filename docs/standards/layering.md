# 分层规范

> **何时读**：新写后端服务/模块；把网络调用、DB、业务逻辑揉在一起的代码拆开；评审分层争议时。
> **一句话**：分层只有一个目的——把业务规则从 I/O 细节里解放出来，让业务可单测、可复用、可替换。每一层都必须回答「没有它会怎样」。
> 设计取舍的裁决次序（SOLID/DRY/KISS）见 [principles.md](principles.md)。

## 默认骨架（中小型 TS / Python 后端）

```
接口层 controller/route  →  应用层 service  →  领域层 domain  →  基础设施层 infrastructure
```

依赖**单向向下**，禁止反向、禁止跨层跳跃调用实现。

| 层 | 职责 | 允许 | 禁止 |
|---|---|---|---|
| 接口层 | 协议适配：校验入参、调 service、组装出参 | Pydantic/Zod 校验；HTTP 语义；调 service | 业务规则；直接碰 DB / 外部 API |
| 应用层 | 用例编排：事务边界、跨模块协调、错误翻译 | 调领域层 + 端口接口；开事务 | 知道 HTTP/SQL 细节 |
| 领域层 | 业务规则：纯类型 + 纯函数 | 计算、不变量、领域错误 | 任何 I/O——DB/网络/时钟/随机一律作参数注入 |
| 基础设施层 | I/O 实现：repo、外部服务客户端 | 实现端口接口；发请求、读写存储 | 被领域层 import；做业务决策 |

Java/Spring 项目沿用 NestJS/Spring 官方默认形态（route→service→repository，constructor 注入）；Django 项目用 HackSoft 约定（`services.py` 写业务、`selectors.py` 管读查询、views 只做胶水、业务不进 managers/signals）。

## 依赖规则

- **端口在 consumer 侧声明**：service/领域层声明自己需要的接口（`interface PushSender { ... }`），基础设施层实现它。接口为 mock 与替换而生，没有消费者就没有接口（Google Go Style Guide：不为 repository/service 概念预建接口）。
- 跨层传**简单结构**（entity / request / response，见 [data-models.md](data-models.md)），不传框架对象（FastAPI `Request`、ORM session）。
- 时钟与随机数在领域层/应用层作参数（`now: datetime`、`roll: float`），实现由入口注入——这也是可测性的根基。

## 加层信号（YAGNI 判据）

信号未出现前，**controller 直调 repo 是诚实的选择**（spring-petclinic 全库没有 service 层）。出现以下信号才加对应的东西：

| 信号 | 动作 |
|---|---|
| 出现第二个入口（CLI / 队列 / 定时任务要复用同一逻辑） | 抽 service |
| 同一规则在 3+ 处重复 | 抽领域函数 |
| 需要替换/并行的第二个存储或外部服务，或需要无 I/O 单测 | 抽端口 + repo 接口 |
| API 契约与表结构实质分叉 | 建 response schema |

## 选型速查

- **CRUD / 内容展示为主** → transaction script 或 controller 直调 repo；贫血模型 + 表单对象是合理取舍（付域模型的成本前先确认有规则值得装进去）。
- **规则复杂多变** → domain model（行为+数据的对象）+ repository。
- **多 adapter / 要求无 UI 无 DB 跑回归** → hexagonal（端口适配器）。注意 DHH 的警告：为可测性强加分层是 test-induced design damage，只有真出现第二个 adapter 才划算。
- 层数与粒度是选择（Fowler）：先问「没有它会怎样」，再问「它会替我省掉什么重复」。

## 经典 Java 分层（web / service / manager / dao）

阿里 Java 手册的四层在 Spring/Java 大中型项目成熟可用（同工作区的 Official_Web_Backend 参照）：

| 层 | 职责 |
|---|---|
| web | controller：参数校验、调 service、组装响应 |
| service | 具体业务逻辑 |
| manager | **通用业务处理层**：第三方平台的封装/适配收口；多个 service 下沉的通用能力（缓存方案、中间件通用处理）；组合多个 dao 的可复用组合 |
| dao | 数据访问（MyBatis/JPA） |

manager 的引入信号：第三方调用/缓存收口在**多个 service 里重复出现**，下沉一次全网受益。简单 CRUD 项目里 manager 是空转层，不预建。

## DDD 分层（复杂领域）

`user-interface → application（用例编排，薄）→ domain（实体/值对象/聚合/领域服务/仓储接口）→ infrastructure`。与默认四层骨架同构，差别在 domain 层的丰富度。

战术模式按信号逐个引入（别从 entity 一步跳全家桶）：

- 不变量横跨多个对象、需要一致性边界 → **聚合根**。
- 概念有身份用**实体**；值相等即等价、不可变 → **值对象**。
- 规则需要协调多个聚合 → **领域服务**。
- 团队形成了通用语言（ubiquitous language）→ 用它命名所有对象，词汇即设计。

DO/DTO/BO/VO 等领域对象的引入信号见 [data-models.md](data-models.md)。

## 反模式（见到就拆）

- **揉合层**：route 里 fetch 外部 API + 拼 SQL + 业务判断混在一个函数。
- **pass-through service**：纯转发调 repo，不加任何编排价值。
- **fat service**：一切逻辑进 service，entity 纯贫血，规则无处安放也无法复用。
- **entity 漏到 API 响应**：字段变更即破坏契约，还可能暴露懒加载/内部字段。
- **LocalDTO**（Fowler：进程内 DTO "actively harmful"）：为每层复制一份字段几乎相同的对象。
- **generic repository**：为所有表做一个泛型 repo，类型安全归零。
- **按层分包代替按 feature 分包**：找「所有 repo」「所有 controller」不如找「订单」好找。按 feature 分包，层做包内子目录。

## 来源

- Fowler PoEAA：[Layering](https://martinfowler.com/bliki/PresentationDomainDataLayering.html) / [ServiceLayer](https://martinfowler.com/eaaCatalog/serviceLayer.html) / [Repository](https://martinfowler.com/eaaCatalog/repository.html) / [AnemicDomainModel](https://martinfowler.com/bliki/AnemicDomainModel.html) / [LocalDTO](https://martinfowler.com/bliki/LocalDTO.html)
- [Hexagonal Architecture（Cockburn）](https://alistair.cockburn.us/hexagonal-architecture/) / [Onion（Palermo）](https://jeffreypalermo.com/2008/07/the-onion-architecture-part-1/) / [Clean Architecture（Uncle Bob）](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html)
- 实证：[spring-petclinic](https://github.com/spring-projects/spring-petclinic)（无 service 层）/ [HackSoft Django Styleguide](https://github.com/HackSoftware/Django-Styleguide) / [DHH: Test-Induced Design Damage](https://dhh.dk/2014/test-induced-design-damage.html) / [Mat Ryer: How I write HTTP services in Go](https://grafana.com/blog/2024/02/09/how-i-write-http-services-in-go-after-13-years/) / [Google Go Style Guide: decisions](https://google.github.io/styleguide/go/decisions) / [NestJS Providers](https://docs.nestjs.com/providers)
