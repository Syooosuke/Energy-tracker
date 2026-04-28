"""
PDFレポート生成モジュール: report_generator.py  (v2)
=====================================================
matplotlib でグラフを生成し、reportlab で PDF に埋め込む。
kaleido/Chrome 不要。

構成:
  1ページ目: タイトル・プロフィール・環境補正
  2ページ目: E(t)グラフ＋KPIカード・アラート状況
  3ページ目: ペースカーブ・斜度プロファイル
  4ページ目: 補給計画テーブル・アドバイス
"""

from __future__ import annotations
import io
from datetime import datetime
from typing import List, Optional

import numpy as np
import matplotlib
matplotlib.use("Agg")   # GUIなしで動作
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import Patch as MplPatch
from matplotlib import rcParams

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

# ── 日本語フォント ────────────────────────────────
pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
JP = "HeiseiKakuGo-W5"

# ── matplotlib 日本語フォント自動検出 ──────────────
def _setup_jp_font():
    import matplotlib.font_manager as fm
    candidates = [
        "Hiragino Sans", "Hiragino Kaku Gothic ProN",
        "Hiragino Kaku Gothic Pro", "AppleGothic",
        "IPAexGothic", "IPAGothic", "Noto Sans CJK JP", "TakaoPGothic",
    ]
    available = {f.name for f in fm.fontManager.ttflist}
    for font in candidates:
        if font in available:
            rcParams["font.family"] = font
            return
    rcParams["font.family"] = "sans-serif"

_setup_jp_font()

# ── カラー ───────────────────────────────────────
C_GREEN  = colors.HexColor("#2ecc71")
C_RED    = colors.HexColor("#e74c3c")
C_BLUE   = colors.HexColor("#3498db")
C_DARK   = colors.HexColor("#2c3e50")
C_GRAY   = colors.HexColor("#95a5a6")
C_LIGHT  = colors.HexColor("#ecf0f1")
C_ORANGE = colors.HexColor("#f39c12")
C_PURPLE = colors.HexColor("#8e44ad")

W, H = A4


# ══════════════════════════════════════════════════════
# スタイル定義
# ══════════════════════════════════════════════════════

def S(name, size, color=C_DARK, align="LEFT", bold=False, space=2):
    return ParagraphStyle(
        name      = name,
        fontName  = JP,
        fontSize  = size,
        textColor = color,
        alignment = {"LEFT":0, "CENTER":1, "RIGHT":2}[align],
        spaceAfter= space * mm,
        leading   = size * 1.5,
    )

def make_styles():
    return {
        "title"   : S("title",    22, C_DARK,  "CENTER", space=3),
        "subtitle": S("subtitle", 12, C_GRAY,  "CENTER", space=2),
        "caption" : S("caption",   8, C_GRAY,  "CENTER", space=1),
        "h1"      : S("h1",       13, C_GREEN, "LEFT",   space=4),
        "h2"      : S("h2",       10, C_DARK,  "LEFT",   space=2),
        "body"    : S("body",      9, C_DARK,  "LEFT",   space=1),
        "small"   : S("small",     8, C_GRAY,  "LEFT",   space=1),
        "kpi_val" : S("kpi_val",  18, C_GREEN, "CENTER", space=1),
        "kpi_lbl" : S("kpi_lbl",   8, C_GRAY,  "CENTER", space=1),
        "alert"   : S("alert",     9, C_RED,   "LEFT",   space=1),
        "safe"    : S("safe",      9, C_GREEN, "LEFT",   space=1),
        "mono"    : S("mono",      8, C_DARK,  "LEFT",   space=0),
    }


# ══════════════════════════════════════════════════════
# ユーティリティ
# ══════════════════════════════════════════════════════

def fmt_min(minutes: float) -> str:
    h, m = int(minutes) // 60, int(minutes) % 60
    return f"{h}時間{m:02d}分" if h > 0 else f"{m}分"

def fig_to_rl_image(fig, width_mm: float, height_mm: float) -> RLImage:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=180, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return RLImage(buf, width=width_mm * mm, height=height_mm * mm)

