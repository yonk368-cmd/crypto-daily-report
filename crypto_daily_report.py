#!/usr/bin/env python3
"""Generate a crypto daily data report without using an AI API."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Any, Optional, Tuple

import requests
from bs4 import BeautifulSoup


ASSETS = {
    "BTC": {
        "okx": "BTC-USDT",
        "coinbase": "BTC-USD",
        "coingecko": "bitcoin",
    },
    "ETH": {
        "okx": "ETH-USDT",
        "coinbase": "ETH-USD",
        "coingecko": "ethereum",
    },
    "BNB": {
        "okx": "BNB-USDT",
        "coinbase": None,
        "coingecko": "binancecoin",
    },
}

OK_THRESHOLD = 0.003
WARNING_THRESHOLD = 0.008

FARSIDE_ETH_URL = "https://farside.co.uk/ethereum-etf-flow-all-data/"
REPORTS_DIR = Path("reports")


@dataclass
class SourcePrice:
    source: str
    price: Optional[float]
    change_24h: Optional[float] = None
    error: Optional[str] = None


@dataclass
class AssetCheck:
    symbol: str
    primary: SourcePrice
    checks: list[SourcePrice]
    max_deviation: Optional[float]
    status: str
    allow_strong_conclusion: bool


@dataclass
class EtfFlow:
    date: Optional[str]
    total_usd_m: Optional[float]
    status: str
    note: str
    source_url: str


def request_json(url: str, *, params: Optional[dict[str, Any]] = None) -> Any:
    headers = {
        "Accept": "application/json,text/html",
        "User-Agent": "crypto-daily-report/3-lite",
    }
    response = requests.get(url, params=params, headers=headers, timeout=25)
    response.raise_for_status()
    return response.json()


def fetch_okx(symbol: str) -> SourcePrice:
    try:
        data = request_json(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId": ASSETS[symbol]["okx"]},
        )
        ticker = data["data"][0]
        price = float(ticker["last"])
        open_24h = float(ticker["open24h"])
        change_24h = (price - open_24h) / open_24h if open_24h else None
        return SourcePrice("OKX", price, change_24h)
    except Exception as exc:  # noqa: BLE001
        return SourcePrice("OKX", None, error=str(exc))


def fetch_coinbase(symbol: str) -> SourcePrice:
    product = ASSETS[symbol]["coinbase"]
    if not product:
        return SourcePrice("Coinbase", None, error="not available for this asset")
    try:
        data = request_json(f"https://api.exchange.coinbase.com/products/{product}/ticker")
        return SourcePrice("Coinbase", float(data["price"]))
    except Exception as exc:  # noqa: BLE001
        return SourcePrice("Coinbase", None, error=str(exc))


def fetch_coingecko(symbols: list[str]) -> dict[str, SourcePrice]:
    ids = ",".join(ASSETS[symbol]["coingecko"] for symbol in symbols)
    out: dict[str, SourcePrice] = {}
    try:
        data = request_json(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ids, "vs_currencies": "usd", "include_24hr_change": "true"},
        )
        for symbol in symbols:
            gecko_id = ASSETS[symbol]["coingecko"]
            price = data.get(gecko_id, {}).get("usd")
            change = data.get(gecko_id, {}).get("usd_24h_change")
            out[symbol] = SourcePrice(
                "CoinGecko",
                float(price) if price is not None else None,
                float(change) / 100 if change is not None else None,
            )
    except Exception as exc:  # noqa: BLE001
        for symbol in symbols:
            out[symbol] = SourcePrice("CoinGecko", None, error=str(exc))
    return out


def classify_deviation(max_deviation: Optional[float]) -> Tuple[str, bool]:
    if max_deviation is None:
        return "INVALID", False
    if max_deviation <= OK_THRESHOLD:
        return "OK", True
    if max_deviation <= WARNING_THRESHOLD:
        return "WARNING", True
    return "INVALID", False


def percent(value: Optional[float]) -> str:
    if value is None or math.isnan(value):
        return "N/A"
    return f"{value * 100:.3f}%"


def money(value: Optional[float]) -> str:
    if value is None or math.isnan(value):
        return "N/A"
    return f"${value:,.2f}"


def flow_money(value: Optional[float]) -> str:
    if value is None or math.isnan(value):
        return "N/A"
    direction = "净流入" if value > 0 else "净流出" if value < 0 else "持平"
    return f"{direction} {abs(value):,.1f} US$m"


def signed_percent(value: Optional[float]) -> str:
    if value is None or math.isnan(value):
        return "N/A"
    sign = "+" if value > 0 else ""
    return f"{sign}{value * 100:.3f}%"


def status_badge_style(status: str) -> str:
    if status == "OK":
        return "background:#dcfce7;color:#166534;border-color:#86efac;"
    if status == "WARNING":
        return "background:#fef3c7;color:#92400e;border-color:#fcd34d;"
    if status == "INVALID":
        return "background:#fee2e2;color:#991b1b;border-color:#fca5a5;"
    if status in {"已确认", "手动确认"}:
        return "background:#dbeafe;color:#1e40af;border-color:#93c5fd;"
    return "background:#f3f4f6;color:#374151;border-color:#d1d5db;"


def change_color(value: Optional[float]) -> str:
    if value is None or math.isnan(value):
        return "#6b7280"
    if value > 0:
        return "#047857"
    if value < 0:
        return "#b91c1c"
    return "#374151"


def build_asset_checks() -> list[AssetCheck]:
    symbols = list(ASSETS)
    gecko_prices = fetch_coingecko(symbols)
    checks: list[AssetCheck] = []

    for symbol in symbols:
        primary = fetch_okx(symbol)
        secondary: list[SourcePrice] = []
        coinbase_price = fetch_coinbase(symbol)
        if coinbase_price.price is not None:
            secondary.append(coinbase_price)
        gecko_price = gecko_prices[symbol]
        if gecko_price.price is not None:
            secondary.append(gecko_price)

        deviations: list[float] = []
        if primary.price is not None:
            for item in secondary:
                if item.price is not None:
                    deviations.append(abs(primary.price - item.price) / item.price)

        max_deviation = max(deviations) if deviations else None
        status, allow = classify_deviation(max_deviation)
        checks.append(
            AssetCheck(
                symbol=symbol,
                primary=primary,
                checks=secondary,
                max_deviation=max_deviation,
                status=status,
                allow_strong_conclusion=allow,
            )
        )
    return checks


def parse_number(text: str) -> Optional[float]:
    cleaned = text.strip().replace(",", "")
    if not cleaned or cleaned == "-":
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def fetch_eth_etf_flow() -> EtfFlow:
    manual_date = os.getenv("ETH_ETF_DATE")
    manual_total = os.getenv("ETH_ETF_TOTAL_USD_M")
    if manual_date and manual_total:
        parsed_total = parse_number(manual_total)
        if parsed_total is not None:
            return EtfFlow(
                date=manual_date,
                total_usd_m=parsed_total,
                status="手动确认",
                note="使用环境变量 ETH_ETF_DATE 和 ETH_ETF_TOTAL_USD_M 覆盖自动抓取结果。",
                source_url=FARSIDE_ETH_URL,
            )

    try:
        response = requests.get(
            FARSIDE_ETH_URL,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://farside.co.uk/",
            },
            timeout=30,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text("\n")
        lines = [line.strip() for line in text.splitlines() if line.strip()]

        date_pattern = re.compile(r"^\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}$")
        rows: list[tuple[str, float]] = []
        for index, line in enumerate(lines):
            if not date_pattern.match(line):
                continue
            values: list[Optional[float]] = []
            cursor = index + 1
            while cursor < len(lines) and len(values) < 11:
                maybe_value = parse_number(lines[cursor])
                if maybe_value is not None or lines[cursor] == "-":
                    values.append(maybe_value)
                cursor += 1
            total = values[10] if len(values) >= 11 else None
            if total is not None:
                rows.append((line, total))

        if not rows:
            return EtfFlow(
                date=None,
                total_usd_m=None,
                status="待验证",
                note="未能从 Farside 页面解析出最新 ETH ETF Total 数据。",
                source_url=FARSIDE_ETH_URL,
            )

        latest_date, latest_total = rows[-1]
        return EtfFlow(
            date=latest_date,
            total_usd_m=latest_total,
            status="已确认",
            note="北京时间 21:00 附近，美国 ETF 当日最终数据可能尚未完全更新；如日期不是最新交易日，请按待验证处理。",
            source_url=FARSIDE_ETH_URL,
        )
    except Exception as exc:  # noqa: BLE001
        return EtfFlow(
            date=None,
            total_usd_m=None,
            status="待验证",
            note=f"ETH ETF 数据抓取失败：{exc}",
            source_url=FARSIDE_ETH_URL,
        )


def strongest_and_weakest(checks: list[AssetCheck]) -> tuple[str, str]:
    valid = [
        item
        for item in checks
        if item.primary.change_24h is not None and item.allow_strong_conclusion
    ]
    if not valid:
        return "待验证", "待验证"
    ordered = sorted(valid, key=lambda item: item.primary.change_24h or 0, reverse=True)
    return ordered[0].symbol, ordered[-1].symbol


def build_report(checks: list[AssetCheck], etf: EtfFlow) -> str:
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_bj = now_utc.astimezone(dt.timezone(dt.timedelta(hours=8)))
    report_date = now_bj.strftime("%Y-%m-%d")
    strong, weak = strongest_and_weakest(checks)
    all_valid = all(item.allow_strong_conclusion for item in checks)

    lines: list[str] = [
        f"# 每日虚拟货币数据复盘 - {report_date}",
        "",
        f"生成时间：{now_bj.strftime('%Y-%m-%d %H:%M')} 北京时间",
        "",
        "## 1. 数据有效性结论",
        "",
        "| 标的 | 主源价格 | 24h涨跌 | 校验源 | 最大偏差 | 状态 | 是否允许强行情结论 |",
        "|---|---:|---:|---|---:|---|---|",
    ]

    for item in checks:
        check_text = " / ".join(f"{p.source} {money(p.price)}" for p in item.checks) or "N/A"
        lines.append(
            "| "
            + " | ".join(
                [
                    item.symbol,
                    money(item.primary.price),
                    percent(item.primary.change_24h),
                    check_text,
                    percent(item.max_deviation),
                    item.status,
                    "是" if item.allow_strong_conclusion else "否",
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            f"整体结论：{'价格数据可用于趋势判断' if all_valid else '存在异常或缺失，部分强趋势判断需要保守处理'}。",
            "",
            "## 2. BTC / ETH / BNB 当日表现底稿",
            "",
            f"- 24h 表现最强：{strong}",
            f"- 24h 表现最弱：{weak}",
            "- 强弱判断：本版本按 OKX 24h 涨跌幅给出初步排序；如任一标的价格校验异常，则对应强弱结论应保守处理。",
            "- 主导结构：待结合 24h 涨跌幅、成交量、BTC.D 或其他风险偏好指标判断。",
            "",
            "## 3. ETH ETF 流入流出",
            "",
            f"- 最新状态：{etf.status}",
            f"- 数据日期：{etf.date or 'N/A'}",
            f"- ETH ETF Total：{flow_money(etf.total_usd_m)}",
            "- 单位：US$m",
            f"- 来源：{etf.source_url}",
            f"- 备注：{etf.note}",
            "",
            "## 4. 异动原因分析底稿",
            "",
            "- 价格驱动：待结合 24h 涨跌幅、关键价位、成交量确认。",
            "- ETF 资金流：如 ETH ETF 为净流入，通常偏支撑；如净流出，通常偏拖累；如数据日期滞后，应标记为待验证。",
            "- 风险偏好：待结合美股、美元指数、利率预期和加密市场总市值判断。",
            "- ETH 与 BTC 强弱关系：需结合 ETH/BTC 或两者 24h 表现判断。",
            "- 数据不足：本日报不自动编造无法验证的原因。",
            "",
            "## 5. 今日结论",
            "",
            "本报告为自动数据底稿，不接入 OpenAI API。最终分析建议复制下方内容给 ChatGPT。",
            "",
            "## 6. 明日观察点",
            "",
            "- BTC 是否继续保持市场方向主导。",
            "- ETH 是否获得 ETF 净流入支撑。",
            "- BNB 是否只是跟随大盘，还是出现独立强势。",
            "- 任一标的若出现 INVALID，应优先排查数据源而不是直接下行情结论。",
            "",
            "## 7. 复制给 ChatGPT 分析区",
            "",
            "请基于以下数据，给出中文加密货币日报分析：",
            "",
            "```text",
            f"日期：{report_date}",
            "价格数据：",
        ]
    )

    for item in checks:
        checks_text = "; ".join(f"{p.source}={money(p.price)}" for p in item.checks) or "N/A"
        lines.append(
            f"- {item.symbol}: 主源 OKX={money(item.primary.price)}, 校验源 {checks_text}, "
            f"24h涨跌={percent(item.primary.change_24h)}, 最大偏差={percent(item.max_deviation)}, 状态={item.status}, "
            f"允许强结论={'是' if item.allow_strong_conclusion else '否'}"
        )

    lines.extend(
        [
            "",
            "ETH ETF：",
            f"- 状态：{etf.status}",
            f"- 日期：{etf.date or 'N/A'}",
            f"- Total：{flow_money(etf.total_usd_m)}",
            f"- 备注：{etf.note}",
            "",
            "请输出：",
            "1. 数据是否可靠",
            "2. BTC / ETH / BNB 强弱关系",
            "3. ETH ETF 对 ETH 的支撑或拖累",
            "4. 今日市场主线",
            "5. 明日观察点",
            "6. 哪些结论必须标记为待验证",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def build_html_report(checks: list[AssetCheck], etf: EtfFlow) -> str:
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_bj = now_utc.astimezone(dt.timezone(dt.timedelta(hours=8)))
    report_date = now_bj.strftime("%Y-%m-%d")
    generated_at = now_bj.strftime("%Y-%m-%d %H:%M")
    strong, weak = strongest_and_weakest(checks)
    all_valid = all(item.allow_strong_conclusion for item in checks)
    market_status = "价格数据可用于趋势判断" if all_valid else "部分价格数据需保守处理"
    copy_lines = [
        f"日期：{report_date}",
        "价格数据：",
    ]
    for item in checks:
        checks_text = "; ".join(f"{p.source}={money(p.price)}" for p in item.checks) or "N/A"
        copy_lines.append(
            f"- {item.symbol}: 主源 OKX={money(item.primary.price)}, 校验源 {checks_text}, "
            f"24h涨跌={signed_percent(item.primary.change_24h)}, 最大偏差={percent(item.max_deviation)}, "
            f"状态={item.status}, 允许强结论={'是' if item.allow_strong_conclusion else '否'}"
        )
    copy_lines.extend(
        [
            "",
            "ETH ETF：",
            f"- 状态：{etf.status}",
            f"- 日期：{etf.date or 'N/A'}",
            f"- Total：{flow_money(etf.total_usd_m)}",
            f"- 备注：{etf.note}",
            "",
            "请输出：",
            "1. 数据是否可靠",
            "2. BTC / ETH / BNB 强弱关系",
            "3. ETH ETF 对 ETH 的支撑或拖累",
            "4. 今日市场主线",
            "5. 明日观察点",
            "6. 哪些结论必须标记为待验证",
        ]
    )

    rows = []
    for item in checks:
        checks_text = "<br>".join(
            escape(f"{p.source} {money(p.price)}") for p in item.checks
        ) or "N/A"
        rows.append(
            f"""
            <tr>
              <td><strong>{escape(item.symbol)}</strong></td>
              <td class="number">{escape(money(item.primary.price))}</td>
              <td class="number" style="color:{change_color(item.primary.change_24h)};font-weight:700;">{escape(signed_percent(item.primary.change_24h))}</td>
              <td>{checks_text}</td>
              <td class="number">{escape(percent(item.max_deviation))}</td>
              <td><span class="badge" style="{status_badge_style(item.status)}">{escape(item.status)}</span></td>
              <td>{'是' if item.allow_strong_conclusion else '否'}</td>
            </tr>
            """
        )

    etf_style = status_badge_style(etf.status)
    etf_value_color = "#047857" if (etf.total_usd_m or 0) > 0 else "#b91c1c" if (etf.total_usd_m or 0) < 0 else "#374151"
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{
      margin:0;
      padding:0;
      background:#f6f7f9;
      color:#111827;
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,"PingFang SC","Microsoft YaHei",sans-serif;
      line-height:1.55;
    }}
    .wrap {{
      max-width:880px;
      margin:0 auto;
      padding:22px 14px 34px;
    }}
    .header {{
      background:#111827;
      color:#fff;
      border-radius:8px;
      padding:22px;
    }}
    .header h1 {{
      margin:0 0 8px;
      font-size:24px;
      line-height:1.25;
      letter-spacing:0;
    }}
    .muted {{
      color:#6b7280;
      font-size:13px;
    }}
    .header .muted {{
      color:#d1d5db;
    }}
    .summary {{
      display:grid;
      grid-template-columns:repeat(4,1fr);
      gap:10px;
      margin:14px 0;
    }}
    .metric {{
      background:#fff;
      border:1px solid #e5e7eb;
      border-radius:8px;
      padding:14px;
    }}
    .metric-label {{
      color:#6b7280;
      font-size:12px;
      margin-bottom:6px;
    }}
    .metric-value {{
      font-size:19px;
      font-weight:800;
    }}
    .section {{
      background:#fff;
      border:1px solid #e5e7eb;
      border-radius:8px;
      margin-top:14px;
      overflow:hidden;
    }}
    .section h2 {{
      margin:0;
      padding:15px 16px;
      font-size:17px;
      border-bottom:1px solid #e5e7eb;
      background:#fafafa;
    }}
    .body {{
      padding:16px;
    }}
    .table-wrap {{
      overflow-x:auto;
    }}
    table {{
      width:100%;
      border-collapse:collapse;
      font-size:14px;
      min-width:760px;
    }}
    th {{
      background:#f3f4f6;
      color:#374151;
      text-align:left;
      font-weight:700;
      padding:10px;
      border-bottom:1px solid #d1d5db;
      white-space:nowrap;
    }}
    td {{
      padding:11px 10px;
      border-bottom:1px solid #e5e7eb;
      vertical-align:top;
    }}
    .number {{
      text-align:right;
      white-space:nowrap;
      font-variant-numeric:tabular-nums;
    }}
    .badge {{
      display:inline-block;
      border:1px solid;
      border-radius:999px;
      padding:2px 9px;
      font-size:12px;
      font-weight:700;
      white-space:nowrap;
    }}
    .note {{
      background:#f9fafb;
      border-left:4px solid #9ca3af;
      padding:12px 14px;
      margin:0;
      color:#374151;
    }}
    .copy {{
      white-space:pre-wrap;
      word-break:break-word;
      background:#111827;
      color:#f9fafb;
      border-radius:8px;
      padding:14px;
      font-size:13px;
      line-height:1.65;
      overflow-x:auto;
    }}
    ul {{
      margin:0;
      padding-left:20px;
    }}
    li {{
      margin:5px 0;
    }}
    a {{
      color:#2563eb;
    }}
    @media (max-width:720px) {{
      .summary {{
        display:block;
      }}
      .metric {{
        margin-bottom:10px;
      }}
      .header h1 {{
        font-size:21px;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <h1>每日虚拟货币数据复盘</h1>
      <div class="muted">{escape(generated_at)} 北京时间 · V3-lite 数据底稿</div>
    </div>

    <div class="summary">
      <div class="metric">
        <div class="metric-label">整体状态</div>
        <div class="metric-value">{escape('可判断' if all_valid else '需谨慎')}</div>
      </div>
      <div class="metric">
        <div class="metric-label">24h 最强</div>
        <div class="metric-value">{escape(strong)}</div>
      </div>
      <div class="metric">
        <div class="metric-label">24h 最弱</div>
        <div class="metric-value">{escape(weak)}</div>
      </div>
      <div class="metric">
        <div class="metric-label">ETH ETF</div>
        <div class="metric-value"><span class="badge" style="{etf_style}">{escape(etf.status)}</span></div>
      </div>
    </div>

    <div class="section">
      <h2>1. 数据有效性与行情表</h2>
      <div class="body">
        <p class="note"><strong>整体结论：</strong>{escape(market_status)}。偏差小于等于 0.3% 为 OK，0.3%-0.8% 为 WARNING，大于 0.8% 为 INVALID。</p>
        <div class="table-wrap" style="margin-top:14px;">
          <table>
            <thead>
              <tr>
                <th>标的</th>
                <th class="number">OKX 主源价格</th>
                <th class="number">24h 涨跌</th>
                <th>校验源</th>
                <th class="number">最大偏差</th>
                <th>状态</th>
                <th>强结论</th>
              </tr>
            </thead>
            <tbody>
              {''.join(rows)}
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="section">
      <h2>2. 今日重点</h2>
      <div class="body">
        <ul>
          <li><strong>24h 表现最强：</strong>{escape(strong)}</li>
          <li><strong>24h 表现最弱：</strong>{escape(weak)}</li>
          <li><strong>强弱判断：</strong>按 OKX 24h 涨跌幅初步排序；若状态不是 OK，应保守解读。</li>
          <li><strong>主导结构：</strong>仍需结合成交量、BTC.D、ETH/BTC 和宏观风险偏好确认。</li>
        </ul>
      </div>
    </div>

    <div class="section">
      <h2>3. ETH ETF 流入流出</h2>
      <div class="body">
        <table style="min-width:0;">
          <tbody>
            <tr><th>状态</th><td><span class="badge" style="{etf_style}">{escape(etf.status)}</span></td></tr>
            <tr><th>数据日期</th><td>{escape(etf.date or 'N/A')}</td></tr>
            <tr><th>Total</th><td style="color:{etf_value_color};font-weight:800;">{escape(flow_money(etf.total_usd_m))}</td></tr>
            <tr><th>单位</th><td>US$m</td></tr>
            <tr><th>来源</th><td><a href="{escape(etf.source_url)}">{escape(etf.source_url)}</a></td></tr>
          </tbody>
        </table>
        <p class="note" style="margin-top:14px;"><strong>备注：</strong>{escape(etf.note)}</p>
      </div>
    </div>

    <div class="section">
      <h2>4. 待验证与观察点</h2>
      <div class="body">
        <ul>
          <li>若任一标的为 INVALID，优先排查数据源，不直接下强趋势结论。</li>
          <li>ETH ETF 若为净流入通常偏支撑，净流出通常偏拖累；数据日期滞后时必须标记待验证。</li>
          <li>明日重点观察 BTC 是否继续主导方向、ETH 是否获得 ETF 支撑、BNB 是否独立强势。</li>
        </ul>
      </div>
    </div>

    <div class="section">
      <h2>5. 复制给 ChatGPT 分析区</h2>
      <div class="body">
        <div class="copy">{escape(chr(10).join(copy_lines))}</div>
      </div>
    </div>
  </div>
</body>
</html>"""


