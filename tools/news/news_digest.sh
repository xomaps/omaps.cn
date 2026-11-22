#!/bin/bash
# 每日行业热点抓取发布（omaps.cn 官网 news/ 栏目）
# 2026-11-22 随官网迁出 deploy-alone：服务器 cron 已撤销，本脚本改为本地/CI 手动或定时调用。
# 本地执行：bash tools/news/news_digest.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_DIR="${REPO_ROOT}/logs"
LOG_FILE="${LOG_DIR}/news_digest.log"
LOCK_FILE="/tmp/omaps_news_digest.lock"

mkdir -p "${LOG_DIR}" 2>/dev/null || true
exec >> "${LOG_FILE}" 2>&1

echo "[news_digest.sh] ===== 开始执行: $(date '+%F %T') ====="

# 防止重叠执行（抓取+写页面通常 < 1 分钟）
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "[news_digest.sh] 已有实例在运行，本次跳过"
    exit 0
fi

PYTHON_BIN="$(command -v python3 || true)"
if [ -z "${PYTHON_BIN}" ]; then
    echo "[news_digest.sh] 错误: 未找到 python3"
    exit 1
fi

cd "${REPO_ROOT}" || exit 1
"${PYTHON_BIN}" "${REPO_ROOT}/tools/news/news_digest.py"
RC=$?

if [ ${RC} -eq 0 ]; then
    echo "[news_digest.sh] ===== 执行成功: $(date '+%F %T') ====="
else
    echo "[news_digest.sh] ===== 执行失败(rc=${RC}): $(date '+%F %T') ====="
fi
exit ${RC}