def col_widths(ratios, total_mm=None):
    total = (W - 24*mm) if total_mm is None else total_mm * mm
    return [total * r for r in ratios]


# ══════════════════════════════════════════════════════
# グラフ生成
# ══════════════════════════════════════════════════════

def make_energy_graph(sim_result, engine_a_out, profile, supply_events,
                      alert_config, width_mm=170, height_mm=80):
    """E(t) + 心拍数 の2軸グラフ（matplotlib）"""
    t    = sim_result["time_min"]
    E    = sim_result["E_kcal"]
    A    = sim_result["A_kcal"]
    HR   = engine_a_out["hr_predicted"]
    e0   = profile.e_initial_kcal
    ecrit= alert_config.e_crit_kcal if alert_config else 300
    n    = min(len(t), len(E), len(HR))

    fig, ax1 = plt.subplots(figsize=(width_mm/25.4, height_mm/25.4))
    ax2 = ax1.twinx()

    # E(t) 塗りつぶし
    ax1.fill_between(t[:n], E[:n], alpha=0.18, color="#2ecc71")
    ax1.plot(t[:n], E[:n], color="#2ecc71", lw=2.0, label="Energy (kcal)")

    # A(t) 点線
    ax1.plot(t[:n], A[:n], color="#f39c12", lw=1.2, ls="--",
             label="Unabsorbed (kcal)", alpha=0.8)

    # 心拍数
    ax2.plot(t[:n], HR[:n], color="#e74c3c", lw=1.2, alpha=0.75, label="Heart Rate (bpm)")

    # E_crit ライン
    ax1.axhline(ecrit, color="#e74c3c", ls="--", lw=1.2, alpha=0.8)
    ax1.text(t[n//20], ecrit + e0*0.01, f"Alert {ecrit}kcal",
             color="#e74c3c", fontsize=6)

    # 補給イベント 縦線
    for ev in supply_events:
        ax1.axvline(ev.time_min, color="#3498db", ls="--", lw=0.9, alpha=0.7)
        ax1.text(ev.time_min + 1, e0 * 0.95, f"+{ev.kcal:.0f}",
                 color="#3498db", fontsize=5.5, rotation=90, va="top")

    # アラート 縦線
    for at in sim_result.get("alert_times", []):
        ax1.axvline(at, color="#e74c3c", lw=1.5, alpha=0.9)

    ax1.set_xlabel("Time (min)", fontsize=7)
    ax1.set_ylabel("Energy (kcal)", fontsize=7, color="#2ecc71")
    ax2.set_ylabel("Heart Rate (bpm)", fontsize=7, color="#e74c3c")
    ax1.tick_params(labelsize=6)
    ax2.tick_params(labelsize=6)
    ax1.set_ylim(bottom=0)

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2,
               loc="upper right", fontsize=5.5, framealpha=0.8)
    ax1.grid(axis="y", alpha=0.3, lw=0.5)
    fig.tight_layout(pad=0.5)
    return fig_to_rl_image(fig, width_mm, height_mm)


def make_pace_graph(engine_a_out, width_mm=170, height_mm=65):
    """最適ペースカーブ + 斜度プロファイル（matplotlib）"""
    t     = np.arange(len(engine_a_out["speed_curve"])) * (10/60)
    spd   = engine_a_out["speed_curve"]
    slope = engine_a_out["slope_curve"]
    n     = min(len(t), len(spd), len(slope))

    fig, ax1 = plt.subplots(figsize=(width_mm/25.4, height_mm/25.4))
    ax2 = ax1.twinx()

    ax1.fill_between(t[:n], spd[:n], alpha=0.15, color="#3498db")
    ax1.plot(t[:n], spd[:n], color="#3498db", lw=1.8, label="Speed (m/s)")
    ax2.plot(t[:n], slope[:n], color="#8e44ad", lw=1.0, alpha=0.65, label="Slope (%)")
    ax2.axhline(0, color="#8e44ad", lw=0.5, alpha=0.4)

    ax1.set_xlabel("Time (min)", fontsize=7)
    ax1.set_ylabel("Speed (m/s)", fontsize=7, color="#3498db")
    ax2.set_ylabel("Slope (%)", fontsize=7, color="#8e44ad")
    ax1.tick_params(labelsize=6)
    ax2.tick_params(labelsize=6)
    ax1.set_ylim(bottom=0)

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2,
               loc="upper center", bbox_to_anchor=(0.5, -0.16),
               ncol=4, fontsize=6, framealpha=1.0,
               edgecolor="#cccccc", fancybox=True)
    ax1.grid(axis="y", alpha=0.3, lw=0.5)
    fig.tight_layout(pad=0.8)
    fig.subplots_adjust(bottom=0.16)
    return fig_to_rl_image(fig, width_mm, height_mm)


