# omaps.cn 官网（www.omaps.cn 静态站）

> 2026-11-22 自 deploy-alone 仓迁出，改为仓库托管、发布走托管服务（GitHub Pages），不再由单机版服务器 nginx 对外服务。
> 迁出前最后版本：deploy-alone f7e3d6c（含品牌 square-clean、insights 五篇、news 快照）。

## 仓库内容

| 路径 | 说明 |
|---|---|
| `index.html` `css/` `js/` `images/` `insights/` | 官网静态站本体（原样迁入，未改内容） |
| `CNAME` | GitHub Pages 自定义域名 www.omaps.cn |
| `wrangler.jsonc` `scripts/deploy-cf.sh` | Cloudflare Workers 部署（worker 名 omaps-cn，见下节） |

站内品牌署名已全量为「信朴 / Xomaps」（2026-11-23 复核，html/css/js 无「麦朴/麦扑/Xinpure」残留）；`dist/` 为旧快照（gitignore），下次部署由 `scripts/deploy-cf.sh` 的 rsync --delete 重建。

## 每日热点（news）已下线（2026-11-23）

首页「每日热点 Daily News」板块、页脚与洞察页入口、`news/` 栏目页、抓取脚本 `tools/news/` 已全部移除，sitemap 中对应条目同步删除。此前 cron 与 GitHub Actions 两种跑法均不再需要；如未来要恢复栏目，从 git 历史找回 `news/`、`tools/news/` 即可。

## Cloudflare Workers 部署（2026-11-22 加）

与 xomaps.com 仓同套路：`bash scripts/deploy-cf.sh`（rsync 站点文件到 dist/ 快照后 wrangler deploy），worker 名 **omaps-cn**（CF worker 名不允许带点）。

- 当前可访问：https://omaps-cn.jokeway.workers.dev
- `not_found_handling: single-page-application` 与原 nginx `try_files … /index.html` 兜底行为一致
- ⚠️ **域名绑定暂不可行**：omaps.cn 的 DNS 在 DNSPod、zone 不在 Cloudflare 账号（deploy 实测 `Can't infer zone from route`）。Worker 要接 www.omaps.cn 必须先把 zone 迁到 Cloudflare（或 DNSPod 处把 www CNAME 到 workers.dev——**不可行**，workers.dev 自定义域名同样要求 zone 在 CF）。迁 zone 后在 wrangler.jsonc 加 `routes: [{ pattern: "www.omaps.cn", zone_name: "omaps.cn" }]` 重新部署即可。

## 上线步骤（托管服务切换清单）

1. GitHub 仓库 → Settings → Pages → Source 选 `main` 分支 `/ (root)`（本仓已提交 CNAME，Pages 会自动识别 www.omaps.cn）；
2. DNSPod 把 `www.omaps.cn` 的 A 记录（14.103.247.104）改为 **CNAME → xomaps.github.io**；
3. 等证书签发（Pages 自动处理 Let's Encrypt），验证 https://www.omaps.cn；
4. **旧服务器收尾**（切换成功后）：
   - 14.103.247.104 上 `rm -rf /work/omaps-deploy/nginx/sites/omaps-site`、删除 vhost `nginx/sites-enabled/omaps-site.conf`、`docker exec omaps-nginx nginx -s reload`；
   - 撤销 crontab 里的 news_digest 任务（`crontab -e` 删 `30 9 * * *` 那行）；
   - 撤销 www.omaps.cn 的 acme.sh 续期（`~/.acme.sh/acme.sh --remove -d www.omaps.cn`），.env 的 CERT_SUBDOMAINS 去掉 `www`；
   - 删除 nginx/cert/ 下 www.omaps.cn 证书文件。

## 与 deploy-alone 的关系

deploy-alone 已同步移除官网部署内容（站点文件、vhost、upgrade.sh、news cron、compose 挂载），证书校验 webroot 统一改到 `nginx/acme/`。本仓与单机版业务部署（omaps-service / omaps-web）完全解耦。
