#!/usr/bin/env bash
# 按功能清单 v0.1 创建 milestones / labels / 65 个 issues。
# 前提:仓库已存在,gh 已登录有写权限的账号。幂等性:重复运行会重复建 issue,只跑一次。
set -euo pipefail

REPO="${1:-Boyuan-IT-Club/Official_Web_Agent}"

echo "== milestones =="
for m in \
  "M1|骨架与工具层:脚手架、MCP Server、只读工具、CLI ReAct、Langfuse、最小 eval" \
  "M2|简历评估流水线:解析→脱敏→评分→批判回炉→出题,标注集与影子运行" \
  "M3|飞书对话助理:事件订阅、router+子图、checkpointer、写操作 interrupt 确认" \
  "M4|Copilot 与记忆:三态会话、长期记忆 Store、RAG 知识库" \
  "M5|官网双端入口:chat SSE 接口、候选人浮窗、管理后台对话面板" \
  "M6|Copilot 语音模式:流式 ASR、实时追问、录音合规"; do
  title="${m%%|*}"; desc="${m#*|}"
  gh api "repos/$REPO/milestones" -f title="$title" -f description="$desc" --silent 2>/dev/null \
    && echo "  $title" || echo "  $title (已存在,跳过)"
done

echo "== labels =="
for l in \
  "module:INF|0E6E8C|基础设施与入口" \
  "module:TOOL|1A7F5A|工具层与 MCP" \
  "module:GRA|6B4FA0|编排(LangGraph)" \
  "module:EVA|B0621E|简历评估流水线" \
  "module:COP|A03E5C|面试官 Copilot" \
  "module:MEM|3E6BA0|记忆与知识库" \
  "module:SEC|B03030|安全与合规" \
  "module:OBS|556270|评估与可观测" \
  "P0|D93F0B|MVP 必须,缺了主链路不通" \
  "P1|FBCA04|正式可用的必要条件" \
  "P2|C2E0C6|增强与后续扩展" \
  "needs-backend|8B0000|需要后端仓库配合"; do
  IFS='|' read -r name color desc <<< "$l"
  gh label create "$name" -R "$REPO" --color "$color" --description "$desc" --force >/dev/null
  echo "  $name"
done

echo "== issues =="
issue() { # $1=编号 $2=标题 $3=labels $4=milestone;body 从 stdin 读
  local url
  url=$(gh issue create -R "$REPO" -t "[$1] $2" -l "$3" -m "$4" -F -)
  echo "  $1 → $url"
}

# ---------- INF ----------
issue INF-01 "仓库脚手架" "module:INF,P0" M1 <<'EOF'
uv 依赖管理、ruff + mypy、pytest、pre-commit、GitHub Actions CI(lint + 单测 + eval 门禁占位)。

初始骨架已随首个 commit 提交,本 issue 负责补齐 pre-commit 与 CI 完整性验证。
EOF

issue INF-02 "配置管理" "module:INF,P0" M1 <<'EOF'
pydantic-settings 读环境变量:后端地址、服务账号凭证、模型 API key、Redis/Postgres 连接、飞书 app 凭证。真实凭证永不入库。

骨架已含 `config.py`,本 issue 负责随功能推进补齐字段与校验。
EOF

issue INF-03 "CLI 对话入口" "module:INF,P0" M1 <<'EOF'
本地终端多轮对话,流式输出,支持指定模拟身份(候选人/管理员)与会话 id,开发调试主力。

依赖:GRA-01(身份解析)、GRA-04(管理员查询子图)。M1 验收载体。
EOF

issue INF-04 "FastAPI 服务骨架" "module:INF,P0" M3 <<'EOF'
应用工厂、生命周期管理(图与连接池初始化)、`/healthz` 探活、结构化日志、全局异常处理。
EOF

issue INF-05 "飞书事件订阅" "module:INF,P0" M3 <<'EOF'
Webhook 验签与加解密、事件去重、单聊消息与群 @ 消息接收,异步投递给 router 主图(先回 200 再处理,躲开飞书 3 秒超时)。

