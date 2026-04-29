"""
models/report_generator_periodic.py — 週次/月次 PDF レポート生成
=================================================================
matplotlib でグラフを生成し、reportlab で A4 PDF に組み込む。

構成（4ページ）:
  1ページ: 期間サマリー（総距離・時間・獲得標高・消費kcal）
  2ページ: ATL/CTL/TSB トレーニング負荷グラフ
  3ページ: HRV・安静時心拍・リカバリースコアのトレンド
  4ページ: アクティビティ一覧テーブル
"""

from __future__ import annotations

import io
from datetime import date, datetime
from typing import List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib import rcParams

import numpy as np

from reportlab.lib.pagesizes   import A4
from reportlab.lib.units        import mm
from reportlab.lib              import colors
from reportlab.lib.styles       import ParagraphStyle
from reportlab.platypus         import (SimpleDocTemplate, Paragraph, Spacer,
                                        Table, TableStyle, HRFlowable,
                                        PageBreak, Image as RLImage,
                                        KeepTogether)
from reportlab.pdfbase          import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts  import TTFont
from reportlab.lib.utils        import ImageReader

# ── ReportLab 日本語フォント（TrueType優先）────────
def _register_jp_font() -> str:
    import os
    ttf_candidates = [
        "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
        "/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf",
        "/usr/share/fonts/truetype/ipafont-gothic/ipag.ttf",
    ]
    for path in ttf_candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("IPAGothic", path))
                return "IPAGothic"
            except Exception:
                pass
    # フォールバック: CID フォント
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        return "HeiseiKakuGo-W5"
    except Exception:
        return "Helvetica"

JP = _register_jp_font()

