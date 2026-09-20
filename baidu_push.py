# -*- coding: utf-8 -*-
"""百度搜索资源平台「普通收录 → API提交（主动推送）」。

把新/更新的 URL 主动推给百度，比等爬虫自己来快得多（通常几分钟内开始抓）。

接口（百度官方，2026-09-19 实测）：
  推送  POST http://data.zz.baidu.com/urls?site=<站点>&token=<token>
        正文 = 每行一个 URL（text/plain）
        返回 {"remain": 剩余配额, "success": 成功条数,
              "not_same_site": [...], "not_valid": [...]}
  配额  GET  同一个地址（实测本账号返回 400，故 --quota 只作参考，不阻塞流程）

⚠️ 四个实测坑（改这个脚本前先读）：
  1) `site` 必须**原样**拼进 query：`site=https://90qu.com`。
     一旦百分号编码成 `https%3A%2F%2F90qu.com`，接口返回
     `{"error":400,"message":"site init fail"}` —— 看着像「站点没验证」，其实是编码问题。
  2) 只能用 **http://data.zz.baidu.com**。https 那个域名证书不匹配，
     报 `CERTIFICATE_VERIFY_FAILED: Hostname mismatch`。
  3) 配额**比文档小得多**。2026-09-19 实测：当天只在 01:53 成功推过 2 条，
     09:02 再推 4 条就 `HTTP 400 {"error":400,"message":"over quota"}`。
     → 所以单轮上限 DEFAULT_MAX = **1**，一天最多也就 2~4 条
       （靠 --daily 保证每个 URL 每天只推一次）。
     → `over quota` 视为**正常上限**（RESULT: SKIP，退出码 0），不是故障：
       配额用完之后当天怎么推都是这句，不必把它显示成红色。
     `site` 填 `https://90qu.com` / `http://90qu.com` / `90qu.com` 都收，
     但 `https://www.90qu.com` 会进 not_same_site（我们推的是裸域）。
  4) 推什么最值：**新 URL 才值钱**。`/football/<今天>/` 归档页每天都是一个全新
     URL，是主动推送最该花配额的地方；`/football/` 与 `/jc/` 是常年不变的老 URL，
     靠 sitemap 的 lastmod 就能被重抓。清单顺序即优先级（见 default_urls）。
  5) **一条 URL 一次请求**（2026-09-20 修）。原来是把一批 URL 拼成一个请求体发出去，
     而百度是**整批校验**：只要一批里超出剩余配额，整批都回 over quota，一条也进不去。
     现在改成逐条发送，最该推的排第一，配额用完立刻停 —— 「推一条算一条」。
  6) **清单只覆盖 /football/ 是不够的**（2026-09-20 修）：`/jc/`、`/xg/` 是同一个域名
     下内容同样天天更新的栏目，却从来没被推过（旧清单取自 /football/ 的 sitemap，
     而 `--max 1` + 固定优先级又让它们永远排不上队）。现在清单扩到三个栏目 +
     两个栏目的当天归档页，常青页做**最久未推优先**的轮转（见 prioritize）。

凭据来源（两处，任一即可，都不进仓库）：
  1) 环境变量 BAIDU_PUSH_SITE / BAIDU_PUSH_TOKEN   ← CI 用 Actions Secrets 注入
  2) 同目录 baidu_push.json （{"site": ..., "token": ...}）← 本机用，已 gitignore
  环境变量优先。

用法：
  python baidu_push.py                       # 推送清单里的全部 URL
  python baidu_push.py --daily               # ★CI 用的模式：每个 URL 每天最多推一次
  python baidu_push.py --url https://90qu.com/football/
  python baidu_push.py --url A B C           # 多条
  python baidu_push.py --max 5               # 本轮最多推几条（默认 3，防烧配额）
  python baidu_push.py --quota               # 只看今日剩余配额
  python baidu_push.py --check               # 只校验配置，不发请求
  python baidu_push.py --dry-run             # 打印将要发的请求，不发

⚠️ 为什么必须有 --daily：本工作流每 20 分钟跑一轮（一天 ~20 轮），
   不加闸门的话 20 轮 × 2 条 = 40 次提交，而配额是**个位数/天**，上午就烧干。
   状态写在 `.baidu-push-state`（JSON: {"last": {"<url>": "<北京时间日期>"}}）。
   ⚠️ CI 每次都是全新 checkout → 这个文件**必须回写进仓库**才能跨轮生效，
   见 .github/workflows/spdex-update.yml 的「回写数据快照到仓库」步骤。

退出码（沿用本项目约定）：
  0 全部成功 / 1 配置或网络失败（要人介入） / 2 部分 URL 被拒
  ⚠️ 配额用尽（over quota）算 0 —— 那是接口的正常上限，不是故障，
     当天后续轮次会一直重试，配额恢复（通常次日）自动接上。
输出最后一行恒为 `RESULT: OK|WARN|FAIL|SKIP - ...`，供 CI grep 判读。
"""
import io
import os
import sys
import json
import time
import urllib.parse
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(ROOT, "baidu_push.json")
STATE = os.path.join(ROOT, ".baidu-push-state")     # 每个 URL 最后被推送的北京时间日期
ENDPOINT = "http://data.zz.baidu.com/urls"
SITE_BASE = "https://90qu.com"
# 单轮最多推几条。实测配额是个位数/天（09-19 推过 2 条之后，4 条的请求就 over quota）。
# 2026-09-20 起改成 **3**：推送本身已改成逐条发送（见 push_one），配额不够时
# 「推一条算一条」并及时停下，不会像整批那样一条都进不去；而每天只在定稿轮跑一次，
# 推 3 条能把「当天新归档页 + 两个最久没推的常青页」一起照顾到。
DEFAULT_MAX = 3

