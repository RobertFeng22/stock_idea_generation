"""Build the weekly HTML report and send it via Gmail SMTP."""
from __future__ import annotations

import html
import smtplib
from dataclasses import dataclass, field
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .analyze import Opportunity, Synthesis
from .technical import PLACEHOLDER_TEXT as TECH_PLACEHOLDER


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


def _badge(text: str, bg: str, fg: str = "#fff") -> str:
    return (f'<span style="font-size:11px;color:{fg};background:{bg};border-radius:10px;'
            f'padding:2px 8px;margin-left:6px;white-space:nowrap;">{_esc(text)}</span>')


def build_html(results: list[EpisodeResult], synthesis: Synthesis | None = None) -> str:
    today = date.today().strftime("%Y-%m-%d")
    analyzed = [r for r in results if r.status == "analyzed"]
    no_transcript = [r for r in results if r.status == "no_transcript"]
    errored = [r for r in results if r.status == "error"]
    picks = synthesis.picks if synthesis else []

    parts: list[str] = []
    parts.append(
        f"""<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
        max-width:760px;margin:0 auto;color:#1f2328;line-height:1.5;">
        <h1 style="font-size:22px;margin:0 0 4px;">📈 每周播客投资精选</h1>
        <div style="color:#6e7781;font-size:13px;margin-bottom:16px;">{today} ·
        分析 {len(analyzed)} 期节目 · 精选 {len(picks)} 只标的（上限 10）</div>"""
    )

    if synthesis and synthesis.summary:
        parts.append(
            f'<div style="background:#f6f8fa;border-radius:8px;padding:12px 14px;'
            f'font-size:13px;margin-bottom:18px;"><b>本周主线：</b>{_esc(synthesis.summary)}</div>'
        )

    if synthesis and synthesis.error:
        parts.append(
            f'<p style="color:#cf222e;font-size:13px;">精选汇总出错：{_esc(synthesis.error)}</p>'
        )

    # ---- Quick-glance shortlist table ----
    if picks:
        parts.append('<table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:20px;">')
        parts.append(
            '<tr style="text-align:left;color:#6e7781;">'
            '<th style="padding:6px 8px;">#</th><th style="padding:6px 8px;">代码</th>'
            '<th style="padding:6px 8px;">公司</th><th style="padding:6px 8px;">来源数</th>'
            '<th style="padding:6px 8px;">周期</th><th style="padding:6px 8px;">信心</th></tr>'
        )
        for i, p in enumerate(picks, 1):
            n = len(p.sources)
            conv = f'×{n}' + ('  🔥' if n >= 2 else '')
            parts.append(
                f'<tr style="border-top:1px solid #eaeef2;">'
                f'<td style="padding:6px 8px;color:#6e7781;">{i}</td>'
                f'<td style="padding:6px 8px;font-weight:700;">{_esc(p.ticker)}</td>'
                f'<td style="padding:6px 8px;">{_esc(p.name)}</td>'
                f'<td style="padding:6px 8px;color:#57606a;">{_esc(conv)}</td>'
                f'<td style="padding:6px 8px;color:#57606a;">{_esc(p.horizon)}</td>'
                f'<td style="padding:6px 8px;color:{_conf_color(p.conviction)};">{_esc(p.conviction or "n/a")}</td>'
                f'</tr>'
            )
        parts.append("</table>")

    # ---- Detailed pick cards ----
    for i, p in enumerate(picks, 1):
        n = len(p.sources)
        conv_badge = _badge(f"{n} 个来源印证" + (" 🔥" if n >= 2 else ""),
                            "#1a7f37" if n >= 2 else "#6e7781")
        horizon_badge = _badge(p.horizon, "#0969da") if p.horizon else ""
        conf_badge = _badge(p.conviction or "n/a", _conf_color(p.conviction))
        parts.append(
            f'<div style="border:1px solid #d0d7de;border-radius:10px;padding:14px 16px;margin-bottom:16px;">'
            f'<div style="font-size:16px;font-weight:700;">{i}. {_esc(p.ticker)} '
            f'<span style="font-weight:400;color:#57606a;font-size:14px;">{_esc(p.name)}</span></div>'
            f'<div style="margin:6px 0 10px;">{conv_badge}{horizon_badge}{conf_badge}</div>'
            f'<div style="font-size:13px;margin-bottom:10px;">{_esc(p.thesis)}</div>'
        )
        if p.why_not_priced:
            parts.append(
                f'<div style="background:#fff8c5;border-left:3px solid #d4a72c;border-radius:4px;'
                f'padding:8px 10px;margin-bottom:10px;font-size:12.5px;">'
                f'<b>🔍 为何尚未被定价：</b>{_esc(p.why_not_priced)}</div>'
            )
        if p.sources:
            chips = " ".join(
                f'<span style="font-size:11px;color:#0a3069;background:#ddf4ff;'
                f'border-radius:8px;padding:1px 6px;margin:0 4px 4px 0;display:inline-block;">{_esc(s)}</span>'
                for s in p.sources
            )
            parts.append(f'<div style="margin-bottom:8px;"><span style="font-size:12px;color:#6e7781;">来源：</span>{chips}</div>')
        for sp in p.supporting_points:
            point = _esc(str(sp.get("point", "")))
            quote = _esc(str(sp.get("quote", "")))
            src = _esc(str(sp.get("source", "")))
            block = f'<div style="font-size:12.5px;margin:6px 0;">· {point}'
            if quote:
                block += (f'<div style="color:#57606a;border-left:3px solid #d0d7de;padding-left:8px;'
                          f'margin:4px 0 0 10px;font-style:italic;">“{quote}”'
                          + (f' <span style="color:#8c959f;font-style:normal;">— {src}</span>' if src else "")
                          + '</div>')
            block += '</div>'
            parts.append(block)
        if p.risk:
            parts.append(f'<div style="font-size:12.5px;color:#9a6700;margin-top:8px;"><b>风险提示：</b>{_esc(p.risk)}</div>')
        # Bull vs Bear debate
        if p.bull_case or p.bear_case or p.rebuttal:
            parts.append('<div style="background:#f6f8fa;border-radius:8px;padding:10px 12px;margin-top:10px;font-size:12.5px;">')
            if p.bull_case:
                parts.append(f'<div style="margin-bottom:5px;"><b style="color:#1a7f37;">正方 🐂</b>：{_esc(p.bull_case)}</div>')
            if p.bear_case:
                parts.append(f'<div style="margin-bottom:5px;"><b style="color:#cf222e;">反方 🐻</b>：{_esc(p.bear_case)}</div>')
            if p.rebuttal:
                parts.append(f'<div><b style="color:#0969da;">正方反驳 ↩︎</b>：{_esc(p.rebuttal)}</div>')
            parts.append('</div>')
        # Technical analysis (placeholder until a framework is configured)
        ta = p.technical.strip() if p.technical else ""
        ta_html = _esc(ta).replace("\n", "<br>") if ta else f'<i style="color:#8c959f;">{_esc(TECH_PLACEHOLDER)}</i>'
        parts.append(
            f'<div style="border-top:1px dashed #d0d7de;margin-top:10px;padding-top:8px;font-size:12.5px;">'
            f'<b>📊 技术分析</b><div style="margin-top:4px;">{ta_html}</div></div>'
        )
        parts.append("</div>")

    if not picks and not (synthesis and synthesis.error):
        if analyzed:
            parts.append('<p style="color:#6e7781;">本周分析后没有符合规则的精选标的。</p>')
        else:
            parts.append('<p style="color:#6e7781;">本周没有可分析的（带文字稿的）新剧集。</p>')

    # ---- Reference: which episodes were analyzed ----
    if analyzed:
        parts.append(
            '<h2 style="font-size:15px;color:#57606a;border-bottom:1px solid #d0d7de;padding-bottom:6px;margin-top:24px;">'
            '📋 本周分析的节目</h2><ul style="font-size:12.5px;color:#57606a;">'
        )
        for r in analyzed:
            link = f' — <a href="{_esc(r.link)}" style="color:#0969da;">链接</a>' if r.link else ""
            src = '（转录自音频）' if r.transcript_source == "deepgram" else ""
            parts.append(f'<li>{_esc(r.podcast)}：{_esc(r.title)}{src}{link}</li>')
        parts.append("</ul>")

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