# ── matplotlib 日本語フォント ──────────────────────
def _setup_jp_font():
    """
    matplotlib で日本語を正しく描画するためのフォント設定。
    japanize-matplotlib（フォントをバンドル）を優先し、
    なければシステムフォントを検索する。
    """
    import os
    import matplotlib.font_manager as fm

    rcParams["axes.unicode_minus"] = False  # マイナス記号の文字化け防止

    # ── 方法1: japanize-matplotlib（IPAexGothic をバンドル、最も確実） ──
    try:
        import japanize_matplotlib  # noqa: F401  — import するだけで rcParams が設定される
        print("[PDF font] japanize-matplotlib loaded (IPAexGothic)", flush=True)
        return
    except ImportError:
        print("[PDF font] japanize-matplotlib not installed, trying system fonts", flush=True)

    def _apply(path: str) -> bool:
        try:
            fm.fontManager.addfont(path)
            prop = fm.FontProperties(fname=path)
            name = prop.get_name()
            current = list(rcParams.get("font.sans-serif", []))
            rcParams["font.sans-serif"] = [name] + [f for f in current if f != name]
            rcParams["font.family"] = "sans-serif"
            print(f"[PDF font] Using: {name!r} ({path})", flush=True)
            return True
        except Exception as e:
            print(f"[PDF font] Failed to load {path}: {e}", flush=True)
            return False

    # ── 方法2: 既知パスを直接チェック ──────────────────────────────────
    known_paths = [
        "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
        "/usr/share/fonts/truetype/ipafont-gothic/ipag.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    for path in known_paths:
        if os.path.exists(path) and _apply(path):
            return

    # ── 方法3: フォントキャッシュ名前検索（macOS等） ────────────────────
    for name in ["IPAGothic", "IPAexGothic", "Hiragino Sans",
                 "Hiragino Kaku Gothic ProN", "Noto Sans CJK JP", "AppleGothic"]:
        if name in {f.name for f in fm.fontManager.ttflist}:
            current = list(rcParams.get("font.sans-serif", []))
            rcParams["font.sans-serif"] = [name] + [f for f in current if f != name]
            rcParams["font.family"] = "sans-serif"
            print(f"[PDF font] Using by name: {name!r}", flush=True)
            return

    print("[PDF font] WARNING: No Japanese font found — legend text will be garbled.", flush=True)

_setup_jp_font()

# ── カラー ───────────────────────────────────────
C_GREEN  = colors.HexColor("#2ecc71")
C_ORANGE = colors.HexColor("#e67e22")
C_BLUE   = colors.HexColor("#3498db")
C_RED    = colors.HexColor("#e74c3c")
C_GRAY   = colors.HexColor("#95a5a6")
C_BG     = colors.HexColor("#1a1a2e")

W, H = A4
MARGIN = 18 * mm


# ══════════════════════════════════════════════════════
# パブリック API
# ══════════════════════════════════════════════════════

def generate_weekly_report_pdf(
    activities: list,
    daily_metrics: list,
    period_label: str,
    user_display_name: str = "",
) -> bytes:
    """
    週次レポート PDF を生成して bytes で返す。

    Parameters
    ----------
    activities    : list of Activity ORM objects
    daily_metrics : list of DailyMetrics ORM objects
    period_label  : str  例: "2026年第15週 (4/6〜4/12)"
    """
    return _build_pdf(activities, daily_metrics, period_label, user_display_name)


def generate_monthly_report_pdf(
    activities: list,
    daily_metrics: list,
    period_label: str,
    user_display_name: str = "",
) -> bytes:
    """
    月次レポート PDF を生成して bytes で返す。

    Parameters
    ----------
    period_label  : str  例: "2026年4月"
    """
    return _build_pdf(activities, daily_metrics, period_label, user_display_name)


# ══════════════════════════════════════════════════════
# 内部実装
# ══════════════════════════════════════════════════════

def _build_pdf(
    activities: list,
    daily_metrics: list,
    period_label: str,
    user_name: str,
) -> bytes:
    buf  = io.BytesIO()
    doc  = SimpleDocTemplate(
        buf,
        pagesize      = A4,
        leftMargin    = MARGIN,
        rightMargin   = MARGIN,
        topMargin     = MARGIN,
        bottomMargin  = MARGIN,
        title         = f"Energy Tracker レポート — {period_label}",
    )

    story = []
    story += _page_summary(activities, period_label, user_name)
    story.append(PageBreak())
    story += _page_training_load(daily_metrics, period_label)
    story.append(PageBreak())
    story += _page_health_trend(daily_metrics, period_label)
    story.append(PageBreak())
    story += _page_activity_list(activities)

    doc.build(story)
    buf.seek(0)
    return buf.read()


# ── Page 1: サマリー ──────────────────────────────────

def _page_summary(activities: list, period_label: str, user_name: str) -> list:
    styles = _make_styles()
    story  = []

    story.append(Paragraph("Energy Tracker", styles["title"]))
    story.append(Paragraph(f"トレーニングレポート — {period_label}", styles["subtitle"]))
    if user_name:
        story.append(Paragraph(user_name, styles["subtitle"]))
    story.append(Spacer(1, 6 * mm))
    story.append(HRFlowable(width="100%", thickness=1, color=C_GREEN))
    story.append(Spacer(1, 6 * mm))

    # 集計値
    total_acts    = len(activities)
    total_dur_h   = sum((a.duration_sec or 0) for a in activities) / 3600
    total_dist_km = sum((a.distance_m   or 0) for a in activities) / 1000
    total_gain_m  = sum((a.elevation_gain_m or 0) for a in activities)
    total_kcal    = sum((a.calories_kcal or 0) for a in activities)

    avg_hr_list = [a.avg_hr for a in activities if a.avg_hr]
    avg_hr      = sum(avg_hr_list) / len(avg_hr_list) if avg_hr_list else None

    kpi_data = [
        ["指標",          "値"],
        ["アクティビティ数",  f"{total_acts} 件"],
        ["総運動時間",       f"{total_dur_h:.1f} h"],
        ["総距離",          f"{total_dist_km:.1f} km"],
        ["総獲得標高",       f"{total_gain_m:.0f} m"],
        ["消費カロリー",     f"{total_kcal:.0f} kcal" if total_kcal else "—"],
        ["平均心拍",        f"{avg_hr:.0f} bpm" if avg_hr else "—"],
    ]

    kpi_table = Table(kpi_data, colWidths=[90 * mm, 70 * mm])
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), C_BG),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, -1), JP),
        ("FONTSIZE",   (0, 0), (-1, -1), 11),
        ("ALIGN",      (1, 0), (1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
        ("GRID",       (0, 0), (-1, -1), 0.5, C_GRAY),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 8 * mm))

    # 種別内訳
    type_counts: dict = {}
    for a in activities:
        k = _activity_label(a.activity_type)
        type_counts[k] = type_counts.get(k, 0) + 1

    if type_counts:
        type_data = [["種別", "回数"]] + [[k, f"{v} 回"] for k, v in sorted(type_counts.items(), key=lambda x: -x[1])]
        t = Table(type_data, colWidths=[90 * mm, 70 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), C_BG),
            ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
            ("FONTNAME",   (0, 0), (-1, -1), JP),
            ("FONTSIZE",   (0, 0), (-1, -1), 10),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
            ("GRID",       (0, 0), (-1, -1), 0.5, C_GRAY),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(Paragraph("種別内訳", styles["section"]))
        story.append(t)

    return story


