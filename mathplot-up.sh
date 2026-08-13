#!/data/data/com.termux/files/usr/bin/bash
# ===== MathPlot MCP 服务器 手动启动 =====
# 用法: mathplot        前台启动（Ctrl+C 停止，日志直接显示）
#       mathplot -d     后台启动（nohup，日志 ~/mathplot_mcp.log）
# 连接地址: http://127.0.0.1:8000/mcp  (Streamable HTTP)
# ========================================
export PREFIX=/data/data/com.termux/files/usr
export HOME=/data/data/com.termux/files/home
export TMPDIR=$PREFIX/tmp
export LD_LIBRARY_PATH=$PREFIX/lib
export PATH=$PREFIX/bin:$PATH
cd "$HOME"

if [ "$1" = "-d" ]; then
	echo "MathPlot MCP 后台启动中... 日志: ~/mathplot_mcp.log"
	nohup python mathplot_mcp.py >mathplot_mcp.log 2>&1 &
	echo "PID: $!"
	exit 0
fi

echo "MathPlot MCP 前台启动中..."
echo "RikkaHub 连接: http://127.0.0.1:8000/mcp (Streamable HTTP)"
echo "按 Ctrl+C 停止"
exec python mathplot_mcp.py
