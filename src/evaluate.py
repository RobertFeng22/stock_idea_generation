"""Weekly backtest / evaluation of past shortlist picks.

Run with:  python -m src.evaluate

What it does, every week (see .github/workflows/evaluate.yml):
  1. Parse every archived report in data/reports/*.html into a picks ledger.
  2. Fetch daily closes for every picked ticker + benchmark (append-only cache).
  3. Compute an event-study style scorecard (market-adjusted returns, win rate,
     payoff ratio, profit factor, expectancy, t-stats, rank IC, breakdowns).
  4. Ask Claude to critique the methodology against the accumulated history and
     apply whitelisted config tweaks (the weekly iteration loop), logging every
     change to EVAL_CHANGELOG.md.
  5. Render a Chinese HTML report, archive it under data/evaluation/history/,
     and email it.

Methodology notes (grounding in the literature):
  * Market-adjusted abnormal returns and event windows follow the analyst-
    recommendation event-study tradition (Barber/Lehavy/McNichols/Trueman 2001;
    MacKinlay 1997). Benchmark defaults to SPY.
  * Aggregation across overlapping weekly cohorts is biased for t-stats
    (Fama 1998); the weekly-cohort breakdown approximates a calendar-time view.
  * Rank IC (Spearman) follows Grinold & Kahn's information-coefficient usage.
"""
from __future__ import annotations

import html as html_mod
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import yaml

from .config import CONFIG_DIR, DATA_DIR, ROOT
from .prices import EVAL_DIR, ensure_prices, load_cache

REPORTS_DIR = DATA_DIR / "reports"
HISTORY_DIR = EVAL_DIR / "history"
PICKS_PATH = EVAL_DIR / "picks.json"
EVAL_CONFIG_PATH = CONFIG_DIR / "evaluation.yaml"
CHANGELOG_PATH = ROOT / "EVAL_CHANGELOG.md"

DEFAULT_CONFIG: dict = {
    "benchmark": "SPY",
    # Trading-day horizons for fixed-window abnormal returns (1w / 1m / 3m).
    "horizons_trading_days": [5, 21, 63],
    # "report_day_close" (email lands pre-open, first close is achievable) or
    # "next_day_close" (fully conservative).
    "entry_convention": "report_day_close",
    # Winsorize per-pick returns at this percentile (0 = off).
    "winsorize_pct": 0,
    # Don't show breakdown rows for groups smaller than this.
    "min_group_size": 3,
    # Include the pre-shortlist "133 opportunities" broad cohort as a
    # comparison group (does the top-10 filter add value?).
    "evaluate_legacy_cohort": True,
    # Reject entry prices more than this many calendar days before the entry
    # trading day (guards against stale quotes leaking pre-report information).
    "max_entry_staleness_days": 5,
}

# Keys the weekly Claude iteration step is allowed to change on its own.
TUNABLE_KEYS = {
    "benchmark", "horizons_trading_days", "entry_convention",
    "winsorize_pct", "min_group_size", "evaluate_legacy_cohort",
    "max_entry_staleness_days",
}


# --------------------------------------------------------------------------
# 1) Parse archived reports into a picks ledger
# --------------------------------------------------------------------------

def _cells(row_html: str) -> list[str]:
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S)
    return [re.sub(r"\s+", " ", html_mod.unescape(re.sub(r"<[^>]+>", "", c))).strip()
            for c in cells]


def parse_reports() -> list[dict]:
    """Extract every pick from every archived weekly report."""
    picks: list[dict] = []
    for path in sorted(REPORTS_DIR.glob("*.html")):
        report_date = path.stem  # YYYY-MM-DD
        text = path.read_text(encoding="utf-8")
        for table in re.findall(r"<table[^>]*>(.*?)</table>", text, re.S):
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)
            if not rows:
                continue
            header = _cells(rows[0])
            if header[:2] == ["#", "代码"]:  # ranked shortlist (current format)
                for row in rows[1:]:
                    c = _cells(row)
                    if len(c) < 6 or not c[1]:
                        continue
                    m = re.search(r"\d+", c[3])
                    picks.append({
                        "report_date": report_date, "cohort": "shortlist",
                        "rank": int(c[0]), "ticker": c[1], "company": c[2],
                        "sources": int(m.group()) if m else 1,
                        "multi_source": "🔥" in c[3],
                        "horizon": c[4], "confidence": c[5],
                    })
            elif header[:2] == ["代码", "公司"]:  # legacy broad list
                for row in rows[1:]:
                    c = _cells(row)
                    if len(c) < 3 or not c[0]:
                        continue
                    picks.append({
                        "report_date": report_date, "cohort": "legacy_broad",
                        "rank": None, "ticker": c[0], "company": c[1],
                        "sources": None, "multi_source": None,
                        "horizon": None, "confidence": None,
                    })
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    PICKS_PATH.write_text(json.dumps(picks, indent=1, ensure_ascii=False),
                          encoding="utf-8")
    return picks


