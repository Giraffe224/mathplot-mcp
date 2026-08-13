#!/data/data/com.termux/files/usr/bin/python
"""
MathPlot MCP Server (stdlib-only, Streamable HTTP transport)
Runs on Termux/Android. Listens on 127.0.0.1:8000 by default.

Tools:
  plot_function(expr, x_min, x_max, y_min, y_max, title)  -> PNG + text summary
  plot_multiple_functions(expressions, x_min, x_max, title)-> PNG + text summary
  plot_implicit(expr, x_min, x_max, y_min, y_max)          -> PNG + text summary
  analyze_formula(expr)                                    -> text analysis

Design notes:
  * Every image-returning tool ALSO returns a structured text summary,
    because RikkaHub renders MCP image blocks for the user but does NOT
    forward them to the LLM (rikkahub issues #664 / #1138). The text is the
    model's only view of the result.
  * Expressions are parsed with sympy (never eval). Pure stdlib HTTP so the
    phone needs no pydantic/uvicorn/Rust builds.
"""

import base64
import contextlib
import io
import json
import os
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from sympy import (
    Eq,
    S,
    Symbol,
    cos,
    exp,
    factor,
    factorial,
    lambdify,
    latex,
    log,
    oo,
    pi,
    simplify,
    sin,
    sqrt,
    symbols,
    tan,
)
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

# ---------------------------------------------------------------------------
# 安全解析：白名单函数，绝不 eval
# ---------------------------------------------------------------------------
ALLOWED_FUNCS = {
    "sin": sin,
    "cos": cos,
    "tan": tan,
    "exp": exp,
    "log": log,
    "log10": lambda x: log(x, 10),
    "sqrt": sqrt,
    "abs": abs,
    "pi": pi,
    "e": S.Exp1,
    "E": S.Exp1,
    "oo": oo,
    "inf": oo,
    "factorial": factorial,
}
# convert_xor: 让 ^ 表示乘方（控制工程习惯写 s^2，sympy 默认把 ^ 当异或）
TRANSFORMS = standard_transformations + (
    implicit_multiplication_application,
    convert_xor,
)


def safe_parse(expr_str: str):
    """Parse a math expression safely. Returns (sympy_expr, err_msg_or_None)."""
    if not isinstance(expr_str, str) or not expr_str.strip():
        return None, "表达式为空"
    s = expr_str.strip()
    # 移除明显的危险构造
    if re.search(r"(__|import|exec|eval|os\.|system|open\(|subprocess|compile\()", s):
        return None, "表达式包含不允许的语法"
    try:
        expr = parse_expr(
            s,
            transformations=TRANSFORMS,
            evaluate=True,
            local_dict={
                "x": Symbol("x"),
                "y": Symbol("y"),
                "s": Symbol("s"),
                **ALLOWED_FUNCS,
            },
        )
        return expr, None
    except Exception as e:
        return None, f"表达式解析失败: {e}"


def coerce_number(v, name: str) -> float:
    """Coerce a tool argument to float; raises ValueError with a clean message."""
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"参数 {name} 需要是数字，收到: {v!r}") from None


def coerce_opt(v, name: str) -> "float | None":
    """Coerce an optional numeric argument (None stays None)."""
    if v is None:
        return None
    return coerce_number(v, name)


def safe_lambdify(expr, vars_list):
    """Lambdify with numpy modules; fall back to math for single-var."""
    try:
        return lambdify(vars_list, expr, modules=["numpy"])
    except Exception:
        return lambdify(vars_list, expr, modules=["math", "numpy"])


# ---------------------------------------------------------------------------
# 渲染辅助
# ---------------------------------------------------------------------------
def render_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


def make_image_block(png_bytes: bytes) -> dict:
    return {
        "type": "image",
        "data": base64.b64encode(png_bytes).decode(),
        "mimeType": "image/png",
    }


# ---------------------------------------------------------------------------
# 图片持久化 + 对话渲染支持
#
# RikkaHub 的 MCP 客户端（McpManager.kt）只会把工具返回的 image 内容块渲染在
# “工具调用”卡片里，不会把它放进对话流，也不会把图片传给 LLM（issue #1138）。
# 因此这里把每张图存成文件并通过 /plots/<id>.png 提供，同时把 markdown 图片行
# 放进文本摘要——模型在回复中原样引用该行后，RikkaHub 的 MarkdownNew 渲染器
# 会用 Coil 直接加载 <img>，图片就能显示在对话里。
# ---------------------------------------------------------------------------
PLOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plots")
MAX_PLOTS = 100
_PLOT_NAME_RE = re.compile(r"^[0-9a-f]{32}\.png$")
_tls = threading.local()


def _plot_url_base() -> str:
    """图片 URL 主机：优先取当前请求的 Host（客户端怎么连的就怎么取图），
    这样同一份代码在 127.0.0.1（手机）或局域网 IP（电脑）下都能生成正确地址。"""
    host = getattr(_tls, "host", None)
    if host:
        return f"http://{host}/plots"
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = os.environ.get("MCP_PORT", "8000")
    return f"http://{host}:{port}/plots"


def save_plot(png_bytes: bytes) -> str:
    """保存 PNG 并返回 URL 路径（如 /plots/<id>.png）。"""
    try:
        os.makedirs(PLOTS_DIR, exist_ok=True)
        name = uuid.uuid4().hex + ".png"
        with open(os.path.join(PLOTS_DIR, name), "wb") as fh:
            fh.write(png_bytes)
    except OSError as e:
        raise RuntimeError(f"无法保存图片: {e}") from e
    try:
        files = sorted(
            (p for p in os.listdir(PLOTS_DIR) if p.endswith(".png")),
            key=lambda p: os.path.getmtime(os.path.join(PLOTS_DIR, p)),
            reverse=True,
        )
        for old in files[MAX_PLOTS:]:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(PLOTS_DIR, old))
    except OSError:
        pass
    return f"/plots/{name}"


