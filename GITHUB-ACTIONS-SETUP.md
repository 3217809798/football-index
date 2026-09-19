# 用 GitHub Actions 实现「每 20 分钟自动更新」——摆脱本地电脑

这份文档对应的是已经放在本项目里的这几个文件，你不需要再写代码：

| 文件 | 作用 |
| --- | --- |
| `.github/workflows/spdex-update.yml` | 主定时任务：每 20 分钟抓数据 + FTP 上传 |
| `.github/workflows/keepalive.yml` | 保活任务：防止定时任务 60 天后被 GitHub 自动停用 |
| `build_publish_conf.py` | 在云端用 Secrets 现场生成 `publish.json`（密码不落仓库） |
| `bifaw_fetch.py` | 抓「竞彩必发」（bifaw.com）—— 需要登录 + 过验证码 + 有界面浏览器 |
| `.gitignore` | 挡住 `publish.json`、`bifaw_conf.json`、`bifaw_profile/` 等不该进仓库的东西 |

原理和你现在的本地计划任务完全一样，只是把「跑 `publish.py` 的那台机器」
从你的电脑换成了 GitHub 的服务器：

```
GitHub 服务器（每 20 分钟）
   └─ 抓 bifaw.com（竞彩必发，登录+验证码）→ bf.json
        ↓
   生成 dist/ → FTP 覆盖到 90qu.com/football
```

> 数据文件名在 2026-09-17 做过一次「去源站痕迹」改名：`bifaw.json → bf.json`、
> `bifaw-inline.js → bf-inline.js`（原名字等于把来源写在 URL 上）。服务器上的旧
> 文件名已删除。

页面顶栏有「数据来源」下拉，选项后面标着该数据的时间。

> ⛔ **超级指数（c.spdex.com）已于 2026-09-16 下线。** 该站整体改版成
> Nuxt SPA + 会员登录站，老页面不复存在 —— 任何路径（连 `/robots.txt`）都只
> 返回同一个登录页，抓取每轮「一场都没抓到」。抓取脚本和页面适配器都还留着，
> 要恢复：`python publish.py --with-spdex`（详见「第 6 步」）。

好处：电脑关机、断网、休眠都不影响。代价有两点，都已实测解决：
① 抓取源站从「中国家庭宽带 IP」变成「GitHub 海外机房 IP」；
② **竞彩必发要登录**，需要 OCR 过验证码 + 用有界面浏览器（云端靠 `xvfb`）。
详见「第 3.5 步」。

---

## 第 0 步：FTP 配置（已实测完成 ✅）

你这台是**衡天云的 DirectAdmin（DA）面板**主机，和 cPanel 的目录结构不同，
`remote_dir` 是最容易填错的一项。以下是实测连通并验证过的结果：

```
服务器：ProFTPD @ 43.225.44.236:21
登录名：qucom              （DA 主账号，FTP 起始目录 = /）
远程目录：/domains/90qu.com/public_html/football
```

`publish.json` 已经按这个结果写好了，跑 `python publish.py --check` 返回
`RESULT: OK - ftp config verified`，即：能连接、能进目录、**能写入**。

### 关于 DA 目录结构的说明（换机器时用得上）

DA 里你的账号根目录 `/` 下并没有 `public_html` 的真实文件夹，而是：

| 路径 | 是什么 |
| --- | --- |
| `/` | FTP 登录后的起始目录（= 你的家目录） |
| `/domains/90qu.com/public_html` | **网站真正的根目录**（WordPress 就装在这里） |
| `/public_html` | DA 提供的软链接，指向上面那个，也能用 |

所以 `remote_dir` 有两种等价写法，推荐用完整路径：

```
/domains/90qu.com/public_html/football    ← 已采用（更明确）
/public_html/football                      ← 等价，DA 会解析成上面的路径
```

⚠️ 如果你以后在 DA 面板里**新建了一个专用 FTP 账号**（推荐，见文末安全提示），
该账号会被锁定在它自己的目录里，登录后 `pwd` 就是 `/`。
**这时 `remote_dir` 必须改成 `/`，否则会报「进不去远程目录」。**

### 三种连接方式实测结论（决定 `use_tls` 怎么填）

| 方式 | 结果 |
| --- | --- |
| 明文 FTP（`use_tls: false`，端口 21） | ✅ **可用**，真实上传测试通过 |
| FTPS（`use_tls: true`，AUTH TLS + `prot_p`） | ❌ `425 Unable to build data connection` |
| FTPS + 明文数据通道（`prot_c`） | ❌ 同样 425 |
| SFTP（端口 22） | ❌ 端口关闭。2222 端口实测是个 HTTP 服务，不是 SSH |

结论：**本机只能走明文 FTP**，所以 `publish.json` 里 `use_tls` 填 `false`，
GitHub Secrets 里 `FTP_USE_TLS` **不用加**（默认就是 false）。
`mode` 保持 `ftp`，不要切 `sftp`（22 端口是关的，切了必然失败）。

