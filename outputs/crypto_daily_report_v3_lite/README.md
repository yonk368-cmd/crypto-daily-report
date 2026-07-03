# Crypto Daily Report V3-lite

这是一个不接入 OpenAI API 的云端自动加密货币数据日报项目。

它会每天自动：

- 获取 BTC / ETH / BNB 价格
- 使用 OKX、Coinbase、CoinGecko 做多源校验
- 获取 ETH ETF flow 数据
- 生成中文 Markdown 数据日报
- 发送日报到 Gmail
- 把日报归档到 GitHub 仓库的 `reports/` 文件夹

## 为什么是 V3-lite

这个版本不调用 OpenAI API，不需要 `OPENAI_API_KEY`。

日报里会生成一个“复制给 ChatGPT 分析区”。你每天把这段内容复制给 ChatGPT，就可以继续得到完整中文分析。

## GitHub Secrets

在 GitHub 仓库中进入：

`Settings -> Secrets and variables -> Actions -> New repository secret`

至少添加：

- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`
- `REPORT_TO_EMAIL`

`GMAIL_APP_PASSWORD` 不是 Gmail 登录密码。它是 Google Account 里生成的 16 位 App Password，通常需要先启用 2-Step Verification。

## 可选 GitHub Variables

Farside 有时会拦截脚本访问。如果 ETH ETF 自动抓取失败，日报会继续生成，并把 ETF 状态标记为“待验证”。

如果你想手动覆盖 ETF 数据，可以进入：

`Settings -> Secrets and variables -> Actions -> Variables`

添加：

- `ETH_ETF_DATE`，例如 `01 Jul 2026`
- `ETH_ETF_TOTAL_USD_M`，例如 `148.5` 或 `(32.1)`

脚本会优先使用这两个变量，并把 ETF 状态标记为“手动确认”。

## 运行时间

工作流配置为：

```yaml
cron: "10 13 * * *"
```

也就是北京时间每天 21:10。

GitHub Actions 的定时任务按 UTC 执行，并且实际触发可能有几分钟延迟。

## 手动测试

上传到 GitHub 后：

1. 进入仓库的 `Actions`
2. 选择 `Crypto Daily Report`
3. 点击 `Run workflow`

成功后应该看到：

- Gmail 收到日报
- 仓库生成 `reports/YYYY-MM-DD.md`
- 仓库生成 `reports/YYYY-MM-DD.json`

## 本地测试

```bash
pip install -r requirements.txt
python crypto_daily_report.py
```

如果没有配置 Gmail 环境变量，脚本仍会生成本地报告，但不会发邮件。

## 数据有效性规则

| 最大偏差 | 状态 |
|---:|---|
| <= 0.3% | OK |
| 0.3% - 0.8% | WARNING |
| > 0.8% | INVALID |

出现 `INVALID` 时，日报会提醒不要基于该标的输出强趋势判断。