INSTRUCTION = "【必须执行】在最终回复中展示图片：把下面这一行 markdown 原样包含到你的回复末尾，一字不改："


def plot_markdown(url_path: str) -> str:
    """返回可在回复中引用的 markdown 图片行，如 ![plot](http://127.0.0.1:8000/plots/x.png)。

    模型把这一行原样写进最终回复后，RikkaHub 的 MarkdownNew 渲染器会用 Coil 加载并
    显示在对话里（已在第四次测试验证）。回复中的 markdown 图片保持为文本部件，
    不会被转发成 image_url 出站，因此不会触发服务商报错。
    """
    return f"![plot]({_plot_url_base()}/{os.path.basename(url_path)})"


def _safe_eval_ys(func, xs):
    with np.errstate(all="ignore"):
        ys = func(xs)
        ys = np.asarray(ys, dtype=float)
    return np.where(np.isfinite(ys), ys, np.nan)


def _nice_range(expr, xs):
    """Return (clip_min, clip_max) sane display range for y."""
    x = Symbol("x")
    try:
        func = safe_lambdify(expr, [x])
        ys = _safe_eval_ys(func, xs)
        finite = ys[np.isfinite(ys)]
        if len(finite) == 0:
            return None, None
        lo, hi = float(np.percentile(finite, 1)), float(np.percentile(finite, 99))
        span = hi - lo
        if span <= 0 or not np.isfinite(span):
            return None, None
        pad = span * 0.15
        return lo - pad, hi + pad
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
def plot_function(
    expr: str,
    x_min: float = -10,
    x_max: float = 10,
    y_min: "float | None" = None,
    y_max: "float | None" = None,
    title: str = "",
):
    x = Symbol("x")
    f, err = safe_parse(expr)
    if err:
        return {"isError": True, "content": [{"type": "text", "text": err}]}
    assert f is not None
    try:
        x_min = coerce_number(x_min, "x_min")
        x_max = coerce_number(x_max, "x_max")
        y_min = coerce_opt(y_min, "y_min")
        y_max = coerce_opt(y_max, "y_max")
    except ValueError as e:
        return {"isError": True, "content": [{"type": "text", "text": str(e)}]}
    if x_min >= x_max:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "x_min 必须小于 x_max"}],
        }

    xs = np.linspace(x_min, x_max, 2000)
    func = safe_lambdify(f, [x])
    ys = _safe_eval_ys(func, xs)

    fig, ax = plt.subplots(figsize=(8, 6))
    ax.plot(xs, ys, lw=2.0)
    ax.axhline(0, color="gray", lw=0.8)
    ax.axvline(0, color="gray", lw=0.8)
    ax.grid(True, alpha=0.3)
    try:
        t = latex(f)
    except Exception:
        t = expr
    ax.set_title(title or f"y = {t}")
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    if y_min is not None and y_max is not None and y_min < y_max:
        ax.set_ylim(y_min, y_max)
    else:
        lo, hi = _nice_range(f, xs)
        if lo is not None:
            ax.set_ylim(lo, hi)

    png = render_png(fig)
    url_path = save_plot(png)

    # 文本摘要（给 LLM 看的；RikkaHub 不会把图片传给模型，所以图片引用必须放文本里）
    finite = ys[np.isfinite(ys)]
    summary_lines = [
        f"已绘制函数: y = {latex(f)}（原式: {expr}）",
        f"x 范围: [{x_min}, {x_max}]",
    ]
    if len(finite) > 0:
        try:
            fmin = float(np.nanmin(finite))
            fmax = float(np.nanmax(finite))
        except ValueError:
            fmin = fmax = 0.0
        summary_lines.append(
            f"y 实际取值范围(约): [{fmin:.4g}, {fmax:.4g}]（图内已裁剪异常尖峰）"
        )
    try:
        import sympy as sm

        den = sm.denom(f)
        bad = sm.solve(den, x)
        if bad:
            summary_lines.append(
                f"无定义点: x ∈ {[float(v) for v in bad if v.is_number]}"
            )
    except Exception:
        pass
    summary_lines.append("")
    summary_lines.append("")
    summary_lines.append(INSTRUCTION)
    summary_lines.append(plot_markdown(url_path))

    return {"content": [{"type": "text", "text": "\n".join(summary_lines)}]}


def plot_multiple_functions(
    expressions, x_min: float = -10, x_max: float = 10, title: str = ""
):
    if not isinstance(expressions, list) or len(expressions) == 0:
        return {
            "isError": True,
            "content": [
                {"type": "text", "text": "expressions 需要是至少一个表达式的列表"}
            ],
        }
    try:
        x_min = coerce_number(x_min, "x_min")
        x_max = coerce_number(x_max, "x_max")
    except ValueError as e:
        return {"isError": True, "content": [{"type": "text", "text": str(e)}]}
    if x_min >= x_max:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "x_min 必须小于 x_max"}],
        }
    x = Symbol("x")
    xs = np.linspace(x_min, x_max, 2000)
    fig, ax = plt.subplots(figsize=(8, 6))
    latexes = []
    for i, e in enumerate(expressions):
        f, err = safe_parse(e)
        if err:
            plt.close(fig)
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"第{i + 1}个表达式: {err}"}],
            }
        assert f is not None
        func = safe_lambdify(f, [x])
        ys = _safe_eval_ys(func, xs)
        try:
            lab = latex(f)
        except Exception:
            lab = str(e)
        latexes.append(lab)
        ax.plot(xs, ys, lw=2.0, label=f"y = {lab}")
    ax.axhline(0, color="gray", lw=0.8)
    ax.axvline(0, color="gray", lw=0.8)
    ax.grid(True, alpha=0.3)
    if len(latexes) > 1:
        ax.legend(fontsize=10)
    ax.set_title(title or "Multiple functions")
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    png = render_png(fig)
    url_path = save_plot(png)

    text = "\n".join(
        [f"已绘制 {len(latexes)} 个函数，x ∈ [{x_min}, {x_max}]："]
        + [f"  {i + 1}. y = {lab}" for i, lab in enumerate(latexes)]
    )
    text += "\n\n" + INSTRUCTION + "\n" + plot_markdown(url_path)
    return {"content": [{"type": "text", "text": text}]}


