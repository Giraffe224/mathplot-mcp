#!/data/data/com.termux/files/usr/bin/bash
# ===== MathPlot MCP 服务器 手动启动 =====
# 用法: mathplot        前台启动（Ctrl+C 停止，日志直接显示）
#       mathplot -d     后台启动（nohup，日志 ~/mathplot_mcp.log）
#       mathplot -k     停止服务器
#       mathplot -s     查看状态
# 连接地址: http://127.0.0.1:8000/mcp  (Streamable HTTP)
# ========================================
export PREFIX=/data/data/com.termux/files/usr
export HOME=/data/data/com.termux/files/home
export TMPDIR=$PREFIX/tmp
export LD_LIBRARY_PATH=$PREFIX/lib
export PATH=$PREFIX/bin:$PATH
cd "$HOME" || exit 1

# 查找已运行的 mathplot_mcp 进程
_get_pid() { pgrep -x python -a 2>/dev/null | grep mathplot_mcp | head -1 | awk '{print $1}'; }

# 停止
if [ "$1" = "-k" ]; then
    pid=$(_get_pid)
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null
        sleep 1
        # 确认已停止
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null
            echo "已强制停止 MathPlot MCP (PID $pid)"
        else
            echo "已停止 MathPlot MCP (PID $pid)"
        fi
    else
        echo "MathPlot MCP 未在运行"
    fi
    exit 0
fi

# 状态
if [ "$1" = "-s" ]; then
    pid=$(_get_pid)
    if [ -n "$pid" ]; then
        echo "MathPlot MCP 运行中 (PID $pid)"
        curl -s -m 2 http://127.0.0.1:8000/health 2>/dev/null && echo || echo "(健康检查无响应)"
    else
        echo "MathPlot MCP 未在运行"
    fi
    exit 0
fi

# 防重复启动：检查是否有 mathplot_mcp 在跑
existing=$(_get_pid)
if [ -n "$existing" ]; then
    echo "MathPlot MCP 已在运行 (PID $existing)"
    echo "  停止:  mathplot -k"
    echo "  状态:  mathplot -s"
    exit 1
fi

# 后台模式
if [ "$1" = "-d" ]; then
    echo "MathPlot MCP 后台启动中... 日志: ~/mathplot_mcp.log"
    nohup python mathplot_mcp.py > mathplot_mcp.log 2>&1 &
    echo "PID: $!"
    exit 0
fi

# 前台模式
echo "MathPlot MCP 前台启动中..."
echo "RikkaHub 连接: http://127.0.0.1:8000/mcp (Streamable HTTP)"
echo "按 Ctrl+C 停止"
exec python mathplot_mcp.py