> 如果你在意密码明文传输，可以在 GitHub Actions 上把 `FTP_USE_TLS` 设为 `true` 试一次
> ——本机 FTPS 失败也可能是本地网络/防火墙拦了 FTPS 的数据连接端口，
> 云端环境不一定相同。但既然明文已验证可用，先用 `false` 跑起来更稳妥。

### 安全提示（建议做，但非必须）

现在用的是 DA **主账号 `qucom`**，它的密码同时是**面板登录密码**，
能访问整个主机（包括 WordPress 的 `wp-config.php`，里面有数据库密码）。
一旦泄露，风险是整个站点。

更安全的做法：DA 面板 → **FTP Management** → **Create FTP Account**，
建一个只锁在 `public_html/football` 目录的专用账号（用户名为 `xxx@90qu.com` 形式），
用它跑自动更新。**记得同时把 `remote_dir` 改成 `/`**（见上面的说明）。

另外，你这个密码已经出现在本次对话记录里了，如果在意，建议在 DA 面板里改一次，
再更新 `publish.json` 和 GitHub Secrets 即可——改密码不需要动任何代码。

---

## 第 1 步：建一个**公开**仓库

打开 https://github.com/new

- **Repository name**：随便，例如 `football-index`
- **Visibility：必须选 Public**（原因见下面的额度说明）
- 不要勾选 "Add a README file"
- 点 Create repository

> **为什么必须公开？**
> GitHub 私有仓库的免费额度是 **2000 分钟/月**。每 20 分钟跑一次 = 72 次/天，
> 一个月约 2160 次，单次约 1～1.5 分钟 → 约 **2200～3200 分钟/月**，仍会超额。
> 公开仓库的 GitHub 托管运行器**不计费**。
> （2026-09-17 由 15 分钟改为 20 分钟，是**为了减少对源站的登录/抓取压力**，
> 不是为了省额度 —— 私有仓库这条路在 20 分钟下依然超标。）
> 如果仓库名不便公开，也可以留在私有仓库，但把频率降到**每小时一次**
> （24 次/天 ≈ 1080 分钟/月，在额度内）——见第 7 节怎么改。
>
> ⚠️ 2026-09-17 补充：**「数据本来就是公开抓的、公开没损失」这个判断后来被推翻了一半。**
> 公开仓库等于把抓取脚本（`bifaw_fetch.py`，含源站地址、登录流程、验证码处理）
> 和历史提交一起公开挂着，源站据此很容易定位到自己被谁抓。页面侧已经做过一轮
> 「源码去源站痕迹」（见 `published-page-source-scrub` 技能），仓库侧要不要跟着
> 转私有，还没定。转私有必须同时把频率降到每小时（否则超额）。

---

## 第 2 步：把文件传上去

### 要提交的文件（12 个）

```
.github/workflows/spdex-update.yml
.github/workflows/keepalive.yml
.gitignore
index.html
bf.json             竞彩必发数据快照
bf-inline.js        file:// 打开时的内联兜底（竞彩必发）
bifaw_fetch.py     抓竞彩必发（登录 + 验证码 OCR）
publish.py         抓取 + 打包 + 上传
archive.py         按天归档页 /football/<日期>/ 与 sitemap 清单
build_publish_conf.py   CI 里由 Secrets 生成 publish.json
baidu_push.py       把页面 URL 主动推给百度（定稿轮推 1 条：当天新归档页）
build_seo_files.py  生成 robots.txt / sitemap-data.xml（URL 清单取自 archive.py）
```

**两个文件不在上面的清单里，但 CI 自己会提交它们**（都只有日期/URL，不含凭据）：

| 文件 | 作用 | 不提交会怎样 |
| --- | --- | --- |
| `.baidu-push-state` | 哪些 URL 今天已经推过百度 | 同一 URL 会被重复推送，把当天配额白烧掉（配额是个位数/天） |
| `.archive-index.json` | 历史上有哪些天被归档了 | 归档索引页与 sitemap 只剩今天一天，昨天/前天的归档页变孤儿 |

两者都由 workflow 的「回写数据快照到仓库」步骤提交，且**只在真抓到新数据（fetch 轮）
或真定稿了新归档（finalize 轮）那轮**提交。
（`spdex_fetch.py` / `data.json` / `data-inline.js` 属于已下线的超级指数，
留着不提交也行；它们已不再被 pipeline 使用，提交了也不会有影响。）

### 绝对不要提交的文件

`publish.json`（含 FTP 密码）、`bifaw_conf.json`（含必发账号密码）、
`bifaw_profile/`（浏览器登录态，含 cookie）、`logs/`、`dist/`、
`.publish-fingerprint`、`.publish.lock`、`__pycache__/`
——这些已经写进 `.gitignore`，用命令行传会自动跳过。

> ⚠️ 仓库是**公开**的，所以凭据一律不能进代码。`bifaw_fetch.py` 里的账号密码
> 只从环境变量 / `bifaw_conf.json` 读，代码本身不含任何明文凭据。

### 方式 A：命令行（推荐，一次搞定）

在项目目录里执行：

```bash
cd "C:\Users\ZPM\WorkBuddy\2026-09-13-13-33-03"
git init
git add .
git status          # 看一眼，确认列表里没有 publish.json
git commit -m "feat: 竞彩指数自动更新"
git branch -M main
git remote add origin https://github.com/你的用户名/football-index.git
git push -u origin main
```