def make_energy_bar_graph(sim_result, profile, alert_config,
                          width_mm=170, height_mm=65):
    """エネルギー収支の横並び棒グラフ（スタート vs 到着時）"""
    e0    = profile.e_initial_kcal
    E_arr = sim_result["E_kcal"]
    ecrit = alert_config.e_crit_kcal if alert_config else 300
    e_end = max(E_arr[-1], 0)
    e_cons= e0 - e_end

    fig, ax = plt.subplots(figsize=(width_mm/25.4, height_mm/25.4))

    # x位置を数値で指定して棒の間隔を制御
    x     = [0.3, 0.7]   # 2本の棒のx位置（0〜1の範囲に収める）
    bw    = 0.18          # 棒の幅
    labels= ["出発時", "到着時"]

    # 出発時: 緑一色
    ax.bar(x[0], e0,    width=bw, color="#2ecc71", alpha=0.85, label="残エネルギー")
    # 到着時: 残量(緑) + 消費量(赤)を積み上げ
    ax.bar(x[1], e_end, width=bw, color="#2ecc71", alpha=0.85)
    ax.bar(x[1], e_cons,width=bw, bottom=e_end, color="#e74c3c", alpha=0.6, label="消費量")

    # 値のラベル
    ax.text(x[0], e0    + e0*0.02, f"{e0:.0f} kcal",  ha="center", fontsize=8, color="#2c3e50", fontweight="bold")
    ax.text(x[1], e_end + e0*0.02, f"{e_end:.0f} kcal", ha="center", fontsize=8, color="#2c3e50", fontweight="bold")
    ax.text(x[1], e_end/2,          f"-{e_cons:.0f}",   ha="center", fontsize=7, color="white", fontweight="bold")

    # 警告ライン
    ax.axhline(ecrit, color="#e74c3c", ls="--", lw=1.2, alpha=0.7)
    ax.text(x[1]+0.12, ecrit + e0*0.01, f"警告ライン {ecrit} kcal",
            color="#e74c3c", fontsize=6.5)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_xlim(0, 1.0)
    ax.set_ylabel("Energy (kcal)", fontsize=8)
    ax.tick_params(axis="y", labelsize=7)
    ax.set_ylim(0, e0 * 1.2)
    ax.legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.12),
              ncol=2, framealpha=1.0, edgecolor="#cccccc", fancybox=True)
    ax.grid(axis="y", alpha=0.3, lw=0.5)
    fig.tight_layout(pad=0.8)
    fig.subplots_adjust(bottom=0.18)
    return fig_to_rl_image(fig, width_mm, height_mm)


# ══════════════════════════════════════════════════════
# テーブル生成
# ══════════════════════════════════════════════════════

def kpi_table(kpis, styles):
    n   = len(kpis)
    cw  = [(W - 24*mm) / n] * n
    hdr = [Paragraph(k["label"], styles["kpi_lbl"]) for k in kpis]
    val = [Paragraph(k["value"], styles["kpi_val"]) for k in kpis]
    t   = Table([hdr, val], colWidths=cw)
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,0), C_LIGHT),
        ("BOX",          (0,0),(-1,-1), 0.5, C_GRAY),
        ("INNERGRID",    (0,0),(-1,-1), 0.3, C_GRAY),
        ("ALIGN",        (0,0),(-1,-1), "CENTER"),
        ("VALIGN",       (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING",   (0,0),(-1,-1), 4),
        ("BOTTOMPADDING",(0,0),(-1,-1), 4),
    ]))
    return t

