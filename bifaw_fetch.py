# -*- coding: utf-8 -*-
"""
bifaw_fetch.py —— 必发指数网「竞彩必发」数据抓取器
https://bifaw.com/bifaw/sporttery.php

该站与 c.spdex.com 完全不同：
    · 页面本体没有任何数据，表格由 ES Module 前端 JS 在**登录成功后**才渲染
    · 数据接口的参数经过加密（bifawdataencrypt.min.js），静态复刻接口成本极高
    · 登录必须过 4 位图形验证码（/securitycode/vdimgck.php）
→ 因此走「真实浏览器 + 验证码 OCR + 读渲染后的 DOM」这条路，而不是拼接口。

登录态用 Chrome 用户目录持久化（bifaw_profile/）：首次登录成功后，
后续运行只要 cookie 没过期就直接抓，不必每次过验证码；
失效时才重新登录（最多重试 N 次，验证码由 ddddocr 识别）。

用法:
    python bifaw_fetch.py                 # 抓取并写 bifaw.json
    python bifaw_fetch.py --capture       # 额外把原始 HTML/截图存到 logs/（调试用）
    python bifaw_fetch.py --out x.json
    python bifaw_fetch.py --login-only    # 只做登录，验证凭据
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(ROOT, "logs")
PROFILE = os.path.join(ROOT, "bifaw_profile")          # Chrome 用户目录（持久化登录态）
CONF = os.path.join(ROOT, "bifaw_conf.json")           # 账号密码（被 .gitignore 挡住，不进仓库）

BASE = "https://bifaw.com"
HOME = BASE + "/bifaw/sporttery.php"
LOGIN = BASE + "/member/login.php?gourl=%2Fbifaw%2Fsporttery.php"
CHROME = os.environ.get("BIFAW_CHROME", "")


def credentials():
    """凭据来源：环境变量优先，其次 bifaw_conf.json。

    ⚠️ 仓库是公开的，账号密码**绝不能写进代码**。CI 走 Secrets（环境变量），
    本机走 bifaw_conf.json（已 gitignore）。
    """
    user = os.environ.get("BIFAW_USER")
    pwd = os.environ.get("BIFAW_PWD")
    if not (user and pwd) and os.path.exists(CONF):
        try:
            with open(CONF, encoding="utf-8") as f:
                c = json.load(f)
            user = user or c.get("user")
            pwd = pwd or c.get("password")
        except Exception:
            pass
    if not (user and pwd):
        raise SystemExit(
            "缺少必发账号：请设置环境变量 BIFAW_USER / BIFAW_PWD，"
            "或创建 %s（格式 {\"user\":\"...\",\"password\":\"...\"}）" % CONF)
    return user, pwd


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def find_chrome():
    """定位真实 Chrome。找不到就返回 None，交给 playwright 自带的 chromium。

    CI（ubuntu-latest）上 Chrome 装在 /usr/bin/google-chrome，会被这里命中，
    于是不必再下载 playwright 的浏览器（省一次 100+MB 的下载）。
    """
    local = os.environ.get("LOCALAPPDATA", "")
    cands = [CHROME,
             "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
             "/usr/bin/chromium", "/usr/bin/chromium-browser",
             "/opt/google/chrome/chrome",
             r"C:\Program Files\Google\Chrome\Application\chrome.exe",
             r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    if local:
        cands.append(os.path.join(local, r"Google\Chrome\Application\chrome.exe"))
    for p in cands:
        if p and os.path.exists(p):
            return p
    return None


class Bifaw:
    def __init__(self, headless=True, capture=False, user="", pwd=""):
        self.capture = capture
        self.headless = headless
        self.user = user
        self.pwd = pwd
        self.pw = None
        self.ctx = None
        self.page = None

    # ---------------- 浏览器 ----------------
    def open(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        os.makedirs(PROFILE, exist_ok=True)
        os.makedirs(LOGDIR, exist_ok=True)
        exe = find_chrome()
        log("使用浏览器：%s" % (exe or "playwright 自带 chromium"))
        kw = dict(user_data_dir=PROFILE, headless=self.headless,
                  args=["--disable-blink-features=AutomationControlled",
                        "--no-sandbox", "--disable-dev-shm-usage"],
                  viewport={"width": 1440, "height": 1000},
                  locale="zh-CN", device_scale_factor=2)
        if exe:
            kw["executable_path"] = exe
        self.ctx = self.pw.chromium.launch_persistent_context(**kw)
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(45000)
        return self

    def close(self):
        for fn in (lambda: self.ctx and self.ctx.close(),
                   lambda: self.pw and self.pw.stop()):
            try:
                fn()
            except Exception:
                pass

    def shot(self, name):
        if not self.capture:
            return
        try:
            self.page.screenshot(path=os.path.join(LOGDIR, name + ".png"), full_page=True)
        except Exception:
            pass

    def save_html(self, name):
        if not self.capture:
            return
        try:
            with open(os.path.join(LOGDIR, name + ".html"), "w", encoding="utf-8") as f:
                f.write(self.page.content())
        except Exception:
            pass

    def body_text(self):
        """导航过程中读 body 可能抛「execution context destroyed」，退避重试"""
        for _ in range(6):
            try:
                return self.page.eval_on_selector("body", "e=>e.innerText")
            except Exception:
                time.sleep(1.0)
        try:
            return re.sub(r"<[^>]+>", " ", self.page.content())
        except Exception:
            return ""

    # ---------------- 登录 ----------------
    def is_logged_in(self):
        """打开竞彩必发页，看是否被挡在登录墙外。

        页面脚本在登录态时会把 #ErrMsgWin 填成「你还没有登陆，请点击此处登陆!」，
        这一句出现得很快（数据本身才需要等），所以用它判断最省时间。
        """
        self.page.goto(HOME, wait_until="domcontentloaded")
        for _ in range(8):
            self.page.wait_for_timeout(1000)
            try:
                t = self.page.evaluate(
                    "() => { const e=document.getElementById('ErrMsgWin');"
                    " return e && e.offsetParent!==null ? e.innerText : ''; }")
            except Exception:
                t = ""
            if t and ("登陆" in t or "登录" in t):
                return False
            if self.page.query_selector("#abf_match table.oddstable"):
                return True
        txt = self.body_text()
        return "你还没有登陆" not in txt and "你还没有登录" not in txt

    def _ocr(self, png):
        import ddddocr
        if not hasattr(self, "_ocr_engine"):
            self._ocr_engine = ddddocr.DdddOcr(show_ad=False)
        code = self._ocr_engine.classification(png)
        return re.sub(r"[^0-9A-Za-z]", "", code)

    def login(self, max_round=8):
        log("登录态失效，开始登录（验证码 OCR）…")
        for n in range(1, max_round + 1):
            self.page.goto(LOGIN, wait_until="domcontentloaded")
            self.page.wait_for_timeout(1200)
            el = self.page.query_selector("#vdimgck")
            if not el:
                log("  第%d轮：没找到验证码，页面=%s" % (n, self.page.url))
                time.sleep(2)
                continue
            try:
                png = el.screenshot()
            except Exception as exc:
                log("  第%d轮：验证码截图失败 %s" % (n, exc))
                time.sleep(1.5)
                continue
            if self.capture:
                with open(os.path.join(LOGDIR, "_cap_%d.png" % n), "wb") as f:
                    f.write(png)
            code = self._ocr(png)
            log("  第%d轮 验证码 OCR=%r" % (n, code))
            if len(code) != 4:
                try:
                    self.page.click("#vdimgck")
                except Exception:
                    pass
                self.page.wait_for_timeout(800)
                continue

            self.page.fill("input[name='userid'], #userid", self.user)
            self.page.fill("input[name='pwd'], #pwd", self.pwd)
            self.page.fill("input[name='vdcode'], #vdcode", code)
            try:
                self.page.click("input[type='image'], input[name='submit']")
            except Exception as exc:
                log("  第%d轮：提交异常 %s" % (n, exc))
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass
            self.page.wait_for_timeout(1800)
            t = self.body_text()
            flat = re.sub(r"\s+", " ", t)
            if "验证码错误" in flat:
                log("  第%d轮：验证码识别错误，重试" % n)
                continue
            if "密码错误" in flat or "用户名不存在" in flat or "账号" in flat and "错误" in flat:
                log("  第%d轮：账号/密码被拒 → %s" % (n, flat[:160]))
                return False
            log("  第%d轮：登录通过（%s）" % (n, flat[:120]))
            self.shot("_bifaw_afterlogin")
            return True
        log("登录失败：重试 %d 轮仍未通过" % max_round)
        return False

    # ---------------- 抓取 ----------------
    # 页面结构（每场比赛一张 <table class="oddstable" id="<matchId>">）：
    #   <tr> 表头：16 个 <th>，第 1 个是队名槽，其余 15 个为
    #        项 / 买家挂牌 / 价位 / 卖家挂牌 / 成交量 / 比例 / 赔付率 /
    #        必指 / 赔指 / 盈亏 / 欧初 / 欧终 / 凯指 / 凯差 / 热指
    #   <tr> 主 / 和 / 客   三行（第 1 行首格是 rowspan 的队名格）
    #   <tr> 大小球两行（class 含 oumatch，项为 大 / 小）
    EXTRACT_JS = r"""
    () => {
      const clean = s => (s || '').replace(/\s+/g, ' ').trim();
      const out = [];
      document.querySelectorAll('#abf_match table.oddstable').forEach(tb => {
        const m = { id: tb.id, rows: [], ou: [] };
        const lg = tb.querySelector('span[data-action="history-league"]');
        if (lg) m.leagueText = clean(lg.textContent);
        // 开赛时间在队名格里的 <span style="float:right">
        // ⚠️ 队名格 <th> 的底色按「日期分组」变色（#6699FF / #0000DB …），
        //    所以不能用颜色筛，必须沿着 ul#team_ 往上找最近的 th
        const ulAny = tb.querySelector('ul[id^="team_"]');
        if (ulAny) {
          const th = ulAny.closest('th');
          const sp = th && th.querySelector('span[style*="float"]');
          if (sp) m.kickoff = clean(sp.textContent);
        }
        const ul = tb.querySelector('ul[id^="team_"]');
        if (ul) {
          const p = (ul.textContent || '').split('@').map(clean).filter(Boolean);
          m.league = p[0] || ''; m.home = p[1] || ''; m.away = p[2] || '';
        }
        const ti = tb.querySelector('.teaminfo li');
        if (ti) m.total = clean(ti.textContent).replace(/^[^:：]*[:：]/, '');
        const oti = tb.querySelector('.oumatch .teaminfo li');
        if (oti) {
          const t = clean(oti.textContent);
          m.ouTotal = t.replace(/^[^:：]*[:：]/, '');
          const mm = t.match(/\(([\d.]+)\)/);
          if (mm) m.ouLine = mm[1];
        }
        // 大小球块首格文字里带 (2.5)
        const firstCell = tb.querySelector('td[bgcolor="#EBEBEB"] .teaminfo li');
        if (firstCell && /\(([\d.]+)\)/.test(clean(firstCell.textContent))) {
          m.ouLine = clean(firstCell.textContent).match(/\(([\d.]+)\)/)[1];
        }

        tb.querySelectorAll('tr').forEach(tr => {
          const tds = Array.from(tr.querySelectorAll('td'));
          if (!tds.length) return;
          // 找「项」格：bgcolor #00A8A8
          let i = tds.findIndex(td => (td.getAttribute('bgcolor') || '').toUpperCase() === '#00A8A8');
          if (i < 0) return;
          const item = clean(tds[i].textContent);
          const vals = tds.slice(i + 1).map(td => {
            const sp = td.querySelector('span');
            return { t: clean(td.textContent), c: sp ? (sp.style.color || '') : '' };
          });
          const rec = { item: item, v: vals.map(x => x.t), c: vals.map(x => x.c) };
          // 价位格是浅蓝底
          const hi = [];
          tds.slice(i + 1).forEach((td, k) => {
            if ((td.getAttribute('bgcolor') || '').toUpperCase() === '#DAEBFE') hi.push(k);
          });
          rec.hi = hi;
          if ((tr.className || '').indexOf('oumatch') >= 0) m.ou.push(rec);
          else m.rows.push(rec);
        });
        out.push(m);
      });
      return out;
    }
    """

    def fetch(self):
        self.page.goto(HOME, wait_until="domcontentloaded")
        try:
            self.page.wait_for_selector("#abf_match .match, #abf_match table, #abf_match tr",
                                        timeout=45000)
        except Exception:
            log("等待比赛表格超时（可能没有赛事）")
        # 数据是异步拉的，等行数稳定下来
        last, stable = -1, 0
        for _ in range(40):
            self.page.wait_for_timeout(1500)
            try:
                n = self.page.evaluate(
                    "() => document.querySelectorAll('#abf_match table.oddstable').length")
            except Exception:
                n = -1
            if n == last and n > 0:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last = n

        self.save_html("_bifaw_page")
        self.shot("_bifaw_page")

        err = self.page.query_selector("#ErrMsgWin")
        if err and err.is_visible():
            txt = err.inner_text().strip()
            log("页面报错：%s" % txt)
            if "登录" in txt:
                return None

        raw = self.page.evaluate(self.EXTRACT_JS)
        if self.capture:
            with open(os.path.join(LOGDIR, "_bifaw_extract.json"), "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=1)
        log("DOM 抽到 %d 场比赛" % len(raw))
        return {"raw": raw}

    # ---------------- 定型 ----------------
    COLUMNS = ["买家挂牌", "价位", "卖家挂牌", "成交量", "比例", "赔付率",
               "必指", "赔指", "盈亏", "欧初", "欧终", "凯指", "凯差", "热指"]

    def build(self, raw):
        matches = []
        for t in raw:
            rows = [{"item": r["item"], "v": r["v"]} for r in t.get("rows", [])]
            ou = [{"item": r["item"], "v": r["v"]} for r in t.get("ou", [])]
            if not rows and not ou:
                continue
            matches.append({
                "id": t.get("id") or "",
                "league": t.get("league") or "",
                "leagueText": t.get("leagueText") or "",
                "kickoff": t.get("kickoff") or "",
                "home": t.get("home") or "",
                "away": t.get("away") or "",
                "total": t.get("total") or "",
                "ouLine": t.get("ouLine") or "",
                "ouTotal": t.get("ouTotal") or "",
                "rows": rows,
                "ou": ou,
            })
        data = {
            "source": "bifaw",
            "sourceName": "竞彩必发（必发指数网）",
            "url": HOME,
            "fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "columns": self.COLUMNS,
            "matches": matches,
        }
        return data


def check_data(d):
    """数据可上线校验：一场都没有 → 认为抓取失败，保留线上旧数据"""
    ms = (d or {}).get("matches") or []
    if not ms:
        return False, "一场都没抓到"
    blank = [m["id"] for m in ms if not m.get("rows")]
    if len(blank) == len(ms):
        return False, "%d 场全部没有指数行" % len(ms)
    note = "%d 场" % len(ms)
    if blank:
        note += "，其中 %d 场缺指数行" % len(blank)
    return True, note


def fetch_once(out, capture=False, headless=False, user="", pwd="", login_only=False):
    """跑一次完整流程，返回 (退出码, 说明)。供命令行与 publish.py 共用。

    ⚠️ 默认**必须有界面**：该站会识别纯 headless 浏览器，识别到就只给一个
    「undefined，请点击此处重新获取!」，拿不到任何数据（已实测）。
    CI 里用 xvfb 提供虚拟显示，效果等同于有界面。
    """
    os.makedirs(LOGDIR, exist_ok=True)
    b = Bifaw(headless=headless, capture=capture, user=user, pwd=pwd).open()
    try:
        if b.is_logged_in():
            log("已有有效登录态，跳过登录")
            if login_only:
                return 0, "session valid"
        else:
            if not b.login():
                return 2, "login failed"
            if login_only:
                return 0, "login succeeded"

        got = b.fetch()
        if got is None:
            return 2, "fetch rejected"

        data = b.build(got["raw"])
        ok, why = check_data(data)
        log("数据校验：%s（%s）" % ("通过" if ok else "不通过", why))
        if not ok:
            return 2, "data unusable: " + why

        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log("已写入 %s（%.1f KB）" % (out, os.path.getsize(out) / 1024.0))

        # 内联快照：file:// 直接打开 index.html 时可用（与 spdex 的 data-inline.js 同款）
        inline = os.path.join(os.path.dirname(os.path.abspath(out)), "bifaw-inline.js")
        with open(inline, "w", encoding="utf-8") as f:
            f.write("/* 自动生成：bifaw 抓取快照，供 file:// 打开时使用 */\n")
            f.write("window.__BIFAW__ = %s;\n"
                    % json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        log("内联快照 -> %s" % os.path.basename(inline))
        return 0, why
    finally:
        b.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "bifaw.json"))
    ap.add_argument("--capture", action="store_true", help="保存原始 HTML / 截图便于调试")
    ap.add_argument("--login-only", action="store_true")
    ap.add_argument("--headless", action="store_true",
                    help="强制无界面（⚠️ 该站会识别无头浏览器并拒绝给数据，仅供排障对照）")
    ap.add_argument("--required", action="store_true",
                    help="抓取失败时返回码 2（CI 用它决定是否中断发布）")
    a = ap.parse_args()

    user, pwd = credentials()
    rc, why = fetch_once(a.out, capture=a.capture, headless=a.headless,
                         user=user, pwd=pwd, login_only=a.login_only)
    if rc == 0:
        log("RESULT: OK - %s" % why)
        return 0
    log("RESULT: FAIL - %s" % why)
    return 2 if a.required else rc


if __name__ == "__main__":
    sys.exit(main())