# --------------------------------------------------------------------------
# 2) Metrics
# --------------------------------------------------------------------------

@dataclass
class PickResult:
    pick: dict
    entry_date: str | None = None
    entry_price: float | None = None
    latest_date: str | None = None
    latest_price: float | None = None
    ret: float | None = None            # simple return since entry
    bench_ret: float | None = None
    abnormal: float | None = None       # ret - bench_ret (market-adjusted)
    horizon_rets: dict = field(default_factory=dict)   # {"5": ret, ...}
    horizon_abn: dict = field(default_factory=dict)
    missing: str = ""


def _trading_days(bench: dict[str, float]) -> list[str]:
    return sorted(bench.keys())


def _entry_trading_day(days: list[str], report_date: str, convention: str) -> str | None:
    after = [d for d in days if d >= report_date]
    if not after:
        return None
    if convention == "next_day_close" and len(after) > 1 and after[0] == report_date:
        return after[1]
    return after[0]


def _at_or_before(series: dict[str, float], day: str) -> tuple[str, float] | None:
    prior = [d for d in series if d <= day]
    if not prior:
        return None
    d = max(prior)
    return d, series[d]


def evaluate_picks(picks: list[dict], prices: dict[str, dict[str, float]],
                   cfg: dict) -> list[PickResult]:
    bench = prices.get(cfg["benchmark"], {})
    days = _trading_days(bench)
    if not days:
        raise SystemExit(f"No benchmark ({cfg['benchmark']}) price data available.")
    latest_day = days[-1]
    results: list[PickResult] = []
    for pick in picks:
        r = PickResult(pick=pick)
        series = prices.get(pick["ticker"], {})
        entry_day = _entry_trading_day(days, pick["report_date"],
                                       cfg["entry_convention"])
        if not series:
            r.missing = "no price data"
            results.append(r)
            continue
        if not entry_day or entry_day not in bench:
            r.missing = "no benchmark data at entry"
            results.append(r)
            continue
        entry = _at_or_before(series, entry_day)
        # Entry must be at/just after the report; a stale price would leak
        # pre-report information.
        if not entry or (date.fromisoformat(entry_day)
                         - date.fromisoformat(entry[0])).days > cfg.get(
                             "max_entry_staleness_days", 5):
            r.missing = f"no entry price near {entry_day}"
            results.append(r)
            continue
        r.entry_date, r.entry_price = entry
        latest = _at_or_before(series, latest_day)
        if not latest or latest[0] <= r.entry_date:
            r.missing = "no post-entry price"
            results.append(r)
            continue
        r.latest_date, r.latest_price = latest
        r.ret = r.latest_price / r.entry_price - 1
        # Align the benchmark window to the stock's actual last quote date so a
        # stale ticker isn't compared against extra benchmark days.
        b0 = bench.get(entry_day)
        bench_latest = _at_or_before(bench, r.latest_date)
        b1 = bench_latest[1] if bench_latest else None
        if b0 and b1:
            r.bench_ret = b1 / b0 - 1
            r.abnormal = r.ret - r.bench_ret
        # Fixed trading-day horizons that have matured.
        i0 = days.index(entry_day)
        for h in cfg["horizons_trading_days"]:
            if i0 + h >= len(days):
                continue
            day_h = days[i0 + h]
            p_h = _at_or_before(series, day_h)
            if not p_h or p_h[0] <= r.entry_date:
                continue
            ret_h = p_h[1] / r.entry_price - 1
            r.horizon_rets[str(h)] = ret_h
            if bench.get(day_h):
                r.horizon_abn[str(h)] = ret_h - (bench[day_h] / bench[entry_day] - 1)
        results.append(r)
    return results


