# 开发日志（CHANGELOG）

MathPlot MCP — RikkaHub 数学绘图 & 控制工程 MCP 服务器

版本约定：`v1.x.y`。所有版本均已在红米手机（Redmi, Android 16, Termux）实测。

---

## v1.6.0 — 2026-08-21 执行优化 backlog（P0+P1+P2，OO 绘图 API 重构）

**背景**：执行 [docs/2026-08-21-代码评估与优化计划.md](docs/2026-08-21-代码评估与优化计划.md) 的 P0-P2 项（P3 按计划推迟单独做）。

**改动**：

- **绘图全面改用 OO API（P1-1，最核心）**：`plt.subplots()` → `Figure()` + `fig.subplots()`，
  消除 pyplot 全局 figure 管理器的并发软肋（两个并发工具调用不再互相污染画面）；
  `plot_root_locus` 从依赖隐式当前 axes 的 `ctrl.root_locus(plot=True)` 改为显式
  `ctrl.root_locus_plot(sys, ax=ax)`（0.10.2 实测支持）；`plt.close` 全部移除
  （OO figure 随引用释放）。
- **工具异常落盘（P0-1）**：`tools/call` 的异常处理加 `traceback` 写日志，
  绘图 bug 不再无痕。
- **死代码清理（P0-2）**：删 `make_image_block()` + `base64` import；
  删只写不读的 `SESSION_IDS`（DELETE 保持幂等 200）。
- **请求体上限（P1-2）**：POST body >10MB 回 413。
- **空闲连接超时（P1-3）**：handler `timeout=65`，僵死 keep-alive 连接自动释放线程。
- **工具注册表合一（P2-1）**：名字校验元组 + 分发表 → `TOOL_FUNCS` 单一字典；
  新增 `_check_tool_schemas()` 启动自检（schema 属性 ⊆ 签名、required ⊆ 无默认值），
  schema/签名漂移在启动瞬间暴露（不再出现 x_label 幽灵参数这类问题）。
- **描述段落去重（P2-2）**：9 处重复的图片引用指令抽成 `IMG_NOTE` 常量。
- **样式（P2-3）**：`_cors` 条件简化；12 处 isError 样板统一用 `_err()`。

**推迟（P3）**：启动延迟（先 bind 后导入）与 schema 自动生成——改动面大、
收益一般，按计划单独做。

**验证**：11 个工具全量回归通过（见下方回归记录）；RikkaHub 重连验证；
并发请求测试（两个 plot_function 同时调用，图各归各）。

---

## v1.5.0 — 2026-08-21 修复中文渲染 + 根治 RikkaHub 连接卡死

**背景**：实测复现三大问题（完整诊断见
[docs/2026-08-21-诊断报告-连接与中文渲染.md](docs/2026-08-21-诊断报告-连接与中文渲染.md)）：
中文渲染为方框；RikkaHub 先于 MCP 启动时无法连接；服务器中断 >30s 后 RikkaHub 永久卡死。

**修复**：

- **中文渲染**：启动时从 Android 系统字体目录加载 `NotoSansCJK-Regular.ttc`
  （matplotlib 3.11 实测支持 ttc），rcParams 回退链 `Noto Sans CJK SC → DejaVu Sans`
  （3.6+ 逐字形回退，西文/数学符号仍由 DejaVu 渲染）；加载失败优雅降级。
  同时 `axes.unicode_minus = False`（CJK 字体缺 U+2212）。
- **GET /mcp 改返回 405**（规范允许：不支持服务器推送流）。这是连接稳定性的关键：
  kotlin-sdk 会对 GET /mcp 建常驻 SSE 流，服务器重启后 SDK 内部重连耗尽报
  "Maximum reconnection attempts exceeded"，而 RikkaHub 的 `isSseStreamGiveUpError()`
  会特意忽略该错误 → 永久卡死。返回 405 时 SDK 进入 "stream disabled" 模式，
  不建流不报错；配合 RikkaHub callTool 的 `connectedConfig==null` 兜底重连，
  「RikkaHub 先启动」和「服务器中途重启」两个场景都能在下一条消息自愈。
  本服务全部工具均为请求-响应式，无需推送流；附带消灭了每客户端一条常驻线程。