# ── Page 2: ATL/CTL/TSB ───────────────────────────────

def _page_training_load(daily_metrics: list, period_label: str) -> list:
    styles = _make_styles()
    story  = [Paragraph("トレーニング負荷 (ATL / CTL / TSB)", styles["section"])]
    story.append(Spacer(1, 3 * mm))

    metrics_with_load = [m for m in daily_metrics if m.atl is not None or m.ctl is not None]
    if not metrics_with_load:
        story.append(Paragraph("データがありません。Garmin または Apple Health をアップロードしてください。", styles["body"]))
        return story

    dates = [m.date for m in metrics_with_load]
    atl   = [m.atl or 0 for m in metrics_with_load]
    ctl   = [m.ctl or 0 for m in metrics_with_load]
    tsb   = [m.tsb or 0 for m in metrics_with_load]

    fig, axes = plt.subplots(2, 1, figsize=(7.0, 5.5))
    fig.patch.set_facecolor("#f5f5f5")

    ax1 = axes[0]
    ax1.fill_between(dates, atl, alpha=0.4, color="#e74c3c", label="ATL (短期疲労)")
    ax1.fill_between(dates, ctl, alpha=0.4, color="#3498db", label="CTL (フィットネス)")
    ax1.plot(dates, atl, color="#e74c3c", linewidth=1.5)
    ax1.plot(dates, ctl, color="#3498db", linewidth=1.5)
    ax1.legend(fontsize=8)
    ax1.set_ylabel("トレーニング負荷", fontsize=9)
    ax1.grid(True, alpha=0.3)
    ax1.tick_params(axis="x", rotation=30, labelsize=7)

    ax2 = axes[1]
    ax2.fill_between(dates, tsb, 0, where=[t >= 0 for t in tsb], alpha=0.4, color="#2ecc71", label="フレッシュ")
    ax2.fill_between(dates, tsb, 0, where=[t < 0 for t in tsb],  alpha=0.4, color="#e74c3c", label="疲労")
    ax2.plot(dates, tsb, color="#2ecc71", linewidth=1.5)
    ax2.axhline(0, color="black", linewidth=0.5, linestyle="--")
    ax2.axhline(-30, color="#e74c3c", linewidth=0.5, linestyle=":", label="過負荷ライン (-30)")
    ax2.legend(fontsize=8)
    ax2.set_ylabel("TSB (コンディション)", fontsize=9)
    ax2.grid(True, alpha=0.3)
    ax2.tick_params(axis="x", rotation=30, labelsize=7)

    plt.tight_layout()
    story.append(_fig_to_image(fig))
    plt.close(fig)

    return story


# ── Page 3: HRV・安静時心拍・リカバリー ─────────────────

