# omaps.cn 官网（www.omaps.cn 静态站）

> 2026-11-22 自 deploy-alone 仓迁出，改为仓库托管、发布走托管服务（GitHub Pages），不再由单机版服务器 nginx 对外服务。
> 迁出前最后版本：deploy-alone f7e3d6c（含品牌 square-clean、insights 五篇、news 快照）。

## 仓库内容

| 路径 | 说明 |
|---|---|
| `index.html` `css/` `js/` `images/` `insights/` | 官网静态站本体（原样迁入，未改内容） |
| `news/` | 每日行业热点栏目（含 2026-11-17 快照与 data/*.json） |
| `tools/news/` | 每日热点抓取发布脚本（news_digest.py / .sh / news_sources.json） |
| `CNAME` | GitHub Pages 自定义域名 www.omaps.cn |

⚠️ 待办：站内品牌署名仍是「麦朴」，与信朴口径不一致，改造为信朴前不建议大范围引流。

## 每日热点（news）生成方式变化

原为服务器 cron 每天 09:30 运行（/work/omaps-deploy/jobs/news_digest.sh），迁出后服务器 cron 随 www.omaps.cn 下线一并撤销。此后两种跑法：

1. **本地手动**：`bash tools/news/news_digest.sh`（产出写入 `news/` 与首页 DAILY-NEWS 区块，提交推送即发布）；
2. **GitHub Actions 定时**（后续可选）：cron 触发 tools/news/news_digest.sh 后自动 commit——需要时再加。

`tools/news/news_digest.py` 的站点根已改为仓库根（SITE_DIR = REPO_ROOT），路径无需再适配。企微通知走环境变量 `QY_WEBHOOK`，未配置则跳过。

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