`git push` 时会让你登录（浏览器弹窗授权，或粘贴 Personal Access Token）。

### 方式 B：网页上传（不想碰命令行）

GitHub 仓库页点 **Add file → Upload files**，把上面 8 个文件拖进去。
**注意 `.github/workflows/` 这两个文件也要一起拖**，保持目录结构：
直接把项目文件夹整个拖进浏览器，GitHub 会保留相对路径。
`.gitignore` 名字以点开头，Windows 拖拽可能看不见它——它只影响命令行方式，
网页上传时你只要不选 `publish.json` 就行。

---

## 第 3 步：配置 Secrets（密码放这里，不进仓库）

仓库页 → **Settings** → 左侧 **Secrets and variables** → **Actions**
→ 点 **New repository secret**，逐个添加：

| Name（必须一模一样） | Value | 是否必填 |
| --- | --- | --- |
| `FTP_HOST` | `43.225.44.236` | ✅ 必填 |
| `FTP_USER` | `qucom` | ✅ 必填 |
| `FTP_PASSWORD` | 你的 FTP 密码（`publish.json` 里那一串） | ✅ 必填 |
| `FTP_REMOTE_DIR` | `/domains/90qu.com/public_html/football` | ✅ 必填 |
| `BIFAW_USER` | `17780539736`（必发指数网账号） | ✅ 必填 |
| `BIFAW_PWD` | 必发指数网密码 | ✅ 必填 |
| `BAIDU_PUSH_SITE` | `https://90qu.com`（百度资源平台里验证的那个站点，格式要一模一样） | ✅ 必填（不推百度就不影响发布） |
| `BAIDU_PUSH_TOKEN` | 百度资源平台 →「普通收录 → API提交」里那串 token | ✅ 必填 |
| `FTP_PORT` | 可留空，默认就是 `21` | 选填 |
| `FTP_USE_TLS` | **不用加**，默认 `false`（本机 FTPS 实测失败） | 选填 |
| `FTP_MODE` | **不用加**，默认 `ftp`（22 端口关闭，不能用 sftp） | 选填 |

即：**只需要建上面那 6 个**，其余三个都别填，代码里的默认值就是对的。

补充说明：
- 密码里带 `@ # / 空格` 等特殊字符**不用转义**，原样粘贴即可——
  云端是用 Python 的 `json.dump` 写入的，不会出现引号冲突。
- Secret 保存后无法再查看，只能覆盖重填。填错了就再来一遍。
- `FTP_HOST` 也可以填域名（如 `ftp.90qu.com`），但前提是 DNS 解析到了这台机器；
  直接填 IP 最稳。以后若主机 IP 变更，改这一个 Secret 即可。
- 不想抓竞彩必发时，把 workflow 里的命令加上 `--skip-bifaw` 即可（见第 7 节）。

### 踩过的两个坑（用来排查「为什么 Actions 拿到的 Secret 是空」）

#### 坑 A：Windows Git Bash 里 `printf '%s' x | gh secret set --body -` 会把字符串截成 1 个字符

`printf '%s'` 不带换行符本应能完整传给 stdin，但 Git Bash 自带的管道在某些场景
下只把第一段塞过去。结果是 Secret 里只存了 1 个字符（一个数字 4、一段 `qucom` 里的 `q`、一个 `/`）。
Actions 跑的时候 `host` 是空、`user` 是 1 字符、所有凭据失效，workflow 全失败，
但 `build_publish_conf.py` 写文件那一步还认为已正常生成——因为它对**有 1 字符的非空值**不会报错。

**对策**：把值用**双引号字面量**直接传：
```bash
gh secret set FTP_HOST --body "43.225.44.236"   # ✅ 这才是对的
# 不要这样：
printf '%s' "43.225.44.236" | gh secret set FTP_HOST --body -   # ❌ Git Bash 下会被截成 1 字符
```

怎么自查：你刚设的 Secret 是不是完整的，看 `build_publish_conf.py` 在 build 步骤
输出的 `Secret <name> len=<N>` 行，对比你写的值的真实字节数即可。

#### 坑 B：沙箱代理常会拦截 `github.com` 但放过 `api.github.com`

如果你跑 `gh auth login --web` 报 `unexpected EOF` 或 `502 Tunnel connection failed`，
而 `curl https://api.github.com/rate_limit` 能通——说明环境里有透明代理做了白名单。
device flow 需要访问 `github.com/login/oauth`，正好被截了。

**对策**：让 `github.com` 直连，`api.github.com` 仍走代理（代理放行且更稳定）：

```bash
unset ALL_PROXY all_proxy
export NO_PROXY='github.com' no_proxy='github.com'
export HTTP_PROXY='http://127.0.0.1:55105'
export HTTPS_PROXY='http://127.0.0.1:55105'
# 国内直连 github.com 不稳，加重试：
for i in 1 2 3 4 5; do
  gh auth login --hostname github.com --git-protocol https --web --skip-ssh-key \
    && break || sleep 2
done
```

---