def _winsorize(values: list[float], pct: float) -> list[float]:
    if not values or pct <= 0:
        return values
    lo, hi = _percentile(values, pct), _percentile(values, 100 - pct)
    return [min(max(v, lo), hi) for v in values]


def _percentile(values: list[float], p: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def _mean(v: list[float]) -> float | None:
    return sum(v) / len(v) if v else None


def _t_stat(v: list[float]) -> float | None:
    if len(v) < 3:
        return None
    m = _mean(v)
    var = sum((x - m) ** 2 for x in v) / (len(v) - 1)
    if var == 0:
        return None
    return m / math.sqrt(var / len(v))


def _binom_p_two_sided(wins: int, n: int) -> float | None:
    """Exact two-sided binomial test against p=0.5."""
    if n == 0:
        return None
    p_obs = sum(math.comb(n, k) for k in range(wins, n + 1)) / 2 ** n
    p_obs_low = sum(math.comb(n, k) for k in range(0, wins + 1)) / 2 ** n
    return min(1.0, 2 * min(p_obs, p_obs_low))


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None

    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: v[i])
        rk = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk

    rx, ry = ranks(xs), ranks(ys)
    mx, my = _mean(rx), _mean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def _agg(results: list[PickResult], cfg: dict, use_abnormal: bool = True) -> dict:
    vals = [(r.abnormal if use_abnormal else r.ret)
            for r in results if r.abnormal is not None]
    vals = _winsorize(vals, cfg.get("winsorize_pct", 0))
    if not vals:
        return {"n": 0}
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v <= 0]
    avg_win = _mean(wins)
    avg_loss = _mean(losses)
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    win_rate = len(wins) / len(vals)
    return {
        "n": len(vals),
        "win_rate": win_rate,
        "avg": _mean(vals),
        "median": _percentile(vals, 50),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": (avg_win / abs(avg_loss)) if wins and losses and avg_loss else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "expectancy": win_rate * (avg_win or 0) + (1 - win_rate) * (avg_loss or 0),
        "t_stat": _t_stat(vals),
        "binom_p": _binom_p_two_sided(len(wins), len(vals)),
        "best": max(vals),
        "worst": min(vals),
    }


def compute_metrics(results: list[PickResult], cfg: dict) -> dict:
    shortlist = [r for r in results if r.pick["cohort"] == "shortlist"]
    ok = [r for r in shortlist if r.abnormal is not None]
    metrics: dict = {
        "as_of": max((r.latest_date or "" for r in ok), default=""),
        "n_picks": len(shortlist),
        "n_priced": len(ok),
        "missing": [
            {"ticker": r.pick["ticker"], "report_date": r.pick["report_date"],
             "reason": r.missing}
            for r in shortlist if r.missing
        ],
        "overall_abnormal": _agg(ok, cfg, use_abnormal=True),
        "overall_absolute": _agg(ok, cfg, use_abnormal=False),
    }

    # Fixed matured horizons.
    horizons = {}
    for h in cfg["horizons_trading_days"]:
        vals = [r.horizon_abn[str(h)] for r in ok if str(h) in r.horizon_abn]
        if vals:
            sub = [PickResult(pick=r.pick, abnormal=v) for r, v in
                   [(r, r.horizon_abn[str(h)]) for r in ok if str(h) in r.horizon_abn]]
            horizons[str(h)] = _agg(sub, cfg)
    metrics["horizons"] = horizons

    # Breakdowns.
    def breakdown(key_fn) -> dict:
        groups: dict[str, list[PickResult]] = {}
        for r in ok:
            groups.setdefault(str(key_fn(r.pick)), []).append(r)
        return {k: _agg(v, cfg) for k, v in sorted(groups.items())
                if len(v) >= cfg["min_group_size"]}

    metrics["by_confidence"] = breakdown(lambda p: p.get("confidence"))
    metrics["by_multi_source"] = breakdown(
        lambda p: "multi(🔥)" if p.get("multi_source") else "single")
    metrics["by_week"] = breakdown(lambda p: p["report_date"])
    metrics["by_rank_bucket"] = breakdown(
        lambda p: "rank 1-3" if (p.get("rank") or 99) <= 3
        else ("rank 4-6" if (p.get("rank") or 99) <= 6 else "rank 7-10"))
    metrics["by_stated_horizon"] = breakdown(lambda p: p.get("horizon"))

    # Rank IC per weekly cohort (does the model's ordering predict returns?).
    ics = []
    for week in sorted({r.pick["report_date"] for r in ok}):
        cohort = [r for r in ok if r.pick["report_date"] == week
                  and r.pick.get("rank")]
        if len(cohort) >= 4:
            ic = _spearman([-r.pick["rank"] for r in cohort],
                           [r.abnormal for r in cohort])
            if ic is not None:
                ics.append({"week": week, "ic": ic, "n": len(cohort)})
    metrics["rank_ic"] = {"per_week": ics, "avg": _mean([x["ic"] for x in ics])}

    # Optional: legacy broad cohort as a filter-value comparison.
    if cfg.get("evaluate_legacy_cohort"):
        legacy = [r for r in results
                  if r.pick["cohort"] == "legacy_broad" and r.abnormal is not None]
        if legacy:
            metrics["legacy_cohort"] = _agg(legacy, cfg)

    # Per-pick table for the report.
    metrics["picks"] = [
        {
            "report_date": r.pick["report_date"], "rank": r.pick.get("rank"),
            "ticker": r.pick["ticker"], "company": r.pick.get("company"),
            "confidence": r.pick.get("confidence"),
            "sources": r.pick.get("sources"),
            "entry_date": r.entry_date, "entry": r.entry_price,
            "latest_date": r.latest_date, "latest": r.latest_price,
            "ret": r.ret, "bench_ret": r.bench_ret, "abnormal": r.abnormal,
            "horizon_abn": r.horizon_abn,
        }
        for r in sorted(ok, key=lambda r: r.abnormal, reverse=True)
    ]
    return metrics