def plot_implicit(
    expr: str,
    x_min: float = -10,
    x_max: float = 10,
    y_min: float = -10,
    y_max: float = 10,
    title: str = "",
):
    x, y = symbols("x y")
    f, err = safe_parse(expr)
    if err:
        return {"isError": True, "content": [{"type": "text", "text": err}]}
    assert f is not None
    try:
        x_min = coerce_number(x_min, "x_min")
        x_max = coerce_number(x_max, "x_max")
        y_min = coerce_number(y_min, "y_min")
        y_max = coerce_number(y_max, "y_max")
    except ValueError as e:
        return {"isError": True, "content": [{"type": "text", "text": str(e)}]}
    if x_min >= x_max or y_min >= y_max:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "范围参数不合法"}],
        }
    try:
        func = safe_lambdify(f, [x, y])
    except Exception as e:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"无法对隐式方程求值: {e}"}],
        }

    nx = ny = 400
    xs = np.linspace(x_min, x_max, nx)
    ys = np.linspace(y_min, y_max, ny)
    X, Y = np.meshgrid(xs, ys)
    with np.errstate(all="ignore"):
        Z = np.asarray(func(X, Y), dtype=float)
    Z = np.where(np.isfinite(Z), Z, np.nan)

    fig, ax = plt.subplots(figsize=(8, 6))
    ax.contour(X, Y, Z, levels=[0.0], colors="C0", linewidths=2)
    ax.axhline(0, color="gray", lw=0.8)
    ax.axvline(0, color="gray", lw=0.8)
    ax.grid(True, alpha=0.3)
    ax.set_xlim(x_min, x_max)
    ax.set_ylim(y_min, y_max)
    try:
        t = latex(Eq(f, 0))
    except Exception:
        t = expr
    ax.set_title(title or t)
    ax.set_aspect("equal", adjustable="box")
    png = render_png(fig)
    url_path = save_plot(png)

    text = f"已绘制隐式方程: {latex(Eq(f, 0))}（原式: {expr}），x∈[{x_min},{x_max}], y∈[{y_min},{y_max}]"
    text += "\n\n" + INSTRUCTION + "\n" + plot_markdown(url_path)
    return {"content": [{"type": "text", "text": text}]}


def analyze_formula(expr: str):
    x = Symbol("x")
    f, err = safe_parse(expr)
    if err:
        return {"isError": True, "content": [{"type": "text", "text": err}]}
    assert f is not None
    lines = [f"## 函数分析: y = {latex(f)}", f"原式: {expr}", ""]

    def add(section, items):
        if items:
            lines.append(f"**{section}**")
            lines.extend(items)
            lines.append("")

    sf = f
    fd = f
    try:
        sf = simplify(f)
        if sf != f:
            add("化简", [f"simplify: y = {latex(sf)}"])
    except Exception:
        pass
    try:
        fd = factor(f)
        if fd != f and fd != sf:
            add("因式分解", [f"factor: y = {latex(fd)}"])
    except Exception:
        pass
    # 定义域
    try:
        import sympy as sm

        undef = []
        for bad in sm.solve(sm.denom(f), x):
            if bad.is_number:
                undef.append(f"x ≠ {float(bad):.4g}")
        # 根号/对数限制
        for s in sm.preorder_traversal(f):
            if isinstance(s, sm.log) and s.args[0] != x:
                undef.append(f"{latex(s)} 需 {latex(s.args[0])} > 0")
            if (
                isinstance(s, sm.Pow)
                and s.exp.is_number
                and abs(s.exp) < 1
                and s.exp != 0
            ):
                undef.append(f"{latex(s)} 需 {latex(s.args[0])} ≥ 0")
        if undef:
            add("定义域限制", undef)
        else:
            add("定义域", ["x ∈ ℝ（所有实数）"])
    except Exception:
        pass
    # 奇偶性 / 周期性
    try:
        import sympy as sm

        even = sm.simplify(f.subs(x, -x) - f) == 0
        odd = sm.simplify(f.subs(x, -x) + f) == 0
        parity = []
        if even:
            parity.append("偶函数（关于 y 轴对称）")
        elif odd:
            parity.append("奇函数（关于原点对称）")
        else:
            parity.append("既非奇函数也非偶函数")
        per = sm.periodicity(f, x)
        if per:
            parity.append(f"周期函数，最小正周期 ≈ {float(per):.4g}")
        add("对称性与周期性", parity)
    except Exception:
        pass
    # 导数与驻点
    try:
        import sympy as sm

        d = sm.diff(f, x)
        crit = []
        try:
            sols = sm.solve(d, x)
            for v in sols:
                if v.is_real:
                    try:
                        yv = float(f.subs(x, v))
                        crit.append(f"x = {float(v):.4g} 处 y = {yv:.4g}")
                    except Exception:
                        crit.append(f"x = {float(v):.4g}")
        except Exception:
            pass
        if crit:
            add("驻点（f'(x)=0）", crit[:8])
        lines.insert(0, f"导数: f'(x) = {latex(d)}")
    except Exception:
        pass
    # 极限
    try:
        import sympy as sm

        lims = []
        for infp in (sm.oo, -sm.oo):
            try:
                v = sm.limit(f, x, infp)
                lims.append(f"x→{infp}: {v if v.is_number else latex(v)}")
            except Exception:
                pass
        if lims:
            add("无穷远极限", lims)
    except Exception:
        pass

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