## 第 3.5 步：竞彩必发（bifaw）为什么特殊

页面唯一的数据源：`https://bifaw.com/bifaw/sporttery.php`（必发指数网·竞彩必发）。
它和一般的静态站完全不是一回事，有三个硬约束，都已在本项目里解决：

| 约束 | 现象 | 解决方式 |
| --- | --- | --- |
| **必须登录** | 未登录时页面只显示「你还没有登陆，请点击此处登陆!」 | 用 `BIFAW_USER` / `BIFAW_PWD` 登录，登录态存进 `bifaw_profile/` 复用 |
| **登录要过 4 位图形验证码** | 空验证码提交直接返回「验证码错误！」 | `ddddocr` 识别，失败自动换一张重试，**最多 8 轮**（2026-09-16 实测：第 1 轮 OCR 常有错，第 2 轮即中，所以重试是必需的） |
| **识别无头浏览器** | 纯 headless 下页面只报「undefined，请点击此处重新获取!」，一行数据都没有 | 必须有界面浏览器；CI 里用 `xvfb-run` 提供虚拟显示 |
| **登录成功后会自行跳转** | 页面上是「成功登录,正在跳转页面」。此时若立刻再打开主页，浏览器会中断导航并抛 `Page.goto: net::ERR_ABORTED`，整轮抓取崩掉 | 用 `goto_safe()`：失败后等跳转结束再重试，若已在目标页就直接复用。**本地因有持久登录态、跳过登录，复现不了，只有 CI 全新登录才踩** |
| **验证码识别错时页面不报错** | 只是把登录表单重刷一遍，文本里没有「验证码错误」等字眼，看一眼就以为是登录成功了，直到抓取阶段才发现整页写着「你还没有登陆」 | `_confirm_logged_in()` 提交后**回头核实**登录态（出现「你还没有登陆」或仍停在 `login.php` 即视为失败），没登进去就换图重试 |

第 3 点是**最容易踩的坑**：它不报错、不封你，只是「安静地不给数据」。
所以 workflow 里抓取那一步是这样写的：

```yaml
xvfb-run -a python publish.py --upload
```

`xvfb-run -a` 会自动挑一个空闲的虚拟显示号。这样 Chrome 以为自己在有界面的
环境里跑，源站的检测就过了。本地调试时不用管 xvfb，直接跑就会弹出一个真窗口。

### 数据是怎么拿到的

该站的数据接口参数做了加密（`bifawdataencrypt.min.js`），静态复刻接口成本极高，
所以走的是「**真实浏览器 + 登录 + 读渲染后的 DOM**」这条路：
`bifaw_fetch.py` 打开页面 → 等表格渲染稳定 → 用一段 JS 一次性把每场比赛的
「主 / 和 / 客 + 大小球」行抽成 JSON。表头 15 列：

```
项 | 买家挂牌 | 价位 | 卖家挂牌 | 成交量 | 比例 | 赔付率 | 必指 | 赔指 | 盈亏 | 欧初 | 欧终 | 凯指 | 凯差 | 热指
```

### 登录态能复用多久

`bifaw_profile/` 会被保留（CI 上不跨运行保留，所以云端每次都是新登录一次）。
因为验证码是自动过的，**每轮重新登录也没问题**；本项目实测本地连续多次运行
都是「已有有效登录态，跳过登录」。

### 抓不到会怎样

- bifaw 抓不到（登录失败 / 验证码连续失败 / 被识别成无头）→ 保留旧 `bf.json`
  继续上线，结果标 `WARN`，退出码 2（CI 出黄灯不出红灯），下一轮自动重试。
- 单源之后没有「互相兜底」可言：bifaw 是唯一的新数据来源。
  万一它长时间抓不到，线上会停在最后一份好数据 —— **页面上会出现红色
  「数据已过期」提示条**，把抓取时间和期号直接写出来，不会默默显示旧数据。

> 页面侧的数据新鲜度防线（2026-09-16 加）：抓取时间距今超过 3 小时标黄、
> 超过 12 小时标红；那个红色提示条就在搜索栏那一行的下方，滚动时始终可见。

---

## 第 4 步：手动跑一次，验证全链路

1. 仓库页顶部点 **Actions** 标签。
2. 首次进入会提示工作流需要启用，点 **I understand my workflows, go ahead and enable them**。
   左侧应能看到 `spdex-update` 和 `keepalive` 两个。
3. 点左侧 **spdex-update** → 右侧 **Run workflow** 下拉 → 绿色 **Run workflow** 按钮。
4. 等几秒刷新，会多出一条运行记录。点进去，展开 **抓取并上传** 步骤看实时日志。
5. 结束时看这次运行页面的 **Summary**：会有一张表格，其中「结果」一行写着
   `RESULT: OK - uploaded 4 file(s)`。
6. 浏览器打开你的网站，Ctrl+F5 强制刷新，确认数据是刚抓的。

看到 `RESULT: OK` 就算成功。之后每 20 分钟会自动跑，你什么都不用管。

---

## 第 5 步：确认定时已生效（已于 2026-09-16 完成部署 ✅）

**部署已完成**，仓库地址：

```
https://github.com/3217809798/football-index
```

