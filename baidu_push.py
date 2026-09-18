# -*- coding: utf-8 -*-
"""百度搜索资源平台「普通收录 → API提交（主动推送）」。

把新/更新的 URL 主动推给百度，比等爬虫自己来快得多（通常几分钟内开始抓）。

接口（百度官方，2026-09-19 实测）：
  推送  POST http://data.zz.baidu.com/urls?site=<站点>&token=<token>
        正文 = 每行一个 URL（text/plain）
        返回 {"remain": 剩余配额, "success": 成功条数,
              "not_same_site": [...], "not_valid": [...]}
  配额  GET  同一个地址（实测本账号返回 400，故 --quota 只作参考，不阻塞流程）

⚠️ 三个实测坑（改这个脚本前先读）：
  1) `site` 必须**原样**拼进 query：`site=https://90qu.com`。
     一旦百分号编码成 `https%3A%2F%2F90qu.com`，接口返回
     `{"error":400,"message":"site init fail"}` —— 看着像「站点没验证」，其实是编码问题。
  2) 只能用 **http://data.zz.baidu.com**。https 那个域名证书不匹配，
     报 `CERTIFICATE_VERIFY_FAILED: Hostname mismatch`。
  3) 新站配额约 **10 条/天**（返回里的 remain）。别拿真接口做试探，会烧配额。
     `site` 填 `https://90qu.com` / `http://90qu.com` / `90qu.com` 都收，
     但 `https://www.90qu.com` 会进 not_same_site（我们推的是裸域）。

凭据来源（两处，任一即可，都不进仓库）：
  1) 环境变量 BAIDU_PUSH_SITE / BAIDU_PUSH_TOKEN   ← CI 用 Actions Secrets 注入
  2) 同目录 baidu_push.json （{"site": ..., "token": ...}）← 本机用，已 gitignore
  环境变量优先。

用法：
  python baidu_push.py                       # 推送 sitemap 里的全部 URL
  python baidu_push.py --daily               # ★CI 用的模式：每个 URL 每天最多推一次
  python baidu_push.py --url https://90qu.com/football/
  python baidu_push.py --url A B C           # 多条
  python baidu_push.py --max 5               # 本轮最多推几条（默认 8，防烧配额）
  python baidu_push.py --quota               # 只看今日剩余配额
  python baidu_push.py --check               # 只校验配置，不发请求
  python baidu_push.py --dry-run             # 打印将要发的请求体，不发

⚠️ 为什么必须有 --daily：本工作流每 20 分钟跑一轮（一天 ~20 轮），
   不加闸门的话 20 轮 × 2 条 = 40 次提交，而新站配额只有 10 条/天，中午就烧干。
   状态写在 `.baidu-push-state`（JSON: {"last": {"<url>": "<北京时间日期>"}}）。
   ⚠️ CI 每次都是全新 checkout → 这个文件**必须回写进仓库**才能跨轮生效，
   见 .github/workflows/spdex-update.yml 的「回写数据快照到仓库」步骤。

退出码（沿用本项目约定）：
  0 全部成功 / 1 配置或网络失败（要人介入） / 2 部分 URL 被拒
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
DEFAULT_MAX = 8                                     # 单轮最多推几条，防烧配额（新站约 10 条/天）


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


def default_urls():
    """复用 sitemap 的 URL 清单 —— 一处维护，两边一致。"""
    try:
        sys.path.insert(0, ROOT)
        import build_seo_files
        return [u for u, _p, _n in build_seo_files.pages()]
    except Exception as e:
        print("!! 取 sitemap URL 清单失败(%s)，退回默认两条" % e)
        return ["https://90qu.com/football/", "https://90qu.com/jc/"]


def bj_date():
    """北京时间当天日期（不看本机时区）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))


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


def push(urls, dry=False):
    site, token = load_conf()
    api = build_url(site, token)
    print("站点: %s" % site)
    print("接口: %s" % api.replace(token, token[:4] + "***" + token[-2:]))
    print("待推 URL %d 条:" % len(urls))
    for u in urls:
        print("  " + u)
    body = "\n".join(urls)
    if dry:
        print("--- 请求体（未发送）---")
        print(body)
        return None
    try:
        st, txt = http(api, "POST", body)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        print("!! HTTP %s %s | %s" % (e.code, e.reason, detail[:300]))
        if e.code in (401, 403):
            print("   → token 或站点不匹配，去 ziyuan.baidu.com 的「普通收录→API提交」重新复制")
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
    site, _t = load_conf()
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
    if cap > 0 and len(urls) > cap:
        print("超过单轮上限 %d 条（--max 可调），本轮只推前 %d 条，其余下轮再推" % (cap, cap))
        urls = urls[:cap]

    res = push(urls, dry="--dry-run" in argv)
    if res is None:
        print("RESULT: SKIP - dry run")
        return 0
    if res.get("_http_error") or res.get("_net_error") or "_raw" in res:
        print("RESULT: FAIL - push request failed")
        return 1

    ok = int(res.get("success") or 0)
    rejected = (res.get("not_valid") or []) + (res.get("not_same_site") or [])
    # 被明确拒掉的 URL 也记账：它们重推多少次都是一样的结果，不记就会一直烧配额
    if "--daily" in argv:
        today = bj_date()
        for u in urls:
            if u not in rejected:
                last[u] = today
        save_state(last)
        print("已记入状态：%s" % STATE)
    bad = len(rejected)
    if bad:
        print("被拒 URL: %s" % ", ".join(map(str, rejected)))
    if ok == len(urls) and not bad:
        print("RESULT: OK - pushed %d/%d, remain=%s" % (ok, len(urls), res.get("remain")))
        return 0
    if ok:
        print("RESULT: WARN - pushed %d/%d, remain=%s" % (ok, len(urls), res.get("remain")))
        return 2
    print("RESULT: FAIL - pushed 0/%d, resp=%s" % (len(urls), res))
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("!! 未捕获异常: %s" % e)
        print("RESULT: FAIL - %s" % e)
        sys.exit(1)