# --------------------------------------------------------------------------
# 3) Weekly methodology iteration (Claude critique loop)
# --------------------------------------------------------------------------

ITERATION_PROMPT = """\
你是一个量化评估方法论审查员。下面是一个「播客→选股」系统的每周回测评估：
当前评估配置、历史各周的指标摘要、以及方法论迭代日志。

请基于学术文献（Barber/Lehavy/McNichols/Trueman 2001 分析师推荐事件研究、
MacKinlay 1997 事件研究法、Fama 1998 日历时间组合、Grinold & Kahn 的 IC 框架、
样本量与多重检验问题）审查本周结果，并回答：

1. 这套评估方法目前最大的缺陷是什么？（例如：重叠窗口、幸存者偏差、
   基准选择、样本量、看涨偏差）
2. 是否需要调整评估参数？只能调整这些键：{tunable}
3. 从结果里能否得出「如何提高选股胜率/赔率」的可操作建议？
   （给 config/rules.md 的改进建议，写中文，面向下一周的选股提示词）

当前配置:
{config}

历史指标摘要（按周）:
{history}

迭代日志（最近几条）:
{changelog}

只输出一个 JSON 对象，不要其它文字：
{{
  "assessment": "本周方法论评估（中文，3-6 句）",
  "config_updates": {{}},            // 仅允许键: {tunable}; 无需改动则留空
  "changelog_entry": "一句话描述本周迭代（中文）",
  "rule_suggestions": "给选股规则的改进建议（中文，可分点）",
  "experiments": ["下周想尝试的评估实验，中文，0-3 条"]
}}
"""


def iterate_methodology(metrics: dict, cfg: dict) -> dict | None:
    """Ask Claude to critique this week's evaluation and tune whitelisted knobs."""
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key or os.environ.get("EVAL_ITERATE", "1") == "0":
        return None
    from anthropic import Anthropic

    history_lines = []
    for path in sorted(HISTORY_DIR.glob("*.json"))[-8:]:
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
            o = m.get("overall_abnormal", {})
            history_lines.append(
                f"{path.stem}: n={o.get('n')} win={o.get('win_rate')} "
                f"avg={o.get('avg')} payoff={o.get('payoff_ratio')} "
                f"t={o.get('t_stat')}")
        except Exception:  # noqa: BLE001
            continue
    summary = {k: metrics[k] for k in
               ("n_picks", "n_priced", "overall_abnormal", "overall_absolute",
                "horizons", "by_confidence", "by_multi_source",
                "by_rank_bucket", "rank_ic") if k in metrics}
    history_lines.append(f"本周(将存档): {json.dumps(summary, ensure_ascii=False)}")

    changelog_tail = ""
    if CHANGELOG_PATH.exists():
        changelog_tail = "\n".join(
            CHANGELOG_PATH.read_text(encoding="utf-8").splitlines()[-30:])

    prompt = ITERATION_PROMPT.format(
        tunable=sorted(TUNABLE_KEYS),
        config=yaml.safe_dump(cfg, allow_unicode=True),
        history="\n".join(history_lines),
        changelog=changelog_tail or "(空)",
    )
    client = Anthropic(api_key=api_key)
    model = (os.environ.get("ANTHROPIC_MODEL") or "").strip() or "claude-sonnet-4-6"
    try:
        resp = client.messages.create(
            model=model, max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group()) if m else None
    except Exception as exc:  # noqa: BLE001
        print(f"  ! iteration step failed: {exc}")
        return None