# ---------------------------------------------------------------------------
# 控制工程工具（需要 python-control + scipy）
# ---------------------------------------------------------------------------
_ctrl_cache = None


def _get_ctrl():
    global _ctrl_cache
    if _ctrl_cache is None:
        import control as ctrl  # 惰性导入，避免拖慢启动

        _ctrl_cache = ctrl
    return _ctrl_cache


def _err(msg: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": msg}]}


def _ctrl_result(text: str, fig) -> dict:
    """绘图工具统一出口：图片落盘 + 文本返回（不含 MCP image 内容块）。

    返回文本带一行 markdown 图片引用（![plot](url)），模型须原样写进最终回复，
    这样图片才会显示在对话里。工具结果保持纯文本：MCP image 内容块会被 RikkaHub
    编码成 image_url 转发给模型服务商，DeepSeek 等 OpenAI 兼容服务商会拒绝
    （unknown variant image_url），导致对话中断；而回复文本中的 markdown 图片
    只用于渲染、不会出站，已在第四次测试验证安全。
    """
    png = render_png(fig)
    url_path = save_plot(png)
    text += "\n\n" + INSTRUCTION + "\n" + plot_markdown(url_path)
    return {"content": [{"type": "text", "text": text}]}


def parse_transfer_function(tf_str: str):
    """解析传递函数字符串（如 '(s+1)/(s^2+3*s+2)'）→ (sys, sympy_expr, err)。"""
    s_sym = Symbol("s")
    expr, err = safe_parse(tf_str)
    if err:
        return None, None, err
    assert expr is not None
    try:
        import sympy as sm

        num, den = expr.as_numer_denom()
        num_p = sm.Poly(num, s_sym)
        den_p = sm.Poly(den, s_sym)
        num_c = [float(c) for c in num_p.all_coeffs()]
        den_c = [float(c) for c in den_p.all_coeffs()]
        if not den_c or all(abs(c) < 1e-12 for c in den_c):
            return None, None, "分母多项式无效"
        ctrl = _get_ctrl()
        sys = ctrl.TransferFunction(num_c, den_c)
        assert sys is not None
        return sys, expr, None
    except Exception as e:
        return None, None, f"传递函数解析失败（需为 s 的有理多项式）: {e}"


def _fmt_cplx(p) -> str:
    try:
        r, im = float(np.real(p)), float(np.imag(p))
    except (TypeError, ValueError):
        return str(p)
    return f"{r:+.4g}{im:+.4g}j" if abs(im) > 1e-12 else f"{r:.4g}"


def _stability_text(poles) -> str:
    real = np.real(poles)
    if np.any(real > 1e-12):
        return "不稳定（右半平面存在极点）"
    if np.any(np.abs(real) <= 1e-12):
        return "临界稳定（存在虚轴极点，含原点）"
    return "稳定"


def plot_root_locus(transfer_function: str, title: str = ""):
    """根轨迹图。transfer_function 为开环传递函数 G(s)，如 '(s+1)/(s*(s^2+s+1))'。"""
    sys, expr, err = parse_transfer_function(transfer_function)
    if err:
        return _err(err)
    assert sys is not None and expr is not None
    ctrl = _get_ctrl()
    fig, ax = plt.subplots(figsize=(8, 7))
    try:
        ctrl.root_locus(sys, plot=True, grid=False)
    except Exception as e:
        plt.close(fig)
        return _err(f"根轨迹绘制失败: {e}")
    ax.axhline(0, color="gray", lw=0.8)
    ax.axvline(0, color="gray", lw=0.8)
    ax.set_title(title or "Root Locus")
    ax.grid(True, alpha=0.3)
    text = f"已绘制根轨迹: G(s) = {latex(expr)}（原式: {transfer_function}）"
    try:
        poles = np.asarray(sys.poles(), dtype=complex)
        zeros = np.asarray(sys.zeros(), dtype=complex)
        text += "\n开环极点: " + ", ".join(_fmt_cplx(p) for p in poles)
        if zeros.size:
            text += "\n开环零点: " + ", ".join(_fmt_cplx(z) for z in zeros)
    except Exception:
        pass
    return _ctrl_result(text, fig)


def plot_bode(
    transfer_function: str,
    omega_min: float = 0.01,
    omega_max: float = 1000.0,
    title: str = "",
):
    """波特图（幅频 + 相频，双面板）。omega 单位 rad/s。"""
    sys, expr, err = parse_transfer_function(transfer_function)
    if err:
        return _err(err)
    assert sys is not None and expr is not None
    ctrl = _get_ctrl()
    try:
        om_min = coerce_number(omega_min, "omega_min")
        om_max = coerce_number(omega_max, "omega_max")
    except ValueError as e:
        return _err(str(e))
    if om_min <= 0 or om_max <= om_min:
        return _err("omega_min/omega_max 需满足 0 < omega_min < omega_max")
    omega = np.logspace(np.log10(om_min), np.log10(om_max), 600)
    try:
        mag, phase, wout = ctrl.bode_plot(sys, omega=omega, plot=False)
    except Exception as e:
        return _err(f"波特图计算失败: {e}")
    mag = np.asarray(mag, dtype=float)
    phase = np.asarray(phase, dtype=float)
    wout = np.asarray(wout, dtype=float)
    with np.errstate(all="ignore"):
        mag_db = 20.0 * np.log10(np.maximum(mag, 1e-12))
    phase_deg = np.degrees(phase)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 7), sharex=True)
    ax1.semilogx(wout, mag_db, lw=2)
    ax1.set_ylabel("Magnitude (dB)")
    ax1.grid(True, which="both", alpha=0.3)
    ax2.semilogx(wout, phase_deg, lw=2)
    ax2.set_ylabel("Phase (deg)")
    ax2.set_xlabel("Frequency (rad/s)")
    ax2.grid(True, which="both", alpha=0.3)
    fig.suptitle(title or "Bode Diagram")

    text = f"已绘制波特图: G(s) = {latex(expr)}（原式: {transfer_function}）"
    try:
        gm, pm, wg, wp = ctrl.margin(sys)
        if np.isfinite(pm):
            text += f"\n相位裕度 PM: {pm:.2f}°" + (
                f"（增益穿越 ω = {wg:.4g} rad/s）" if np.isfinite(wg) else ""
            )
        if np.isfinite(gm):
            text += (
                f"\n增益裕度 GM: {gm:.4g}（线性）≈ {20.0 * np.log10(max(float(gm), 1e-12)):.2f} dB"
                + (f"（相位穿越 ω = {wp:.4g} rad/s）" if np.isfinite(wp) else "")
            )
        else:
            text += "\n增益裕度 GM: ∞（相位曲线不穿越 -180°）"
    except Exception:
        pass
    return _ctrl_result(text, fig)


