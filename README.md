# 界面新闻 · 快报每日简报

每天定时抓取 [界面新闻](https://www.jiemian.com/)「快报」模块下的三个子栏目——**今日热点 / 公司头条 / 时事追踪**，生成每日新闻简报，并通过**微信**推送给你。

- 抓取方式：直接解析界面服务端直出的快报分类页（稳定，不依赖易变的异步接口）
- 定时方式：[GitHub Actions](https://github.com/features/actions) 免费定时运行，无需自己买服务器
- 推送方式：支持 **pushplus / Server酱 / 企业微信** 三种微信推送，任选其一

## 目录结构

```
.
├── news_brief.py              # 主程序：抓取 + 解析 + 简报 + 推送
├── requirements.txt           # Python 依赖
├── .env.example               # 配置样例（复制为 .env 本地使用）
├── .github/workflows/daily.yml# GitHub Actions 定时任务
└── README.md
```

## 工作原理

1. 程序访问快报聚合页 `https://www.jiemian.com/lists/4.html`，动态解析出三个子栏目的直出页链接。
2. 分别抓取 `今日热点 / 公司头条 / 时事追踪` 三个页面。
3. 用 BeautifulSoup 解析每条快讯（标题、链接、摘要、时间），按**北京时间**过滤出「今日」快讯。
4. 生成 HTML / 纯文本两种简报，调用所选推送服务发到你的微信。

## 快速开始（本地）

```bash
pip install -r requirements.txt

# 复制配置样例并填入你的推送 token
cp .env.example .env
# 编辑 .env，设置 PUSH_METHOD 与对应 token

# 调试：只打印简报，不推送
DRY_RUN=1 python news_brief.py

# 正式运行（会真的推送到微信）
python news_brief.py
```

## 微信推送配置（三选一）

| 方式 | 环境变量 | 开通地址 | 说明 |
|------|----------|----------|------|
| **pushplus**（推荐） | `PUSHPLUS_TOKEN` | https://www.pushplus.plus/ | 扫码登录，复制「一对一推送」token，免费、稳定，推到微信服务号 |
| **Server酱** | `SERVERCHAN_KEY` | https://sct.ftqq.com/ | 微信扫码得 SENDKEY，推到微信（Turbo 版接口） |
| **企业微信** | `WECOM_KEY` | 企业微信建群→添加群机器人 | 推到企业微信群，可转发到个人微信，完全免费 |

设置 `PUSH_METHOD=pushplus|serverchan|wecom` 选择其一，并填好对应 token 即可。

## 部署到 GitHub（每日自动运行）

1. 把这个仓库推到你的 GitHub（公开或私有均可）。
2. 进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，添加：
   - `PUSH_METHOD`（如 `pushplus`）
   - 对应推送方式的 token（如 `PUSHPLUS_TOKEN`）
   - 可选：`MAX_ITEMS`、`TODAY_ONLY`、`TIMEZONE_OFFSET`
3. 进入仓库 **Actions** 标签，启用 `界面新闻每日快报` 工作流。
4. 它会按 `daily.yml` 里的 cron（默认**北京时间每天 08:00**）自动运行并推送到你微信。
   - 想立即看效果，可点 **Run workflow** 手动触发一次。

### 修改推送时间

编辑 `.github/workflows/daily.yml` 中的 `cron`。GitHub Actions 使用 **UTC** 时间，换算关系：

| 北京时间 | UTC cron |
|----------|----------|
| 每天 08:00 | `0 0 * * *` |
| 每天 09:00 | `0 1 * * *` |
| 每天 12:00 | `0 4 * * *` |
| 每天 20:00 | `0 12 * * *` |

> 注：官方 runner 在高峰期可能延迟数分钟到数十分钟触发，属正常现象。

## 环境变量一览

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PUSH_METHOD` | `pushplus` | 推送方式 |
| `PUSHPLUS_TOKEN` | 空 | pushplus token |
| `SERVERCHAN_KEY` | 空 | Server酱 sendkey |
| `WECOM_KEY` | 空 | 企业微信机器人 key |
| `MAX_ITEMS` | `15` | 每个栏目最多条数 |
| `TODAY_ONLY` | `1` | 是否只取今日快讯（0 则取最新 N 条） |
| `TIMEZONE_OFFSET` | `8` | 时区偏移（小时） |
| `DRY_RUN` | `0` | 设为 `1` 只打印不推送 |

## 常见问题

- **抓不到新闻？** 界面可能对频繁请求做限制，GitHub Actions 的 IP 段通常没问题；本地测试可加 `DRY_RUN=1` 看是否有报错。
- **想加更多栏目？** 编辑 `news_brief.py` 顶部的 `CATEGORY_URLS`（以及 `resolve_category_urls` 里的匹配名）即可。
- **推送失败？** 检查对应 token 是否正确、推送服务是否额度用尽（免费版一般有每日条数限制）。

## 对话模式（chat）：接入微信助手随问随答

除了定时推送，你也可以让对话助手（如 OpenClaw 部署的「悟空」）在微信里被问到「今日简报」时，
实时抓取并回复。该模式**只输出文本、不推送、也不写去重状态**，与定时链路完全隔离。

```bash
python news_brief.py --chat            # 精简版：标题 + 链接（自动按 ~1700 字/条分块）
python news_brief.py --chat --detail   # 详细版：标题 + 时间 + 摘要(截断180字) + 链接
python news_brief.py --chat --json      # JSON 输出 {"messages":[...]}，便于程序逐条发送
```

- 普通文本模式：多条消息之间以 `§§§SPLIT§§§` 分隔，调用方程序按此切分后逐条发送微信。
- JSON 模式：直接 `json.loads` 得到 `messages` 数组，遍历逐条发送，最便于自动化接线。
- 微信单条约 2000 中文字上限，脚本已自动分块，调用方无需再处理长度。

### 在 OpenClaw 等助手里接线（示意）

让助手识别到用户消息含「今日简报」时，执行：

```bash
python /path/to/news_brief.py --chat --json
```

读取 stdout 的 JSON `messages` 数组，逐条调用助手的「主动发送消息」能力发给用户即可。
想要带摘要的版本，把 `--json` 换成 `--detail --json`。