def profile_table(profile, styles):
    act = {"hiking":"登山・ハイキング","running":"ランニング",
           "cycling":"サイクリング"}.get(profile.activity_type, profile.activity_type)
    rows = [
        ["体重",           f"{profile.body_weight_kg:.0f} kg",
         "荷物",           f"{profile.load_weight_kg:.0f} kg"],
        ["最大心拍数",     f"{profile.hr_max:.0f} bpm",
         "活動種別",       act],
        ["初期エネルギー", f"{profile.e_initial_kcal:.0f} kcal",
         "平均標高",       f"{profile.altitude_m:.0f} m"],
    ]
    cw = col_widths([0.25, 0.25, 0.25, 0.25])
    t  = Table([[Paragraph(c, styles["body"]) for c in r] for r in rows], colWidths=cw)
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(0,-1), C_LIGHT),
        ("BACKGROUND",   (2,0),(2,-1), C_LIGHT),
        ("BOX",          (0,0),(-1,-1), 0.5, C_GRAY),
        ("INNERGRID",    (0,0),(-1,-1), 0.3, C_GRAY),
        ("TOPPADDING",   (0,0),(-1,-1), 3),
        ("BOTTOMPADDING",(0,0),(-1,-1), 3),
        ("FONTNAME",     (0,0),(-1,-1), JP),
        ("FONTSIZE",     (0,0),(-1,-1), 9),
    ]))
    return t

def supply_table(supply_plan, food_master, styles):
    if not supply_plan:
        return Paragraph("補給計画が設定されていません。", styles["body"])
    hdr = ["順番","タイミング","食品","カロリー","吸収速度"]
    rows = [[Paragraph(h, styles["h2"]) for h in hdr]]
    for i, ev in enumerate(supply_plan):
        fname = next((n for n,v in food_master.items()
                      if abs(v["ka"]-ev.ka)<0.01), "補給食")
        speed = "速い ⚡" if ev.ka>=1.5 else ("普通 🔶" if ev.ka>=0.9 else "遅い 🐢")
        rows.append([
            Paragraph(f"第{i+1}回",        styles["body"]),
            Paragraph(fmt_min(ev.time_min), styles["body"]),
            Paragraph(fname,               styles["body"]),
            Paragraph(f"{ev.kcal:.0f} kcal", styles["body"]),
            Paragraph(speed,               styles["body"]),
        ])
    cw = col_widths([0.10, 0.20, 0.35, 0.20, 0.15])
    t  = Table(rows, colWidths=cw)
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,0), C_DARK),
        ("TEXTCOLOR",    (0,0),(-1,0), colors.white),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, C_LIGHT]),
        ("BOX",          (0,0),(-1,-1), 0.5, C_GRAY),
        ("INNERGRID",    (0,0),(-1,-1), 0.3, C_GRAY),
        ("ALIGN",        (0,0),(-1,-1), "CENTER"),
        ("VALIGN",       (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING",   (0,0),(-1,-1), 3),
        ("BOTTOMPADDING",(0,0),(-1,-1), 3),
        ("FONTNAME",     (0,0),(-1,-1), JP),
        ("FONTSIZE",     (0,0),(-1,-1), 9),
    ]))
    return t


# ══════════════════════════════════════════════════════
# PDF生成メイン
# ══════════════════════════════════════════════════════

