#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
omaps 官网每日行业热点抓取
=========================
抓取与「户外媒体 / 分众传媒」等相关的新闻，提炼后发布到官网：

  1. news/index.html                              —— 每日热点页（最新一期）
  2. news/archive-*.html                          —— 历史归档页（保留 90 天）
  3. news/data/*.json                             —— 原始数据
  4. 首页 index.html 中 <!-- DAILY-NEWS:BEGIN/END --> 标记块注入 Top 5

数据源（均为免鉴权接口/页面，实测国内服务器可达）：
  - 东方财富资讯搜索 API（仅作「发现层」：按关键词捞到稿子后，只保留
    TRUSTED_MEDIA_RE 白名单里的原发媒体，聚合器/自媒体/营销号一律丢弃）
  - 中国新闻网站内搜索（关键词 + 时间倒序，内嵌 docArr JSON）
  - 新浪财经 7x24 全球快讯
  - 广告门 / 数英（创意与媒介资源，需命中广告行业词）
  - 虎嗅首页（相对时间「N 小时前」，需换算绝对时间）
  - 搜狗微信（weixin.sogou.com）公众号公开文章：微信无开放接口，借搜狗检索；
    时间筛选对未登录请求失效（一律 302 回首页），故多关键词 + 本地按时间戳卡窗口
  - 界面新闻 / 钛媒体 / 中国新闻网 / 爱范儿 RSS（自采直连，不看聚合器脸色）

时效：周边资讯只收近 NEWS_WINDOW_DAYS(=3) 天；核心主题（户外/梯媒/分众/兆讯）
放宽到 CORE_WINDOW_DAYS(=7) 天——财报、并购只在发布当天有一波稿。解析不出日期
的条目直接丢弃（人民网/新华网部分 feed 停在 2025 年，不能当「今日」用）。

用法：
  python3 news_digest.py                 # 正常抓取并发布
  python3 news_digest.py --dry-run       # 只抓取打印，不写文件

部署：服务端 crontab 由 jobs/news_digest.sh 调度，见 jobs/crontab.md。
"""

import difflib
import email.utils as email_utils
import html as html_mod
import http.cookiejar
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# ---------------------------------------------------------------- 配置

# 关键词与权重（标题命中 ×3，正文命中 ×1；得分越高越靠前）
KEYWORDS = {
    "分众传媒": 3,
    "兆讯传媒": 3,
    "新潮传媒": 3,
    "梯媒": 3,
    "电梯媒体": 3,
    "户外广告": 2,
    "户外媒体": 2,
    "户外大屏": 2,
    "地铁广告": 2,
    "公交广告": 2,
    "OOH": 2,
    "LED大屏": 1,
    "广告": 1,
}

# 财经流量稿噪音（ETF/基金/资金流），标题命中直接淘汰
NOISE_RE = re.compile(
    r"ETF|基金|净值|吸金|涨停|跌停|龙虎榜|融资余额|打新|指数涨|复盘|早报|午评|收评"
    r"|大宗交易|机构持仓|机构风向标|前十大机构|Choice数据|融资融券|股东减持|股权质押")

# 东财搜索关键词（每个词查一次，取最新 30 条）
EM_KEYWORDS = ["分众传媒", "户外广告", "户外媒体", "户外传媒", "梯媒", "电梯广告",
               "楼宇广告", "社区媒体", "户外大屏", "地铁广告", "高铁广告",
               "数字户外", "广告投放", "兆讯传媒"]

# 广告行业站（广告门/数英）内容偏创意，需收窄到户外/媒介资源相关才收录
INDUSTRY_STATION_RE = re.compile(
    r"户外|梯媒|电梯媒体|电梯广告|大屏|点位|地铁广告|公交广告|媒介|媒体资源|OOH")

# 核心主题（户外/梯媒本身）——排序时优先于泛广告类稿件
CORE_RE = re.compile(
    r"户外|梯媒|电梯媒体|电梯广告|分众|兆讯|新潮传媒|大屏|地铁广告|公交广告|点位|OOH")

EM_API = "https://search-api-web.eastmoney.com/search/jsonp"
SINA_API = "https://zhibo.sina.com.cn/api/zhibo/feed"
ADQUAN_HOME = "https://www.adquan.com/"
HUXIU_HOME = "https://www.huxiu.com/"
# 中国新闻网站内搜索：结果内嵌成 var docArr JSON，按时间倒序，字段齐全
CNNEWS_SEARCH = "https://sou.chinanews.com.cn/search.do?q=%s&sortType=time"
CNNEWS_KEYWORDS = ["户外广告", "户外媒体", "分众传媒", "电梯广告", "梯媒", "广告投放"]

# 搜狗微信（weixin.sogou.com）：在微信外部检索公众号公开文章。
# 注意：时间筛选参数 tsn=1/2/3/5 对未登录请求一律 302 回首页（实测各种参数组合
# 都失败），只有默认「相关度」排序可用。所以靠多关键词 + 多页扩大样本，
# 再在本地按 timeConvert 时间戳卡 3 天窗口——宁缺毋滥。
SOGOU_WX_HOME = "https://weixin.sogou.com/"
SOGOU_WX_SEARCH = ("https://weixin.sogou.com/weixin?type=2&query=%s&ie=utf8"
                   "&page=%d&s_from=input&_sug_=n&_sug_type_=")
WX_KEYWORDS = ["户外广告", "分众传媒", "户外媒体", "电梯广告", "梯媒", "广告位"]
WX_MAX_PAGES = 2
WX_GAP_SECONDS = 1.2          # 关键词/翻页之间强制间隔，触发验证码就整源放弃
WX_SOURCE_PREFIX = "公众号 · "
# 链接里出现这些就是 JS 模板占位符没被替换（__biz=${window.biz}），点开必然报错
WX_BAD_URL_RE = re.compile(r"\$\{|%24%7B|\{[A-Za-z_][\w.]*\}")
# 打开公众号文章时，页面里出现这些字样就是死链（实测空 sn 会返回「参数错误」）
WX_DEAD_MARKERS = ("参数错误", "该内容已被发布者删除", "此内容因违规无法查看",
                   "链接已过期", "该公众号已迁移", "内容已被删除")
# 公众号条目在页面上统一显示成「公众号 · 账号名」
# 营销号黑名单：投放代理 / 招商加盟 / 副业培训，这类账号日更几十条同质稿。
# 不写裸「科技」——会误伤腾讯科技、36氪这类正规科技媒体；标题门槛已能兜住。
WX_SPAM_ACCOUNT_RE = re.compile(
    r"代理|服务商|渠道|开户|加盟|招商|创业|副业|拉新|接单|代运营|推广|运营中心"
    r"|信息流|竞价|优化师|广告投放|广告服务|网络科技")
# 公众号稿件的选题门槛：必须落在户外/点位本体上（泛「广告」不行的——
# 搜狗相关度排序里泛广告稿几乎全是投放教程），比 KEYWORDS 严格一档
WX_CORE_TITLE_RE = re.compile(
    r"户外|梯媒|电梯广告|电梯媒体|楼宇|社区媒体|地铁广告|公交广告|候车亭|站台"
    r"|大屏|LED屏|灯箱|点位|广告位|媒体资源|分众|兆讯|新潮")
# 标题黑名单：投放教程 / 开户报价 / 获客话术类（这类占了搜狗微信结果的多数）
WX_SPAM_TITLE_RE = re.compile(
    r"朋友圈|信息流|开户|代运营|加盟|招商|副业|拉新|获客|教程|课程|避坑|实操"
    r"|报价|多少钱|怎么投放|投放流程|一文讲清|一文看懂|一文透|新手|小白|案例拆解")

# 券商研报（东财研报库，免鉴权）：按股票代码查核心标的的研究观点。
# 「三季度业绩承压/不及预期」这类判断的原始出处通常是研报，新闻端往往只有
# 一份财报稿，所以单列一组「研究观点」，来源标成「XX证券 · 研报」。
RESEARCH_API = "https://reportapi.eastmoney.com/report/list"
RESEARCH_STOCKS = [("002027", "分众传媒"), ("301102", "兆讯传媒")]
RESEARCH_MAX_ITEMS = 4
RESEARCH_SOURCE_SUFFIX = " · 研报"
# 招投标源 1：中国政府采购网（政府 / 公共机构，含地铁·机场·公交等枢纽广告）
CCGP_SEARCH = "http://search.ccgp.gov.cn/bxsearch"
TENDER_KEYWORDS = ["户外广告", "广告投放", "广告位"]
# 招投标源 2：中国移动采购与招标网（电信运营商，含各省公司，公开免登录接口）
CMCC_API = ("https://b2b.10086.cn/api-b2b/api-sync-es/"
            "white_list_api/b2b/publish/queryList")
CMCC_DETAIL = ("https://b2b.10086.cn/#/noticeDetail?publishId=%s&publishUuid=%s"
               "&publishType=%s&publishOneType=%s")
CMCC_KEYWORDS = ["户外广告", "广告投放", "媒体投放", "灯箱", "电梯广告"]
CMCC_SOURCE = "中国移动采购与招标网"

TENDER_WINDOW_DAYS = 10       # 搜近 10 天的公告
TENDER_MAX_ITEMS = 15         # 每日最多展示条数
TENDER_GAP_SECONDS = 12       # ccgp 关键词之间强制间隔，避免触发站点限流
CMCC_GAP_SECONDS = 1.5        # 移动接口宽松，间隔小一点

# 线下广告投放类招标的标题特征
AD_TENDER_RE = re.compile(
    r"户外广告|广告位|广告投放|广告服务|广告牌|灯箱|候车亭|楼宇广告|电梯广告"
    r"|公交广告|车身广告|站台广告|高铁广告|机场广告|媒体投放|户外大屏|LED显示屏"
    r"|广告发布|广告制作|媒介资源|媒介采购|广告宣传")

# 行业分类（按顺序匹配标题→采购人，命中即归类，用于页面打标签）
TENDER_CATS = [
    ("电信运营商", r"移动通信|移动公司|移动集团|联通|电信|铁塔|广电网络|通信集团"),
    ("交通出行", r"地铁|轨道交通|公交|机场|航空|铁路|高铁|车站|客运|轨道公司"),
    ("房产地产", r"地产|置业|房地产|万科|保利|华润置地|招商蛇口|龙湖|金地"
                 r"|绿城|碧桂园|雅居乐|融创|物业"),
    ("消费酒类", r"酒业|酒厂|酒股份|茅台|五粮液|泸州老窖|洋河|汾酒|啤酒|乳业"
                 r"|饮料|食品|伊利|蒙牛|烟草"),
    ("电商平台", r"淘宝|天猫|阿里巴巴|1688|京东|拼多多|美团|抖音|快手|字节"
                 r"|腾讯|小红书|唯品会|携程"),
    ("政务公共", r"人民政府|管理局|管理委员会|街道办|镇政府|事业单位|大学|学院"
                 r"|医院|公安|法院|检察|体育|文化馆|图书馆"),
]

# 内置 RSS 源：直连权威媒体，不依赖聚合器
BUILTIN_FEEDS = [
    ("界面新闻", "https://a.jiemian.com/index.php?m=article&a=rss"),
    ("界面新闻", "https://a.jiemian.com/index.php?m=article&a=rss&cid=3"),
    ("钛媒体", "https://www.tmtpost.com/rss"),
    ("中国新闻网", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
    ("爱范儿", "https://www.ifanr.com/feed"),
]

# 原发媒体白名单：东方财富只是聚合器，稿子质量取决于原发媒体。
# 命中才收（含自采直连源），自媒体、营销号、未知站点一律丢弃。
TRUSTED_MEDIA_RE = re.compile(
    r"新华|人民日报|人民网|央视|央视网|央广|中国日报|中新社|中国新闻网|中新经纬"
    r"|经济日报|光明日报|经济参考报|第一财经|21世纪经济|证券时报|证券日报"
    r"|上海证券报|中国证券报|中国证券网|每日经济新闻|经济观察报|财新|财经杂志"
    r"|界面|澎湃|新京报|南方都市报|南方周末|北京商报|北京青年报|中国经营报"
    r"|时代周报|财联社|蓝鲸|观察者网|封面新闻|红星新闻|大河财立方|广州日报"
    r"|深圳商报|新浪财经|广告门|数英|虎嗅|钛媒体|爱范儿|SocialBeta|麦迪逊邦|梅花网"
    r"|刀法|TopMarketing|36氪|亿邦动力")

# 行业垂直站（广告门/数英）的收稿门槛：广告营销相关即可，
# 不强制要求出现户外/媒介词，否则这两个日更的行业站会一条都进不来
AD_INDUSTRY_RE = re.compile(
    r"广告|营销|品牌|媒介|投放|创意|代理商|4A|campaign|传播|户外|梯媒|大屏")

# 自定义 RSS 源（可选）：jobs/news_sources.json
SOURCES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "news_sources.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 12

# 抓取窗口：周边资讯只收近 3 天（用户要求日期要新，宁缺毋滥）；
# 核心主题（户外/梯媒/分众/兆讯）放宽到 7 天——财报、并购这类事件往往只在
# 发布当天有一波稿，3 天窗口会把「正在发酵的大事」整个漏掉。
NEWS_WINDOW_DAYS = 3
CORE_WINDOW_DAYS = 7
CORE_WINDOW_HOURS = 24 * CORE_WINDOW_DAYS
WINDOW_HOURS = 24 * NEWS_WINDOW_DAYS
FRESH_HOURS = 24                    # 排序时把 24 小时内的再提一层
TOP_N = 12          # news 页展示条数
HOME_TOP_N = 5      # 首页注入条数
KEEP_DAYS = 90      # 归档保留天数

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tools/news/ 上跳两层 = 仓库根
SITE_DIR = REPO_ROOT  # 2026-11-22 迁出 deploy-alone：站点文件即仓库根（GitHub Pages 托管）
NEWS_DIR = os.path.join(SITE_DIR, "news")
DATA_DIR = os.path.join(NEWS_DIR, "data")
HOME_HTML = os.path.join(SITE_DIR, "index.html")

DRY_RUN = "--dry-run" in sys.argv

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------- 抓取工具

def http_get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_post_json(url, payload, referer="", timeout=TIMEOUT):
    """POST JSON（招投标平台的公开查询接口都走这个）。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
    }
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = referer.split("/#")[0].rstrip("/")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def clean(text, limit=140):
    """去标签/实体/多余空白，截断。"""
    if not text:
        return ""
    text = html_mod.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def score_of(text):
    s = 0
    for kw, w in KEYWORDS.items():
        if kw.lower() in text.lower():
            s += w
    return s


def score_item(it):
    """标题命中权重 ×3，正文命中 ×1；标题含财经流量噪音则判废。"""
    if it.get("kind") == "research":
        return 6          # 研报标题常不带行业词（「Q3业绩有所承压」），按核心标的授予固定分
    title = it.get("title", "")
    body = it.get("summary", "")
    if NOISE_RE.search(title):
        return 0
    title_score = score_of(title)
    if title_score <= 0:
        return 0          # 标题不含任何行业关键词 → 不是行业热点
    return title_score * 3 + score_of(body)


# ---------------------------------------------------------------- 数据源

def fetch_eastmoney():
    """东方财富资讯搜索：标题+摘要+媒体+链接+时间。

    东方财富只是聚合器，同一关键词能搜到大量自媒体/营销号转载，
    因此只保留原发媒体命中 TRUSTED_MEDIA_RE 的条目，未知来源一律丢弃。

    同一关键词跑两种排序：time 抓最新、default 抓最相关——
    只按时间排会漏掉「只有相关性排序才排得上来」的行业深度稿。

    searchScope 必须是 title：default 是全文匹配，东财的 ETF/社保基金/资金面
    复盘稿会顺手提到上百只股票，把 30 个名额全占满，真正标题里带关键词的稿件
    （比如「分众传媒发布第三季度报告」）反而挤不进来。改标题检索后同一关键词
    能稳定取到分众/兆讯这类标的的财报与经营新闻。
    """
    items, dropped = [], {}
    for kw in EM_KEYWORDS:
        for sort in ("time", "default"):
            param = {
                "uid": "", "keyword": kw, "type": ["cmsArticleWebOld"],
                "client": "web", "clientType": "web", "clientVersion": "curr",
                "param": {"cmsArticleWebOld": {
                    "searchScope": "title", "sort": sort,
                    "pageIndex": 1, "pageSize": 30,
                    "preTag": "", "postTag": ""}},
            }
            url = "%s?cb=cb&param=%s" % (
                EM_API, urllib.parse.quote(json.dumps(param, ensure_ascii=False)))
            try:
                raw = http_get(url)
                data = json.loads(raw[raw.index("(") + 1: raw.rindex(")")])
                arts = (data.get("result") or {}).get("cmsArticleWebOld") or []
            except Exception as e:
                print("  [warn] eastmoney %s/%s 失败: %s" % (kw, sort, e))
                continue
            for a in arts:
                try:
                    dt = datetime.strptime(a.get("date", ""), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                title = clean(a.get("title", ""), 100)
                if not title:
                    continue
                media = (a.get("mediaName") or "").strip()
                if not TRUSTED_MEDIA_RE.search(media):
                    dropped[media or "(未知来源)"] = dropped.get(media or "(未知来源)", 0) + 1
                    continue
                items.append({
                    "title": title,
                    "summary": clean(a.get("content", ""), 150),
                    "url": a.get("url", "").replace("http://", "https://"),
                    "source": media or "东方财富",
                    "published": dt.strftime("%Y-%m-%d %H:%M"),
                    "ts": dt.timestamp(),
                })
    if dropped:
        top = sorted(dropped.items(), key=lambda x: -x[1])[:6]
        print("  东财已过滤非白名单来源 %d 条（%s）"
              % (sum(dropped.values()), "、".join("%s×%d" % kv for kv in top)))
    return items


def fetch_sina7x24():
    """新浪财经 7x24 快讯，抓前 3 页按关键词过滤。"""
    items = []
    for page in (1, 2, 3):
        url = "%s?page=%d&page_size=50&zhibo_id=152&tag_id=0&dire=f&dpc=1" % (SINA_API, page)
        try:
            data = json.loads(http_get(url))
            feed = ((data.get("result") or {}).get("data") or {}).get("feed") or {}
            rows = feed.get("list") or []
        except Exception as e:
            print("  [warn] sina page%d 失败: %s" % (page, e))
            continue
        for r in rows:
            try:
                dt = datetime.strptime(r.get("create_time", ""), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            text = clean(r.get("rich_text", ""), 200)
            if score_of(text) <= 0:
                continue
            # 快讯里若带链接，抽第一条原文链接
            m = re.search(r"https?://t\.cn/\w+|https?://finance\.sina\.com\.cn/\S+", r.get("rich_text", ""))
            link = m.group(0) if m else "https://finance.sina.com.cn/7x24/"
            items.append({
                "title": text[:60] + ("…" if len(text) > 60 else ""),
                "summary": text,
                "url": link,
                "source": "新浪财经7x24",
                "published": dt.strftime("%Y-%m-%d %H:%M"),
                "ts": dt.timestamp(),
            })
    return items


_ADQUAN_TITLE = re.compile(
    r'<a class="article_2_href" href="https://www\.adquan\.com/article/(\d+)"[^>]*>\s*'
    r'<p class="article_2_p">(.*?)</p>', re.S)
_ADQUAN_DATE = re.compile(r"(\d{4})/(\d{2})/(\d{2})")


def fetch_adquan():
    """广告门首页：标题（p.article_2_p）+ 卡片内日期 + 摘要（article_1_fu 首段）。"""
    items, seen = [], set()
    try:
        page = http_get(ADQUAN_HOME)
    except Exception as e:
        print("  [warn] adquan 失败: %s" % e)
        return items

    for m in _ADQUAN_TITLE.finditer(page):
        aid, raw_title = m.group(1), m.group(2)
        title = clean(raw_title, 100)
        if not title or len(title) < 6 or title in seen:
            continue
        if score_of(title) <= 0 or NOISE_RE.search(title):
            continue
        if not AD_INDUSTRY_RE.search(title):
            continue

        # 在当前卡片附近向上找日期与摘要（同一 article_1 区块内）
        window = page[max(0, m.start() - 1200):m.start()]
        dates = _ADQUAN_DATE.findall(window)
        published = "%s-%s-%s" % dates[-1] if dates else datetime.now().strftime("%Y-%m-%d")
        ps = re.findall(r"<p>(?!\s*\d{4}/\d{2}/\d{2})(.*?)</p>", window, re.S)
        summary = clean(ps[-1], 120) if ps else ""
        if summary == title:
            summary = ""

        seen.add(title)
        try:
            ts = datetime.strptime(published[:10], "%Y-%m-%d").timestamp()
        except ValueError:
            ts = datetime.now().timestamp()
        items.append({
            "title": title,
            "summary": summary,
            "url": "https://www.adquan.com/article/%s" % aid,
            "source": "广告门",
            "published": published[:10],
            "ts": ts,
        })
    return items[:12]


HUXIU_REL_TIME = re.compile(r"(\d+)\s*(分钟|小时|天)前")


def fetch_huxiu():
    """虎嗅桌面首页：文章卡片带「N 小时前」相对时间，可还原真实发布时间。

    移动端首页只剩 3 条推荐位，改用 www.huxiu.com（约 60 篇文章）。
    标题取卡片窗口里最长的一段纯文本（卡片结构由 Vue 渲染、类名会变，
    不绑 DOM 层级），时间用相对时间换算；无时间的卡片直接丢弃。
    """
    items, seen = [], set()
    try:
        page = http_get(HUXIU_HOME, timeout=20)
    except Exception as e:
        print("  [warn] huxiu 失败: %s" % e)
        return items

    pos = {}
    for m in re.finditer(r'href="https://www\.huxiu\.com/article/(\d+)\.html"', page):
        pos.setdefault(m.group(1), []).append(m.start())

    now = datetime.now()
    for aid, starts in pos.items():
        seg = page[max(0, min(starts) - 1500): max(starts) + 1500]
        tm = HUXIU_REL_TIME.search(seg)
        if not tm:
            continue
        n, unit = int(tm.group(1)), tm.group(2)
        delta = n * (60 if unit == "分钟" else 3600 if unit == "小时" else 86400)
        if delta > CORE_WINDOW_DAYS * 86400:
            continue
        dt = now - timedelta(seconds=delta)

        best = ""
        for chunk in re.findall(r">([^<>]{8,70})<", seg):
            chunk = clean(chunk, 70)
            if HUXIU_REL_TIME.search(chunk) or chunk in ("阅读", "评论", "收藏"):
                continue
            if len(chunk) > len(best):
                best = chunk
        if not best or score_of(best) <= 0 or NOISE_RE.search(best):
            continue

        seen.add(aid)
        items.append({
            "title": best,
            "summary": "",
            "url": "https://www.huxiu.com/article/%s.html" % aid,
            "source": "虎嗅",
            "published": dt.strftime("%Y-%m-%d %H:%M"),
            "ts": dt.timestamp(),
        })
    return items[:10]


def fetch_digitaling():
    """数英网文章列表（广告/营销行业，含户外投放与案例报道）。

    列表项结构：<h3><a title="标题"></a></h3> + 摘要 p + <label>日期</label>，
    日期取真实发布日（此前用 now() 会把旧稿伪装成今天，违反时效要求）。
    """
    items, seen = [], set()
    try:
        page = http_get("https://www.digitaling.com/articles")
    except Exception as e:
        print("  [warn] digitaling 失败: %s" % e)
        return items

    pat = re.compile(
        r'<h3[^>]*>\s*<a[^>]+href="https://www\.digitaling\.com/articles/(\d+)\.html"'
        r'[^>]*title="([^"]*)"[^>]*>.*?</a>\s*</h3>(.*?)'
        r'<label[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*</label>', re.S)
    now = datetime.now()
    for aid, title_raw, mid, date_str in pat.findall(page):
        title = clean(title_raw, 100)
        if len(title) < 6 or aid in seen:
            continue
        if score_of(title) <= 0 or NOISE_RE.search(title):
            continue
        if not AD_INDUSTRY_RE.search(title):
            continue
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        if now - dt > timedelta(days=CORE_WINDOW_DAYS):
            continue
        seen.add(aid)
        sm = re.search(r'<p[^>]*class="[^"]*font_14[^"]*"[^>]*>(.*?)</p>', mid, re.S)
        items.append({
            "title": title,
            "summary": clean(sm.group(1), 120) if sm else "",
            "url": "https://www.digitaling.com/articles/%s.html" % aid,
            "source": "数英",
            "published": dt.strftime("%Y-%m-%d"),
            "ts": dt.timestamp(),
        })
    return items[:10]


def _parse_feed(xml_text):
    """解析 RSS/Atom，返回 [(title, link, summary, pub_str)]。

    先试标准 XML；失败时退回正则逐 <item>/<entry> 抠字段——
    部分站点（36氪、亿邦等）的 feed 带非法字符或未转义实体，ET 会直接报错。
    """
    try:
        root = ET.fromstring(xml_text.encode("utf-8")
                             if isinstance(xml_text, str) else xml_text)
        out = []
        for node in root.iter():
            if node.tag.split("}")[-1] not in ("item", "entry"):
                continue
            title = link = summary = pub = ""
            for child in node:
                ctag = child.tag.split("}")[-1]
                if ctag == "title":
                    title = clean(child.text or "", 100)
                elif ctag == "link":
                    link = (child.text or "").strip() or child.get("href", "")
                elif ctag in ("description", "summary", "content"):
                    summary = summary or clean(child.text or "", 140)
                elif ctag in ("pubDate", "published", "updated", "date"):
                    pub = (child.text or "").strip()
            if title and link:
                out.append((title, link, summary, pub))
        if out:
            return out
    except Exception:
        pass

    # 容错解析
    out = []
    for block in re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>",
                            xml_text, re.S):
        def grab(*tags):
            for t in tags:
                m = re.search(r"<%s\b[^>]*>(.*?)</%s>" % (t, t), block, re.S)
                if m:
                    return m.group(1)
            return ""
        link = grab("link")
        lm = re.search(r'<link\b[^>]*href="([^"]+)"', block)
        if not link.strip() and lm:
            link = lm.group(1)
        title = clean(grab("title"), 100)
        title = clean(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title), 100)
        if title and link.strip():
            out.append((title, link.strip(),
                        clean(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1",
                                     grab("description", "summary", "content")), 140),
                        grab("pubDate", "published", "updated", "date").strip()))
    return out


def fetch_rss():
    """内置权威媒体 RSS + jobs/news_sources.json 自定义源。

    日期要求严格：解析不出发布时间的条目直接丢弃，
    避免僵尸源（人民网/新华网部分 feed 停在 2025 年）混进「今日」。
    """
    feeds = list(BUILTIN_FEEDS)
    if os.path.exists(SOURCES_FILE):
        try:
            with open(SOURCES_FILE, encoding="utf-8") as f:
                conf = json.load(f)
            for src in conf.get("feeds", []):
                if src.get("name") and src.get("url"):
                    feeds.append((src["name"], src["url"]))
        except Exception as e:
            print("  [warn] news_sources.json 解析失败: %s" % e)

    items, undated, stale = [], 0, 0
    now = datetime.now()
    for name, url in feeds:
        try:
            xml_text = http_get(url)
        except Exception as e:
            print("  [warn] rss %s 失败: %s" % (name, e))
            continue
        rows = _parse_feed(xml_text)
        if not rows:
            print("  [warn] rss %s 无可解析条目" % name)
            continue
        got = 0
        for title, link, summary, pub in rows:
            if score_of(title) <= 0 or NOISE_RE.search(title):
                continue
            dt = None
            if pub:
                try:
                    dt = email_utils.parsedate_to_datetime(pub).replace(tzinfo=None)
                except Exception:
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                                "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
                        try:
                            dt = datetime.strptime(pub[:len(fmt) + 2].strip(), fmt)
                            break
                        except ValueError:
                            continue
            if dt is None:
                undated += 1
                continue
            if now - dt > timedelta(days=CORE_WINDOW_DAYS):
                stale += 1
                continue
            items.append({
                "title": title,
                "summary": summary,
                "url": link,
                "source": name,
                "published": dt.strftime("%Y-%m-%d %H:%M"),
                "ts": dt.timestamp(),
            })
            got += 1
        print("    · %s: %d 条" % (name, got))
    if undated or stale:
        print("  已丢弃无日期 %d 条、超出 %d 天 %d 条"
              % (undated, CORE_WINDOW_DAYS, stale))
    return items


def fetch_cnnews_search():
    """中国新闻网站内搜索（关键词 + 时间倒序）。

    补 RSS 的短板：RSS 只给「当日要闻」，命中率极低；站内搜索能按关键词
    捞到垂直稿件，且结果内嵌成 `var docArr = [...]`，无需解析 HTML 结构。
    仍严格守 NEWS_WINDOW_DAYS：搜索是按相关度混排的，旧稿必须按 pubtime 剔除。
    """
    now = datetime.now()
    out, seen, stale, undated = [], set(), 0, 0

    def _flat(v):
        """docArr 里部分字段是高亮分段数组（["<em>户外</em>", "广告"]），先摊平。"""
        if isinstance(v, list):
            v = "".join(str(x) for x in v)
        return str(v or "")

    for kw in CNNEWS_KEYWORDS:
        try:
            page = http_get(CNNEWS_SEARCH % urllib.parse.quote(kw))
        except Exception as e:
            print("  [warn] 中新网搜索「%s」失败: %s" % (kw, e))
            continue
        m = re.search(r"var docArr = (\[.*?\])\s*;\s*\n", page, re.S)
        if not m:
            print("  [warn] 中新网搜索「%s」无结果块" % kw)
            continue
        try:
            rows = json.loads(m.group(1))
        except ValueError:
            print("  [warn] 中新网搜索「%s」docArr 解析失败" % kw)
            continue
        for r in rows:
            url = _flat(r.get("url")).strip()
            title = clean(re.sub(r"<[^>]+>", "", _flat(r.get("title"))), 100)
            if not url or not title or url in seen:
                continue
            pub = _flat(r.get("pubtime") or r.get("createtime")).strip()[:19]
            dt = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(pub[:len(fmt) + 2].strip(), fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                undated += 1
                continue
            if now - dt > timedelta(days=CORE_WINDOW_DAYS):
                stale += 1
                continue
            seen.add(url)
            out.append({
                "title": title,
                "summary": clean(re.sub(r"<[^>]+>", "",
                                        _flat(r.get("content_without_tag"))), 140),
                "url": url,
                "source": "中国新闻网",
                "published": dt.strftime("%Y-%m-%d %H:%M"),
                "ts": dt.timestamp(),
            })
    if undated or stale:
        print("  中新网搜索已丢弃无日期 %d 条、超出 %d 天 %d 条"
              % (undated, CORE_WINDOW_DAYS, stale))
    return out


# ---- 搜狗微信（公众号公开文章）----

_WX_OPENER = None


def _wx_get(url, referer=None, timeout=TIMEOUT):
    """带 cookie 的 GET：搜狗要先落 SUID 等 cookie，否则结果页会 302 回首页。"""
    global _WX_OPENER
    if _WX_OPENER is None:
        jar = http.cookiejar.CookieJar()
        _WX_OPENER = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar),
            urllib.request.HTTPSHandler(context=SSL_CTX))
        _WX_OPENER.addheaders = [("User-Agent", UA),
                                 ("Accept-Language", "zh-CN,zh;q=0.9")]
    req = urllib.request.Request(url)
    if referer:
        req.add_header("Referer", referer)
    with _WX_OPENER.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _wx_url_ok(url):
    """链接形状校验：必须是 mp.weixin 文章地址，且参数完整、不含 JS 模板占位符。

    两种可用形态：
      1) 搜狗签名地址 …/s?src=11&timestamp=…&signature=…（短期有效）
      2) 永久地址 …/s?__biz=…&mid=…&idx=…&sn=…（**四个参数缺一不可**）
    曾经只查了 __biz/mid/idx，结果 sn 抓空时拼出 …&sn= 这种地址，打开就是
    「参数错误」——线上那条坏链就是这么来的，所以 sn 必须一起校验。
    """
    if not url or not url.startswith("https://mp.weixin.qq.com/s"):
        return False
    if WX_BAD_URL_RE.search(url):
        return False
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if q.get("signature"):
        return True
    return all(q.get(k) for k in ("__biz", "mid", "idx", "sn"))


def _wx_article_ok(url):
    """真实可用性校验：拉一次页面，看正文标题在不在。

    返回 True（能打开）/ False（明确是死链）/ None（无法判定）。
    无法判定时调用方一律按「保留」处理——服务器出口 IP 偶尔被 mp.weixin 判成
    异常环境，宁可留个待复查的链接，也不能把好链接批量清掉。
    """
    try:
        page = _wx_get(url, timeout=TIMEOUT)
    except Exception:
        return None
    if "rich_media_title" in page or "msg_title" in page:
        return True
    if any(m in page for m in WX_DEAD_MARKERS):
        return False
    return None


def _wx_resolve_link(href, referer):
    """搜狗 /link 中转页用 JS 把真实地址拆成若干 url += '...' 片段，拼回来即可。"""
    try:
        page = _wx_get("https://weixin.sogou.com" + html_mod.unescape(href), referer)
    except Exception:
        return ""
    real = "".join(re.findall(r"url \+= '([^']*)'", page)).replace("\\x26", "&")
    return real if _wx_url_ok(real) else ""


def _wx_permanent_link(signed_url):
    """把搜狗签名地址换成公众号永久链接（__biz/mid/idx/sn），避免归档页链接过期。

    签名地址（src=11&timestamp=…&signature=…）由平台按时间签发，放几天就会
    提示「链接已过期」。原文页有两种写法：
      1) var msg_link = "https://mp.weixin.qq.com/s?__biz=MzA5...&mid=2650...&idx=1&sn=..."
      2) var msg_link = "https://mp.weixin.qq.com/s?__biz=${window.biz}&mid=${window.mid}…"
    第二种是 JS 模板串，直接取会得到带 ${} 的假地址，必须改从
    「var biz = "" || "MzA5…"」这类变量里取值重新拼。取不到就退回签名地址。
    """
    try:
        page = _wx_get(signed_url, timeout=TIMEOUT * 2)
    except Exception:
        return ""

    m = re.search(r'https?://mp\.weixin\.qq\.com/s\?__biz=[^"\'<>\s\\]+', page)
    if m:
        link = html_mod.unescape(m.group(0)).replace("http://mp.weixin.qq.com",
                                                     "https://mp.weixin.qq.com")
        link = link.split("#")[0]
        if _wx_url_ok(link):
            return link

    # 模板串写法：从页面变量里拼真实永久地址
    vals = _wx_page_vars(page)
    biz = vals.get("biz", "")
    if not (re.fullmatch(r"[A-Za-z0-9+/=]{8,}", biz)
            and vals.get("mid", "").isdigit() and vals.get("idx", "").isdigit()
            and len(vals.get("sn", "")) >= 8):
        return ""       # 缺 sn 拼出来就是「参数错误」页，宁可不给链接
    link = ("https://mp.weixin.qq.com/s?__biz=%s&mid=%s&idx=%s&sn=%s"
            % (biz, vals["mid"], vals["idx"], vals["sn"]))
    if not _wx_url_ok(link):
        return ""
    return link if _wx_article_ok(link) is not False else ""


def _wx_page_vars(page):
    """从原文页里抠出 biz/mid/idx/sn。

    写法五花八门，实测至少三种：
      var biz = "" || "MzA5…";              ← 常见
      window.biz = "MzA5…";
      var sn = getCookie("sn") || "abcdef…";← 旧正则在这里会抓成 "sn"，于是拼出 &sn=
    所以按「该行所有引号串里挑最后一个合法值」来取，而不是死磕固定格式。
    """
    vals = {}
    for key in ("biz", "mid", "idx", "sn"):
        m = re.search(r"(?:var|window\.)\s*%s\s*=([^\n;]*)" % key, page)
        if not m:
            continue
        # 注意用 [^"]* 而不是 [^"]{2,}：`"" || "值"` 这种写法里，{2,} 会跨过
        # 两个引号之间的空档（把 " || " 当成值），结果反而取不到真实值
        for cand in re.findall(r'"([^"]*)"', m.group(1)):
            cand = cand.strip()
            if key == "biz" and re.fullmatch(r"[A-Za-z0-9+/=]{8,}", cand):
                vals[key] = cand
            elif key in ("mid", "idx") and cand.isdigit():
                vals[key] = cand
            elif key == "sn" and re.fullmatch(r"[0-9A-Za-z_-]{8,}", cand) and cand != "sn":
                vals[key] = cand
    if "sn" not in vals:        # 兜底：页面别处可能直接写了 &sn=xxxx
        m = re.search(r"[?&]sn=([0-9A-Za-z_-]{8,})", page)
        if m:
            vals["sn"] = m.group(1)
    return vals


def _wx_best_link(signed):
    """给定搜狗签名地址，尽力产出**实际打得开**的链接；明确是死链才返回空串。

    顺序：永久地址（sn 齐全且实测非死链）→ 签名地址。
    以前是 `_wx_permanent_link(x) or x`，只要永久地址非空就直接用，
    连 sn 为空这种必死链接也照发；现在以「拉一次页面看正文在不在」为准。

    注意「无法判定」（网络抖动 / 服务器出口 IP 被 mp.weixin 判成异常环境）
    不否决：签名地址刚由搜狗解析出来，本来就是能点的，宁可留着让它显示，
    也不能因为一次探测失败就把整条公众号新闻丢掉。
    """
    if not signed:
        return ""
    perm = _wx_permanent_link(signed)
    if perm:
        return perm
    return "" if _wx_article_ok(signed) is False else signed


def _wx_title_ok(title):
    return (bool(title) and bool(WX_CORE_TITLE_RE.search(title))
            and not WX_SPAM_TITLE_RE.search(title))


def _wx_account_ok(account):
    return bool(account) and not WX_SPAM_ACCOUNT_RE.search(account)


def fetch_wechat_sogou():
    """搜狗微信文章检索：补上「公众号」这一块（微信无公开接口，只能借搜狗）。

    两道质量闸门：标题须命中行业词且不含投放教程话术；账号名须像行业媒体或
    垂直号（先用黑名单挡掉投放代理 / 招商加盟 / 副业培训类营销号）。
    链接只对通过筛选的少量条目才去解 /link 中转页——请求量小才不会触发验证码。
    """
    try:
        _wx_get(SOGOU_WX_HOME)
    except Exception as e:
        print("  [warn] 搜狗微信首页不可达: %s" % e)
        return []

    now = datetime.now()
    out, seen_titles = [], []
    stale = spam = undated = blocked = link_fail = 0
    for kw in _source_keywords("wx_keywords", WX_KEYWORDS):
        for page in range(1, WX_MAX_PAGES + 1):
            url = SOGOU_WX_SEARCH % (urllib.parse.quote(kw), page)
            try:
                page_html = _wx_get(url, SOGOU_WX_HOME)
            except Exception as e:
                print("  [warn] 搜狗微信「%s」p%d 失败: %s" % (kw, page, e))
                blocked += 1
                break
            blocks = re.findall(r'<li id="sogou_vr_11002601_box_\d+".*?</li>',
                                page_html, re.S)
            if "VerifyCode" in page_html or "需要您协助验证" in page_html:
                # 搜狗按 IP 限频，触发后同一出口 IP 一段时间内全是验证码页。
                # 这时继续请求只会延长封锁，直接整源放弃，其它源照常。
                print("  [warn] 搜狗微信触发验证码（IP 限频），本源本次放弃")
                return out
            if not blocks:
                blocked += 1        # 验证码页或没有下一页，都按到底处理
                break
            for b in blocks:
                m_t = re.search(r'<h3>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
                m_s = re.search(r'<p class="txt-info"[^>]*>(.*?)</p>', b, re.S)
                m_a = re.search(r'<span class="all-time-y2">([^<]*)</span>', b)
                m_d = re.search(r"timeConvert\('(\d{10})'\)", b)
                if not (m_t and m_d):
                    undated += 1
                    continue
                title = clean(m_t.group(2), 100)
                account = html_mod.unescape(m_a.group(1) if m_a else "").strip()
                if not _wx_title_ok(title) or not _wx_account_ok(account):
                    spam += 1
                    continue
                dt = datetime.fromtimestamp(int(m_d.group(1)))
                if now - dt > timedelta(days=CORE_WINDOW_DAYS):
                    stale += 1
                    continue
                norm = re.sub(r"\W", "", title)
                if any(_similar(norm, x) for x in seen_titles):
                    continue
                link = _wx_resolve_link(m_t.group(1), url)
                if not link:
                    link_fail += 1
                    continue
                link = _wx_best_link(link)
                seen_titles.append(norm)
                out.append({
                    "title": title,
                    "summary": clean(m_s.group(1) if m_s else "", 140),
                    "url": link,
                    "source": WX_SOURCE_PREFIX + account,
                    "published": dt.strftime("%Y-%m-%d %H:%M"),
                    "ts": dt.timestamp(),
                })
                time.sleep(0.6)
            time.sleep(WX_GAP_SECONDS)
    print("  搜狗微信：%d 条（营销号 %d、超窗 %d、无日期 %d、链接失败 %d、空页 %d）"
          % (len(out), spam, stale, undated, link_fail, blocked))
    return out


def fetch_research_reports():
    """券商研报（东财研报库）：核心标的研究观点，单独成组。

    新闻端只在财报当天有稿，「承压/不及预期」这类判断其实出自研报。
    按股票代码查（分众 002027、兆讯 301102），日期粒度到天，
    所以窗口按自然日算，比核心新闻窗口宽一天。
    """
    today = datetime.now()
    begin = (today - timedelta(days=CORE_WINDOW_DAYS + 1)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    out, seen = [], set()
    for code, stock in RESEARCH_STOCKS:
        url = ("%s?cb=cb&industryCode=*&pageSize=30&industry=*&rating=&ratingChange="
               "&beginTime=%s&endTime=%s&pageNo=1&fields=&qType=0&orgCode=&code=%s"
               "&rcode=&p=1&pageNum=1&pageNumber=1"
               % (RESEARCH_API, begin, end, code))
        try:
            raw = http_get(url)
            data = json.loads(raw[raw.index("(") + 1: raw.rindex(")")])
        except Exception as e:
            print("  [warn] 研报 %s 失败: %s" % (stock, e))
            continue
        rows = data.get("data") or []
        kept = 0
        for r in rows:
            info = r.get("infoCode") or ""
            if not info or info in seen:
                continue
            pub = (r.get("publishDate") or "")[:10]
            try:
                dt = datetime.strptime(pub, "%Y-%m-%d")
            except ValueError:
                continue
            if (today.date() - dt.date()).days > CORE_WINDOW_DAYS:
                continue
            title = clean(r.get("title", ""), 100)
            if not title:
                continue
            org = (r.get("orgSName") or "券商").strip()
            rating = (r.get("emRatingName") or "").strip()
            seen.add(info)
            kept += 1
            out.append({
                "title": title,
                "summary": "%s · %s%s" % (stock, org,
                                          (" · 评级 " + rating) if rating else ""),
                "url": "https://data.eastmoney.com/report/info/%s.html" % info,
                "source": org + RESEARCH_SOURCE_SUFFIX,
                "published": pub + " 00:00",
                "ts": dt.timestamp(),
                "kind": "research",
            })
        print("    · %s(%s): %d 篇研报" % (stock, code, kept))
    out.sort(key=lambda x: -x["ts"])
    return out[:RESEARCH_MAX_ITEMS]


def tender_keywords():
    """ccgp 搜索关键词：默认三条，可在 jobs/news_sources.json 的 tender_keywords 覆盖。"""
    return _source_keywords("tender_keywords", TENDER_KEYWORDS)


def cmcc_keywords():
    """移动采购站搜索关键词：默认五条，可在 jobs/news_sources.json 的 cmcc_keywords 覆盖。"""
    return _source_keywords("cmcc_keywords", CMCC_KEYWORDS)


def _source_keywords(key, default):
    try:
        with open(SOURCES_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        kws = [k.strip() for k in (cfg.get(key) or [])
               if isinstance(k, str) and k.strip()]
    except Exception:
        kws = []
    return kws or default


def _tender_field(seg, label):
    """从结果项片段里取「标签：值」字段（标签后可能跟若干内联标签）。"""
    m = re.search(label + r"[：:]\s*(?:<[^>]*>\s*)*([^<]{1,40})", seg)
    return clean(m.group(1), 40) if m else ""


def tender_cat(*texts):
    """按标题/采购人判定行业分类，命中第一条即返回。"""
    blob = " ".join(t for t in texts if t)
    for label, pattern in TENDER_CATS:
        if re.search(pattern, blob):
            return label
    return "其他"


def fetch_tenders_ccgp():
    """线下广告投放相关的招投标公告（中国政府采购网搜索）。

    - 官方免费全文，链接直达公告原文；
    - 站点有频率限制（频繁访问即返回提示页），因此关键词之间强制间隔，
      被限流时直接返回空，当日沿用已发布的招投标列表；
    - 解析不依赖具体 DOM 结构：只要页面里有指向公告的 a 链接就提取标题，
      日期从 URL（t20261110_xxx.htm）解析，采购人/地域/类型从该结果项片段里取，
      避免整体改版即失效。
    """
    items, seen = [], set()
    now = datetime.now()
    start = (now - timedelta(days=TENDER_WINDOW_DAYS)).strftime("%Y:%m:%d")
    end = now.strftime("%Y:%m:%d")
    keywords = tender_keywords()

    for idx, kw in enumerate(keywords):
        if idx:
            time.sleep(TENDER_GAP_SECONDS)
        params = {
            "searchtype": "1", "page_index": "1", "bidSort": "0",
            "buyerName": "", "projectId": "", "pinMu": "0", "bidType": "0",
            "dbselect": "bidx", "kw": kw, "timeType": "6",
            "start_time": start, "end_time": end,
            "displayZone": "", "zoneId": "", "pppStatus": "0", "agentName": "",
        }
        url = CCGP_SEARCH + "?" + urllib.parse.urlencode(params)
        try:
            page = http_get(url, timeout=20)
        except Exception as e:
            print("  [warn] ccgp「%s」请求失败: %s" % (kw, e))
            continue
        if "频繁访问" in page or len(page) < 4000:
            print("  [warn] ccgp「%s」被限流（站点按 IP 限频），本次跳过"
                  % kw)
            break

        for m in re.finditer(
                r'<a\b[^>]*href="(https?://www\.ccgp\.gov\.cn/[^"]*?t(\d{8})_\d+\.htm)"'
                r'[^>]*>(.*?)</a>', page, re.S):
            link, datestr, raw_title = m.group(1), m.group(2), m.group(3)
            title = clean(raw_title, 120)
            if not title or not AD_TENDER_RE.search(title) or link in seen:
                continue
            try:
                dt = datetime.strptime(datestr, "%Y%m%d")
            except ValueError:
                continue
            if (now - dt).days > TENDER_WINDOW_DAYS:
                continue

            # 该结果项到下一项之间的片段，字段都在里面
            nxt = page.find("<li", m.end())
            seg = page[m.end(): nxt if 0 < nxt - m.end() < 1500 else m.end() + 800]
            buyer = _tender_field(seg, "采购人")
            region = _tender_field(seg, r"地\s*域")
            raw_kind = _tender_field(seg, r"类\s*型")
            if not region:
                # 兜底：从标题里猜地域（省/市/区县）
                rm = re.search(r"([\u4e00-\u9fa5]{2,8}(?:省|市|自治区|自治州|区|县))", title)
                region = clean(rm.group(1), 10) if rm else ""
            kind_src = raw_kind or title
            kind = "中标/成交" if re.search(r"中标|成交|结果", kind_src) else "招标公告"

            seen.add(link)
            items.append({
                "title": title,
                "url": link,
                "buyer": buyer[:30],
                "region": region[:10],
                "kind": kind,
                "cat": tender_cat(title, buyer),
                "channel": "政府采购",
                "published": dt.strftime("%Y-%m-%d"),
                "ts": dt.timestamp(),
            })
    return items


def fetch_tenders_cmcc():
    """中国移动采购与招标网（各省公司公开采购公告，含电信类广告投放）。

    接口是站点自己的公开查询（white_list_api，免登录），
    标题按关键词模糊匹配；返回体里带省份（companyTypeName）和公告类型，
    详情页是 hash 路由，用 publishId/publishUuid 拼出可点的原文链接。
    """
    items, seen = [], set()
    now = datetime.now()
    for idx, kw in enumerate(cmcc_keywords()):
        if idx:
            time.sleep(CMCC_GAP_SECONDS)
        payload = {"current": 1, "size": 30, "name": kw,
                   "publishType": "PROCUREMENT", "sfactApplColumn5": "PC"}
        try:
            resp = http_post_json(CMCC_API, payload,
                                  referer="https://b2b.10086.cn/", timeout=20)
        except Exception as e:
            print("  [warn] 移动「%s」请求失败: %s" % (kw, e))
            continue
        if resp.get("code") != 0:
            print("  [warn] 移动「%s」返回异常: %s" % (kw, resp.get("msg")))
            continue

        for it in ((resp.get("data") or {}).get("content") or []):
            title = clean(it.get("name") or "", 120)
            if not title or not AD_TENDER_RE.search(title):
                continue
            pid, puuid = it.get("id"), it.get("uuid")
            if not pid or not puuid:
                continue
            link = CMCC_DETAIL % (pid, puuid, it.get("publishType") or "",
                                  it.get("publishOneType") or "")
            if link in seen:
                continue
            pdate = (it.get("publishDate") or "")[:10]
            try:
                dt = datetime.strptime(pdate, "%Y-%m-%d")
            except ValueError:
                continue
            if (now - dt).days > TENDER_WINDOW_DAYS:
                continue

            raw_type = it.get("publishOneType") or ""
            kind = ("中标/成交" if raw_type in
                    ("CANDIDATE_PUBLICITY", "SELECTION_RESULTS")
                    or re.search(r"中标|成交|中选|结果", title) else "招标公告")
            # 采购人：标题里最后一个「有限公司/分公司」，取不到就写「中国移动」
            bm = re.match(r"^([\u4e00-\u9fa5A-Za-z0-9（）()]{2,30}"
                          r"(?:有限责任公司|有限公司|分公司|公司))", title)
            buyer = clean(bm.group(1), 30) if bm else "中国移动"
            region = clean(it.get("companyTypeName") or "", 10)
            # 专业公司（中移动信息/研究院等）的 companyTypeName 是公司名而非省份，
            # 这种情况拿它当采购人，地域留空，避免出现「地域：信息技术公司」
            if re.search(r"公司|集团|中心|研究院|事业部", region):
                if buyer == "中国移动":
                    buyer = region
                region = ""

            seen.add(link)
            items.append({
                "title": title,
                "url": link,
                "buyer": buyer,
                "region": region,
                "kind": kind,
                "cat": "电信运营商",
                "channel": "中国移动",
                "published": pdate,
                "ts": dt.timestamp(),
            })
    return items


def fetch_tenders():
    """汇总所有招投标源；单源失败不影响其它源。"""
    all_items, seen = [], set()
    for fn, label in ((fetch_tenders_ccgp, "政府采购网"),
                      (fetch_tenders_cmcc, "中国移动")):
        try:
            got = fn()
            print("  招投标·%s: %d 条" % (label, len(got)))
        except Exception as e:
            print("  [error] 招投标·%s 异常: %s" % (label, e))
            continue
        for it in got:
            key = normalize_url(it["url"])
            if key in seen:
                continue
            seen.add(key)
            all_items.append(it)
    all_items.sort(key=lambda x: -x["ts"])
    return all_items[:TENDER_MAX_ITEMS]


def collect():
    print("== 抓取数据源 ==")
    all_items = []
    for fn in (fetch_eastmoney, fetch_sina7x24, fetch_adquan,
               fetch_huxiu, fetch_digitaling, fetch_rss, fetch_cnnews_search,
               fetch_wechat_sogou):
        try:
            got = fn()
            print("  %s: %d 条" % (fn.__name__, len(got)))
            all_items.extend(got)
        except Exception as e:
            print("  [error] %s 异常: %s" % (fn.__name__, e))
    return all_items


# ---------------------------------------------------------------- 去重/筛选

def normalize_url(u):
    u = re.sub(r"^https?://", "", u or "").rstrip("/")
    return u.lower()


def dedupe_and_rank(items, window_hours=None):
    """去重 —— URL/标题键去重，再按标题相似度合并同一事件的多家转载。

    排序优先级：48 小时内 > 户外/梯媒核心主题 > 得分 > 时间。
    """
    now = datetime.now().timestamp()
    fresh_cut = now - FRESH_HOURS * 3600
    by_key, kept = {}, []
    for it in items:
        k1 = normalize_url(it["url"])
        k2 = re.sub(r"\W", "", it["title"])[:18]
        key = k1 or k2
        if key in by_key:
            # 同一条：合并保留信息更全的
            old = by_key[key]
            if not old.get("summary") and it.get("summary"):
                old.update({"summary": it["summary"], "source": it["source"]})
            continue
        by_key[key] = it
        it["_score"] = score_item(it)
        it["_core"] = 1 if CORE_RE.search(it["title"]) else 0
        kept.append(it)

    pool = [it for it in kept
            if it["_score"] > 0
            and now - it["ts"] <= (CORE_WINDOW_HOURS if it["_core"] else WINDOW_HOURS) * 3600]
    # 核心主题优先于泛广告稿；同层内 48 小时内优先，再看得分
    pool.sort(key=lambda x: (-x["_core"],
                             0 if x["ts"] >= fresh_cut else 1,
                             -x["_score"], -x["ts"]))

    # 同一事件常被多家媒体改写标题转载：标题相似度 ≥ 0.62 视为同一条
    picked = []
    for it in pool:
        t = re.sub(r"\W", "", it["title"])
        if any(_similar(t, p) for p in picked):
            continue
        picked.append(t)
        it["_keep"] = True
    return [it for it in pool if it.get("_keep")]


def _similar(title_a, title_b):
    b = title_b
    if not title_a or not b:
        return False
    return difflib.SequenceMatcher(None, title_a, b).ratio() >= 0.62


ENTITY_RE = re.compile(r"分众传媒|兆讯传媒|新潮传媒")
# 事件归一：财报类关键词统一归为「财报」，否则同一份季报会被当成多个事件
EVENT_RE = re.compile(
    r"三季报|半年报|年报|季报|业绩|营收|净利润|收购|并购|中标|战略合作|"
    r"融资|处罚|人事|出海|跨境|设立|子公司")
EARNINGS_RE = re.compile(r"三季报|半年报|年报|季报|业绩|营收|净利润")


def cap_topics(items, max_per=2):
    """同一公司同一事件（如三季报）最多保留 max_per 条，避免整页被一份财报占满。"""
    counter, out = {}, []
    for it in items:
        ent = ENTITY_RE.search(it["title"])
        ev = EVENT_RE.search(it["title"] + it.get("summary", ""))
        if ent and ev:
            ev_name = "财报" if EARNINGS_RE.search(it["title"] + it.get("summary", "")) else ev.group(0)
            key = (ent.group(0), ev_name)
            if counter.get(key, 0) >= max_per:
                continue
            counter[key] = counter.get(key, 0) + 1
        out.append(it)
    return out


# ---------------------------------------------------------------- HTML 生成

NAV_HOME = (
    '  <header class="site-header">\n'
    '    <div class="container header-inner">\n'
    '      <a class="brand" href="../index.html" aria-label="麦扑 OMAPS 首页">\n'
    '        <span class="brand-mark">O</span>\n'
    '        <span class="brand-name">OMAPS<em>麦扑</em></span>\n'
    '      </a>\n'
    '\n'
    '      <nav class="nav" id="nav" aria-label="主导航">\n'
    '        <a href="../index.html#features">产品功能</a>\n'
    '        <a href="../index.html#workflow">操作流程</a>\n'
    '        <a href="../index.html#deployment">部署方式</a>\n'
    '        <a href="../index.html#pricing">价格方案</a>\n'
    '        <a href="../index.html#faq">常见问题</a>\n'
    '        <a href="../insights/index.html">行业洞察</a>\n'
    '        <a href="./index.html" aria-current="page">每日热点</a>\n'
    '        <a href="../index.html#contact">联系我们</a>\n'
    '      </nav>\n'
    '\n'
    '      <div class="header-actions">\n'
    '        <a class="btn btn-ghost" href="https://saas.omaps.cn/#/login">登录系统</a>\n'
    '        <a class="btn btn-primary" href="https://saas.omaps.cn/#/media-entry">媒体入驻</a>\n'
    '      </div>\n'
    '\n'
    '      <button class="nav-toggle" id="navToggle" aria-label="打开菜单" aria-expanded="false" aria-controls="nav">&#9776;</button>\n'
    '    </div>\n'
    '  </header>\n'
)


def esc(s):
    return html_mod.escape(s or "", quote=True)


def item_html(it, rank):
    badge = ""
    if rank <= 3:
        badge = '<span style="display:inline-block;background:var(--primary);color:#fff;'
        badge += 'font-size:12px;border-radius:4px;padding:1px 7px;margin-right:8px;vertical-align:2px;">'
        badge += "TOP%d</span>" % rank
    summary_html = ""
    if it.get("summary") and it["summary"] != it["title"]:
        summary_html = '<p class="post-card-desc">%s</p>' % esc(it["summary"])
    # 兜底：链接里若残留 JS 模板占位符（__biz=${window.biz}）或非 http 地址，
    # 不做成超链接，避免读者点进去看到「参数错误」
    url = it.get("url", "")
    broken = bool(WX_BAD_URL_RE.search(url)) or not url.startswith("http")
    if not broken and "mp.weixin.qq.com/s" in url:
        # 公众号链接还要看参数是否齐全（如 &sn= 空的地址打开就是「参数错误」）
        broken = not _wx_url_ok(url)
    open_tag = ('<div class="post-card">' if broken
                else '<a class="post-card" href="%s" target="_blank" rel="noopener">' % esc(url))
    close_tag = "</div>" if broken else "</a>"
    more = ('<span class="read-more">原文链接暂不可用</span>' if broken
            else '<span class="read-more">查看原文 &rarr;</span>')
    return (
        open_tag + '\n'
        '          <div class="post-card-body">\n'
        '            <span class="tag">%s</span>\n'
        '            <h2>%s%s</h2>\n'
        '            %s\n'
        '            <div class="post-meta"><time datetime="%s">%s</time>'
        '%s</div>\n'
        '          </div>\n'
        '        %s' % (
            esc(it["source"]), badge, esc(it["title"]),
            summary_html, esc(it["published"][:10]), esc(it["published"]),
            more, close_tag,
        )
    )


PAGE_TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>__PAGE_TITLE__</title>
  <meta name="description" content="__PAGE_DESC__" />
  <meta name="keywords" content="户外广告,户外媒体,分众传媒,梯媒,每日热点,行业动态" />
  <meta name="author" content="麦扑科技" />
  <meta name="robots" content="index, follow" />
  <link rel="canonical" href="__CANONICAL__" />

  <meta property="og:type" content="article" />
  <meta property="og:url" content="__CANONICAL__" />
  <meta property="og:site_name" content="麦扑 OMAPS" />
  <meta property="og:title" content="__PAGE_TITLE__" />
  <meta property="og:description" content="__PAGE_DESC__" />
  <meta property="og:locale" content="zh_CN" />

  <link rel="icon" type="image/png" sizes="32x32" href="../favicon-32x32.png" />
  <link rel="icon" type="image/png" sizes="16x16" href="../favicon-16x16.png" />
  <link rel="apple-touch-icon" sizes="180x180" href="../apple-touch-icon.png" />
  <link rel="manifest" href="../site.webmanifest" />
  <meta name="theme-color" content="#ff8000" />
  <link rel="stylesheet" href="../css/style.css" />
  <link rel="stylesheet" href="../css/article.css" />
  <style>
    .tender-table { width: 100%; border-collapse: collapse; font-size: 14px; background: #fff; }
    .tender-table th, .tender-table td { padding: 12px 14px; border-bottom: 1px solid #eee; text-align: left; vertical-align: top; }
    .tender-table th { color: #666; font-weight: 600; background: #fafafa; white-space: nowrap; }
    .tender-table td a { color: #1a1a1a; text-decoration: none; font-weight: 600; }
    .tender-table td a:hover { color: var(--primary); }
    .tender-kind { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px;
                   background: var(--primary-soft); color: var(--primary); font-weight: 600; }
    .tender-kind.is-bid { background: #eef7ee; color: #2f8f46; }
    .tender-cat { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px;
                  background: #f2f4f7; color: #55606e; }
    /* 往期日历：默认只显示一个按钮，点开才弹出月历 */
    .news-cal-row { text-align: center; padding: 26px 0 0; }
    .news-cal-trigger { display: inline-flex; align-items: center; gap: 9px; padding: 10px 20px;
                        border: 1px solid var(--border); background: #fff; border-radius: 999px;
                        font-size: 14px; font-weight: 600; color: #333; cursor: pointer;
                        transition: color .2s ease, border-color .2s ease, box-shadow .2s ease; }
    .news-cal-trigger:hover { color: var(--primary); border-color: var(--primary);
                              box-shadow: 0 8px 20px rgba(255,128,0,.14); }
    .news-cal-trigger svg { width: 16px; height: 16px; flex: none; }
    .news-cal-trigger .hint { font-weight: 400; color: #999; font-size: 13px; }

    .news-cal-modal { position: fixed; inset: 0; z-index: 300; display: flex;
                      align-items: center; justify-content: center; padding: 20px; }
    .news-cal-modal[hidden] { display: none; }
    .news-cal-backdrop { position: absolute; inset: 0; background: rgba(20, 24, 33, .55); }
    .news-cal-dialog { position: relative; width: 100%; max-width: 380px; background: #fff;
                       border-radius: 16px; padding: 20px 20px 14px;
                       box-shadow: 0 24px 60px rgba(0, 0, 0, .28);
                       animation: newsCalIn .18s ease-out; }
    @keyframes newsCalIn { from { opacity: 0; transform: translateY(10px) scale(.98); }
                           to { opacity: 1; transform: none; } }
    body.news-cal-locked { overflow: hidden; }
    .news-cal-head { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
    .news-cal-label { font-size: 14px; font-weight: 600; color: #333; }
    .news-cal-ctrl { display: flex; align-items: center; gap: 4px; margin-left: auto; }
    .news-cal-title { font-size: 14px; font-weight: 600; min-width: 84px; text-align: center; }
    .news-cal-close { width: 28px; height: 28px; border: 0; background: #f4f5f7; border-radius: 50%;
                      color: #666; font-size: 17px; line-height: 1; cursor: pointer; margin-left: 4px; }
    .news-cal-close:hover { background: var(--primary-soft); color: var(--primary); }
    .news-cal-nav { width: 26px; height: 26px; border: 1px solid #e5e5e5; background: #fff;
                    border-radius: 8px; cursor: pointer; color: #666; font-size: 14px;
                    line-height: 1; padding: 0; }
    .news-cal-nav:hover:not(:disabled) { border-color: var(--primary); color: var(--primary); }
    .news-cal-nav:disabled { opacity: .3; cursor: not-allowed; }
    .news-cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; }
    .news-cal-w { text-align: center; font-size: 12px; color: #aaa; padding: 2px 0 6px; }
    .news-cal-d { display: flex; align-items: center; justify-content: center; height: 34px;
                  border-radius: 8px; font-size: 13px; color: #c8c8c8; }
    .news-cal-d.has { color: #333; background: #f5f6f8; font-weight: 600; text-decoration: none; }
    .news-cal-d.has:hover { background: var(--primary-soft); color: var(--primary); }
    .news-cal-d.is-current, .news-cal-d.is-current:hover { background: var(--primary); color: #fff; }
    .news-cal-d.is-empty { background: none; }
    .news-cal-tip { margin: 12px 0 0; font-size: 12px; color: #aaa; line-height: 1.7; }
    .news-cal-tip a { color: var(--primary); text-decoration: none; }
  </style>

  <script>
    var _hmt = _hmt || [];
    (function () {
      var hm = document.createElement("script");
      hm.src = "https://hm.baidu.com/hm.js?91d1ed7b8e9f57cc6d6af12e7feed6ed";
      var s = document.getElementsByTagName("script")[0];
      s.parentNode.insertBefore(hm, s);
    })();
  </script>
</head>
<body>
  <a class="skip-link" href="#main">跳到主要内容</a>

__NAV__

  <main id="main">
    <section class="page-hero">
      <div class="container">
        <span class="section-badge">每日热点 <span class="en">Daily News</span></span>
        <h1>__H1__</h1>
        <p>自动汇集全网与户外广告、户外媒体、分众传媒相关的最新资讯，每日一篇，当日多次运行只更新本篇。</p>
      </div>
    </section>

__CALENDAR_SECTION__

    <section class="section">
      <div class="container">
        <p style="color:#999;font-size:14px;margin:0 0 18px;">__META_LINE__</p>
__ITEMS__
      </div>
    </section>

__TENDER_SECTION__
  </main>

  <footer class="site-footer">
    <div class="container">
      <p>&copy; 2026 麦扑科技 · OMAPS 户外媒体点位管理系统</p>
    </div>
  </footer>

  <script src="../js/main.js"></script>
</body>
</html>
"""


CALENDAR_JS = """
  <script>
  (function () {
    var CAL = window.NEWS_CAL || {}, CUR = window.NEWS_CUR || "";
    var keys = Object.keys(CAL).sort();
    var modal = document.getElementById("news-cal-modal");
    var openBtn = document.getElementById("news-cal-open");
    if (!modal || !openBtn) return;
    var grid = document.getElementById("news-cal-grid");
    var title = document.getElementById("news-cal-title");
    var prev = document.getElementById("news-cal-prev");
    var next = document.getElementById("news-cal-next");
    var closeBtn = document.getElementById("news-cal-close");
    if (!grid || !title || !prev || !next || !closeBtn) return;
    if (!keys.length) { openBtn.parentNode.style.display = "none"; return; }
    var minM = keys[0].slice(0, 7), maxM = keys[keys.length - 1].slice(0, 7);
    var state = (CUR || keys[keys.length - 1]).slice(0, 7);
    var lastFocus = null;

    function pad(n) { return n < 10 ? "0" + n : "" + n; }
    function shift(m, delta) {
      var y = +m.slice(0, 4), mo = +m.slice(5, 7) + delta;
      if (mo < 1) { mo = 12; y -= 1; }
      if (mo > 12) { mo = 1; y += 1; }
      return y + "-" + pad(mo);
    }
    function render() {
      var y = +state.slice(0, 4), mo = +state.slice(5, 7);
      title.textContent = y + " 年 " + mo + " 月";
      var lead = (new Date(y, mo - 1, 1).getDay() + 6) % 7;   /* 周一为首列 */
      var days = new Date(y, mo, 0).getDate();
      var html = "";
      ["一", "二", "三", "四", "五", "六", "日"].forEach(function (w) {
        html += '<span class="news-cal-w">' + w + "</span>";
      });
      for (var i = 0; i < lead; i++) html += '<span class="news-cal-d is-empty"></span>';
      for (var d = 1; d <= days; d++) {
        var ds = state + "-" + pad(d), href = CAL[ds];
        var cls = "news-cal-d" + (href ? " has" : "") + (ds === CUR ? " is-current" : "");
        html += href
          ? '<a class="' + cls + '" href="' + href + '" title="查看 ' + ds + ' 热点">' + d + "</a>"
          : '<span class="' + cls + '">' + d + "</span>";
      }
      grid.innerHTML = html;
      prev.disabled = state <= minM;
      next.disabled = state >= maxM;
    }
    function open() {
      lastFocus = document.activeElement;
      state = (CUR || keys[keys.length - 1]).slice(0, 7);
      render();
      modal.hidden = false;
      document.body.classList.add("news-cal-locked");
      closeBtn.focus();
    }
    function close() {
      modal.hidden = true;
      document.body.classList.remove("news-cal-locked");
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    }
    openBtn.addEventListener("click", open);
    closeBtn.addEventListener("click", close);
    modal.addEventListener("click", function (e) {
      if (e.target.hasAttribute("data-close")) close();      /* 点遮罩关闭 */
    });
    document.addEventListener("keydown", function (e) {
      if ((e.key === "Escape" || e.key === "Esc") && !modal.hidden) close();
    });
    prev.addEventListener("click", function () { state = shift(state, -1); render(); });
    next.addEventListener("click", function () { state = shift(state, 1); render(); });
  })();
  </script>"""


def calendar_section(archive_dates, issue_date, self_href="./index.html"):
    """往期日历（弹出式）：默认只占一个按钮，点开才弹出月历。

    self_href 是当前页面自己的地址（首页=./index.html，归档页=./archive-*.html），
    避免归档页把「自己那天」错指到永远是最新一期的 index.html。
    无 JS 时按钮隐藏，退化成一行最近 12 期的文字链接。
    """
    dates = sorted(set(archive_dates) | {issue_date})
    links = {d: (self_href if d == issue_date else "./archive-%s.html" % d)
             for d in dates}
    recent = dates[::-1][:12]
    fallback = " · ".join('<a href="%s">%s</a>' % (links[d], d) for d in recent)
    return (
        '    <section class="section" style="padding:0;">\n'
        '      <div class="container">\n'
        '        <div class="news-cal-row">\n'
        '          <button type="button" class="news-cal-trigger" id="news-cal-open"'
        ' aria-haspopup="dialog" aria-controls="news-cal-modal">\n'
        '            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'
        ' stroke-width="1.8" stroke-linecap="round" aria-hidden="true">'
        '<rect x="3" y="5" width="18" height="16" rx="3"/>'
        '<path d="M8 3v4M16 3v4M3 10h18"/></svg>\n'
        '            往期日历\n'
        '            <span class="hint">共 %d 期</span>\n'
        '          </button>\n'
        '        </div>\n'
        '        <noscript><style>#news-cal-open{display:none}</style>\n'
        '          <p class="news-cal-tip" style="text-align:center;">'
        '最近 12 期：%s</p></noscript>\n'
        '      </div>\n'
        '    </section>\n'
        '    <div class="news-cal-modal" id="news-cal-modal" hidden role="dialog"'
        ' aria-modal="true" aria-labelledby="news-cal-label">\n'
        '      <div class="news-cal-backdrop" data-close></div>\n'
        '      <div class="news-cal-dialog" role="document">\n'
        '        <div class="news-cal-head">\n'
        '          <span class="news-cal-label" id="news-cal-label">往期日历</span>\n'
        '          <div class="news-cal-ctrl">\n'
        '            <button type="button" id="news-cal-prev" class="news-cal-nav"'
        ' aria-label="上一个月">&#8249;</button>\n'
        '            <span class="news-cal-title" id="news-cal-title"></span>\n'
        '            <button type="button" id="news-cal-next" class="news-cal-nav"'
        ' aria-label="下一个月">&#8250;</button>\n'
        '          </div>\n'
        '          <button type="button" class="news-cal-close" id="news-cal-close"'
        ' aria-label="关闭日历">&times;</button>\n'
        '        </div>\n'
        '        <div class="news-cal-grid" id="news-cal-grid"></div>\n'
        '        <p class="news-cal-tip">深色日期为已发布的当期热点，点击即可查看；'
        '仅保留最近 %d 天。</p>\n'
        '      </div>\n'
        '    </div>\n'
        '    <script>window.NEWS_CAL=%s;window.NEWS_CUR="%s";</script>%s\n'
        % (len(dates), fallback, KEEP_DAYS,
           json.dumps(links, ensure_ascii=False, sort_keys=True), issue_date, CALENDAR_JS)
    )


def render_items_html(groups):
    """groups: [(分组标题, 分组说明, [items...]), ...]，编号全局连续。"""
    parts, counter = [], 0
    for gtitle, gsub, items in groups:
        if not items:
            continue
        parts.append(
            '<h3 style="margin:6px 0 16px;font-size:18px;">%s '
            '<span style="color:#999;font-size:13px;font-weight:400;">%s</span></h3>'
            % (esc(gtitle), esc(gsub)))
        parts.append('<div class="post-list" style="margin-bottom:26px;">')
        for it in items:
            counter += 1
            parts.append(item_html(it, counter))
        parts.append('</div>')
    return "\n".join(parts)


def tender_section(tenders):
    """招投标信息表格：日期 / 行业 / 类型 / 项目 / 采购人·地域 / 来源。"""
    if tenders:
        rows = []
        for t in tenders:
            who = " · ".join(x for x in (t.get("buyer"), t.get("region")) if x) or "—"
            rows.append(
                '<tr>'
                '<td style="white-space:nowrap;">%s</td>'
                '<td style="white-space:nowrap;"><span class="tender-cat">%s</span></td>'
                '<td style="white-space:nowrap;"><span class="tender-kind%s">%s</span></td>'
                '<td><a href="%s" target="_blank" rel="noopener">%s</a></td>'
                '<td style="white-space:nowrap;color:#666;">%s</td>'
                '<td style="white-space:nowrap;color:#aaa;font-size:12px;">%s</td>'
                '</tr>' % (
                    esc(t.get("published", "")),
                    esc(t.get("cat") or "其他"),
                    " is-bid" if "中标" in t.get("kind", "") else "",
                    esc(t.get("kind", "")),
                    esc(t["url"]), esc(t["title"]), esc(who),
                    esc(t.get("channel", "")),
                ))
        body = ('    <div style="overflow-x:auto;">\n'
                '      <table class="tender-table">\n'
                '        <thead><tr><th>发布日期</th><th>行业</th><th>类型</th>'
                '<th>项目名称</th><th>采购人 / 地域</th><th>来源</th></tr></thead>\n'
                '        <tbody>%s</tbody>\n'
                '      </table>\n'
                '    </div>' % "".join(rows))
    else:
        body = ('    <p style="color:#999;font-size:14px;margin:0;">'
                '本期未取到符合条件的公告（数据源按 IP 限频时会自动跳过，次日恢复）。'
                '可直接到 <a href="http://search.ccgp.gov.cn/" target="_blank" '
                'rel="noopener" style="color:var(--primary);">中国政府采购网</a>'
                ' 或 <a href="https://b2b.10086.cn/" target="_blank" rel="noopener" '
                'style="color:var(--primary);">中国移动采购与招标网</a>'
                ' 查看最新公告。</p>')
    return (
        '<section class="section" style="border-top:1px solid #eee;">\n'
        '  <div class="container">\n'
        '    <div class="section-head">\n'
        '      <span class="section-badge">招投标 <span class="en">Bidding</span></span>\n'
        '      <h2>线下广告投放招投标</h2>\n'
        '      <p>近 %d 天中国政府采购网、中国移动采购与招标网发布的户外广告、广告位、'
        '媒体投放相关采购与中选公告，点击项目名可查看原文。</p>\n'
        '    </div>\n'
        '%s\n'
        '  </div>\n'
        '</section>' % (TENDER_WINDOW_DAYS, body)
    )


def write_page(path, groups, issue_date, archive_dates, meta, is_index=False):
    """meta: {date, first, updated, runs, count}；每日一篇，页内标注首发/更新时间。"""
    # 资讯条数只算新闻，别把「研究观点」组的研报也并进来（否则 8 条资讯 + 1 篇研报会显示成 9 条资讯）
    total = meta.get("count") or sum(
        len(g[2]) for g in groups if g[0] != "研究观点")
    canonical = ("https://www.omaps.cn/news/" if is_index
                 else "https://www.omaps.cn/news/archive-%s.html" % issue_date)
    page_title = "%s 户外媒体每日热点 · 第 %d 期 | 麦扑 OMAPS" % (issue_date, meta["runs"])
    page_desc = ("%s 户外广告、户外媒体、分众传媒等行业动态汇总，共 %d 条，"
                 "更新于 %s，点击可查看原文。" % (issue_date, total, meta["updated"]))

    tender_n = len(meta.get("tenders") or [])
    tender_txt = "　·　招投标 %d 条" % tender_n if tender_n else ""
    research_n = len(meta.get("research") or [])
    research_txt = "　·　研报 %d 篇" % research_n if research_n else ""
    span = "周边近 %d 天 / 核心近 %d 天，已去重" % (NEWS_WINDOW_DAYS, CORE_WINDOW_DAYS)
    if meta["runs"] > 1:
        meta_line = ("本期：%s　资讯 %d 条（%s）%s%s　·　"
                     "首次发布 %s　·　最近更新 %s（第 %d 次更新）"
                     % (issue_date, total, span, tender_txt, research_txt,
                        meta["first"], meta["updated"], meta["runs"]))
    else:
        meta_line = ("本期：%s　资讯 %d 条（%s）%s%s　·　"
                     "发布于 %s" % (issue_date, total, span, tender_txt,
                                  research_txt, meta["first"]))

    page = PAGE_TPL
    page = page.replace("__NAV__", NAV_HOME.rstrip("\n"))
    page = page.replace("__PAGE_TITLE__", esc(page_title))
    page = page.replace("__PAGE_DESC__", esc(page_desc))
    page = page.replace("__CANONICAL__", canonical)
    page = page.replace("__H1__", "%s 户外媒体每日热点" % issue_date)
    page = page.replace("__META_LINE__", meta_line)
    page = page.replace("__ITEMS__", render_items_html(groups))
    page = page.replace("__TENDER_SECTION__", tender_section(meta.get("tenders") or []))
    page = page.replace("__WINDOW_DAYS__", str(NEWS_WINDOW_DAYS))
    page = page.replace("__CORE_WINDOW_DAYS__", str(CORE_WINDOW_DAYS))
    page = page.replace("__CALENDAR_SECTION__",
                        calendar_section(archive_dates, issue_date,
                                         "./index.html" if is_index
                                         else "./archive-%s.html" % issue_date))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    os.chmod(path, 0o644)


# ---------------------------------------------------------------- 首页注入

HOME_BLOCK_TPL = """<ul style="list-style:none;padding:0;margin:0;display:grid;gap:12px;">
          {items}
        </ul>"""


def home_item_html(it):
    return (
        '<li style="border:1px solid #eee;border-radius:12px;padding:14px 20px;">'
        '<a href="{url}" target="_blank" rel="noopener" style="font-weight:600;'
        'color:#1a1a1a;text-decoration:none;">{title}</a>'
        '<span style="color:#999;font-size:13px;margin-left:10px;white-space:nowrap;">'
        '{source} · {published}</span>'
        '{summary}'
        '</li>'
    ).format(
        url=esc(it["url"]), title=esc(it["title"]),
        source=esc(it["source"]), published=esc(it["published"]),
        summary=('<p style="margin:6px 0 0;color:#666;font-size:14px;">%s</p>' % esc(it["summary"]))
        if it.get("summary") and it["summary"] != it["title"] else "",
    )


def update_home(items, issue_date):
    """替换首页 DAILY-NEWS 标记块内容；标记不存在则跳过并告警。"""
    block = HOME_BLOCK_TPL.format(
        items="\n          ".join(home_item_html(it) for it in items))
    block += ('\n          <p style="margin:16px 0 0;">'
              '<a href="./news/index.html" style="color:var(--primary);font-weight:600;'
              'text-decoration:none;">查看全部 %s 期热点 →</a></p>' % issue_date)

    with open(HOME_HTML, encoding="utf-8") as f:
        content = f.read()
    m = re.search(r"<!--\s*DAILY-NEWS:BEGIN\s*-->(.*?)<!--\s*DAILY-NEWS:END\s*-->",
                  content, re.S)
    if not m:
        print("  [warn] 首页未找到 DAILY-NEWS 标记块，跳过注入")
        return False
    new_content = (content[:m.start(1)] + "\n" + block + "\n          " +
                   content[m.end(1):])
    with open(HOME_HTML, "w", encoding="utf-8") as f:
        f.write(new_content)
    return True


# ---------------------------------------------------------------- sitemap

def update_sitemap(today):
    """把 sitemap.xml 里 /news/ 的 lastmod 更新为今日。"""
    path = os.path.join(SITE_DIR, "sitemap.xml")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        content = f.read()
    new = re.sub(
        r"(<loc>https://www\.omaps\.cn/news/</loc>\s*<lastmod>)\d{4}-\d{2}-\d{2}",
        r"\g<1>%s" % today, content)
    if new != content:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        print("== sitemap.xml lastmod 已更新 ==")


# ---------------------------------------------------------------- 企微通知
def notify_wecom(markdown_text):
    """通过 QY_WEBHOOK 推送企业微信机器人消息；未配置则跳过。"""
    webhook = os.environ.get("QY_WEBHOOK")
    if not webhook:
        return
    try:
        payload = json.dumps({
            "msgtype": "markdown",
            "markdown": {"content": markdown_text[:4000]},
        }).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"})
        urllib.request.urlopen(req, timeout=10, context=SSL_CTX)
    except Exception as e:
        print("  [warn] 企微通知失败: %s" % e)


def notify_success(top, today, added=0, tender_count=0, added_tenders=0):
    lines = ["## 📰 OMAPS 官网每日热点已更新（%s）" % today, ""]
    for i, it in enumerate(top[:8], 1):
        lines.append("%d. [%s](%s)" % (i, it["title"], it["url"]))
    lines.append("")
    lines.append("当日一篇已更新：https://www.omaps.cn/news/archive-%s.html"
                 "（资讯 %d 条 / 新增 %d 条；招投标 %d 条 / 新增 %d 条）"
                 % (today, len(top), added, tender_count, added_tenders))
    notify_wecom("\n".join(lines))


def notify_failure(reason):
    notify_wecom("## ⚠️ OMAPS 每日热点抓取失败\n\n时间：%s\n原因：%s\n\n> 今日页面保留昨日版本，请检查服务器网络或数据源。"
                 % (datetime.now().strftime("%Y-%m-%d %H:%M"), reason))


# ---------------------------------------------------------------- 主流程

def prune_old_files():
    cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    removed = 0
    for fn in os.listdir(DATA_DIR) if os.path.isdir(DATA_DIR) else []:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\.json$", fn)
        if m and m.group(1) < cutoff:
            os.remove(os.path.join(DATA_DIR, fn))
            arch = os.path.join(NEWS_DIR, "archive-%s.html" % m.group(1))
            if os.path.exists(arch):
                os.remove(arch)
            removed += 1
    if removed:
        print("== 清理 %d 个过期归档 ==" % removed)


def load_existing(today):
    """读取当日已发布的一篇（同日多次运行 = 更新这一篇，不新增）。"""
    path = os.path.join(DATA_DIR, "%s.json" % today)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("  [warn] 读取当日已发布数据失败（按新发布处理）: %s" % e)
        return None


def passes_filters(it, source=None):
    """按当前规则复核条目（用于合并当日已发布内容，规则收紧后旧条目自动淘汰）。"""
    if score_item(it) <= 0:
        return False
    age = datetime.now().timestamp() - it.get("ts", 0)
    # 核心主题放宽到 7 天（财报/并购只在当天有一波稿），周边仍是 3 天
    window = CORE_WINDOW_HOURS if CORE_RE.search(it.get("title", "")) else WINDOW_HOURS
    if age > window * 3600:
        return False
    src = source if source is not None else it.get("source", "")
    if src.startswith(WX_SOURCE_PREFIX):
        # 公众号条目不走媒体白名单：改按「账号像行业媒体 + 标题非投放教程」复核。
        # 顺带清掉历史上带 ${window.biz} 这类模板占位符的坏链接（点开必然报错）。
        return (_wx_account_ok(src[len(WX_SOURCE_PREFIX):])
                and not WX_SPAM_TITLE_RE.search(it["title"])
                and _wx_url_ok(it.get("url", "")))
    if not TRUSTED_MEDIA_RE.search(src):
        return False        # 非白名单原发媒体：历史遗留的低质转载一并清掉
    if ("广告门" in src or "数英" in src) and not AD_INDUSTRY_RE.search(it["title"]):
        return False
    return True


def merge_with_existing(existing_items, new_items):
    """当日已发布条目优先保留，新抓到的并入其后；按 URL / 标题相似度去重。

    已发布条目也会按当前规则复核一遍：不符合的（比如规则收紧后）直接淘汰。
    """
    existing_items = [it for it in existing_items if passes_filters(it)]
    merged, urls, titles = [], set(), []
    for it in list(existing_items) + list(new_items):
        url_key = normalize_url(it["url"])
        t = re.sub(r"\W", "", it["title"])
        if url_key and url_key in urls:
            continue
        if any(_similar(t, x) for x in titles):
            continue
        urls.add(url_key)
        titles.append(t)
        it["_score"] = score_item(it)
        it["_core"] = 1 if CORE_RE.search(it["title"]) else 0
        merged.append(it)
    return merged


def merge_tenders(existing_tenders, fresh_tenders):
    """当日已发布的招投标保留，新抓到的并入；URL + 标题双去重，日期倒序截断。"""
    out, seen, seen_titles = [], set(), set()
    for t in list(existing_tenders) + list(fresh_tenders):
        url = normalize_url(t.get("url", ""))
        title = t.get("title", "")
        tkey = re.sub(r"[\s_（）()【】\[\]\-—·:：,，.。]", "", title)
        if not url or url in seen or (tkey and tkey in seen_titles):
            continue
        if not AD_TENDER_RE.search(title):
            continue
        seen.add(url)
        if tkey:
            seen_titles.add(tkey)
        # 老数据没有分类字段时补上，避免历史条目渲染成空白
        t.setdefault("cat", tender_cat(title, t.get("buyer", "")))
        t.setdefault("channel", "政府采购")
        out.append(t)
    out.sort(key=lambda x: -x.get("ts", 0))
    return out[:TENDER_MAX_ITEMS]


def merge_research(existing_reports, fresh_reports):
    """当日已发布的研报观点保留，新抓到的并入；按 URL 去重，日期倒序截断。"""
    out, seen = [], set()
    for r in list(existing_reports) + list(fresh_reports):
        url = normalize_url(r.get("url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(r)
    out.sort(key=lambda x: -x.get("ts", 0))
    return out[:RESEARCH_MAX_ITEMS]


def rank_by_priority(items):
    """核心主题 > 48h 内 > 得分 > 时间，再按「同一公司同一事件」限流。"""
    now = datetime.now().timestamp()
    fresh_cut = now - FRESH_HOURS * 3600
    pool = [it for it in items if it.get("_score", 0) > 0]
    pool.sort(key=lambda x: (-x["_core"],
                             0 if x["ts"] >= fresh_cut else 1,
                             -x["_score"], -x["ts"]))
    return cap_topics(pool)


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(DATA_DIR, exist_ok=True)

    items = collect()
    if not items:
        print("!! 所有数据源均失败，保留当日已有页面不动")
        notify_failure("所有数据源均无返回（东财/新浪/广告门）")
        sys.exit(2)

    print("== 抓取招投标 ==")
    new_tenders = fetch_tenders()
    print("  fetch_tenders: %d 条" % len(new_tenders))

    print("== 抓取券商研报 ==")
    new_research = fetch_research_reports()
    print("  fetch_research_reports: %d 条" % len(new_research))

    existing = load_existing(today)
    existing_items = (existing or {}).get("items", [])
    existing_tenders = (existing or {}).get("tenders", [])
    existing_research = (existing or {}).get("research", [])
    new_ranked = dedupe_and_rank(items)
    merged = merge_with_existing(existing_items, new_ranked)
    ranked = rank_by_priority(merged)

    core = [it for it in ranked if it["_core"]][:TOP_N]
    other = [it for it in ranked if not it["_core"]][:max(0, TOP_N - len(core))]
    research = merge_research(existing_research, new_research)
    groups = [("核心动态", "户外媒体 · 梯媒 · 分众传媒", core),
              ("行业周边", "广告与传媒行业动态", other),
              ("研究观点", "券商研报对核心标的的判断", research)]
    top = core + other

    # 与当日已发布版本比对：无新增则不重写文件（保持页面稳定、不刷 sitemap）
    tenders = merge_tenders(existing_tenders, new_tenders)

    old_urls = {normalize_url(i["url"]) for i in existing_items}
    new_urls = {normalize_url(i["url"]) for i in top}
    old_tender_urls = {normalize_url(t["url"]) for t in existing_tenders}
    new_tender_urls = {normalize_url(t["url"]) for t in tenders}
    old_research_urls = {normalize_url(r["url"]) for r in existing_research}
    new_research_urls = {normalize_url(r["url"]) for r in research}
    added = len(new_urls - old_urls)
    added_tenders = len(new_tender_urls - old_tender_urls)
    # 老格式（无 runs 字段）也强制重写一次，让当日文章套上新模板
    changed = bool(existing) and (old_urls != new_urls or not existing.get("runs")
                                  or old_tender_urls != new_tender_urls
                                  or old_research_urls != new_research_urls)

    print("== 本期候选 %d 条 → 核心 %d + 周边 %d（新增 %d 条）；招投标 %d 条（新增 %d 条）；"
          "研报 %d 条（新增 %d 条）==" %
          (len(ranked), len(core), len(other), added, len(tenders), added_tenders,
           len(research), len(new_research_urls - old_research_urls)))
    for i, it in enumerate(top, 1):
        print("  %2d. [%d分] %s —— %s" % (i, it["_score"], it["title"], it["source"]))
    for i, r in enumerate(research, 1):
        print("  研%d. %s —— %s" % (i, r["title"], r["source"]))

    if existing and not changed:
        print("== 当日文章无新增内容，保持原样（第 %d 次运行）==" % (existing.get("runs", 1) + 1))
        return
    if DRY_RUN:
        print("(dry-run) 不写文件")
        return

    now_str = datetime.now().strftime("%H:%M")
    meta = {
        "date": today,
        "first": (existing or {}).get("first_published_at") or now_str,
        "updated": now_str,
        "runs": (existing or {}).get("runs", 0) + 1,
        "count": len(top),
        "tenders": tenders,
        "research": research,
    }

    # 归档历史（排除今天）
    archive_dates = sorted(
        (fn[:-5] for fn in os.listdir(DATA_DIR)
         if re.match(r"^\d{4}-\d{2}-\d{2}\.json$", fn) and fn[:-5] != today),
        reverse=True)

    # 1) 数据 JSON（同日覆盖，保留首发时间与运行次数）
    json_path = os.path.join(DATA_DIR, "%s.json" % today)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"date": today,
                   "first_published_at": meta["first"],
                   "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "runs": meta["runs"],
                   "item_count": len(top),
                   "tender_count": len(tenders),
                   "research_count": len(research),
                   "items": [{k: v for k, v in it.items() if not k.startswith("_")}
                             for it in top],
                   "tenders": tenders,
                   "research": research},
                  f, ensure_ascii=False, indent=2)

    # 2) 当日那一篇（归档页）+ 首页展示的最新一篇
    write_page(os.path.join(NEWS_DIR, "archive-%s.html" % today),
               groups, today, archive_dates, meta)
    write_page(os.path.join(NEWS_DIR, "index.html"),
               groups, today, archive_dates, meta, is_index=True)
    print("== 已写入当日文章 archive-%s.html（第 %d 版，共 %d 条）==" %
          (today, meta["runs"], len(top)))

    # 3) 首页注入 Top 5
    if update_home(top[:HOME_TOP_N], today):
        print("== 首页热点区块已更新（Top %d）==" % HOME_TOP_N)

    update_sitemap(today)
    prune_old_files()
    notify_success(top, today, added, len(tenders), added_tenders)


def _render_day(data, today):
    """按当日 JSON 重新生成 archive-<date>.html 与首页 index.html（不联网、不改数据）。

    与 main() 里的渲染段落保持同一口径，供 --repair-wx / --rerender 复用。
    """
    items = data.get("items") or []
    core = [it for it in items if CORE_RE.search(it["title"])]
    other = [it for it in items if not CORE_RE.search(it["title"])]
    groups = [("核心动态", "户外媒体 · 梯媒 · 分众传媒", core),
              ("行业周边", "广告与传媒行业动态", other),
              ("研究观点", "券商研报对核心标的的判断", data.get("research") or [])]
    meta = {"date": today, "first": data.get("first_published_at") or "",
            "updated": (data.get("updated_at") or "")[11:16],
            "runs": data.get("runs", 1), "count": len(items),
            "tenders": data.get("tenders") or [],
            "research": data.get("research") or []}
    archive_dates = sorted(
        (fn[:-5] for fn in os.listdir(DATA_DIR)
         if re.match(r"^\d{4}-\d{2}-\d{2}\.json$", fn) and fn[:-5] != today),
        reverse=True)
    write_page(os.path.join(NEWS_DIR, "archive-%s.html" % today),
               groups, today, archive_dates, meta)
    write_page(os.path.join(NEWS_DIR, "index.html"), groups, today,
               archive_dates, meta, is_index=True)


def rerender_today():
    """不改数据、不联网，只用当日 JSON 重新渲染页面。

    改了渲染逻辑（比如坏链兜底）后，已发布的页面不会自己跟着变——main() 在
    「无新增内容」时会提前返回、保持原样。跑这个命令就能让线上页面立刻用新
    模板重出，不用等下一次抓取。

    用法：python3 jobs/news_digest.py --rerender
    """
    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(DATA_DIR, "%s.json" % today)
    if not os.path.exists(path):
        print("== 当日无数据文件：%s ==" % path)
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    _render_day(data, today)
    print("== 已按当日数据重渲染（条目 %d 条）==" % len(data.get("items") or []))


def check_wechat_links():
    """逐条实测当日公众号链接是否真能打开（不依赖搜狗，随时可跑）。

    - 明确死链（页面报「参数错误 / 已被删除 / 已过期」）→ 清空 url，条目保留
    - 打不开也无法判定（网络抖动、出口 IP 被风控）→ 保留链接，只记「待复查」
    这样即使搜狗被验证码挡住，也能先把线上点不开的链接处理掉。

    用法：python3 jobs/news_digest.py --check-wx
    """
    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(DATA_DIR, "%s.json" % today)
    if not os.path.exists(path):
        print("== 当日无数据文件：%s ==" % path)
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items") or []
    wx = [it for it in items if it.get("source", "").startswith(WX_SOURCE_PREFIX)]
    if not wx:
        print("== 当日没有公众号条目 ==")
        return
    print("== 待实测 %d 条公众号链接 ==" % len(wx))
    dead, unknown = [], 0
    for it in wx:
        url = it.get("url", "")
        st = _wx_article_ok(url) if _wx_url_ok(url) else False
        tag = "OK   " if st is True else ("DEAD " if st is False else "?????")
        print("  %s %s —— %s" % (tag, it["source"][len(WX_SOURCE_PREFIX):], it["title"][:30]))
        if st is False:
            it["url"] = ""
            dead.append(it)
        elif st is None:
            unknown += 1
        time.sleep(0.8)

    if dead:
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _render_day(data, today)
    print("== 死链 %d 条已清空链接（条目保留），无法判定 %d 条保留原样 =="
          % (len(dead), unknown))


def repair_wechat_links():
    """一次性修复：把当日已发布条目里的坏公众号链接重新解析回来。

    典型场景：早期版本把原文页里的 JS 模板串（__biz=${window.biz}）当成了真实
    地址，页面上的「查看原文」点开报「参数错误」。这条命令按标题回搜搜狗，
    命中同账号同标题就换回可用链接；找不到就清空链接，让渲染层显示
    「原文链接暂不可用」——**不删条目**，新闻本身仍是有效资讯。

    用法：python3 jobs/news_digest.py --repair-wx
    """
    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(DATA_DIR, "%s.json" % today)
    if not os.path.exists(path):
        print("== 当日无数据文件：%s ==" % path)
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items") or []
    # 形状不合格（含 sn 空 / 带 ${}）或实测打不开，都算待修
    broken = []
    for it in items:
        if not it.get("source", "").startswith(WX_SOURCE_PREFIX):
            continue
        url = it.get("url", "")
        if not _wx_url_ok(url) or _wx_article_ok(url) is False:
            broken.append(it)
    if not broken:
        print("== 当日没有需要修复的公众号链接 ==")
        return
    print("== 待修复 %d 条 ==" % len(broken))

    try:
        _wx_get(SOGOU_WX_HOME)
    except Exception as e:
        print("  [warn] 搜狗微信不可达: %s" % e)
        return

    fixed, cleared = 0, []
    for it in broken:
        account = it["source"][len(WX_SOURCE_PREFIX):]
        print("  · %s —— %s" % (account, it["title"][:40]))
        want = re.sub(r"\W", "", it["title"])
        url = SOGOU_WX_SEARCH % (urllib.parse.quote(it["title"]), 1)
        try:
            page_html = _wx_get(url, SOGOU_WX_HOME)
        except Exception as e:
            print("     搜索失败: %s" % e)
            continue
        if "VerifyCode" in page_html or "需要您协助验证" in page_html:
            print("     搜狗验证码，停止修复（稍后重试）")
            break
        link = ""
        for b in re.findall(r'<li id="sogou_vr_11002601_box_\d+".*?</li>', page_html, re.S):
            m_t = re.search(r'<h3>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
            m_a = re.search(r'<span class="all-time-y2">([^<]*)</span>', b)
            if not m_t:
                continue
            same_title = _similar(want, re.sub(r"\W", "", clean(m_t.group(2), 100)))
            same_acc = (m_a and html_mod.unescape(m_a.group(1)).strip() == account)
            if same_title and same_acc:
                link = _wx_resolve_link(m_t.group(1), url)
                if link:
                    link = _wx_best_link(link)
                break
            time.sleep(0.5)
        if link:
            it["url"] = link
            fixed += 1
            print("     ✔ 已换回: %s" % link[:80])
        else:
            it["url"] = ""
            cleared.append(it)
            print("     ✘ 未找到同账号同标题，清空链接（条目保留）")
        time.sleep(1.2)

    data["items"] = items
    data["item_count"] = len(items)
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("== 修复 %d 条、清空链接 %d 条（条目保留）==" % (fixed, len(cleared)))

    _render_day(data, today)
    print("== 页面已重渲染 ==")


if __name__ == "__main__":
    if "--repair-wx" in sys.argv:
        repair_wechat_links()
    elif "--check-wx" in sys.argv:
        check_wechat_links()
    elif "--rerender" in sys.argv:
        rerender_today()
    else:
        main()