9 个文件已推送、6 个 Secret 已配置、第一次运行已手动触发过。
之后只需要确认「每隔 20 分钟真的会有新的运行记录」：

回到 **Actions → spdex-update** 列表，过 15～30 分钟刷新一次，
应该会陆续出现由 `schedule` 触发的记录（触发者显示为机器人，不是你的头像）。

> 更直接的判据：**看线上 `bf.json` 里的 `fetchedAt` 是否一直在变新**。
> 配置、日志、文档都可能骗人，这个时间戳不会 —— 它没变就是没在更新。
> 另外页面上下拉选项里也标着每份数据的抓取时间，看一眼即知。

之前如果在本机建过 Windows 计划任务，现在可以**删掉**了：

```
schtasks /Delete /TN "Football-Index-AutoUpdate" /F
```

> **本项目实测结论（2026-09-13）**：扫描本机 181 个计划任务（按任务名 +
> 执行命令双重筛选），**本机从未注册过这个任务** —— 当初生成的 `setup-task.bat`
> 一直没有被双击执行过。这也正是「网站数据无法自动更新」的真正原因：
> 静态页面不会自己变，而定时抓取的任务压根没建起来。
>
> 所以这一步**通常什么都不用做**。如果确定自己建过，就手动删；不确定就按下面查：
> 打开「任务计划程序」→ 右侧「任务计划程序库」→ 按名称找，或看任务的
> 「操作」里是否指向 `publish.py` / `auto-update.bat`。
>
> 用于注册本地任务的三个脚本（`setup-task.bat`、`auto-update.bat`、`run-hidden.vbs`）
> 已随本次整理删除，避免日后误点造成「本机 + Actions 两边同时上传」。
> 想手动更新数据，随时可以跑 `python publish.py --upload`。

---

## 第 6 步：必须知道的 6 个坑

### ① 海外 IP 登录必发可能触发风控（最大风险）

原来这条讲的是 `c.spdex.com` 不认海外 IP —— 那个源已在 2026-09-16 下线
（源站自己改成了会员登录站），现在唯一要盯的是**竞彩必发**：
账号在 GitHub 海外 IP 上登录，有可能被源站风控。

- **2026-09-16 云端实测结论：登录本身能过。** 第 1 轮验证码 OCR 就命中
  （`第1轮 验证码 OCR='4571'`），站点返回「成功登录,正在跳转页面」——
  说明这个账号在海外 IP 上没有被风控。当时真正把整轮打挂的是**登录后的
  页面导航**（`Page.goto: net::ERR_ABORTED`），已修，见第 6 步之后新增的说明。
- 日志里看到 `bifaw | 登录失败：重试 8 轮仍未通过` 或 `bifaw | DOM 抽到 0 场`，
  才基本是这个原因。本地跑一次 `python bifaw_fetch.py` 对照：
  本机能过、云端不能过 → 确认是 IP 问题。
- 真发生时**线上不会变空白**，只是停在最后一份好数据，页面会亮红色过期提示条，
  而运行结果会标成 `WARN`（不是 OK，这也是 2026-09-16 修的）。
  要临时止血就在 workflow 的抓取命令里加 `--skip-bifaw`（那样就什么都不更新了，
  所以只适合短期）。
- 另一个需要观察的点：云端每次运行都是**全新登录**（没有持久 profile），
  等于每 20 分钟登录一次。若哪天开始频繁 OCR 失败/登录被拒，
  优先怀疑是频率触发了限流，可把 cron 降到每 30 分钟或每小时。
- **第 4 步手动跑一次，就是为了验证这条。**

### ② 定时不是准点，可能延迟几分钟

GitHub 的 `schedule` 是「尽力而为」：整点、半点前后任务最挤，
实际触发常比设定时间晚 **3～15 分钟**，偶尔更久。
把「每 20 分钟」理解成「每 20～35 分钟」更接近现实。
而且高峰时段 GitHub 还可能**直接跳过**某几轮（尤其整点那一波），
所以真实更新间隔偶尔会比 20 分钟更长，对指数数据完全够用。

### ③ 时区：cron 用 UTC，但这里不用换算

`*/20 * * * *` 落在每小时第 0/20/40 分。
北京时间是 UTC+8（480 分钟，正好是 20 的整数倍），
所以日历上同样是 0/20/40 分，无需换算。
（改频率时优先选**能整除 60** 的间隔：15/20/30，整点天然对齐；
若用 `*/25` 之类不能整除 60 的，跨小时会出现 0/25/50 → 00/25/50 → 跳点。）

### ④ 仓库 60 天没提交活动，定时任务会被自动停用

这是 GitHub 的硬规则，跟你的代码无关。已放进 `keepalive.yml`
（每周一自动提交一个心跳文件）来规避。
如果哪天你发现 Actions 里计划任务不再触发，去仓库 Actions 页面
点一下 **Enable workflow** 手动恢复即可。

### ⑤ 页面文件和数据文件都会被覆盖，所以改页面不用手动上传

主任务用的是 `--upload`（全量，传 `index.html` + `bf.json` + `.htaccess` +
`bf-inline.js`），所以：