def plot_nyquist(transfer_function: str, title: str = ""):
    """奈奎斯特图（含临界点 -1）。

    注意：必须用 nyquist_response().response 的数据绘图。老 API
    nyquist_plot(plot=False, return_contour=True) 返回的 contour 是 s 平面的
    D 围道本身（实部≈0 的虚轴），不是 G(jω) 曲线——直接用会把图画成一条竖线。
    """
    sys, expr, err = parse_transfer_function(transfer_function)
    if err:
        return _err(err)
    assert sys is not None and expr is not None
    ctrl = _get_ctrl()
    try:
        resp = ctrl.nyquist_response(sys)
        enc = int(resp.count)
    except Exception as e:
        return _err(f"奈奎斯特图计算失败: {e}")
    fig, ax = plt.subplots(figsize=(7, 7))
    try:
        # control 的规范绘图：正确的 G(jω) 曲线 + 虚轴极点凹陷弧 + 临界点标记
        resp.plot(ax=ax)
        ax.set_title(title or "Nyquist Diagram")
    except Exception as e:
        plt.close(fig)
        return _err(f"奈奎斯特图绘制失败: {e}")
    poles = np.asarray(sys.poles(), dtype=complex)
    try:
        p_rhp = int(np.sum(np.real(poles) > 1e-12))  # 严格右半平面（虚轴极点不计入）
    except (TypeError, ValueError):
        p_rhp = 0
    # 经实测校准：control 的环绕数为带符号值（顺时针为正），判据为 Z = P + N
    z = p_rhp + enc
    verdict = (
        "稳定"
        if z == 0
        else (
            f"不稳定（右半平面 {z} 个闭环极点）"
            if z > 0
            else "判据边界情况（存在虚轴极点）"
        )
    )
    text = f"已绘制奈奎斯特图: G(s) = {latex(expr)}（原式: {transfer_function}）"
    text += f"\n临界点 -1 环绕次数 N = {enc}；开环右半平面极点数 P = {p_rhp}"
    text += f"\n闭环右半平面极点数 Z = P + N = {z} → 闭环{verdict}"
    return _ctrl_result(text, fig)


def plot_nichols(transfer_function: str, title: str = ""):
    """尼科尔斯图（含 M/N 等值线网格）。"""
    sys, expr, err = parse_transfer_function(transfer_function)
    if err:
        return _err(err)
    assert sys is not None and expr is not None
    ctrl = _get_ctrl()
    fig, ax = plt.subplots(figsize=(8, 6))
    try:
        ctrl.nichols_plot(sys, ax=ax, grid=True)
    except Exception as e:
        plt.close(fig)
        return _err(f"尼科尔斯图绘制失败: {e}")
    ax.set_title(title or "Nichols Chart")
    text = f"已绘制尼科尔斯图: G(s) = {latex(expr)}（原式: {transfer_function}）\n（横轴相位 deg，纵轴幅值 dB）"
    try:
        gm, pm, wg, wp = ctrl.margin(sys)
        if np.isfinite(pm):
            text += f"\n相位裕度 PM: {pm:.2f}°"
        if np.isfinite(gm):
            text += f"\n增益裕度 GM: {gm:.4g}（线性）≈ {20.0 * np.log10(max(float(gm), 1e-12)):.2f} dB"
    except Exception:
        pass
    return _ctrl_result(text, fig)


def plot_step_response(transfer_function: str, t_end: float = 10.0, title: str = ""):
    """单位阶跃响应曲线。"""
    sys, expr, err = parse_transfer_function(transfer_function)
    if err:
        return _err(err)
    assert sys is not None and expr is not None
    ctrl = _get_ctrl()
    try:
        te = coerce_number(t_end, "t_end")
    except ValueError as e:
        return _err(str(e))
    if te <= 0 or te > 1e6:
        te = 10.0
    T = np.linspace(0, te, 800)
    try:
        resp = ctrl.step_response(sys, T=T)
    except Exception as e:
        return _err(f"阶跃响应计算失败: {e}")
    t = np.asarray(resp.t, dtype=float)
    y = np.asarray(resp.y, dtype=float).reshape(-1)
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(t, y, lw=2)
    ax.axhline(0, color="gray", lw=0.8)
    ax.grid(True, alpha=0.3)
    ax.set_title(title or "Step Response")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Amplitude")
    final = 0.0
    peak = 0.0
    tpk = 0.0
    try:
        if y.size:
            final = float(y[-1])
            peak = float(np.max(y))
            tpk = float(t[np.argmax(y)])
    except (TypeError, ValueError):
        pass
    overshoot = (peak - final) / final if abs(final) > 1e-12 else 0.0
    text = f"已绘制阶跃响应: G(s) = {latex(expr)}（原式: {transfer_function}）"
    text += f"\n稳态值: {final:.4g}"
    if overshoot > 0.005:
        text += f"\n超调量: {overshoot * 100:.2f}% @ t = {tpk:.4g} s"
    return _ctrl_result(text, fig)


