# 开发日志（CHANGELOG）

MathPlot MCP — RikkaHub 数学绘图 & 控制工程 MCP 服务器

版本约定：`v1.x.y`。所有版本均已在红米手机（Redmi, Android 16, Termux）实测。

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
