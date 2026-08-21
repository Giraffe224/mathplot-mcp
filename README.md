# MathPlot MCP — RikkaHub 数学绘图 & 控制工程 MCP 服务器

在 RikkaHub（Android AI 助手）里通过 MCP 协议提供**数学函数绘图**与**控制工程图表**（根轨迹、波特图、奈奎斯特图等）的轻量服务器。

运行在 **Termux（Android 本地）**，纯 Python 标准库实现 Streamable HTTP 传输，零 Web 框架依赖。

---

## ✨ 特性

- **11 个工具**：4 个数学绘图/分析 + 7 个控制工程工具
- **纯 stdlib HTTP**：手写 MCP Streamable HTTP 服务端，无需 uvicorn/pydantic（Termux 上免 Rust 编译）
- **安全解析**：sympy `parse_expr` + 函数白名单，绝不 `eval`；支持 `^` 幂运算（控制工程习惯写法）
- **对话内显示图片**：工具返回文本携带 markdown 图片引用，模型嵌入回复后 RikkaHub 直接渲染；同时避免向模型服务商转发图片（设计决策见 [CHANGELOG](CHANGELOG.md) v1.3.0）
- **中文渲染**：自动加载 Android 系统 Noto Sans CJK 字体（matplotlib 3.11 实测支持 ttc），中文标题/图例不再是方框
- **连接稳定性**：GET /mcp 按规范返回 405（不支持推送流），规避 RikkaHub+kotlin-sdk 的 SSE 重连耗尽导致连接永久卡死的 bug（详见 [CHANGELOG](CHANGELOG.md) v1.5.0）；访问日志统一落盘 `~/mathplot_mcp.log`
- **手动启动**：`mathplot` 一条命令启动，适合日常用机按需运行（runit 托管可选）

## 🛠 工具清单

| 工具 | 用途 | 示例输入 |
| --- | --- | --- |
| `plot_function` | 一元函数 y=f(x) 图像 | `sin(x)*exp(-x**2/10)` |
| `plot_multiple_functions` | 多函数同图对比 | `['sin(x)', 'x**2']` |
| `plot_implicit` | 隐式方程（圆/椭圆/双曲线） | `x**2+y**2-4` |
| `analyze_formula` | 函数分析：导数/定义域/奇偶/极限（纯文本） | `sin(x)/x` |
| `plot_root_locus` | **根轨迹图** | `(s+1)/(s*(s^2+s+1))` |
| `plot_bode` | **波特图**（幅频+相频，含 PM/GM） | `10/(s*(s+1))` |
| `plot_nyquist` | **奈奎斯特图**（自动判稳 Z=P+N） | `1/(s*(s+1)*(s+2))` |
| `plot_nichols` | 尼科尔斯图（M/N 等值线） | `10/(s*(s+1))` |
| `plot_step_response` | 阶跃响应（稳态值/超调量） | `10/(s^2+2*s+10)` |
| `plot_pzmap` | 零极点图（稳定性判断） | `(s+1)/(s^2+3*s+2)` |
| `analyze_transfer_function` | 传函分析：阶次/型别/极点/裕度/阻尼比（纯文本） | `10/(s*(s+1)*(s+2))` |