- **改页面 → 直接改 GitHub 上的 `index.html` 并提交，20 分钟内自动上线**，
  不用再像以前那样手动跑上传。
- 想省流量、只传数据，把 workflow 里那一行改掉即可：

  ```yaml
  python publish.py --upload            # 现在：全量（4 个文件）
  python publish.py --upload --only-data # 改成：只传数据文件
  ```

  注意改成 `--only-data` 后，你每次修改页面都需要手动跑一次全量上传。

### ⑥ 必发指数网的登录，正常日志长这样

第 4 步手动跑时**重点看这几行**（下面是 2026-09-16 本地实测的原样输出，CI 同理）：

```
bifaw | 第1轮 验证码 OCR='d962'
bifaw | 第1轮：提交后仍是未登录状态（多半验证码识别错了），重试
bifaw | 第2轮 验证码 OCR='0345'
bifaw | 第2轮：登录通过（必发指数网bifaw，成功登录,正在跳转页面 ...）
bifaw | DOM 抽到 27 场比赛
bifaw | 数据校验：通过（27 场）
```

**第 1 轮的 OCR 出错是常态，不是故障** —— 验证码图很小，偶尔会把数字认成字母
（上面 `d962` 就是错的）。脚本会换一张重试，最多 8 轮。

- 若 8 轮全部「提交后仍是未登录状态」→ OCR 在这台机器上不灵，或站点对海外 IP
  加强了验证。本机跑一次对照即可区分（见 ①）。
- 登录成功但 `DOM 抽到 0 场比赛` → 多半是 xvfb 那步没生效：
  抓取命令必须是 `xvfb-run -a python publish.py --upload`。
  该站会识别无头浏览器，识别到就只回「undefined，请点击此处重新获取!」，
  不报错也不封号 —— 所以这一条不看日志很容易误判成「今天没比赛」。

---

## 7. 常用调整

**改成每小时一次（想用私有仓库时这样设）**

编辑 `.github/workflows/spdex-update.yml`，把那行 cron 改成：

```yaml
- cron: "0 * * * *"
```

**临时关掉自动更新**

Actions → spdex-update → 右上角 `...` → **Disable workflow**。
再开就点 **Enable workflow**。

**改 ftp / 密码**

Settings → Secrets and variables → Actions → 点 `FTP_PASSWORD` → Update secret。
不用改任何代码，下一次运行就生效。

**临时恢复超级指数（现已下线）**

超级指数的源站 2026-09-16 已改成会员登录站，抓也抓不到。真要试：

```yaml
python publish.py --upload --with-spdex      # 页面侧还要把 index.html 里
                                             # spdex 适配器的 enabled 改回 true
```

**本地手动更新一次**

```bash
python publish.py --upload        # 抓竞彩必发 + 打包 + 上传
python bifaw_fetch.py             # 只抓竞彩必发（会弹出一个 Chrome 窗口）
python publish.py --no-fetch      # 用现有数据重新打包，不上传
python spdex_server.py            # 本地预览（默认不自动抓，用页面上的「立即刷新」）
```

---

## 8. 出问题时怎么查

每次运行的 **Summary** 表格里已经写明结果和退出码，对照下表：

| 结果 / 提示 | 含义 | 怎么办 |
| --- | --- | --- |
| `RESULT: OK - uploaded N file(s)` | 抓取+上传都成功 | 正常，无需处理 |
| `RESULT: OK - data unchanged, nothing uploaded` | 数据没变，跳过上传（只在用 `--only-data` 时出现） | 正常 |
| `超级指数（spdex）已下线，本轮跳过该源` | 正常提示，不是错误 | 无需处理 |
| `RESULT: WARN - ... spdex ...` | 只在你用 `--with-spdex` 恢复了超级指数时才会出现；上传成功但那份是旧的 | 正常现象 |
| `缺少 Secret：FTP_HOST` | 某个 Secret 名字写错或没建 | 回第 3 步核对名字拼写 |
| `缺少必发账号：请设置环境变量 BIFAW_USER / BIFAW_PWD` | 必发的两个 Secret 没建 | 回第 3 步补 `BIFAW_USER` / `BIFAW_PWD` |
| 页面顶着红色「数据已过期」提示条 | 抓取或上传断了一段时间 | 看 Actions 最近几次运行是否失败；本机跑 `python publish.py --upload` 对照 |
| `bifaw \| 登录失败：重试 8 轮仍未通过` | 验证码 OCR 连续失败或海外 IP 风控 | 先本地跑 `python bifaw_fetch.py` 对照；必要时临时 `--skip-bifaw` |
| `bifaw \| DOM 抽到 0 场比赛` | 浏览器是无头的，源站识别并拒绝给数据 | 确认抓取命令前面有 `xvfb-run -a` |
| `bifaw \| 导航失败（第N/4 次）：Page.goto: net::ERR_ABORTED` | 登录成功后站点自行跳转，脚本紧接着又打开主页，导航被中断。**本地有持久登录态、跳过登录，所以本地永远复现不了** | 已修：`bifaw_fetch.py` 的 `goto_safe()` 会等跳转结束再重试并复用当前页；4 次全失败才需查网络 |
| 结果 `RESULT: WARN - ... bifaw fetch failed (...)` | 本轮没抓到新数据，已沿用上一份（线上数据没有更新） | 点进该次运行看日志定位原因；页面也会同时亮「数据已过期」提示条 |
| `FTP 连接/登录失败` | 密码错、IP 被主机商拉黑 | 本地跑 `python publish.py --check` 复核 |
| `进不去远程目录` | `FTP_REMOTE_DIR` 路径不对 | 本地 `--check` 会打印账号的起始目录，按它拼路径 |
| `RESULT: FAIL - local bf.json unusable` | 连本地旧数据都没有（首次部署时才会见） | 先本地跑一次 `python publish.py` 生成数据 |
| 任务变红（`::error::`） | 配置或上传出问题，需要你介入 | 点进失败的运行，展开日志看具体那行 |