def apply_iteration(iteration: dict, cfg: dict) -> dict:
    """Apply whitelisted config updates and append the changelog entry."""
    updates = {k: v for k, v in (iteration.get("config_updates") or {}).items()
               if k in TUNABLE_KEYS}
    if updates:
        cfg = {**cfg, **updates}
        EVAL_CONFIG_PATH.write_text(
            yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
    today = date.today().isoformat()
    lines = [f"\n## {today}", ""]
    if iteration.get("changelog_entry"):
        lines.append(f"- 迭代: {iteration['changelog_entry']}")
    if updates:
        lines.append(f"- 参数调整: `{json.dumps(updates, ensure_ascii=False)}`")
    for exp in iteration.get("experiments") or []:
        lines.append(f"- 下周实验: {exp}")
    with CHANGELOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return cfg


# --------------------------------------------------------------------------
# 4) Report rendering + email
# --------------------------------------------------------------------------

def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:+.1f}%"


def _num(v: float | None, nd: int = 2) -> str:
    return "—" if v is None else f"{v:.{nd}f}"


def _agg_row(label: str, a: dict) -> str:
    if not a or not a.get("n"):
        return ""
    return (f"<tr><td style='padding:5px 8px;'>{label}</td>"
            f"<td style='padding:5px 8px;'>{a['n']}</td>"
            f"<td style='padding:5px 8px;'>{a['win_rate'] * 100:.0f}%</td>"
            f"<td style='padding:5px 8px;'>{_pct(a['avg'])}</td>"
            f"<td style='padding:5px 8px;'>{_pct(a['median'])}</td>"
            f"<td style='padding:5px 8px;'>{_num(a['payoff_ratio'])}</td>"
            f"<td style='padding:5px 8px;'>{_num(a['profit_factor'])}</td>"
            f"<td style='padding:5px 8px;'>{_pct(a['expectancy'])}</td>"
            f"<td style='padding:5px 8px;'>{_num(a['t_stat'])}</td></tr>")


AGG_HEADER = ("<tr style='text-align:left;color:#6e7781;'>"
              "<th style='padding:5px 8px;'>分组</th><th>n</th><th>胜率</th>"
              "<th>平均超额</th><th>中位数</th><th>赔率</th><th>盈亏比</th>"
              "<th>期望</th><th>t 值</th></tr>")