依赖:INF-04。
EOF

issue INF-06 "飞书消息与卡片发送" "module:INF,P0" M3 <<'EOF'
文本/富文本回复、流式更新(编辑消息模拟打字)、交互卡片构建器(确认卡、候选人卡片、报表卡)。
EOF

issue INF-07 "飞书卡片回调" "module:INF,P0" M3 <<'EOF'
"确认 / 拒绝"按钮回调 → 定位 checkpointer 中挂起的 interrupt 断点 → 恢复或终止图执行,幂等防重放。

依赖:MEM-01(checkpointer)、GRA-05(写操作子图)。
EOF

issue INF-08 "官网 chat SSE 接口" "module:INF,P1" M5 <<'EOF'
`POST /chat` 建会话 + SSE 流式返回 token、工具调用状态事件、确认请求事件;校验官网 JWT 并解析角色。

接口契约先行冻结供前端开发 —— 契约冻结是 INF-09/10 的前置。
EOF

issue INF-09 "官网候选人浮窗" "module:INF,P1,needs-backend" M5 <<'EOF'
官网前台聊天浮窗组件(前端仓库配合,iframe 版兜底),复用登录态,只读问答。

依赖:INF-08 契约冻结。
EOF

issue INF-10 "管理后台对话面板" "module:INF,P1,needs-backend" M5 <<'EOF'
管理后台内嵌对话面板,支持页面内写操作确认组件(替代飞书卡片),报表结果可导出。

依赖:INF-08 契约冻结。
EOF

issue INF-11 "部署与运维" "module:INF,P1" M3 <<'EOF'
Dockerfile + compose(agent / Postgres / Langfuse),接入双机集群 nginx 与现有监控;版本化发布流程对齐主仓库习惯。
EOF

# ---------- TOOL ----------
issue TOOL-01 "后端 API 客户端" "module:TOOL,P0" M1 <<'EOF'
httpx 异步客户端:服务账号登录与 token 自动续期、统一错误映射(BusinessException 错误码 → 可行动提示)、超时重试。

依赖:SEC-01(服务账号,后端前置)。骨架见 `tools/client.py`。
EOF

issue TOOL-02 "MCP Server" "module:TOOL,P0" M1 <<'EOF'
基于官方 mcp SDK 注册全部只读工具,支持 stdio(本地 Claude Code 挂载)与 HTTP 两种 transport。写工具不经 MCP 对外暴露。

骨架见 `mcp_server.py`。
EOF

issue TOOL-03 "只读工具集 ×9" "module:TOOL,P0" M1 <<'EOF'
get_open_cycle / search_resumes / get_resume_detail / get_my_interview / find_available_sessions / list_unassigned / list_reschedule_requests / get_recruit_statistics / get_candidate_card。

接口映射见设计方案 §05;签名骨架见 `tools/readonly.py`。依赖:TOOL-01。
EOF

issue TOOL-04 "写工具集 ×3 + 确认令牌" "module:TOOL,P0" M2 <<'EOF'
move_interview / handle_reschedule / submit_resume_score;代码层强制校验 interrupt 确认令牌(与 checkpointer 中挂起的 interrupt 匹配、一次性),无令牌抛错,不依赖模型自觉。

骨架见 `tools/write.py`(令牌缺失已抛错,校验逻辑待实现)。
EOF

issue TOOL-05 "返回投影与摘要" "module:TOOL,P0" M1 <<'EOF'
长列表只回决策所需字段 + 总数摘要,提示用详情工具下钻;所有工具返回控制在模型友好的体积内。
EOF

issue TOOL-06 "可行动错误规范" "module:TOOL,P0" M1 <<'EOF'
参数错误返回具体修正指引("日期格式应为 YYYY-MM-DD"),权限错误明确说"当前身份无权",让模型能自我修正而非瞎重试。
EOF