def plot_pzmap(transfer_function: str, title: str = ""):
    """零极点图。"""
    sys, expr, err = parse_transfer_function(transfer_function)
    if err:
        return _err(err)
    assert sys is not None and expr is not None
    poles = np.asarray(sys.poles(), dtype=complex)
    zeros = np.asarray(sys.zeros(), dtype=complex)
    fig, ax = plt.subplots(figsize=(7, 7))
    if zeros.size:
        ax.plot(
            np.real(zeros), np.imag(zeros), "o", mfc="none", ms=10, mew=2, label="Zeros"
        )
    ax.plot(np.real(poles), np.imag(poles), "x", ms=12, mew=2.5, label="Poles")
    ax.axhline(0, color="gray", lw=0.8)
    ax.axvline(0, color="gray", lw=0.8)
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, alpha=0.3)
    ax.legend()
    ax.set_title(title or "Pole-Zero Map")
    text = f"已绘制零极点图: G(s) = {latex(expr)}（原式: {transfer_function}）"
    text += "\n极点: " + ", ".join(_fmt_cplx(p) for p in poles)
    if zeros.size:
        text += "\n零点: " + ", ".join(_fmt_cplx(z) for z in zeros)
    text += f"\n开环稳定性: {_stability_text(poles)}"
    return _ctrl_result(text, fig)


def analyze_transfer_function(transfer_function: str):
    """传递函数文本分析：阶次/型别、极点零点、稳定性、稳定裕度、阻尼比。"""
    sys, expr, err = parse_transfer_function(transfer_function)
    if err:
        return _err(err)
    assert sys is not None and expr is not None
    ctrl = _get_ctrl()
    lines = [f"## 传递函数分析: G(s) = {latex(expr)}", f"原式: {transfer_function}", ""]
    poles = np.asarray(sys.poles(), dtype=complex)
    zeros = np.asarray(sys.zeros(), dtype=complex)
    den_c = sys.den[0][0]
    order = len(den_c) - 1
    try:
        type_n = int(np.sum(np.abs(poles) < 1e-12))
    except (TypeError, ValueError):
        type_n = 0
    lines.append(f"**系统阶次**: {order} 阶，{type_n} 型（含 {type_n} 个积分环节）")
    lines.append("")
    lines.append("**极点**: " + ", ".join(_fmt_cplx(p) for p in poles))
    if zeros.size:
        lines.append("**零点**: " + ", ".join(_fmt_cplx(z) for z in zeros))
    lines.append("")
    lines.append(f"**开环稳定性**: {_stability_text(poles)}")
    if bool(np.all(np.real(poles) < 0)):
        try:
            gm, pm, wg, wp = ctrl.margin(sys)
            parts = []
            if np.isfinite(pm):
                parts.append(
                    f"相位裕度 PM = {pm:.2f}°"
                    + (f" @ ω = {wg:.4g} rad/s" if np.isfinite(wg) else "")
                )
            if np.isfinite(gm):
                parts.append(
                    f"增益裕度 GM = {gm:.4g}（线性）≈ {20.0 * np.log10(max(float(gm), 1e-12)):.2f} dB"
                    + (f" @ ω = {wp:.4g} rad/s" if np.isfinite(wp) else "")
                )
            else:
                parts.append("增益裕度 GM = ∞（相位不穿越 -180°）")
            if parts:
                lines.append("**稳定裕度**: " + "；".join(parts))
        except Exception:
            pass
        try:
            wn, zeta, dp = ctrl.damp(sys)
            pairs = [
                f"ω_n = {w:.4g} rad/s, ζ = {z:.4g}"
                for w, z in zip(np.asarray(wn), np.asarray(zeta), strict=True)
            ]
            lines.append("**二阶模态**: " + "；".join(pairs))
        except Exception:
            pass
    try:
        dc = ctrl.dcgain(sys)
        dc_v = float(np.real(dc)) if np.isfinite(np.real(dc)) else None
        lines.append("")
        if dc_v is not None:
            lines.append(f"**直流增益 G(0)**: {dc_v:.4g}")
        else:
            lines.append("**直流增益 G(0)**: ∞（含积分环节，阶跃输入稳态输出发散）")
    except Exception:
        pass
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