# ── 清单分档（顺序即优先级；每档内部按「最久没推过的排前」轮转，见 prioritize）──
# ① 一天里的**全新 URL**：两个项目各自的按天归档页（/football/<日期>/、/jc/<日期>/）
# ② 内容天天变、URL 常年不变的栏目主页 —— 最值得反复推的常青页
EVERGREEN_URLS = [
    SITE_BASE + "/football/",
    SITE_BASE + "/jc/",
    SITE_BASE + "/xg/",
]
# ③ 归档索引页：内容随归档增长而变，但变得慢，排在栏目主页之后
ARCHIVE_INDEX_URLS = [
    SITE_BASE + "/football/archive/",
    SITE_BASE + "/jc/archive/",
]


def load_conf():
    """环境变量优先，其次 baidu_push.json。返回 (site, token) 或抛出异常。"""
    site = os.environ.get("BAIDU_PUSH_SITE", "").strip()
    token = os.environ.get("BAIDU_PUSH_TOKEN", "").strip()
    if not (site and token) and os.path.isfile(CONF):
        try:
            with io.open(CONF, encoding="utf-8") as f:
                c = json.load(f)
            site = site or (c.get("site") or "").strip()
            token = token or (c.get("token") or "").strip()
        except Exception as e:
            raise RuntimeError("baidu_push.json 解析失败: %s" % e)
    if not site:
        raise RuntimeError("缺少站点地址（BAIDU_PUSH_SITE 或 baidu_push.json 的 site）")
    if not token:
        raise RuntimeError("缺少推送 token（BAIDU_PUSH_TOKEN 或 baidu_push.json 的 token）")
    return site, token


def build_url(site, token):
    # ⚠️ site 必须**原样**拼进 query，不能做百分号编码：
    #    编码成 https%3A%2F%2F90qu.com 会被接口拒掉，返回 {"error":400,"message":"site init fail"}。
    # ⚠️ 只能用 http://，https://data.zz.baidu.com 证书域名不匹配（SSL CERTIFICATE_VERIFY_FAILED）。
    return "%s?site=%s&token=%s" % (ENDPOINT, site, token)