- **访问日志统一落盘** `~/mathplot_mcp.log`（带时间戳，>2MB 滚动为 .old）：
  之前前台运行时日志只在终端，排查连接问题只能靠 logcat。
- **杂项**：`SESSION_IDS` 封顶 1024 防泄漏；处理 `DELETE /mcp`（幂等回 200）；
  initialize 响应附 instructions 提示中文 title 可用；代码风格统一
  `contextlib.suppress`。

**未采纳（用户决策）**：服务器常驻方案（runit 自启 / wake-lock / Termux:Boot /
禁用幽灵进程杀手）——本机为日常用机，保持按需手动启动（`mathplot`），
依赖上述自愈能力保证体验。

---

## v1.4.1 — 2026-08-13 修复奈奎斯特曲线绘制 bug

**问题**：`plot_nyquist` 画出来的是一条竖线（实部≈0），不是真实奈奎斯特曲线。

**根因**（python-control 0.10.2 API 陷阱）：旧 API
`nyquist_plot(plot=False, return_contour=True)` 返回的 `contour` 是 **s 平面 D 围道本身**
（虚轴），**不是 G(jω) 映射曲线**。正确数据在 `nyquist_response()` 对象的 `.response` 字段。

**修复**：

```python
resp = ctrl.nyquist_response(sys)
enc = int(resp.count)
resp.plot(ax=ax)   # control 官方绘图：正确曲线 + 凹陷弧 + 临界点标记
```

**验证**：`1/(s(s+1)(s+2))` 曲线 62% 像素落在左半平面（正确形态）；四个不同系统（含不稳定开环、
II 型、零极点对消）渲染全部正确。

---

## v1.4.0 — 2026-08-13 恢复对话内图片输出

**背景**：v1.3.0 改为纯文本后，第四次测试模型自发嵌入 markdown 图片（对话正常显示、无报错），
但第五次测试模型未嵌入 → 对话里没图。两次测试证明：**纯文本工具结果已根治 image_url 报错，
且回复中的 markdown 图片不会被 RikkaHub 提取成消息图片部件（导出文件实证）**。

**改动**：

- 工具结果恢复"必须执行"指令：要求模型把 `![plot](url)` markdown 行原样写入最终回复 → 对话稳定出图；
- 工具描述同步恢复（9 处）；
- **保持工具结果纯文本**（不返回 MCP image 内容块）——安全性的根本保障；
- `plot_nyquist` / `plot_nichols` / `plot_step_response` / `plot_pzmap` 补齐 `title` 参数
  （第四次测试暴露：模型传 title 会报参数错误）。

---

## v1.3.0 — 2026-08-13 根治 image_url 报错（工具结果改纯文本）

**问题**：第三次测试 11 个工具并行调用后，模型无法输出，报
`Failed to deserialize the JSON body into the target type: messages[4]: unknown variant image_url, expected text`。

**根因**（RikkaHub 2.4.5 源码实证）：

1. MCP 工具返回的 image 内容块被 RikkaHub 转成消息图片部件；
2. 发送给模型服务商时，`ChatCompletionsAPI.kt` 把图片部件编码为 `image_url`
   （助手消息与工具结果均有，且助手消息路径**无**模态防护，工具结果路径有）；
3. DeepSeek 等 OpenAI 兼容服务商**拒绝任何位置的 `image_url`**（全网已知问题：
   vercel/ai #9179、microsoft/vscode #320110 同款报错）；
4. 失败发生在最终回复请求（历史含 9 张工具结果图）→ 服务商拒收。

**改动**：

- 工具结果**不再返回 MCP image 内容块**，只返回文本 + 图片 URL → 出站请求永无 `image_url`；
- 文本中的图片引用改为纯 URL（去掉 markdown 语法，双保险）；
- 顺带修复：`plot_bode`/`plot_implicit` 支持 `title`；极点恰在虚轴时正确判为"临界稳定"；
  奈奎斯特 P 只数严格右半平面极点（`1/(s(s+1)(s+2))` 修正为闭环稳定）。

---

## v1.2.0 — 2026-08-13 控制工程工具箱 + 双端部署