def _page_health_trend(daily_metrics: list, period_label: str) -> list:
    styles = _make_styles()
    story  = [Paragraph("健康指標トレンド", styles["section"])]
    story.append(Spacer(1, 3 * mm))

    hrv_data  = [(m.date, m.hrv_rmssd)      for m in daily_metrics if m.hrv_rmssd]
    rhr_data  = [(m.date, m.resting_hr)     for m in daily_metrics if m.resting_hr]
    rec_data  = [(m.date, m.recovery_score) for m in daily_metrics if m.recovery_score]

    if not any([hrv_data, rhr_data, rec_data]):
        story.append(Paragraph("健康指標データがありません。Apple Health をアップロードしてください。", styles["body"]))
        return story

    fig, axes = plt.subplots(3, 1, figsize=(7.0, 7.0), sharex=False)
    fig.patch.set_facecolor("#f5f5f5")

    def _plot(ax, data, label, color, ylabel):
        if not data:
            ax.text(0.5, 0.5, "データなし", ha="center", va="center", transform=ax.transAxes, fontsize=9)
        else:
            dates, vals = zip(*data)
            ax.plot(dates, vals, color=color, linewidth=1.5, marker="o", markersize=3)
            ax.fill_between(dates, vals, alpha=0.2, color=color)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.set_title(label, fontsize=9, pad=4)
        ax.grid(True, alpha=0.3)
        ax.tick_params(axis="x", rotation=30, labelsize=7)

    _plot(axes[0], hrv_data,  "HRV (RMSSD)",      "#9b59b6", "ms")
    _plot(axes[1], rhr_data,  "安静時心拍",        "#e74c3c", "bpm")
    _plot(axes[2], rec_data,  "リカバリースコア",  "#2ecc71", "スコア (0-100)")

    plt.tight_layout()
    story.append(_fig_to_image(fig))
    plt.close(fig)

    return story


# ── Page 4: アクティビティ一覧 ───────────────────────────

def _page_activity_list(activities: list) -> list:
    styles = _make_styles()
    story  = [Paragraph("アクティビティ一覧", styles["section"])]
    story.append(Spacer(1, 3 * mm))

    if not activities:
        story.append(Paragraph("アクティビティがありません。", styles["body"]))
        return story

    header = ["日付", "種別", "時間 (h)", "距離 (km)", "獲得標高 (m)", "平均HR"]
    rows   = [header]
    for a in sorted(activities, key=lambda x: (x.start_time or datetime.min), reverse=True):
        rows.append([
            a.start_time.date().strftime("%m/%d") if a.start_time else "—",
            _activity_label(a.activity_type),
            f"{(a.duration_sec or 0) / 3600:.1f}",
            f"{(a.distance_m or 0) / 1000:.1f}",
            f"{a.elevation_gain_m:.0f}" if a.elevation_gain_m else "—",
            f"{a.avg_hr:.0f}" if a.avg_hr else "—",
        ])

    col_widths = [22 * mm, 35 * mm, 22 * mm, 28 * mm, 35 * mm, 25 * mm]
    t = Table(rows, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), C_BG),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, -1), JP),
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("ALIGN",         (2, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
        ("GRID",          (0, 0), (-1, -1), 0.3, C_GRAY),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
    ]))
    story.append(t)
    return story


# ── ユーティリティ ─────────────────────────────────────

def _fig_to_image(fig) -> RLImage:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    buf.seek(0)
    # 画像の実寸からアスペクト比を維持した高さを計算
    img_w, img_h = ImageReader(buf).getSize()
    buf.seek(0)
    target_w = W - 2 * MARGIN
    target_h = target_w * img_h / img_w
    img = RLImage(buf, width=target_w, height=target_h)
    return img


def _activity_label(activity_type: str) -> str:
    labels = {
        "hiking":        "ハイキング",
        "running":       "ランニング",
        "trail_running": "トレイルラン",
        "cycling":       "サイクリング",
    }
    return labels.get(activity_type, activity_type)


def _make_styles() -> dict:
    return {
        "title": ParagraphStyle(
            "title",
            fontName=JP, fontSize=22, textColor=C_GREEN, spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            fontName=JP, fontSize=13, textColor=colors.darkgray, spaceAfter=2,
        ),
        "section": ParagraphStyle(
            "section",
            fontName=JP, fontSize=13, textColor=C_BLUE, spaceAfter=4, spaceBefore=6,
        ),
        "body": ParagraphStyle(
            "body",
            fontName=JP, fontSize=10, spaceAfter=4,
        ),
    }