# ---------------------------------------------------------------------------
# MCP Streamable HTTP 服务器
# ---------------------------------------------------------------------------
SERVER_NAME = "MathPlotMCP"
SERVER_VERSION = "1.4.1"
TOOLS = [
    {
        "name": "plot_function",
        "description": "绘制一元函数 y=f(x) 的图像，返回 PNG 图片和文本摘要。"
        "expr 示例: 'sin(x)', 'x**2', 'exp(-x**2/10)', 'log(x)', '1/(1+x**2)'。"
        "支持 + - * / ** 和常见函数 sin cos tan exp log sqrt abs pi e。"
        "重要：返回文本中会包含一行以 ![plot]( 开头的 markdown 图片引用，"
        "你必须在最终回复中原样包含这一行（一字不改），这样图片才能显示在对话中。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expr": {"type": "string", "description": "函数表达式，如 'sin(x)'"},
                "x_min": {"type": "number", "description": "x 下界（默认 -10）"},
                "x_max": {"type": "number", "description": "x 上界（默认 10）"},
                "y_min": {"type": "number", "description": "y 显示下界（可选）"},
                "y_max": {"type": "number", "description": "y 显示上界（可选）"},
                "title": {
                    "type": "string",
                    "description": "图标题（可选，默认自动生成）",
                },
            },
            "required": ["expr"],
        },
    },
    {
        "name": "plot_multiple_functions",
        "description": "在同一坐标系绘制多个函数对比，返回 PNG 图片和文本摘要。"
        "重要：返回文本中会包含一行以 ![plot]( 开头的 markdown 图片引用，"
        "你必须在最终回复中原样包含这一行（一字不改），这样图片才能显示在对话中。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expressions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "函数表达式列表，如 ['sin(x)', 'x**2']",
                },
                "x_min": {"type": "number"},
                "x_max": {"type": "number"},
                "title": {"type": "string"},
            },
            "required": ["expressions"],
        },
    },
    {
        "name": "plot_implicit",
        "description": "绘制隐式方程（如圆 x**2+y**2-1=0、椭圆、双曲线），返回 PNG 图片和文本摘要。"
        "重要：返回文本中会包含一行以 ![plot]( 开头的 markdown 图片引用，"
        "你必须在最终回复中原样包含这一行（一字不改），这样图片才能显示在对话中。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expr": {
                    "type": "string",
                    "description": "隐式方程表达式，如 'x**2+y**2-1'（=0 省略）",
                },
                "x_min": {"type": "number", "description": "默认 -10"},
                "x_max": {"type": "number", "description": "默认 10"},
                "y_min": {"type": "number", "description": "默认 -10"},
                "y_max": {"type": "number", "description": "默认 10"},
            },
            "required": ["expr"],
        },
    },
    {
        "name": "analyze_formula",
        "description": "分析函数：化简、定义域、奇偶性、周期性、驻点、无穷远极限。纯文本返回。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expr": {"type": "string", "description": "函数表达式，如 'sin(x)/x'"},
            },
            "required": ["expr"],
        },
    },
    {
        "name": "plot_root_locus",
        "description": "根轨迹图（root locus）。transfer_function 为开环传递函数 G(s) 字符串，"
        "如 '(s+1)/(s*(s^2+s+1))'、'1/(s*(s+2)*(s+4))'、'k/(s^2+2*s+2)'。"
        "重要：返回文本中会包含一行以 ![plot]( 开头的 markdown 图片引用，"
        "你必须在最终回复中原样包含这一行（一字不改），这样图片才能显示在对话中。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transfer_function": {
                    "type": "string",
                    "description": "开环传递函数 G(s)，如 '(s+1)/(s*(s^2+s+1))'（用 s 表示拉氏变量，支持 * / ** 和括号）",
                },
                "title": {"type": "string", "description": "图标题（可选）"},
            },
            "required": ["transfer_function"],
        },
    },
    {
        "name": "plot_bode",
        "description": "波特图（Bode）：幅频 + 相频双面板，频率轴 rad/s。"
        "transfer_function 示例 '(s+1)/(s^2+3*s+2)'、'10/(s*(s+1))'。"
        "重要：返回文本中会包含一行以 ![plot]( 开头的 markdown 图片引用，"
        "你必须在最终回复中原样包含这一行（一字不改），这样图片才能显示在对话中。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transfer_function": {
                    "type": "string",
                    "description": "传递函数 G(s)",
                },
                "omega_min": {
                    "type": "number",
                    "description": "频率下界 rad/s（默认 0.01）",
                },
                "omega_max": {
                    "type": "number",
                    "description": "频率上界 rad/s（默认 1000）",
                },
            },
            "required": ["transfer_function"],
        },
    },
    {
        "name": "plot_nyquist",
        "description": "奈奎斯特图（Nyquist），标注临界点 -1，并按奈奎斯特判据给出闭环稳定性结论。"
        "支持可选 title 参数。"
        "transfer_function 示例 '1/(s*(s+1)*(s+2))'、'(s+2)/(s^2-1)'。"
        "重要：返回文本中会包含一行以 ![plot]( 开头的 markdown 图片引用，"
        "你必须在最终回复中原样包含这一行（一字不改），这样图片才能显示在对话中。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transfer_function": {
                    "type": "string",
                    "description": "开环传递函数 G(s)",
                },
            },
            "required": ["transfer_function"],
        },
    },
    {
        "name": "plot_nichols",
        "description": "尼科尔斯图（Nichols Chart），含 M/N 等值线网格，可直接读出闭环幅值/相位。"
        "支持可选 title 参数。"
        "transfer_function 示例 '10/(s*(s+1))'。"
        "重要：返回文本中会包含一行以 ![plot]( 开头的 markdown 图片引用，"
        "你必须在最终回复中原样包含这一行（一字不改），这样图片才能显示在对话中。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transfer_function": {
                    "type": "string",
                    "description": "开环传递函数 G(s)",
                },
            },
            "required": ["transfer_function"],
        },
    },
    {
        "name": "plot_step_response",
        "description": "单位阶跃响应曲线（step response），附带稳态值与超调量。"
        "支持可选 title 参数。"
        "transfer_function 示例 '10/(s^2+2*s+10)'、'1/(s+1)'。"
        "重要：返回文本中会包含一行以 ![plot]( 开头的 markdown 图片引用，"
        "你必须在最终回复中原样包含这一行（一字不改），这样图片才能显示在对话中。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transfer_function": {
                    "type": "string",
                    "description": "传递函数 G(s)",
                },
                "t_end": {"type": "number", "description": "仿真时长 s（默认 10）"},
            },
            "required": ["transfer_function"],
        },
    },
    {
        "name": "plot_pzmap",
        "description": "零极点图（Pole-Zero Map），判断开环稳定性。"
        "支持可选 title 参数。"
        "transfer_function 示例 '(s+1)/(s^2+3*s+2)'。"
        "重要：返回文本中会包含一行以 ![plot]( 开头的 markdown 图片引用，"
        "你必须在最终回复中原样包含这一行（一字不改），这样图片才能显示在对话中。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transfer_function": {
                    "type": "string",
                    "description": "传递函数 G(s)",
                },
            },
            "required": ["transfer_function"],
        },
    },
    {
        "name": "analyze_transfer_function",
        "description": "传递函数文本分析：阶次与型别、极点零点、开环稳定性、稳定裕度（GM/PM）、"
        "阻尼比与自然频率、直流增益。纯文本返回。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transfer_function": {
                    "type": "string",
                    "description": "传递函数 G(s)，如 '(s+1)/(s*(s+2)*(s+4))' 或 '10/(s^2+2*s+10)'",
                },
            },
            "required": ["transfer_function"],
        },
    },
]