**新增 7 个控制工程工具**：`plot_root_locus` / `plot_bode` / `plot_nyquist` / `plot_nichols` /
`plot_step_response` / `plot_pzmap` / `analyze_transfer_function`（基于 python-control 0.10.2）。

**控制工程适配**：

- `^` 即乘方（sympy `convert_xor`，控制工程师习惯写 `s^2+2s+5`）；
- 裕度单位实测校准：PM 用度、GM 同时给线性值与 dB、标注穿越频率（control 0.10.2 返回线性幅值、
  相位弧度、GM 线性值——与旧版文档不同，逐项实测确认）；
- 奈奎斯特判据实测校准：环绕数为带符号值（顺时针为正），`Z = P + N`；
- 图片 URL 跟随请求 Host 自适应（同一份代码在 127.0.0.1 与局域网 IP 下均正确）。

**双端**：PC（电脑+局域网，后来按用户要求移除）与手机 Termux 并行部署，后仅保留手机端。

---

## v1.1.0 — 2026-08-13 对话内显示图片（第一版方案）

**背景**：RikkaHub 把 MCP 工具图片只显示在工具调用卡片、不显示在对话流，且不把图片转发给模型
（GitHub issue #664 / #1138，audience 注解未实现）。

**第一版方案**：服务器把每张图落盘并提供 `/plots/<id>.png`，工具文本携带
`【必须执行】...![plot](url)...` 指令，让模型在回复中嵌入 markdown 图片 → RikkaHub 渲染在对话里。
该方案当时有效，但**事后证明工具返回 image 内容块是 v1.3.0 修复的 image_url 报错的隐患来源**。

---

## v1.0.0 — 2026-08-13 Termux 环境搭建 + 基础数学工具

**环境修复**（这台手机的 Termux 处于半升级状态，四个坑）：

| 问题 | 根因 | 修复 |
| --- | --- | --- |
| 镜像 403 | 北外镜像失效 | 切换官方/中科大源 |
| `pip install` 报 `pip._internal.operations.install.wheel` 缺失 | python 3.13→3.14 升级后 pip/expat 版本错配 | 重装 python-pip、升级 libexpat 2.8.3 |
| matplotlib 导入报 pyexpat 缺 `XML_SetHashSalt16Bytes` | libexpat 磁盘文件是过期二进制 | `pkg reinstall libexpat` |
| 任何字形渲染报 `raster overflow` | freetype 过期二进制（同 libexpat） | `pkg reinstall freetype`（升到 2.14.3） |

**架构决策**：Termux 无 pydantic/uvicorn 原生包（pip 装要现场编译 Rust 的 pydantic-core），
故**纯 stdlib 手写 MCP Streamable HTTP 服务端**（initialize / tools/list / tools/call / ping），
零 Web 框架依赖。表达式用 sympy `parse_expr` + 白名单解析（绝不 eval）。

**基础 4 工具**：`plot_function` / `plot_multiple_functions` / `plot_implicit` / `analyze_formula`。

---

## 关键技术决策记录

### 为什么工具结果不返回 MCP image 内容块？

RikkaHub 会把工具返回的图片转成消息图片部件，并在请求模型服务商时编码为 `image_url`。
DeepSeek 等 OpenAI 兼容服务商拒绝该字段 → 对话中断。改为纯文本 + markdown 图片引用后：
图片仍能显示在对话里（RikkaHub 的 MarkdownNew 渲染器 + Coil 加载），且出站请求零图片字段。

### 为什么不用 FastMCP？

FastMCP 依赖 pydantic-core（Rust），Termux 上 pip 无法用预编译 wheel、需现场编译，成本高；
纯 stdlib 实现 MCP Streamable HTTP（两个工具）协议体量很小，自研更可控。

### python-control 0.10.2 的坑

- 新 API：`nyquist_response()` / `poles()` / `zeros()`（非 `pole()`）；
- `bode_plot(plot=False)` 返回**线性幅值**（需自行转 dB）、相位**弧度**（需转度）；
- `margin()` 返回**线性 GM**（需转 dB）；
- `nyquist_plot(plot=False, return_contour=True)` 的 `contour` 是 s 平面 D 围道而非 G(jω) 曲线；
- `nichols_plot` 无 `plot=False`，需传 `ax`。
