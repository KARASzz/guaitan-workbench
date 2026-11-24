#!/bin/bash
# ==============================================================================
# 怪谈·交互式文字生产工作台 - Cloud Run & Cloud SQL 批处理可视化运维启动器
# ==============================================================================

cd "$(dirname "$0")"

echo "=================================================================="
echo "  🔮 正在启动 怪谈工作台 Cloud Run 批处理可视化运维工作台..."
echo "  📂 工作区路径: $(pwd)"
echo "=================================================================="

PYTHON_BIN="$(which python3)"
if [ -z "$PYTHON_BIN" ]; then
    if [ -f "/usr/bin/python3" ]; then
        PYTHON_BIN="/usr/bin/python3"
    elif [ -f "/usr/local/bin/python3" ]; then
        PYTHON_BIN="/usr/local/bin/python3"
    elif [ -f "/opt/homebrew/bin/python3" ]; then
        PYTHON_BIN="/opt/homebrew/bin/python3"
    fi
fi

if [ -z "$PYTHON_BIN" ]; then
    echo "❌ 未检测到 Python 3 环境，请确认已安装 Python。"
    read -p "按回车键退出..."
    exit 1
fi

echo "✅ 使用 Python 解释器: $PYTHON_BIN"
echo "🌐 正在启动本地可视化 Web 控制台 (端口 8586)..."

"$PYTHON_BIN" cloud_run_ops_workbench.py