def build_eval_html(metrics: dict, cfg: dict, iteration: dict | None) -> str:
    o = metrics["overall_abnormal"]
    oa = metrics["overall_absolute"]
    parts = [f"""
<div style="font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;max-width:860px;margin:0 auto;color:#1f2328;">
<h1 style="font-size:20px;">📊 播客选股回测评估 · {date.today().isoformat()}</h1>
<p style="color:#57606a;">数据截至 {metrics['as_of']} · 基准 {cfg['benchmark']} ·
入场约定：{'报告日收盘' if cfg['entry_convention'] == 'report_day_close' else '次日收盘'} ·
共 {metrics['n_picks']} 条精选推荐，{metrics['n_priced']} 条有完整价格。</p>

<h2 style="font-size:16px;">总览（超额收益 = 个股收益 − {cfg['benchmark']}）</h2>
<table style="width:100%;border-collapse:collapse;font-size:13px;">{AGG_HEADER}
{_agg_row('全部精选（超额）', o)}
{_agg_row('全部精选（绝对收益）', oa)}
</table>
<p style="color:#57606a;font-size:12px;">赔率 = 平均盈利/平均亏损；盈亏比 = 总盈利/总亏损；
期望 = 胜率×平均盈利 + 败率×平均亏损；t 值 |t|&gt;2 约等于 95% 显著。
双侧二项检验 p（胜率 vs 50%）：{_num(o.get('binom_p'), 3)}。</p>
"""]

    if metrics.get("horizons"):
        parts.append("<h2 style='font-size:16px;'>固定持有期（已到期样本，超额）</h2>"
                     f"<table style='width:100%;border-collapse:collapse;font-size:13px;'>{AGG_HEADER}")
        label = {"5": "1 周 (5 交易日)", "21": "1 月 (21)", "63": "3 月 (63)"}
        for h, a in metrics["horizons"].items():
            parts.append(_agg_row(label.get(h, f"{h} 交易日"), a))
        parts.append("</table>")

    for key, title in [("by_confidence", "按信心等级"),
                       ("by_multi_source", "按来源数（多来源🔥 vs 单来源）"),
                       ("by_rank_bucket", "按排名分档"),
                       ("by_stated_horizon", "按推荐时给出的周期"),
                       ("by_week", "按周 cohort")]:
        groups = metrics.get(key) or {}
        if groups:
            parts.append(f"<h2 style='font-size:16px;'>{title}</h2>"
                         f"<table style='width:100%;border-collapse:collapse;font-size:13px;'>{AGG_HEADER}")
            parts.extend(_agg_row(k, a) for k, a in groups.items())
            parts.append("</table>")

    ic = metrics.get("rank_ic") or {}
    if ic.get("avg") is not None:
        parts.append(f"<p><b>Rank IC</b>（周内排名 vs 后续超额收益的 Spearman 相关，"
                     f"均值）：{_num(ic['avg'], 3)} — 正值表示排名越靠前表现越好。</p>")
    if metrics.get("legacy_cohort"):
        parts.append("<h2 style='font-size:16px;'>对照组：早期未筛选的完整机会列表</h2>"
                     f"<table style='width:100%;border-collapse:collapse;font-size:13px;'>{AGG_HEADER}"
                     + _agg_row("legacy 全列表", metrics["legacy_cohort"]) + "</table>"
                     "<p style='color:#57606a;font-size:12px;'>若精选组显著优于该对照组，说明 top-10 筛选在创造价值。</p>")

    parts.append("<h2 style='font-size:16px;'>各笔推荐明细（按超额收益排序）</h2>"
                 "<table style='width:100%;border-collapse:collapse;font-size:12px;'>"
                 "<tr style='text-align:left;color:#6e7781;'>"
                 "<th style='padding:4px 6px;'>周</th><th>#</th><th>代码</th><th>信心</th>"
                 "<th>入场</th><th>现价</th><th>收益</th><th>基准</th><th>超额</th></tr>")
    for p in metrics["picks"]:
        color = "#1a7f37" if (p["abnormal"] or 0) > 0 else "#cf222e"
        parts.append(
            f"<tr style='border-top:1px solid #eaeef2;'>"
            f"<td style='padding:4px 6px;color:#6e7781;'>{p['report_date'][5:]}</td>"
            f"<td style='padding:4px 6px;'>{p['rank'] or ''}</td>"
            f"<td style='padding:4px 6px;font-weight:700;'>{p['ticker']}</td>"
            f"<td style='padding:4px 6px;'>{p['confidence'] or ''}</td>"
            f"<td style='padding:4px 6px;'>{_num(p['entry'])}</td>"
            f"<td style='padding:4px 6px;'>{_num(p['latest'])}</td>"
            f"<td style='padding:4px 6px;'>{_pct(p['ret'])}</td>"
            f"<td style='padding:4px 6px;'>{_pct(p['bench_ret'])}</td>"
            f"<td style='padding:4px 6px;color:{color};font-weight:600;'>{_pct(p['abnormal'])}</td></tr>")
    parts.append("</table>")

    if metrics.get("missing"):
        parts.append("<p style='color:#9a6700;font-size:12px;'>数据缺失："
                     + "; ".join(f"{m['ticker']}({m['report_date']}: {m['reason']})"
                                 for m in metrics["missing"]) + "</p>")

    if iteration:
        parts.append("<h2 style='font-size:16px;'>🔁 本周方法论迭代</h2>")
        if iteration.get("assessment"):
            parts.append(f"<p>{html_mod.escape(iteration['assessment'])}</p>")
        if iteration.get("config_updates"):
            parts.append("<p>参数调整：<code>"
                         + html_mod.escape(json.dumps(iteration["config_updates"],
                                                      ensure_ascii=False))
                         + "</code></p>")
        if iteration.get("rule_suggestions"):
            parts.append("<h3 style='font-size:14px;'>对选股规则的建议</h3><p>"
                         + html_mod.escape(iteration["rule_suggestions"]).replace("\n", "<br>")
                         + "</p>")
        if iteration.get("experiments"):
            parts.append("<p>下周实验：" + "；".join(
                html_mod.escape(e) for e in iteration["experiments"]) + "</p>")

    parts.append(
        "<hr style='border:none;border-top:1px solid #eaeef2;margin:18px 0;'>"
        "<p style='color:#8b949e;font-size:11px;'>方法论：事件研究式市场调整超额收益"
        "（Barber/Lehavy/McNichols/Trueman 2001；MacKinlay 1997），周 cohort 视角近似"
        "日历时间组合（Fama 1998），Rank IC 参照 Grinold &amp; Kahn。样本量小、窗口重叠，"
        "显著性指标仅供参考。本报告不构成投资建议。</p></div>")
    return "".join(parts)