本地排障说明也一并保留在 `TROUBLESHOOTING.txt` 里。

---

## 附：按天归档页（第三档 SEO，2026-09-19 上线）

**为什么要有它**

`/football/` 是「同一个 URL 每天换内容」。搜索引擎只认它**当前那一份**，
昨天、前天的场次连同联赛名、队名、日期一起被覆盖掉了 ——
「2026年9月19日 竞彩 必发指数」「上海申花 必发指数」这类长尾词一个都攒不下来。

归档页把每天的抓取结果固化成**独立 URL**，标题/正文里带日期、联赛与队名，
长尾词才有地方落脚，也才有内部链接把权重串起来。

**长什么样**

| URL | 内容 | 谁生成 |
| --- | --- | --- |
| `/football/2026-09-19/` | 那天全部场次的数据快照（自包含单页：内联 CSS、无脚本） | `archive.py` |
| `/football/archive/` | 全部归档日的索引，按年月分组，带场次数 | `archive.py` |
| `/football/`（主页底部） | 「历史数据归档」内链，列最近 30 天 | `archive.py` 注入 |
| `/sitemap-data.xml` | 主页 + `/jc/` + 归档索引 + **每一个归档日** | `archive.sitemap_xml()` |

**它是怎么跑起来的**

`publish.py` 每轮**真抓到数据**（`data_ok` 为真）时会多做几件事，全自动、不需要人工：

1. 生成 `/football/<数据日期>/index.html`（日期取自 `bf.json` 的 `fetchedAt`，即北京时间）；
2. 重新生成 `/football/archive/index.html` 索引页；
3. 刷新 `sitemap-data.xml`（**这一步很重要**：归档日一变、sitemap 不跟着变，百度就永远发现不了新归档页）；
4. 把日期写进 `.archive-index.json` —— ⚠️ **必须等上传成功之后才写**，
   否则一次上传失败就会在索引里留下一个线上并不存在的死链；
5. 上传时用 FTP 子目录机制：`/football/<日期>/index.html`、`/football/archive/index.html`，
   sitemap 用 `abs:` 规格送到站点根（比 `remote_dir` 高一层）。

**四条硬约定（改代码前先读）**

1. **只在 `data_ok` 为真时生成**。抓取失败时磁盘上的 `bf.json` 是「保留的上一份」，
   CI 上那份更是仓库里提交的旧快照 —— 拿它生成归档，等于把旧内容重复写成某一天的归档页，
   还会把过期日期塞进 sitemap 与归档索引。
2. **清单在上传成功之后才落盘**（见上面第 4 点）。
3. **归档日期取自数据的 `fetchedAt`，不是本机当天**。页面显示的是哪天的数据，归档就挂哪天。
4. **归档页内容在当天会持续更新**到 16:00 最后一次成功抓取为止（同一个 URL，内容刷新），
   时段结束后自然固定。所以页面文案写的是「当日截止数据」，不承诺「生成即冻结」。

**手工维护入口**

```
python publish.py --archive-only     # 只传归档页/索引/sitemap，绝不碰主页与数据文件
```

用途：① 首次上线或换主机后验证 FTP 能不能建子目录；
② 某天归档那一步上传失败，单独补一次 —— 不会碰到线上正在展示的当日数据。
⚠️ 它按磁盘上 `bf.json` 的 `fetchedAt` 决定归档日期，跑之前先确认那份数据是哪天的。
（它刻意排在 9:00-16:00 时段闸门**之前**，任何时间都能跑。）

**体检**

```
python build_seo_files.py --check    # 抓 robots / sitemap / 归档索引 / 最新一天归档页的线上状态
```

**重名/改名要注意的**

- 归档目录名就是日期（`2026-09-19`），索引页固定叫 `archive`，两者不会撞。
- 站点导航条在 `index.html` 与 `archive.py` 里**各有一份**（`NAV_HTML` / `NAV_CSS`）——
  改菜单项、logo、配色时两边都要改。
- 表格渲染只有 `archive.match_articles()` 一份，主页注入与归档页共用，改一处两边生效。

---

## 附：百度主动推送（让页面尽快被收录）

页面上线了不等于百度会来抓。「**普通收录 → API提交**」允许我们主动把 URL 推给百度，
通常几分钟内就开始抓，比等爬虫自己排期快得多。

