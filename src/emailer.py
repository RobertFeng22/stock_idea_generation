"""Build the weekly HTML report and send it via Gmail SMTP."""
from __future__ import annotations

import html
import smtplib
from dataclasses import dataclass, field
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .analyze import Opportunity


@dataclass
class EpisodeResult:
    podcast: str
    title: str
    link: str | None
    published: str
    status: str  # "analyzed" | "no_transcript" | "error"
    opportunities: list[Opportunity] = field(default_factory=list)
    note: str = ""
    transcript_source: str = ""  # "feed" | "deepgram"


def _esc(s: str) -> str:
    return html.escape(s or "")


def _conf_color(conf: str) -> str:
    return {"high": "#1a7f37", "medium": "#9a6700", "low": "#6e7781"}.get(conf, "#6e7781")


def build_html(results: list[EpisodeResult]) -> str:
    today = date.today().strftime("%Y-%m-%d")
    analyzed = [r for r in results if r.status == "analyzed"]
    no_transcript = [r for r in results if r.status == "no_transcript"]
    errored = [r for r in results if r.status == "error"]
    total_opps = sum(len(r.opportunities) for r in analyzed)

    # Aggregate tickers across all opportunities for a quick-glance table.
    ticker_rows: dict[str, dict] = {}
    for r in analyzed:
        for opp in r.opportunities:
            for t in opp.tickers:
                sym = str(t.get("ticker", "")).strip().upper()
                if not sym:
                    continue
                row = ticker_rows.setdefault(
                    sym, {"name": t.get("name", ""), "trends": set(), "shows": set()}
                )
                row["trends"].add(opp.trend)
                row["shows"].add(r.podcast)

    parts: list[str] = []
    parts.append(
        f"""<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
        max-width:760px;margin:0 auto;color:#1f2328;line-height:1.5;">
        <h1 style="font-size:22px;margin:0 0 4px;">📈 每周播客投资机会摘要</h1>
        <div style="color:#6e7781;font-size:13px;margin-bottom:16px;">{today} ·
        分析 {len(analyzed)} 期节目 · 发现 {total_opps} 条机会</div>"""
    )

    if ticker_rows:
        parts.append('<h2 style="font-size:16px;border-bottom:1px solid #d0d7de;padding-bottom:6px;">标的速览</h2>')
        parts.append('<table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:20px;">')
        parts.append(
            '<tr style="text-align:left;color:#6e7781;">'
            '<th style="padding:6px 8px;">代码</th><th style="padding:6px 8px;">公司</th>'
            '<th style="padding:6px 8px;">相关趋势</th></tr>'
        )
        for sym in sorted(ticker_rows):
            row = ticker_rows[sym]
            trends = "; ".join(sorted(t for t in row["trends"] if t))
            parts.append(
                f'<tr style="border-top:1px solid #eaeef2;">'
                f'<td style="padding:6px 8px;font-weight:600;">{_esc(sym)}</td>'
                f'<td style="padding:6px 8px;">{_esc(row["name"])}</td>'
                f'<td style="padding:6px 8px;color:#57606a;">{_esc(trends)}</td></tr>'
            )
        parts.append("</table>")

    if not analyzed:
        parts.append('<p style="color:#6e7781;">本周没有可分析的（带文字稿的）新剧集。</p>')

    for r in analyzed:
        title_html = _esc(r.title)
        if r.link:
            title_html = f'<a href="{_esc(r.link)}" style="color:#0969da;text-decoration:none;">{title_html}</a>'
        src_badge = ""
        if r.transcript_source == "deepgram":
            src_badge = ('<span style="font-size:10px;color:#57606a;background:#eaeef2;'
                         'border-radius:8px;padding:1px 6px;margin-left:6px;">转录自音频</span>')
        parts.append(
            f'<h2 style="font-size:16px;border-bottom:1px solid #d0d7de;padding-bottom:6px;margin-top:24px;">'
            f'🎙️ {_esc(r.podcast)}</h2>'
            f'<div style="font-size:14px;margin-bottom:10px;">{title_html} '
            f'<span style="color:#6e7781;font-size:12px;">· {_esc(r.published)}</span>{src_badge}</div>'
        )
        if not r.opportunities:
            parts.append('<p style="color:#6e7781;font-size:13px;">（本期未发现符合规则的机会）</p>')
        for opp in r.opportunities:
            tickers = ", ".join(
                f'<b>{_esc(str(t.get("ticker","")))}</b>' for t in opp.tickers if t.get("ticker")
            ) or "—"
            color = _conf_color(opp.confidence)
            parts.append(
                f'<div style="border:1px solid #d0d7de;border-radius:8px;padding:12px 14px;margin-bottom:12px;">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;">'
                f'<span style="font-weight:600;font-size:14px;">{_esc(opp.trend)}</span>'
                f'<span style="font-size:11px;color:#fff;background:{color};border-radius:10px;padding:2px 8px;">'
                f'{_esc(opp.confidence or "n/a")}</span></div>'
                f'<div style="font-size:13px;margin:8px 0;">{_esc(opp.thesis)}</div>'
                f'<div style="font-size:13px;"><b>标的：</b>{tickers}</div>'
            )
            for t in opp.tickers:
                if t.get("rationale"):
                    parts.append(
                        f'<div style="font-size:12px;color:#57606a;margin-left:8px;">· '
                        f'{_esc(str(t.get("ticker","")))}: {_esc(t.get("rationale",""))}</div>'
                    )
            if opp.risk:
                parts.append(f'<div style="font-size:12px;color:#9a6700;margin-top:6px;"><b>风险：</b>{_esc(opp.risk)}</div>')
            if opp.evidence_quote:
                parts.append(
                    f'<div style="font-size:12px;color:#57606a;border-left:3px solid #d0d7de;'
                    f'padding-left:8px;margin-top:6px;font-style:italic;">“{_esc(opp.evidence_quote)}”</div>'
                )
            parts.append("</div>")

    if no_transcript:
        parts.append(
            '<h2 style="font-size:15px;color:#6e7781;border-bottom:1px solid #d0d7de;padding-bottom:6px;margin-top:24px;">'
            '⏭️ 本周更新、但暂无文字稿（未分析）</h2><ul style="font-size:13px;color:#57606a;">'
        )
        for r in no_transcript:
            link = f' — <a href="{_esc(r.link)}" style="color:#0969da;">链接</a>' if r.link else ""
            reason = f' <span style="color:#8c959f;">({_esc(r.note)})</span>' if r.note else ""
            parts.append(f'<li>{_esc(r.podcast)}：{_esc(r.title)} ({_esc(r.published)}){reason}{link}</li>')
        parts.append("</ul>")

    if errored:
        parts.append('<h2 style="font-size:15px;color:#cf222e;margin-top:24px;">⚠️ 处理出错</h2><ul style="font-size:13px;">')
        for r in errored:
            parts.append(f'<li>{_esc(r.podcast)}：{_esc(r.title)} — {_esc(r.note)}</li>')
        parts.append("</ul>")

    parts.append(
        '<div style="color:#8c959f;font-size:11px;margin-top:28px;border-top:1px solid #eaeef2;padding-top:10px;">'
        '本邮件由播客投资机会工具自动生成，仅为研究线索，不构成投资建议。</div></div>'
    )
    return "\n".join(parts)


def send_email(
    *,
    gmail_address: str,
    app_password: str,
    to: str,
    subject: str,
    html_body: str,
) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = to
    msg.attach(MIMEText("请使用支持 HTML 的邮件客户端查看本报告。", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, app_password)
        server.sendmail(gmail_address, [to], msg.as_string())