issue TOOL-07 "LangGraph 工具接线" "module:TOOL,P0" M1 <<'EOF'
langchain-mcp-adapters 把 MCP 工具转为 langchain-core tool;按会话身份装配可见工具集(候选人拿不到管理员工具,对应 SEC-02)。
EOF

issue TOOL-08 "飞书 OpenAPI 工具" "module:TOOL,P1" M2 <<'EOF'
多维表格写入(评估报告推送)、群消息发送;沿用主仓库的建列/类型转换经验教训(按列真实类型写值、串行建列防重复列,见后端 #174–#176)。
EOF

# ---------- GRA ----------
issue GRA-01 "身份解析节点" "module:GRA,P0" M1 <<'EOF'
确定性节点:飞书 open_id / 官网 JWT / CLI 模拟身份 → 后端用户与角色,决定子图路由与工具集权限。

骨架见 `graphs/router.py`。
EOF

issue GRA-02 "意图分类节点" "module:GRA,P0" M3 <<'EOF'
轻量模型(Haiku 级)分类:问答 / 查询 / 写操作 / 报表,含置信度低时的追问澄清分支。
EOF

issue GRA-03 "候选人问答子图" "module:GRA,P0" M3 <<'EOF'
ReAct 循环,仅只读工具 + RAG 知识库;只能查自己的数据;拒答越权请求并引导人工。

依赖:MEM-06(RAG)可后补,先上纯工具问答。
EOF

issue GRA-04 "管理员查询子图" "module:GRA,P0" M1 <<'EOF'
ReAct 循环,全量读工具;支持多工具组合查询("周六上午空场次 + 未分配的技术部候选人")。

M1 先在 CLI 跑通(配 INF-03),M3 接飞书。
EOF

issue GRA-05 "写操作子图" "module:GRA,P0" M3 <<'EOF'
规划节点生成"将要做什么"的人类可读摘要 → interrupt() 挂起 → 确认后携令牌执行写工具 → 结果回报;拒绝则解释并终止。

依赖:TOOL-04 + MEM-01 + INF-07 三者齐备。
EOF

issue GRA-06 "数据研究子图" "module:GRA,P1" M3 <<'EOF'
Plan-and-Execute:拆解取数计划 → 并行调用统计类工具 → 交叉分析 → 图文周报(飞书卡片/页面渲染)。
EOF

issue GRA-07 "会话摘要压缩" "module:GRA,P1" M3 <<'EOF'
对话超过阈值时,summarization 节点压缩早期轮次,控制上下文与成本。
EOF

issue GRA-08 "模型路由配置" "module:GRA,P1" M2 <<'EOF'
每节点独立绑定模型与温度:分类/抽取用轻量模型,推理/合成用强模型;配置集中可调(config.py 已留 MODEL_LIGHT / MODEL_STRONG)。
EOF

# ---------- EVA ----------
issue EVA-01 "简历拉取与解析" "module:EVA,P0" M2 <<'EOF'
按 cycle 拉取简历字段值与附件 PDF;视觉模型直读页面图,输出结构化摘要(技能、项目、佐证标注)+ 解析置信度,低置信标人工。
EOF

issue EVA-02 "PII 脱敏节点" "module:EVA,P0" M2 <<'EOF'
确定性节点掩码手机号/学号/身份证/住址,规则单测全覆盖;报告中同样不回显。红线功能。
EOF

issue EVA-03 "部门量规管理" "module:EVA,P0" M2 <<'EOF'
各部门评分维度与标准以版本化配置文件维护,支持每周期校准更新。
EOF

issue EVA-04 "分维度评分节点" "module:EVA,P0" M2 <<'EOF'
强模型 + Pydantic 结构化输出:每维度分数必须附引用简历原文的"依据"字段,禁止无依据打分。

依赖:GRA-08、SEC-04 同步落地。
EOF

issue EVA-05 "批判回炉节点" "module:EVA,P0" M2 <<'EOF'
核验引用真实存在、检测幻觉与操纵性内容(提示注入),不合格带意见回炉 ≤2 次,仍不合格标"需人工评审"。
EOF

issue EVA-06 "定制面试题生成" "module:EVA,P0" M2 <<'EOF'
基于项目经历生成 3~5 个针对性追问,标注考察点,供 Copilot(COP-01)与面试官消费。
EOF

issue EVA-07 "批量任务调度" "module:EVA,P0" M2 <<'EOF'
按 cycle 批跑,并发限速、失败重试、断点续跑;由管理员在对话中触发或定时执行,进度可查询。
EOF

issue EVA-08 "确认与双写" "module:EVA,P0" M2 <<'EOF'
评分经人工抽查确认后调 PUT /api/resumes/{id}/score 写回;完整报告推送飞书多维表格(依赖 TOOL-08)。
EOF

issue EVA-09 "影子运行与一致率" "module:EVA,P1" M2 <<'EOF'
对历史周期只评不写,与当年人工初筛结果对比出一致率报告,校准量规后再逐步采信。M2 验收载体。
EOF

issue EVA-10 "跨年查重" "module:EVA,P2" M4 <<'EOF'
与长期记忆中的往年候选人摘要比对,发现重复申请与成长轨迹并在报告中标注。依赖:MEM-02。
EOF

# ---------- COP ----------
issue COP-01 "候选人卡片" "module:COP,P1" M4 <<'EOF'
面试前打包评价看板简历 + EVA 摘要与面试题,生成一页飞书卡片;支持按场次批量预生成。

消费 EVA-06 的产出。
EOF

issue COP-02 "速记 → 追问建议" "module:COP,P1" M4 <<'EOF'
面试中低延迟短输出:结合候选人上下文对速记实时返回追问方向;"面试中"状态零工具调用保证速度。
EOF

issue COP-03 "评价表草稿" "module:COP,P1" M4 <<'EOF'
"整理评价"指令把全程速记按后端评价维度归纳成草稿,面试官修改确认后提交评价看板。
EOF

issue COP-04 "三态会话管理" "module:COP,P1" M4 <<'EOF'
准备中/面试中/整理中状态机,速记与卡片驻留图状态,checkpointer 落 Redis,断线换设备不丢。
EOF

issue COP-05 "场次汇总" "module:COP,P2" M4 <<'EOF'
一场结束后为管理员生成本场次全部候选人的评价汇总视图。
EOF

issue COP-06 "流式 ASR 接入" "module:COP,P2" M6 <<'EOF'
C+:流式语音识别(火山/Paraformer/Whisper streaming 选型)、说话人分离(面试官 vs 候选人)、分句与热词定制。

依赖:COP-02 验证追问质量后启动。
EOF

issue COP-07 "实时追问生成" "module:COP,P2" M6 <<'EOF'
C+:转写流入"面试中"状态,静默窗口触发基于候选人真实回答的下一个追问;转写同样过脱敏与注入防御。
EOF

issue COP-08 "录音合规流程" "module:COP,P2" M6 <<'EOF'
C+:候选人知情同意(报名条款 + 现场告知可拒绝)、音频不落盘或短期加密留存,策略先过 ADR。**动工前置,先于 COP-06/07。**
EOF

# ---------- MEM ----------
issue MEM-01 "Redis Checkpointer" "module:MEM,P0" M3 <<'EOF'
多轮对话状态、interrupt 断点、Copilot 速记的持久化,TTL 过期策略。
EOF

issue MEM-02 "Postgres Store + pgvector" "module:MEM,P1" M4 <<'EOF'
长期记忆存储与向量检索,namespace 分区:管理员偏好 / 候选人 FAQ 画像 / 往年候选人摘要。M1–M3 用 Chroma 过渡。
EOF

issue MEM-03 "反思提取节点" "module:MEM,P1" M4 <<'EOF'
对话结束时判断"有无值得沉淀的事实",提取写入 Store —— 不每轮写(噪声)也不从不写(失忆)。
EOF

issue MEM-04 "记忆召回注入" "module:MEM,P1" M4 <<'EOF'
进子图前按用户 + 意图召回 top-k 记忆注入系统提示,带相关度阈值防串扰。
EOF

issue MEM-05 "记忆管理界面" "module:MEM,P2" M4 <<'EOF'
管理员可查看/删除记忆条目(合规要求:被记忆者有权要求清除)。
EOF

issue MEM-06 "RAG 知识库" "module:MEM,P1" M4 <<'EOF'
社团介绍、部门要求、面试 FAQ 的入库(分块 + embedding)、检索工具、带引用来源的回答;支持从飞书 wiki 同步更新。
EOF

# ---------- SEC ----------
issue SEC-01 "服务账号(后端增量)" "module:SEC,P0,needs-backend" M1 <<'EOF'
后端新增机器账号 + 长效 token 或 client credentials,权限用现有 RBAC 收敛到工具集所需最小集。

**唯一的后端改动,阻塞 TOOL-01 起的一切,应最先做。** 实际代码改动在 Official_Web_Backend 仓库,本 issue 跟踪进度。
EOF

issue SEC-02 "按身份装配工具集" "module:SEC,P0" M3 <<'EOF'
候选人会话用其本人令牌 + 只读工具子集,物理上拿不到管理员工具;权限边界在代码层不在 prompt。
EOF

issue SEC-03 "写操作审计日志" "module:SEC,P0" M3 <<'EOF'
每次写操作记录:触发人、agent 决策依据摘要、变更内容、确认令牌,落库可查。
EOF

issue SEC-04 "提示注入防御" "module:SEC,P0" M2 <<'EOF'
简历/飞书消息/转写在 prompt 中框定为数据区;批判节点显式检测操纵内容;eval 集常备注入样本回归。
EOF

issue SEC-05 "速率与配额" "module:SEC,P1" M3 <<'EOF'
按用户限流(候选人侧防刷)、单会话 token 预算上限、模型调用熔断。
EOF

issue SEC-06 "数据留存 ADR" "module:SEC,P1" M2 <<'EOF'
trace、记忆、评估报告、(C+)音频的留存期限与销毁策略成文,选择不训练用户数据的模型 API 服务。
EOF

# ---------- OBS ----------
issue OBS-01 "Langfuse 部署与接线" "module:OBS,P0" M1 <<'EOF'
自托管部署,LangGraph callback 全图上报;prompt 托管与版本对比启用。
EOF

issue OBS-02 "trace_id 透传" "module:OBS,P1" M1 <<'EOF'
trace_id 注入后端 API 调用 header,两侧日志可对账。
EOF

issue OBS-03 "Eval 框架与确定性断言" "module:OBS,P0" M1 <<'EOF'
evals/ 数据集结构 + 运行器;工具选择、参数正确性断言进 CI。起步 3 个用例已固化在 `evals/cases/tool_selection.yaml`,本 issue 实现运行器。
EOF

issue OBS-04 "简历评估标注集" "module:OBS,P0" M2 <<'EOF'
20~50 份脱敏真实简历 + 人工标注评分区间与关键抽取字段;改 prompt/换模型必跑。数据不入库(evals/datasets 已 gitignore)。
EOF

issue OBS-05 "LLM-as-judge" "module:OBS,P1" M2 <<'EOF'
终答质量自动评分(正确性/有用性/语气),对话与报表类输出的回归手段。
EOF

issue OBS-06 "Badcase 回流" "module:OBS,P1" M3 <<'EOF'
管理员在对话中一键标记错误回答 → 自动生成回归用例进 eval 集,人工补标注后生效。质量飞轮的核心。
EOF

issue OBS-07 "CI eval 门禁" "module:OBS,P1" M2 <<'EOF'
eval 分数低于基线阻断合入;prompt 与图结构变更一律走 PR。CI 已留 TODO 占位。
EOF

issue OBS-08 "指标看板" "module:OBS,P1" M3 <<'EOF'
任务成功率、单任务 token 成本、工具调用错误率;招新季按功能维度聚合成本。
EOF

echo "== 完成:65 个 issues =="
