# RAG 问答客服「引用来源展示」UI 调研

> 目的:#117/#136 RAG 客服「带引用答疑」的前端交互落地参考。
> 现状:R4 已定消息级 `sources` 下发(source_id+title,前端映射 `[n]→条目`);本报告补「标注形态/hover 展示/多来源组织」的业界做法与取舍。移动端优先(访客多用手机)。

## 1. 主流产品对比

| 产品 | 标注 | hover/点击展示 | 多来源 | 移动/桌面 |
|---|---|---|---|---|
| Perplexity | 句末内联上标 `[1][2]` chip | hover/点 chip → popover:title+favicon+该句片段;点开出原文 | 右侧持久 Sources 栏 | 桌面右栏;移动折叠来源条 |
| ChatGPT(搜索)| 内联上标 chip,`[[CIT-n]]` 流式替换 | 点 chip → Sources 侧栏(URL+title+短片段) | 独立侧栏;后端结构化 JSON 分层 | 桌面侧栏 / 移动底部 overlay |
| Claude | 行内脚注编号 | hover 显 link+片段 | 文档列表附句末 | 桌面 hover;移动点按 |
| Google AI Overview | 总结下方编号 chips | 桌面 hover popover(title+描述+缩略图);移动点按 | 按出现顺序并排 | **移动端重灾区**:来源默认折叠 + 依赖 hover |
| Bing Copilot | 行内脚注编号 | 点编号 → source card(逐声明分组链接) | 按 claim 分组 | 桌面浮动面板 / 移动底部 sheet |
| You.com | 行内编号 + 链接 | 点开简短预览(不离页) | 右侧 sidebar 卡 | 同 Perplexity |

**共性**:引用嵌**句级**(非整篇);hover/点按展示 = **可引用片段 + title + 描述**(做"验证"而非"浏览");正文/标记/来源元数据**分离** 交给前端渲染。

来源:https://www.aiuxplayground.com/teardowns/perplexity/citations/ · https://www.shapeof.ai/patterns/citations · https://funnelstory.ai/blog/engineering/ever-wondered-how-chatgpt-shows-you-its-sources-lets-dive-into-streaming · https://www.stackmatix.com/blog/google-ai-overview-mobile-vs-desktop-differences · https://blogs.bing.com/search/April-2025/Introducing-Copilot-Search-in-Bing

## 2. 对社团客服的建议(移动优先)

数据前提:每个引用 = `{source_id, title, snippet, 可选 url}`。

1. **主模式:句末内联上标 `[n]` chip + hover/点按弹 snippet 卡(推荐)**
   弹卡 = title + snippet(截断 ≤2-3 行)。成本极低(1 组件+tooltip/popover),Perplexity/AIO 已验证;snippet 已有,零抓取。
2. **移动端唯一触发=点击/按压,禁止 hover-only**(Google 教训;访客手机访问)。
3. **回答末尾可折叠「来源」横条/列表**(不做常驻侧栏;窄屏/成本都轻),点 title 开原文,元数据复用不重请求。
4. **弹卡带「打开原文」跳内部文档 URL**(无文档页则该链接降级为仅弹 snippet)。
5. **后端给结构化载荷 `{text, citations:[{marker,source_id}], sources:[...]}`**,前端 marker 注入 chip(免正则)。→ 可延后;若现有 `[n]→sources` 已够用则不必动后端。

## 3. 实现形态

- 触发:句末 chip(可高亮/下划线);桌面 hover + 移动 click/tap(`@media(hover:none)` 或统一 click)。
- 弹层:position 卡(非全文),视口边缘防溢出,`position:fixed`+坐标;移动端避 300ms 延迟(`touch-action`);点外部/ESC 关。
- source 元数据 `{source_id,title,snippet}` 复用。
- 来源折叠条=展开状态组件/`<details>`。

**结论**:内联上标编号 + hover/点按 snippet 卡 + 可选来源折叠条 —— 用满现有三个字段,前端 2 小组件,移动优先成立。
