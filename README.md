# 播客投资机会工具 (Podcast → Stock Ideas)

每周自动监控你订阅的 Podcast，抓取它们的文字稿，用 Claude 按你的规则挖掘潜在投资
机会，匹配到美股标的，并把结果整理成一封邮件发给你。

```
RSS 监控 ──► 获取文字稿 ──► Claude 按规则分析 ──► 匹配美股标的 ──► 每周邮件
```

## 它做什么

1. **监控**：根据 `config/podcasts.yaml` 里的清单，每周扫描各节目过去 7 天的更新。
   节目可以只写名字（自动用 iTunes 查 RSS）、贴苹果链接、或直接给 RSS 地址。
2. **转录（第一版）**：只使用播客 RSS 里自带的文字稿（Podcasting 2.0 的
   `<podcast:transcript>` 标签，支持 VTT / SRT / JSON / HTML / 纯文本）。
   没有自带文字稿的剧集会在邮件里单独列出「暂无文字稿」，方便以后再加自动转录。
3. **分析**：把文字稿连同 `config/rules.md` 里你的规则一起交给 Claude，产出结构化的
   投资机会（趋势、论点、标的、信心等级、风险、原文引用）。
4. **交付**：汇总成一封 HTML 邮件，通过 Gmail SMTP 发到你的邮箱；同时把报告存档到
   `data/reports/`。

## 你需要做的三件事

### 1. 填好你的清单和规则（内容）

- `config/podcasts.yaml` — 你要订阅的播客清单。想随时加节目，照格式加一条、提交即可。
- `config/rules.md` — 「分析大脑」的指令。按你的投资风格修改，写得越具体产出越贴合。

### 2. 配好密钥（GitHub Secrets）

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，
添加：

| Secret 名称           | 说明                                                                 |
| --------------------- | -------------------------------------------------------------------- |
| `ANTHROPIC_API_KEY`   | Anthropic API Key（用来分析文字稿）。在 console.anthropic.com 获取。 |
| `GMAIL_ADDRESS`       | 发件用的 Gmail 地址。                                                 |
| `GMAIL_APP_PASSWORD`  | Gmail **应用专用密码**（见下），不是你平时登录的密码。               |
| `EMAIL_TO`            | 收件地址（可选，默认发给 `GMAIL_ADDRESS`）。                         |
| `ANTHROPIC_MODEL`     | 可选，分析所用模型，默认 `claude-sonnet-4-6`。                       |

**怎么拿 Gmail 应用专用密码：**

1. 你的 Google 账号需要先开启「两步验证」。
2. 打开 <https://myaccount.google.com/apppasswords>。
3. 起个名字（如 `podcast-tool`），生成一个 16 位密码，把它填进 `GMAIL_APP_PASSWORD`。

### 3. 开启每周自动运行

定时任务已配置好（`.github/workflows/weekly.yml`），默认 **每周一 13:00 UTC** 运行。
推送到仓库后即生效。你也可以在 **Actions** 标签页里手动点 **Run workflow** 立即试跑。

> 想改时间：编辑 workflow 里的 `cron`。例如北京时间周一早 9 点 = UTC 周一 01:00 →
> `cron: "0 1 * * 1"`。

## 本地试运行（可选）

```bash
pip install -r requirements.txt
cp .env.example .env          # 填入你的 key / 邮箱 / 应用专用密码
python -m src.main
```

## 工作原理 & 目录结构

```
config/
  podcasts.yaml      # 播客订阅清单（你维护）
  rules.md           # 投资分析规则（你维护）
src/
  config.py          # 读取配置与环境变量
  feeds.py           # iTunes 查 RSS + 解析 RSS + 定位文字稿（纯标准库 XML）
  transcripts.py     # 把 VTT/SRT/JSON/HTML 文字稿规整成纯文本
  analyze.py         # 调用 Claude，按规则产出结构化机会
  emailer.py         # 生成 HTML 报告并通过 Gmail SMTP 发送
  state.py           # 记录已处理剧集，避免重复分析
  main.py            # 串起整个每周流程
.github/workflows/weekly.yml   # 每周定时任务
data/
  seen.json          # 已处理剧集状态（由 Action 自动回写提交）
  reports/           # 每周报告存档
```

**去重**：用「过去 7 天」时间窗 + `data/seen.json` 双重去重。GitHub Action 每次运行后会
把 `data/seen.json` 和报告自动 commit 回仓库，所以即使运行环境是一次性的，状态也能保留。

## 路线图（以后可加）

- **自动转录**：对没有自带文字稿的剧集，下载音频用本地 Whisper 或云端 API
  （Deepgram / AssemblyAI / OpenAI）转录后再分析。
- 标的去重与跨期跟踪、同一趋势在多期/多节目反复出现时加权。
- 接入行情/基本面数据做二次过滤。

## 免责声明

本工具产出的是研究线索，**不构成投资建议**。请自行做尽职调查。