def generate_report_pdf(
    sim_result   : dict,
    engine_a_out : dict,
    profile,
    supply_plan  : list,
    food_master  : dict,
    weather_data = None,
    alert_config = None,
    route_name   : str = "活動計画書",
) -> bytes:

    styles   = make_styles()
    buf      = io.BytesIO()
    now_str  = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    E_arr    = sim_result["E_kcal"]
    e0       = profile.e_initial_kcal
    eta      = engine_a_out["eta_minutes"]
    alerts   = sim_result.get("alert_times", [])
    ecrit    = alert_config.e_crit_kcal if alert_config else 300

    styles   = make_styles()
    buf      = io.BytesIO()
    now_str  = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    E_arr    = sim_result["E_kcal"]
    e0       = profile.e_initial_kcal
    eta      = engine_a_out["eta_minutes"]
    alerts   = sim_result.get("alert_times", [])
    ecrit    = alert_config.e_crit_kcal if alert_config else 300

    doc = SimpleDocTemplate(buf, pagesize=A4,
                            topMargin=10*mm, bottomMargin=10*mm,
                            leftMargin=12*mm, rightMargin=12*mm)
    story = []

    # ════════════════════════════════
    # PAGE 1: ヘッダー + KPI + E(t)グラフ + 補給計画
    # ════════════════════════════════

    # タイトル行
    story += [
        Paragraph(f"⛰️ {route_name}", styles["title"]),
        Paragraph(f"作成: {now_str}", styles["caption"]),
        HRFlowable(width="100%", thickness=2, color=C_GREEN, spaceAfter=3*mm),
    ]

    # KPI + プロフィール を横並び
    act = {"hiking":"登山","running":"ランニング","cycling":"サイクリング"}.get(
        profile.activity_type, profile.activity_type)
    kpis = [
        {"label": "到着予測",   "value": fmt_min(eta)},
        {"label": "残エネルギー","value": f"{E_arr[-1]:.0f} kcal"},
        {"label": "総消費量",   "value": f"{e0-E_arr[-1]:.0f} kcal"},
        {"label": "体重/荷物",  "value": f"{profile.body_weight_kg:.0f}/{profile.load_weight_kg:.0f} kg"},
        {"label": "活動種別",   "value": act},
    ]
    story += [kpi_table(kpis, styles), Spacer(1, 3*mm)]

    # 天気
    if weather_data:
        wf_pct = (weather_data.weather_factor - 1) * 100
        wrow = [[
            Paragraph("🌤️ 天気", styles["body"]),
            Paragraph(f"{weather_data.condition_ja}  {weather_data.temp_c:.1f}℃  "
                      f"湿度{weather_data.humidity_pct:.0f}%  "
                      f"風{weather_data.wind_speed_ms:.1f}m/s  "
                      f"気象補正 {wf_pct:+.0f}%", styles["body"]),
        ]]
        wt = Table(wrow, colWidths=col_widths([0.18, 0.82]))
        wt.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(0,-1),C_LIGHT),
            ("BOX",(0,0),(-1,-1),0.5,C_GRAY),
            ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
            ("FONTNAME",(0,0),(-1,-1),JP),("FONTSIZE",(0,0),(-1,-1),8),
        ]))
        story += [wt, Spacer(1, 3*mm)]

    # アラート
    if alerts:
        story.append(Paragraph(
            f"⚠️ ハンガーノック警告: {len(alerts)}回 "
            f"[{', '.join(fmt_min(a) for a in alerts)}]", styles["alert"]))
    else:
        story.append(Paragraph(
            f"✅ エネルギー安全（最小 {E_arr.min():.0f}kcal > 閾値 {ecrit:.0f}kcal）",
            styles["safe"]))
    story.append(Spacer(1, 2*mm))

    # E(t) グラフ（高さを小さく）
    story += [
        Paragraph("エネルギー残量の推移", styles["h2"]),
        make_energy_graph(sim_result, engine_a_out, profile, supply_plan,
                          alert_config, width_mm=172, height_mm=72),
        Spacer(1, 3*mm),
    ]

    # 補給計画テーブル
    if supply_plan:
        total_kcal = sum(ev.kcal for ev in supply_plan)
        story += [
            Paragraph(f"補給計画  {len(supply_plan)}回 / 合計 {total_kcal:.0f} kcal",
                      styles["h2"]),
            supply_table(supply_plan, food_master, styles),
        ]

    story.append(PageBreak())

    # ════════════════════════════════
    # PAGE 2: ペースカーブ + 斜度 + アドバイス
    # ════════════════════════════════

    story += [
        Paragraph("ペース配分 & 地形プロファイル", styles["h1"]),
        HRFlowable(width="100%", thickness=1, color=C_LIGHT, spaceAfter=2*mm),
        make_pace_graph(engine_a_out, width_mm=172, height_mm=72),
        Spacer(1, 3*mm),
    ]

    # 区間統計
    spd = engine_a_out["speed_curve"]
    slp = engine_a_out["slope_curve"]
    n3  = len(spd)
    thirds = max(n3 // 3, 1)
    stat_rows = [["区間", "平均ペース", "平均斜度", "備考"]]
    for label, sl, sr in [("前半",0,thirds),("中盤",thirds,thirds*2),("後半",thirds*2,n3)]:
        av = float(spd[sl:sr].mean()) if sr > sl else 0
        ag = float(slp[sl:sr].mean()) if sr > sl else 0
        note = "急登注意" if ag > 25 else ("下り注意" if ag < -20 else "—")
        stat_rows.append([label, f"{av*60:.0f} m/分", f"{ag:+.1f}%", note])
    cw2 = col_widths([0.20, 0.25, 0.25, 0.30])
    st2 = Table([[Paragraph(c, styles["body"]) for c in r] for r in stat_rows], colWidths=cw2)
    st2.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),C_DARK),("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,C_LIGHT]),
        ("BOX",(0,0),(-1,-1),0.5,C_GRAY),("INNERGRID",(0,0),(-1,-1),0.3,C_GRAY),
        ("ALIGN",(0,0),(-1,-1),"CENTER"),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
        ("FONTNAME",(0,0),(-1,-1),JP),("FONTSIZE",(0,0),(-1,-1),8),
    ]))
    story += [st2, Spacer(1, 4*mm)]

    # 補給タイムライン
    if supply_plan:
        story += [
            Paragraph("補給タイムライン", styles["h2"]),
        ]
        fig_tl, ax_tl = plt.subplots(figsize=(172/25.4, 28/25.4))
        ax_tl.barh([0], [eta], height=0.15, color="#ecf0f1", edgecolor="#95a5a6", lw=0.5)
        for ev in supply_plan:
            ax_tl.scatter([ev.time_min], [0], s=100, color="#3498db", zorder=5, marker="v")
            ax_tl.text(ev.time_min, 0.12, fmt_min(ev.time_min),
                       ha="center", va="bottom", fontsize=6, color="#2c3e50")
            ax_tl.text(ev.time_min, -0.12, f"{ev.kcal:.0f}kcal",
                       ha="center", va="top", fontsize=6, color="#3498db")
        ax_tl.axvline(eta, color="#2ecc71", lw=1.5)
        ax_tl.text(eta, 0.2, f"GOAL {fmt_min(eta)}", ha="center", fontsize=7, color="#27ae60")
        ax_tl.set_xlim(0, eta * 1.05)
        ax_tl.set_yticks([])
        ax_tl.set_xlabel("経過時間 (分)", fontsize=7)
        ax_tl.tick_params(labelsize=6)
        fig_tl.tight_layout(pad=0.3)
        fig_tl.subplots_adjust(bottom=0.32)
        story += [fig_to_rl_image(fig_tl, 172, 28), Spacer(1, 4*mm)]

    # アドバイス
    story += [
        Paragraph("アドバイス", styles["h1"]),
        HRFlowable(width="100%", thickness=1, color=C_LIGHT, spaceAfter=2*mm),
    ]
    advices = []
    if E_arr.min() < ecrit:
        advices.append("・補給量を増やすか出発前に十分な食事を摂ってください")
    if profile.altitude_m > 3000:
        advices.append("・高標高（3000m超）では食欲不振が起きやすいため、ジェル・ドリンクを優先してください")
    if weather_data and weather_data.temp_c < 5:
        advices.append("・低温環境では発汗が少なくても水分補給が必要です。温かい飲み物も持参してください")
    if not advices:
        advices.append("・計画通りに補給を実施してください。実際の体調・天候に合わせて柔軟に調整してください")
    for adv in advices:
        story.append(Paragraph(adv, styles["body"]))

    story += [
        Spacer(1, 4*mm),
        HRFlowable(width="100%", thickness=0.5, color=C_GRAY),
        Paragraph("Energy Tracker（PKモデルベース エネルギー残量予測ツール）", styles["small"]),
    ]

    doc.build(story)
    return buf.getvalue()