# --------------------------------------------------------------------------
# 5) Orchestration
# --------------------------------------------------------------------------

def load_eval_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if EVAL_CONFIG_PATH.exists():
        loaded = yaml.safe_load(EVAL_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        cfg.update({k: v for k, v in loaded.items() if v is not None})
    else:
        EVAL_CONFIG_PATH.write_text(
            yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
    return cfg


def run() -> int:
    cfg = load_eval_config()
    picks = parse_reports()
    shortlist = [p for p in picks if p["cohort"] == "shortlist"]
    print(f"Parsed {len(picks)} pick(s) ({len(shortlist)} shortlist) "
          f"from {REPORTS_DIR}.")
    if not shortlist:
        print("No shortlist picks to evaluate yet.")
        return 0

    wanted = {p["report_date"] for p in shortlist}
    tickers = sorted({p["ticker"] for p in
                      (picks if cfg.get("evaluate_legacy_cohort") else shortlist)})
    start = date.fromisoformat(min(wanted)) - timedelta(days=7)
    end = date.today()

    if os.environ.get("EVAL_FETCH", "1") != "0":
        print(f"Fetching prices for {len(tickers)} ticker(s) + {cfg['benchmark']}...")
        prices = ensure_prices([cfg["benchmark"]] + tickers, start, end)
    else:
        prices = load_cache()

    results = evaluate_picks(picks, prices, cfg)
    metrics = compute_metrics(results, cfg)
    o = metrics["overall_abnormal"]
    print(f"Scored {metrics['n_priced']}/{metrics['n_picks']} picks | "
          f"win {o.get('win_rate', 0) * 100:.0f}% | avg abnormal {_pct(o.get('avg'))} | "
          f"payoff {_num(o.get('payoff_ratio'))}")

    iteration = iterate_methodology(metrics, cfg)
    if iteration:
        cfg = apply_iteration(iteration, cfg)
        print(f"Iteration: {iteration.get('changelog_entry', '(no entry)')}")

    html_body = build_eval_html(metrics, cfg, iteration)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    (HISTORY_DIR / f"{stamp}.html").write_text(html_body, encoding="utf-8")
    metrics_slim = {k: v for k, v in metrics.items() if k != "picks"}
    (HISTORY_DIR / f"{stamp}.json").write_text(
        json.dumps(metrics_slim, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Report archived: {HISTORY_DIR / (stamp + '.html')}")

    if os.environ.get("EVAL_SEND_EMAIL", "1") != "0":
        gmail = (os.environ.get("GMAIL_ADDRESS") or "").strip()
        password = (os.environ.get("GMAIL_APP_PASSWORD") or "").strip()
        to = (os.environ.get("EMAIL_TO") or "").strip() or gmail
        if gmail and password:
            from .emailer import send_email
            send_email(gmail_address=gmail, app_password=password, to=to,
                       subject=f"📊 播客选股回测评估 · {stamp}",
                       html_body=html_body)
            print(f"Evaluation email sent to {to}.")
        else:
            print("Email credentials not set; skipped sending.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(run())