控制工程工具基于 [python-control](https://github.com/python-control/python-control) 0.10.2 + scipy + matplotlib。

---

## 📦 安装部署（Termux）

### 1. 依赖安装

```bash
# 系统包（numpy/matplotlib 用 Termux 原生包，免编译）
pkg install python python-numpy python-matplotlib python-scipy
# pip（sympy 纯 Python；control 纯 Python，依赖 scipy）
pip install sympy control
```

> ⚠️ 若 `pkg` 报镜像错误，切换可用源（官方 `packages.termux.dev` 或中科大 `mirrors.ustc.edu.cn/termux/apt/termux-main`）。
> ⚠️ 若 matplotlib 渲染报 `raster overflow`，重装 `libexpat` 与 `freetype`（半升级导致的过期二进制）。

### 2. 部署文件

| 文件 | 位置 | 说明 |
| --- | --- | --- |
| `mathplot_mcp.py` | `~/` | 服务器主程序 |
| `mathplot-up.sh` | `~/` | 手动启动脚本（`chmod +x`） |
| `bashrc_new` | 追加到 `~/.bashrc` | 提供 `mathplot` 别名 |

### 3. 启动方式（任选）

**手动启动（推荐，适合日常用机按需运行）**：

```bash
mathplot      # 前台启动，Ctrl+C 停止
mathplot -d   # 后台启动，日志 ~/mathplot_mcp.log
```

**runit 托管**（可选，崩溃自动重启、会话自启）：

```bash
pkg install termux-services
export SVDIR=$PREFIX/var/service
mkdir -p $SVDIR/mathplot-mcp
# run 脚本: exec python ~/mathplot_mcp.py
sv-enable mathplot-mcp && sv up mathplot-mcp
# 取消托管: sv down mathplot-mcp && touch $SVDIR/mathplot-mcp/down
```

### 4. RikkaHub 配置

1. 设置 → MCP → 新建连接
2. 名称：`MathPlot`；连接字符串：`http://127.0.0.1:8000/mcp`；传输类型：**Streamable HTTP**
3. 保存 → 启用工具 → 聊天界面 `+` → MCP → 启用

---

## 💬 使用示例

> - "画 G(s)=10/(s(s+1)) 的波特图"
> - "分析 G(s)=5/(s^2+2s+5) 的稳定性"（返回 ζ=0.45、ωₙ=2.24、PM=78°）
> - "画 (s+1)/(s*(s^2+s+1)) 的根轨迹"
> - "用奈奎斯特图判断 G(s)=(s+2)/(s^2-1) 闭环是否稳定"
> - "G(s)=10/(s^2+2s+10) 的阶跃响应，超调多少？"

工具返回文本中带一行 markdown 图片引用（`![plot](http://127.0.0.1:8000/plots/xxx.png)`），模型会把它原样嵌入回复 → 图片直接显示在对话里；图片也可用手机浏览器打开该地址查看。

---

## 🔧 技术架构

```text
RikkaHub (Android)
   │  MCP Streamable HTTP (127.0.0.1:8000/mcp)
   ▼
mathplot_mcp.py  ── 纯 stdlib: http.server.ThreadingHTTPServer
   ├── JSON-RPC: initialize / tools/list / tools/call / ping
   ├── sympy: 表达式安全解析（白名单 + convert_xor，绝不 eval）
   ├── matplotlib: PNG 渲染（Agg 后端 + Noto Sans CJK 中文回退链）
   ├── python-control: 根轨迹/波特/奈奎斯特/尼科尔斯/阶跃/零极点
   ├── /plots/<id>.png 静态服务（图片 URL 跟随请求 Host 自适应）
   ├── GET /mcp → 405（无推送流需求；规避 RikkaHub SSE 重连 bug）
   └── 图片自动清理（保留最近 100 张）+ 访问日志落盘（~2MB 滚动）
```

**关键设计**：工具结果**只返回文本**（不返回 MCP image 内容块），文本中带 markdown 图片引用。这样：

1. RikkaHub 把图片渲染在对话里（MarkdownNew 渲染器 + Coil）；
2. 出站请求里**永远不会出现 `image_url`**——DeepSeek 等 OpenAI 兼容服务商禁止该字段，出现即报 `unknown variant image_url` 并中断对话（详见 CHANGELOG v1.3.0）。

## 🏗 项目结构

```text
mathplot-mcp/
├── mathplot_mcp.py          # 服务器主程序（v1.5.0）
├── mathplot-up.sh           # Termux 手动启动脚本
├── bashrc_new               # .bashrc 别名片段（mathplot 命令）
├── README.md
├── CHANGELOG.md             # 开发日志
└── docs/
    ├── 原始设计记录.md       # 项目起源：设计方案讨论（含早期 RikkaHub 调研）
    └── 2026-08-21-诊断报告-连接与中文渲染.md
```

## ⚖️ 说明

- 个人学习/工具用途；图片与数据均存本机，不上传任何服务。
- 依赖：Termux Python 3.14+ / numpy / matplotlib / scipy / sympy / python-control。