def send_email(subject: str, body: str, html_body: Optional[str] = None) -> None:
    gmail_user = os.getenv("GMAIL_USER")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    report_to = os.getenv("REPORT_TO_EMAIL")

    if not gmail_user or not gmail_password or not report_to:
        print("Gmail secrets are not fully configured. Skip email sending.")
        return

    message = EmailMessage()
    message["From"] = gmail_user
    message["To"] = report_to
    message["Subject"] = subject
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(gmail_user, gmail_password)
        smtp.send_message(message)


def write_debug_json(checks: list[AssetCheck], etf: EtfFlow, path: Path) -> None:
    payload = {
        "assets": [
            {
                "symbol": item.symbol,
                "primary": item.primary.__dict__,
                "checks": [check.__dict__ for check in item.checks],
                "max_deviation": item.max_deviation,
                "status": item.status,
                "allow_strong_conclusion": item.allow_strong_conclusion,
            }
            for item in checks
        ],
        "eth_etf": etf.__dict__,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    REPORTS_DIR.mkdir(exist_ok=True)
    now_bj = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8)))
    date_slug = now_bj.strftime("%Y-%m-%d")

    checks = build_asset_checks()
    etf = fetch_eth_etf_flow()
    report = build_report(checks, etf)
    html_report = build_html_report(checks, etf)

    report_path = REPORTS_DIR / f"{date_slug}.md"
    html_path = REPORTS_DIR / f"{date_slug}.html"
    debug_path = REPORTS_DIR / f"{date_slug}.json"
    report_path.write_text(report, encoding="utf-8")
    html_path.write_text(html_report, encoding="utf-8")
    write_debug_json(checks, etf, debug_path)

    subject = f"每日虚拟货币数据复盘 {date_slug}"
    send_email(subject, report, html_report)
    print(f"Report written to {report_path}")
    print(f"HTML report written to {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
