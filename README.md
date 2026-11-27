# omaps.cn 官网（www.omaps.cn，Hugo 静态站）

> 2026-11-22 自 deploy-alone 仓迁出，改为仓库托管、发布走托管服务（GitHub Pages），不再由单机版服务器 nginx 对外服务。
> 迁出前最后版本：deploy-alone f7e3d6c（含品牌 square-clean、insights 五篇、news 快照）。
> 2026-11-26 迁移为 Hugo 工程：URL 零变化（6 个页面路径与旧站逐一等价，验收 diff 除 1 处登记的文案统一外全 PASS）。

## 仓库内容

| 路径 | 说明 |
|---|---|
| `hugo.toml` | Hugo 站点配置（uglyURLs 文件式输出，保持 `/insights/<slug>.html` 路径不变） |
| `content/` | 页面内容（`_index.md` 首页、`insights/*.md` 四篇文章、`insights/_index.md` 列表页） |
| `layouts/` | 模板（partial 化的 head/header/footer/side-tools 等，首页与文章页分调度） |
| `static/` | 原静态资源原样迁入（css/js/images/favicon/CNAME/robots/sitemap/webmanifest） |
| `wrangler.jsonc` `scripts/deploy-cf.sh` | Cloudflare Workers 部署（worker 名 omaps-cn，见下节） |
| `.github/workflows/` | pages.yml（GitHub Pages）+ deploy-cf.yml（CF Workers），均从 Hugo 构建 |

本地构建：`hugo --gc -d dist`（hugo >= 0.170.1，`brew install hugo`）。

## Hugo 迁移要点（2026-11-26）

- URL 保持文件式：`uglyURLs = true` + `[permalinks] insights = "/insights/:filename"`；列表页 front matter `url: "/insights/index.html"` 固定路径。
- `disableKinds` 关掉 taxonomy/term/rss/sitemap/robotsTXT——sitemap.xml 与 robots.txt 用 static/ 下的自有版本。
- `disableHugoGeneratorInject = true` 关掉 Hugo 注入的 generator meta，产物与旧站字节级对齐。
- 关键词 meta 用 `{{ delimit .Params.keywords ", " }}` 渲染（直接输出 `.Params.keywords` 会带方括号）。
- 保真验收：6 页 HTML 与旧站逐 token diff 全 PASS（脚本 /tmp/hugo-migrate-verify.py 口径：URL 归一化后比 head meta 与 body 文本/属性）。唯一登记差异：悬浮栏 App 二维码副标题由「点击前往下载页」统一为首页口径「派单上刊，拍照上传」。

## 每日热点（news）已下线（2026-11-23）

首页「每日热点 Daily News」板块、页脚与洞察页入口、`news/` 栏目页、抓取脚本 `tools/news/` 已全部移除，sitemap 中对应条目同步删除。此前 cron 与 GitHub Actions 两种跑法均不再需要；如未来要恢复栏目，从 git 历史找回 `news/`、`tools/news/` 即可。

## Cloudflare Workers 部署（2026-11-22 加，2026-11-26 改 Hugo 构建）

与 xomaps.com 仓同套路：`bash scripts/deploy-cf.sh`（hugo 构建出 dist/ 后 wrangler deploy），worker 名 **omaps-cn**（CF worker 名不允许带点）。push main 也会由 deploy-cf.yml 自动部署。

## GitHub Pages（2026-11-26 起 Actions 模式）

- 发布链路：push main → pages.yml → hugo 构建 dist/ → deploy-pages 上传发布。
- ⚠️ **切换前置**：仓库 Settings → Pages → Source 必须先改为 **GitHub Actions**（在删除根目录旧 HTML 的提交推送之前完成），否则 Pages 仍按「从分支发布」找根目录 index.html 会 404。
- CNAME 已随 static/ 构建（static/CNAME → dist/CNAME），自定义域名 www.omaps.cn 不受影响。

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
