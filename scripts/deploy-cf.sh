#!/bin/bash
# omaps.cn 官网部署到 Cloudflare Workers
# 用法：bash scripts/deploy-cf.sh
# 说明：站点源码 = Hugo 工程（content/ + layouts/ + static/ + hugo.toml），
#       dist/ 为构建产物（.gitignore 已忽略），每次部署前重新构建。
#       GitHub Pages 走 .github/workflows/pages.yml（同样从 Hugo 构建）。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

HUGO_VERSION="0.170.1"

# 0. 确认 hugo 可用（本地：brew install hugo；CI 由 workflow 注入固定版本）
if ! command -v hugo >/dev/null 2>&1; then
    echo "错误：未找到 hugo 命令，请先安装（brew install hugo），版本需 >= ${HUGO_VERSION}" >&2
    exit 1
fi

# 1. 构建产物（不加 --minify：保证产物与验收通过的基线一致）
hugo --gc -d dist

echo "dist 构建产物：$(find dist -type f | wc -l | tr -d ' ') 个文件"

# 2. 部署到 Cloudflare Workers（--config 绝对路径，防止 cwd 漂移误部署）
npx -y wrangler@latest deploy --config "$REPO_ROOT/wrangler.jsonc"