def _tool_result(payload):
    return {"jsonrpc": "2.0", "id": payload.get("id"), "result": payload.get("result")}


def handle_jsonrpc(msg):
    """msg: parsed JSON-RPC dict. Returns (response_dict_or_None, is_notification)."""
    method = msg.get("method", "")
    mid = msg.get("id")

    if method == "initialize":
        client_ver = (msg.get("params") or {}).get("protocolVersion", "2025-03-26")
        proto = client_ver if str(client_ver).startswith("2025-") else "2025-03-26"
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }, False

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}, False

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}, False

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if name not in (
            "plot_function",
            "plot_multiple_functions",
            "plot_implicit",
            "analyze_formula",
            "plot_root_locus",
            "plot_bode",
            "plot_nyquist",
            "plot_nichols",
            "plot_step_response",
            "plot_pzmap",
            "analyze_transfer_function",
        ):
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32602, "message": f"未知工具: {name}"},
            }, False
        try:
            result = {
                "plot_function": plot_function,
                "plot_multiple_functions": plot_multiple_functions,
                "plot_implicit": plot_implicit,
                "analyze_formula": analyze_formula,
                "plot_root_locus": plot_root_locus,
                "plot_bode": plot_bode,
                "plot_nyquist": plot_nyquist,
                "plot_nichols": plot_nichols,
                "plot_step_response": plot_step_response,
                "plot_pzmap": plot_pzmap,
                "analyze_transfer_function": analyze_transfer_function,
            }[name](**args)
        except TypeError as e:
            result = {
                "isError": True,
                "content": [{"type": "text", "text": f"参数错误: {e}"}],
            }
        except Exception as e:
            result = {
                "isError": True,
                "content": [{"type": "text", "text": f"执行出错: {e}"}],
            }
        return {"jsonrpc": "2.0", "id": mid, "result": result}, False

    # 通知类（无 id）：不响应
    if mid is None:
        return None, True

    return {
        "jsonrpc": "2.0",
        "id": mid,
        "error": {"code": -32601, "message": f"未知方法: {method}"},
    }, False


SESSION_IDS = set()


class MCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MathPlotMCP/1.0"

    def log_message(self, format, *args):
        sys.stderr.write(f"[{self.address_string()}] {format % args}\n")

    def _cors(self):
        # 原生客户端（RikkaHub）不发 Origin，无需 CORS；仅当来源为回环地址时才放行，
        # 便于浏览器里的 MCP Inspector 调试。
        origin = self.headers.get("Origin", "")
        allowed = (
            ("" or origin) in ("", "null")
            or "127.0.0.1" in origin
            or "localhost" in origin
        )
        if not allowed:
            return
        self.send_header("Access-Control-Allow-Origin", origin or "null")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Accept, Mcp-Session-Id, Authorization",
        )
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
        self.send_header("Vary", "Origin")

    def _send_json(self, obj, status=200, session=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if session:
            self.send_header("Mcp-Session-Id", session)
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/plots/"):
            self._serve_plot(path)
            return
        if path in ("/health", "/"):
            self._send_json(
                {
                    "ok": True,
                    "server": SERVER_NAME,
                    "version": SERVER_VERSION,
                    "tools": [t["name"] for t in TOOLS],
                }
            )
            return
        if path == "/mcp":
            # SSE 流（用于服务端通知；客户端可选择性使用）
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self._cors()
                self.end_headers()
                self.wfile.write(b"event: endpoint\r\ndata: {}\r\n\r\n")
                self.wfile.flush()
                # 心跳，保持连接
                while True:
                    self.wfile.write(b": keep-alive\r\n\r\n")
                    self.wfile.flush()
                    threading.Event().wait(15)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        self._send_json({"error": "not found"}, status=404)

    def _serve_plot(self, path: str):
        """服务 /plots/<id>.png：供 RikkaHub 的 markdown 渲染器（Coil）加载图片。"""
        name = os.path.basename(path)
        if not _PLOT_NAME_RE.fullmatch(name):
            self._send_json({"error": "bad name"}, status=404)
            return
        fp = os.path.join(PLOTS_DIR, name)
        try:
            with open(fp, "rb") as fh:
                data = fh.read()
        except OSError:
            self._send_json({"error": "not found"}, status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/mcp":
            self._send_json({"error": "not found"}, status=404)
            return
        _tls.host = self.headers.get(
            "Host"
        )  # 供 /plots 图片 URL 使用（客户端怎么连就怎么取图）
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            msg = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32700, "message": f"解析失败: {e}"},
                },
                status=400,
            )
            return

        session = self.headers.get("Mcp-Session-Id")
        if not session:
            session = str(uuid.uuid4())
        SESSION_IDS.add(session)

        try:
            resp, is_notif = handle_jsonrpc(msg)
        except Exception as e:
            resp = {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "error": {"code": -32603, "message": f"内部错误: {e}"},
            }
            is_notif = False

        if is_notif:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()
            return
        self._send_json(resp, session=session)


def main():
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("MCP_PORT", "8000"))
    except ValueError:
        port = 8000
    srv = ThreadingHTTPServer((host, port), MCPHandler)
    print(f"MathPlot MCP listening on http://{host}:{port}/mcp", flush=True)
    print(f"Tools: {[t['name'] for t in TOOLS]}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