def bj_date():
    """北京时间当天日期（不看本机时区）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))


def today_new_urls():
    """一天里可能出现的**全新 URL**：两个项目各自的按天归档页。

    ⚠️ /football/<今天>/ 由本仓库产出，但**推送步骤跑在「回写 .archive-index.json」之前**
       （CI 步骤顺序：归档定稿 → 推送 → 回写），所以此刻磁盘清单里还没有它 ——
       不能只靠 archive.sitemap_entries()，得显式补进来。
    ⚠️ /jc/<今天>/ 由另一个项目（xGpb_daily）构建并上传，本仓库看不到它是否已生成，
       所以调用方要用 url_exists() 探一下再决定推不推。
    """
    t = bj_date()
    return [SITE_BASE + "/football/%s/" % t, SITE_BASE + "/jc/%s/" % t]


def url_exists(url, timeout=20):
    """URL 是否真的存在（HEAD 2xx/3xx 算存在）。

    ⚠️ 为什么必须探：/jc/<今天>/ 由别的项目产出，本站 CI 无从得知它有没有生成。
       把不存在的 URL 推给百度只会换来 not_valid，而配额是个位数/天，浪费不起。
    ⚠️ 探测失败（网络问题）一律当作「不存在」—— 宁可不推，也不要白烧配额。
    """
    req = urllib.request.Request(
        url, method="HEAD",
        headers={"User-Agent": "Mozilla/5.0 (compatible; 90qu-push/1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 400
    except urllib.error.HTTPError as e:
        return 200 <= e.code < 400
    except Exception:
        return False


def default_urls(verify=True):
    """要推的 URL 清单（**未排序**；顺序由 prioritize 决定）。

    配额是个位数/天，所以清单里放什么、按什么顺序推，直接决定效果：
      1) 今天两个栏目的归档页 —— 每天都是全新 URL、全新内容，最该花配额；
      2) 三个栏目主页 /football/ /jc/ /xg/ —— 内容天天变、URL 不变；
      3) 两个归档索引页；
      4) /football/ 的其余历史归档页（各自「当天」就推过了，这里只是兜底）。

    ⚠️ /football/ 那条线统一来自 archive.sitemap_entries()，和 sitemap 同源，
       别在这里另抄一份 —— 两处不一致就会出现「sitemap 说有、推送却推不到」。
    ⚠️ /jc/、/xg/ 不在本仓库项目的 sitemap 里（它们是另外两个项目），
       所以在这里显式补上：同一个域名、同一份百度配额，没理由只推一个栏目。
    """
    urls = []
    try:
        sys.path.insert(0, ROOT)
        import archive
        urls += [u for u, _p, _f, _m in archive.sitemap_entries()]
    except Exception as e:
        print("!! 取 sitemap URL 清单失败(%s)，退回常青页" % e)
        urls += [SITE_BASE + "/football/", SITE_BASE + "/jc/"]

    for u in today_new_urls():
        if not verify or url_exists(u):
            urls.append(u)
        else:
            print("跳过（当前不存在）：%s" % u)

    urls += EVERGREEN_URLS + ARCHIVE_INDEX_URLS

    seen = set()
    out = []
    for u in urls:                       # 去重保序
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def prioritize(urls, last):
    """按优先级排序：当天新 URL → 栏目主页 → 归档索引 → 历史归档页。

    同一档内按「最久没推过的排前」（从没推过的记空串、排最前）—— 配额是个位数/天，
    只有轮转才能让 /jc/、/xg/ 这类页面也有机会，否则永远被 /football/ 挤掉
    （旧版固定顺序 + --max 1 的结果就是：它们一条都没推过）。
    并列时（比如三个栏目主页都没推过）按 EVERGREEN_URLS 的书写顺序定序：
    数据量最大、最该先推的 /football/ 写在最前。
    """
    fresh = [u for u in today_new_urls() if u in urls]
    ev = [u for u in EVERGREEN_URLS if u in urls]
    ai = [u for u in ARCHIVE_INDEX_URLS if u in urls]
    flat = set(fresh) | set(ev) | set(ai)
    rest = [u for u in urls if u not in flat]
    ev_pref = dict((u, "%d" % i) for i, u in enumerate(EVERGREEN_URLS))

    def by_oldest(lst, pref=None):
        pref = pref or {}
        return sorted(lst, key=lambda u: (last.get(u) or "", pref.get(u) or u))

    return fresh + by_oldest(ev, ev_pref) + by_oldest(ai) + by_oldest(rest)


def load_state():
    """{url: 最后推送日期}；文件坏了就当成空（宁可多推一次也不卡住流程）。"""
    try:
        with io.open(STATE, encoding="utf-8") as f:
            return json.load(f).get("last") or {}
    except Exception:
        return {}


def save_state(last):
    try:
        with io.open(STATE, "w", encoding="utf-8") as f:
            f.write(json.dumps({"last": last}, ensure_ascii=False, indent=2, sort_keys=True))
            f.write("\n")
    except Exception as e:
        print("!! 状态写入失败(%s)：本轮推送仍算成功，但下一轮可能重复推" % e)


def http(url, method, body=None, timeout=30):
    data = None
    headers = {"User-Agent": "Mozilla/5.0 (compatible; 90qu-push/1.0)"}
    if body is not None:
        data = body.encode("utf-8")
        headers["Content-Type"] = "text/plain"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def push_one(url, site, token, dry=False):
    """推送**单条** URL，返回百度响应 dict（dry run 返回 None）。

    ⚠️ 一次只发一条（2026-09-20 改）。百度是**整批校验**：一个请求里只要超出剩余配额，
       整批都回 over quota、一条都进不去。逐条发才能「推一条算一条」：
       最该推的排第一，配额用完立刻停，不会因为多带了一条把前面的也一起带崩。
    ⚠️ 代价只是一轮多条 HTTP 请求，而它一天只在定稿轮跑一次，可以忽略。
    """
    api = build_url(site, token)
    if dry:
        print("--- 请求体（未发送）---")
        print(url)
        return None
    try:
        st, txt = http(api, "POST", url)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        print("!! HTTP %s %s | %s" % (e.code, e.reason, detail[:300]))
        if e.code in (401, 403):
            print("   → token 或站点不匹配，去 ziyuan.baidu.com 的「普通收录→API提交」重新复制")
        elif "over quota" in detail.lower():
            print("   → 当日配额已用尽（正常上限，不是配置问题）：等配额恢复（通常次日）。")
        return {"_http_error": e.code, "_detail": detail}
    except Exception as e:
        print("!! 请求失败: %s" % e)
        return {"_net_error": str(e)}
    print("--- 百度返回 ---")
    print(txt)
    try:
        return json.loads(txt)
    except Exception:
        return {"_raw": txt}


def quota():
    """配额查询：百度文档说 GET 同一个地址能拿到 remain，但**实测本账号始终 400**。
    所以这个子命令只是「试着问一下」，失败不算故障；真实配额看推送返回里的 remain。
    """
    site, token = load_conf()
    api = build_url(site, token)
    try:
        st, txt = http(api, "GET")
    except urllib.error.HTTPError as e:
        if e.code == 400:
            print("配额查询返回 400 —— 百度这个 GET 接口对本账号不开放（实测始终如此，与本脚本无关）。")
            print("真实剩余配额看**推送返回里的 remain**：每次推送成功时脚本都会打印。")
            return 0
        print("!! HTTP %s %s" % (e.code, e.reason))
        return 1
    except Exception as e:
        print("!! 请求失败: %s" % e)
        return 1
    print("配额查询返回: %s" % txt)
    return 0


def arg_after(flag, argv):
    return argv[argv.index(flag) + 1] if flag in argv else None


def is_over_quota(res):
    """响应是不是「当日配额用尽」。

    ⚠️ 实测有两种形态，两种都要算（2026-09-19）：
       · HTTP 400 + 正文 {"error":400,"message":"over quota"}（push_one() 包成 _http_error）
       · HTTP 200 + 同一个 JSON（有些账号走这条，会被当成正常 JSON 返回）
    配额用尽是接口的**正常上限**，不是故障 —— 调用方要把它当 SKIP 而不是 FAIL，
    否则 CI 每天都会飘一条红色假警报。
    """
    if not isinstance(res, dict):
        return False
    detail = str(res.get("_detail") or "")
    msg = str(res.get("message") or "")
    return "over quota" in (detail + " " + msg).lower()


def main():
    argv = sys.argv[1:]
    if "--check" in argv:
        try:
            site, token = load_conf()
            print("配置 OK：site=%s token=%s…%s" % (site, token[:4], token[-2:]))
            print("RESULT: OK - config present")
            return 0
        except Exception as e:
            print("配置缺失: %s" % e)
            print("RESULT: FAIL - %s" % e)
            return 1
    if "--quota" in argv:
        rc = quota()
        print("RESULT: %s - quota probe" % ("OK" if rc == 0 else "FAIL"))
        return rc

    urls = []
    if "--url" in argv:
        i = argv.index("--url")
        urls = [a for a in argv[i + 1:] if a.startswith("http")]
        if not urls:
            print("!! --url 后面没跟 URL")
            print("RESULT: FAIL - no url")
            return 1
    else:
        urls = default_urls()

    # 只推本站 URL，避免 not_same_site 白跑
    site, token = load_conf()
    host = urllib.parse.urlsplit(site).netloc
    keep = [u for u in urls if urllib.parse.urlsplit(u).netloc in (host, "www." + host)]
    dropped = [u for u in urls if u not in keep]
    if dropped:
        print("跳过非本站 URL: %s" % ", ".join(dropped))
    urls = keep
    if not urls:
        print("没有可推的 URL")
        print("RESULT: SKIP - nothing to push")
        return 0

    last = load_state()
    if "--daily" in argv:
        today = bj_date()
        done = [u for u in urls if last.get(u) == today]
        todo = [u for u in urls if last.get(u) != today]
        if done:
            print("今天(%s)已推过，跳过 %d 条：%s" % (today, len(done), ", ".join(done)))
        urls = todo
        if not urls:
            print("RESULT: SKIP - already pushed today")
            return 0

    try:
        cap = int(arg_after("--max", argv) or DEFAULT_MAX)
    except ValueError:
        cap = DEFAULT_MAX

    # 排序：当天新 URL → 栏目主页 → 归档索引 → 历史归档（同档内「最久没推过」的排前）。
    # ⚠️ 必须在 --daily 过滤之后、--max 截断之前：截断的必须是**排序后**的清单，
    #    否则 /jc/、/xg/ 永远排在 /football/ 后面、永远进不了截断窗口 —— 这正是旧版
    #    里它们一次都没被推过的原因。
    urls = prioritize(urls, last)
    shown = urls if cap <= 0 else urls[:cap]
    print("")
    print("按优先级排序后共 %d 条（单轮上限 %d）：" % (len(urls), cap))
    for i, u in enumerate(shown, 1):
        print("  %d) %s   上次推送=%s" % (i, u, last.get(u) or "从未"))
    if cap > 0 and len(urls) > cap:
        print("  …（其余 %d 条下轮再推）" % (len(urls) - cap))
        urls = urls[:cap]
    print("")

    dry = "--dry-run" in argv
    pushed, rejected, failed = [], [], []
    quota_out = False
    for i, u in enumerate(urls, 1):
        print("=== [%d/%d] %s ===" % (i, len(urls), u))
        res = push_one(u, site, token, dry=dry)
        if res is None:
            continue
        if is_over_quota(res):
            print("百度当日配额已用尽（over quota）→ 立即停止本轮，剩余 %d 条留到下次；"
                  "配额恢复（通常次日）后自动接上。这不是故障。" % (len(urls) - i))
            quota_out = True
            break
        # 接口用 HTTP 200 返错时（{"error":400,...}）也要当成失败
        if res.get("_http_error") or res.get("_net_error") or "_raw" in res or res.get("error"):
            print("!! 这一条没推成功：%s" % str(res)[:200])
            failed.append(u)
            continue
        okn = int(res.get("success") or 0)
        rej = [str(x) for x in (res.get("not_valid") or []) + (res.get("not_same_site") or [])]
        if okn:
            pushed.append(u)
        if rej:
            rejected += rej
            print("被拒不推的 URL：%s" % ", ".join(rej))
        print("本条：success=%s remain=%s" % (okn, res.get("remain")))
        # 记账：推成功的、以及被**明确拒掉**的（重推多少次结果都一样，不记会一直烧配额）
        if "--daily" in argv and (okn or rej):
            last[u] = bj_date()
    print("")

    if dry:
        print("RESULT: SKIP - dry run")
        return 0
    if "--daily" in argv:
        save_state(last)
        print("已记入状态：%s" % STATE)

    print("本轮小结：成功 %d、被拒 %d、失败 %d%s"
          % (len(pushed), len(rejected), len(failed),
             "、配额用尽提前停止" if quota_out else ""))
    if pushed and not (failed or rejected):
        print("RESULT: OK - pushed %d/%d%s"
              % (len(pushed), len(urls), "（配额用尽，剩余下轮再推）" if quota_out else ""))
        return 0
    if pushed:
        print("RESULT: WARN - pushed %d/%d（被拒 %d、失败 %d）"
              % (len(pushed), len(urls), len(rejected), len(failed)))
        return 2
    if rejected and not failed:
        print("RESULT: WARN - all pushed URLs rejected")
        return 2
    if failed:
        print("RESULT: FAIL - push request failed")
        return 1
    if quota_out:
        print("RESULT: SKIP - daily push quota exhausted")
        return 0
    print("RESULT: SKIP - nothing to push")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("!! 未捕获异常: %s" % e)
        print("RESULT: FAIL - %s" % e)
        sys.exit(1)
