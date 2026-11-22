#!/bin/bash
# omaps.cn 官网部署到 Cloudflare Workers
# 用法：bash scripts/deploy-cf.sh
# 说明：仓库根目录 = 站点本体（GitHub Pages 从根目录发布），CF 侧只部署 dist/ 快照。
#       dist/ 为构建产物（.gitignore 已忽略），每次部署前重新 rsync。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# 1. 生成部署快照（排除非站点文件）
mkdir -p dist
rsync -a --delete \
    --exclude '.git/' \
    --exclude '.github/' \
    --exclude '.gitignore' \
    --exclude '.DS_Store' \
    --exclude 'README.md' \
    --exclude 'tools/' \
    --exclude 'CNAME' \
    --exclude 'wrangler.jsonc' \
    --exclude 'scripts/' \
    --exclude 'dist/' \
    --exclude 'logs/' \
    ./ dist/

echo "dist 快照：$(find dist -type f | wc -l | tr -d ' ') 个文件"

# 2. 部署到 Cloudflare Workers（--config 绝对路径，防止 cwd 漂移误部署）
npx -y wrangler@latest deploy --config "$REPO_ROOT/wrangler.jsonc"