**前提（一次性，已做完）**

1. 在 https://ziyuan.baidu.com/ 用百度账号登录；
2. 「站点管理 → 添加网站」填 `https://90qu.com`（协议必须与线上一致）；
3. 验证归属：选**文件验证**最简单 —— 把平台给的
   `baidu_verify_codeva-XXXXXXXX.html` 传到站点根目录，能通过
   `https://90qu.com/baidu_verify_codeva-XXXXXXXX.html` 打开即可。
   ⚠️ 验证通过后**别删这个文件**，百度会定期复查。
   仓库里有工具：`python build_seo_files.py --verify <文件名>`（自动生成内容 + FTP 上传 + 探测 200）。
4. 「普通收录 → API提交」抄下 token，填进 `baidu_push.json`（本机）和
   `BAIDU_PUSH_TOKEN` secret（CI）。

**日常怎么跑**

- 本机手动推：`python baidu_push.py`（推 sitemap 里的全部 URL）
- 只看配置对不对：`python baidu_push.py --check`
- CI 自动推：workflow 里的「**推送给百度**」步骤 —— **只在每天 16:30 的归档定稿轮**
  跑一次（`if: mode == 'finalize'`），执行 `python baidu_push.py --daily --max 1`：
  只推 1 条，也就是当天新生成的那个归档页 URL。

**为什么只在定稿轮推、而且只推 1 条**（2026-09-19 实测后改的口径）

- 配额**比文档小得多**。09-19 当天：凌晨只成功推过 2 条，上午 09:02 再推 4 条就直接
  `HTTP 400 {"error":400,"message":"over quota"}`。白天每 20 分钟推一轮，等于把配额
  在上午就打光。
- 主动推送最值钱的是**全新 URL**。一天里唯一的新 URL 就是 16:30 才生成的
  `/football/<今天>/` 归档页；`/football/` 与 `/jc/` 是常年不变的老 URL，
  靠 sitemap 的 `lastmod` 就会被重抓，不值得花配额。
- 于是：配额 ≈ 1 条/天，全部花在当天的新归档页上。**清单顺序 = 优先级**：
  1. **今天那天的归档页** `/football/<今天>/`（定稿轮才有）
  2. 主页 `/football/`、`/jc/`（万一那天没生成归档，退化成推主页）
  3. 归档索引 `/football/archive/`
  4. 其余历史归档页（日期倒序）—— 正常各自「当天」就推过了，`--daily` 台账会滤掉
- `--max` 默认就是 **1**；要临时多推几条用 `--max N`。
- 配额用尽（over quota）在脚本里算 **SKIP（退出码 0）**，不是 FAIL —— 那是接口的
  正常上限，不是配置错误。当天后续轮次会一直重试，配额恢复（通常次日）自动接上。

清单统一来自 `archive.sitemap_entries()`，和 sitemap 同源，不会出现两边不一致。

**五个必须知道的坑（都踩过）**

| 坑 | 现象 | 说明 |
| --- | --- | --- |
| `site` 参数被百分号编码 | `{"error":400,"message":"site init fail"}`，看着像「站点没验证」 | 必须**原样**拼 `site=https://90qu.com`，不能 `https%3A%2F%2F...` |
| 用了 https 接口 | `CERTIFICATE_VERIFY_FAILED: Hostname mismatch` | 只能用 `http://data.zz.baidu.com/urls` |
| 没做每日去重 | 20 分钟一轮 × 2 条 = 一天 40 次，上午就把配额烧干 | `--daily` + `.baidu-push-state` 台账，需要回写仓库才跨轮生效 |
| 一轮推太多条 | `{"error":400,"message":"over quota"}` | 配额是个位数/天，且可能被资源平台网页端的「提交」一起消耗；**每轮只推 1 条** |
| `site` 填了 `www.` | 返回 `not_same_site`，成功数为 0 但**照样扣配额** | 我们推的是裸域，`BAIDU_PUSH_SITE` 要和它一致 |

返回体里 `remain` 是**当天剩余配额**（数值很小，会随站点成长变多）。
`ok == 全部` 时脚本输出 `RESULT: OK - pushed N/N, remain=X`；
配额用尽时是 `RESULT: SKIP - daily push quota exhausted`。

**万一推送失败**（`RESULT: FAIL - push request failed`，比如 Runner 连不上百度接口）：
推送失败**不会**影响发布 —— workflow 里那一步一律 exit 0，只是百度那边收不到推送。
这时改成在本机跑：把 `python baidu_push.py --daily --max 1` 加进本机的计划任务即可，
代码不用改。

---

## 附：本地计划任务 vs GitHub Actions

| | 本地计划任务 | GitHub Actions |
| --- | --- | --- |
| 电脑必须开机联网 | 是 | 否 |
| 费用 | 电费 | 公开仓库免费 |
| 触发准时度 | 准 | 可能延迟 3～15 分钟 |
| 源站 IP | 国内家庭宽带（稳） | 海外机房（**需先验证**） |
| 改密码 | 改 `publish.json` | 改 Secrets |
| 看日志 | `logs/publish-*.log` | Actions 页面的 Summary |
