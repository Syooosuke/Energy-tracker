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
from matplotlib.collections import LineCollection
from matplotlib import rcParams

# PIL の解凍爆弾チェックを無効化（自前生成の PNG なので安全）
try:
    from PIL import Image as _PILImage
    _PILImage.MAX_IMAGE_PIXELS = None
except ImportError:
    pass

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
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return RLImage(buf, width=width_mm * mm, height=height_mm * mm)


def _ds(arr, max_pts: int = 600):
    """配列をダウンサンプリングする（データ点が多い場合のみ）。"""
    arr = np.asarray(arr)
    if len(arr) <= max_pts:
        return arr
    idx = np.round(np.linspace(0, len(arr) - 1, max_pts)).astype(int)
    return arr[idx]

def col_widths(ratios, total_mm=None):
    total = (W - 24*mm) if total_mm is None else total_mm * mm
    return [total * r for r in ratios]


# ══════════════════════════════════════════════════════
# グラフ生成
# ══════════════════════════════════════════════════════

def make_energy_graph(sim_result, engine_a_out, profile, supply_events,
                      alert_config, width_mm=170, height_mm=80):
    """E(t) + 心拍数 の2軸グラフ（matplotlib）"""
    t    = _ds(sim_result["time_min"])
    E    = _ds(sim_result["E_kcal"])
    A    = _ds(sim_result["A_kcal"])
    HR   = _ds(engine_a_out["hr_predicted"])
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
    t     = _ds(np.arange(len(engine_a_out["speed_curve"])) * (10/60))
    spd   = _ds(engine_a_out["speed_curve"])
    slope = _ds(engine_a_out["slope_curve"])
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


def make_elevation_graph(elev_km, elev_m, slope_arr,
                         width_mm=170, height_mm=65):
    """標高プロファイル（斜度で色分けしたラインチャート）"""
    elev_km  = _ds(np.asarray(elev_km,  dtype=float))
    elev_m   = _ds(np.asarray(elev_m,   dtype=float))
    slope_ds = _ds(np.asarray(slope_arr, dtype=float))

    # slope_arr は elev_km より1要素少ないことがある → 補完
    n = len(elev_km)
    if len(slope_ds) < n:
        slope_ds = np.append(slope_ds, slope_ds[-1] if len(slope_ds) > 0 else 0)
    slope_ds = slope_ds[:n]

    fig, ax = plt.subplots(figsize=(width_mm/25.4, height_mm/25.4))

    # 斜度ごとに色を決定
    def _slope_color(s):
        if   s >= 20: return "#c0392b"   # 急登: 赤
        elif s >= 10: return "#e67e22"   # 中登: オレンジ
        elif s >=  0: return "#27ae60"   # 緩登: 緑
        elif s >= -10: return "#3498db"  # 緩下: 青
        else:          return "#8e44ad"  # 急下: 紫

    # LineCollection でセグメントごとに色付け
    points = np.array([elev_km, elev_m]).T.reshape(-1, 1, 2)
    segs   = np.concatenate([points[:-1], points[1:]], axis=1)
    seg_colors = [_slope_color(float(slope_ds[i])) for i in range(len(segs))]
    lc = LineCollection(segs, colors=seg_colors, linewidths=2.0, alpha=0.9)
    ax.add_collection(lc)

    # 塗りつぶし（薄いグレー）
    ax.fill_between(elev_km, elev_m, alpha=0.08, color="#7f8c8d")

    ax.set_xlim(elev_km[0], elev_km[-1])
    ax.set_ylim(max(0, elev_m.min() - 50), elev_m.max() + 80)
    ax.set_xlabel("距離 (km)", fontsize=7)
    ax.set_ylabel("標高 (m)",  fontsize=7)
    ax.tick_params(labelsize=6)
    ax.grid(axis="y", alpha=0.3, lw=0.5)

    # 凡例
    legend_elems = [
        mpatches.Patch(color="#c0392b", label="急登 ≥20%"),
        mpatches.Patch(color="#e67e22", label="中登 ≥10%"),
        mpatches.Patch(color="#27ae60", label="緩登 ≥0%"),
        mpatches.Patch(color="#3498db", label="緩下 <0%"),
        mpatches.Patch(color="#8e44ad", label="急下 <-10%"),
    ]
    ax.legend(handles=legend_elems, fontsize=5.5, loc="upper right",
              framealpha=0.85, ncol=5, edgecolor="#cccccc")

    fig.tight_layout(pad=0.6)
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
    elev_km      = None,
    elev_m       = None,
    slope_arr    = None,
) -> bytes:

    styles   = make_styles()
    buf      = io.BytesIO()
    now_str  = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    E_arr    = sim_result["E_kcal"]
    e0       = profile.e_initial_kcal
    eta      = engine_a_out["eta_minutes"]
    alerts   = sim_result.get("alert_times", [])
    ecrit    = alert_config.e_crit_kcal if alert_config else 300

    doc = SimpleDocTemplate(buf, pagesize=A4,
                            topMargin=14*mm, bottomMargin=14*mm,
                            leftMargin=12*mm, rightMargin=12*mm)
    story = []

    # ╔══════════════════════════════════════════╗
    # ║  PAGE 1: タイトル・プロフィール・環境   ║
    # ╚══════════════════════════════════════════╝
    story += [
        Spacer(1, 6*mm),
        Paragraph("⛰️ 活動計画書", styles["title"]),
        Paragraph(route_name, styles["subtitle"]),
        Paragraph(f"作成日時: {now_str}", styles["caption"]),
        HRFlowable(width="100%", thickness=2, color=C_GREEN, spaceAfter=5*mm),
    ]

    story += [Paragraph("👤 プロフィール", styles["h1"]),
              profile_table(profile, styles), Spacer(1, 4*mm)]

    # 環境補正
    env_rows = []
    if weather_data:
        env_rows += [
            ["天気", weather_data.condition_ja, "気温", f"{weather_data.temp_c:.1f}℃"],
            ["湿度", f"{weather_data.humidity_pct:.0f}%",
             "風速", f"{weather_data.wind_speed_ms:.1f} m/s"],
            ["気象補正", f"+{(weather_data.weather_factor-1)*100:.0f}%",
             "内訳", weather_data.factor_detail],
        ]
    if profile.altitude_m > 1500:
        from ke_estimator import calc_altitude_factor
        af = calc_altitude_factor(profile.altitude_m)
        env_rows.append(["高度補正", f"+{(af-1)*100:.0f}%",
                         "平均標高", f"{profile.altitude_m:.0f} m"])
    if env_rows:
        story.append(Paragraph("🌤️ 環境補正", styles["h1"]))
        cw = col_widths([0.25,0.25,0.25,0.25])
        et = Table([[Paragraph(c, styles["body"]) for c in r] for r in env_rows],
                   colWidths=cw)
        et.setStyle(TableStyle([
            ("BACKGROUND",   (0,0),(0,-1), C_LIGHT),
            ("BACKGROUND",   (2,0),(2,-1), C_LIGHT),
            ("BOX",          (0,0),(-1,-1), 0.5, C_GRAY),
            ("INNERGRID",    (0,0),(-1,-1), 0.3, C_GRAY),
            ("TOPPADDING",   (0,0),(-1,-1), 3),
            ("BOTTOMPADDING",(0,0),(-1,-1), 3),
            ("FONTNAME",     (0,0),(-1,-1), JP),
            ("FONTSIZE",     (0,0),(-1,-1), 9),
        ]))
        story += [et, Spacer(1, 4*mm)]

    story.append(PageBreak())

    # ╔══════════════════════════════════════════╗
    # ║  PAGE 2: E(t)グラフ・KPI               ║
    # ╚══════════════════════════════════════════╝
    story += [Paragraph("📊 シミュレーション結果", styles["h1"]),
              HRFlowable(width="100%", thickness=1, color=C_LIGHT, spaceAfter=3*mm)]

    kpis = [
        {"label": "到着予測時間",   "value": fmt_min(eta)},
        {"label": "残エネルギー",   "value": f"{E_arr[-1]:.0f} kcal"},
        {"label": "総消費量",       "value": f"{e0-E_arr[-1]:.0f} kcal"},
        {"label": "アラート",       "value": f"{len(alerts)} 回"},
    ]
    story += [kpi_table(kpis, styles), Spacer(1, 4*mm)]

    # アラート状況
    if alerts:
        alert_str = "、".join([fmt_min(a) for a in alerts])
        story.append(Paragraph(
            f"⚠️ ハンガーノック警告: {len(alerts)}回  [{alert_str}]",
            styles["alert"]))
    else:
        story.append(Paragraph(
            f"✅ エネルギーは安全に維持されます（最小: {E_arr.min():.0f} kcal > 閾値: {ecrit} kcal）",
            styles["safe"]))
    story.append(Spacer(1, 4*mm))

    # E(t)グラフ
    story += [
        Paragraph("エネルギー残量 & 心拍数の推移", styles["h2"]),
        make_energy_graph(sim_result, engine_a_out, profile, supply_plan,
                          alert_config, width_mm=170, height_mm=95),
        Spacer(1, 4*mm),
    ]



    story.append(PageBreak())

    # ╔══════════════════════════════════════════╗
    # ║  PAGE 3: ペースカーブ・斜度             ║
    # ╚══════════════════════════════════════════╝
    story += [
        Paragraph("🏃 最適ペースカーブ & 斜度プロファイル", styles["h1"]),
        HRFlowable(width="100%", thickness=1, color=C_LIGHT, spaceAfter=3*mm),
        make_pace_graph(engine_a_out, width_mm=170, height_mm=65),
        Spacer(1, 4*mm),
    ]

    # 標高プロファイル（ルートデータがある場合のみ）
    if elev_km is not None and elev_m is not None and slope_arr is not None:
        story += [
            Paragraph("標高プロファイル（斜度カラー）", styles["h2"]),
            make_elevation_graph(elev_km, elev_m, slope_arr, width_mm=170, height_mm=60),
            Spacer(1, 4*mm),
        ]

    # 区間統計テーブル
    spd = engine_a_out["speed_curve"]
    slp = engine_a_out["slope_curve"]
    n3  = len(spd)
    thirds = n3 // 3
    stat_rows = [["区間", "平均速度", "平均斜度", "推定ペース"]]
    for i, (label, sl, sr) in enumerate([
        ("前半",   0,       thirds),
        ("中盤",   thirds,  thirds*2),
        ("後半",   thirds*2, n3),
    ]):
        av = float(spd[sl:sr].mean())
        ag = float(slp[sl:sr].mean())
        pace = f"{3600/av/60:.1f} 分/km" if av > 0.5 else "-"
        stat_rows.append([label, f"{av:.2f} m/s", f"{ag:+.1f}%", pace])

    cw2 = col_widths([0.20, 0.25, 0.25, 0.30])
    st2 = Table([[Paragraph(c, styles["body"]) for c in r] for r in stat_rows],
                colWidths=cw2)
    st2.setStyle(TableStyle([
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
    story += [Paragraph("区間別統計", styles["h2"]), st2]

    story.append(PageBreak())

    # ╔══════════════════════════════════════════╗
    # ║  PAGE 4: 補給計画・アドバイス           ║
    # ╚══════════════════════════════════════════╝
    story += [
        Paragraph("🍙 補給計画", styles["h1"]),
        HRFlowable(width="100%", thickness=1, color=C_LIGHT, spaceAfter=3*mm),
    ]
    if supply_plan:
        total_kcal = sum(ev.kcal for ev in supply_plan)
        story += [
            Paragraph(f"補給回数: {len(supply_plan)}回  /  総補給量: {total_kcal:.0f} kcal",
                      styles["body"]),
            Spacer(1, 3*mm),
            supply_table(supply_plan, food_master, styles),
            Spacer(1, 4*mm),
        ]

        # ── 補給タイムライン（縦棒グラフ: x=補給時刻, y=補給カロリー）──
        fig_tl, ax_tl = plt.subplots(figsize=(170/25.4, 60/25.4))

        times  = [ev.time_min for ev in supply_plan]
        kcals  = [ev.kcal     for ev in supply_plan]
        bar_colors = ["#2ecc71" if ev.ka >= 1.5 else
                      ("#f39c12" if ev.ka >= 0.9 else "#3498db")
                      for ev in supply_plan]

        bars = ax_tl.bar(times, kcals, width=max(eta*0.04, 5),
                         color=bar_colors, alpha=0.85, edgecolor="white", lw=0.8)

        # 各棒にラベル（時刻 + kcal）
        for ev, bar, c in zip(supply_plan, bars, bar_colors):
            h = bar.get_height()
            ax_tl.text(bar.get_x() + bar.get_width()/2,
                       h + max(kcals)*0.03,
                       f"{fmt_min(ev.time_min)}\n{ev.kcal:.0f}kcal",
                       ha="center", va="bottom", fontsize=6.5, color="#2c3e50")

        # x軸の目盛りを時刻ラベルに
        ax_tl.set_xticks(times)
        ax_tl.set_xticklabels([fmt_min(t) for t in times],
                               fontsize=6.5, rotation=30, ha="right")
        ax_tl.set_xlim(-eta*0.05, eta*1.05)
        ax_tl.set_ylim(0, max(kcals) * 1.35)
        ax_tl.set_xlabel("経過時間", fontsize=8)
        ax_tl.set_ylabel("補給量 (kcal)", fontsize=8)
        ax_tl.set_title("補給タイミングと摂取カロリー", fontsize=9, pad=6)
        ax_tl.grid(axis="y", alpha=0.3, lw=0.5)
        ax_tl.tick_params(axis="y", labelsize=7)

        # ゴールの縦線
        ax_tl.axvline(eta, color="#2ecc71", lw=1.5, ls="--", alpha=0.8)
        ax_tl.text(eta, max(kcals)*1.25,
                   f"ゴール\n{fmt_min(eta)}", ha="center", fontsize=7,
                   color="#2ecc71", fontweight="bold")

        # 凡例（色の説明）
        legend_elements = [
            MplPatch(facecolor="#2ecc71", alpha=0.85, label="吸収速い (Ka≥1.5)"),
            MplPatch(facecolor="#f39c12", alpha=0.85, label="吸収普通 (Ka≥0.9)"),
            MplPatch(facecolor="#3498db", alpha=0.85, label="吸収遅い (Ka<0.9)"),
        ]
        ax_tl.legend(handles=legend_elements, fontsize=6.5,
                     loc="upper right", framealpha=1.0,
                     edgecolor="#cccccc", fancybox=True)

        fig_tl.tight_layout(pad=0.8)
        story += [
            Paragraph("補給タイミングと摂取カロリー", styles["h2"]),
            fig_to_rl_image(fig_tl, 170, 60),
            Spacer(1, 4*mm),
        ]
    else:
        story += [Paragraph("補給計画が設定されていません。", styles["body"]),
                  Spacer(1, 4*mm)]

    # アドバイス
    story += [
        Paragraph("💡 アドバイス", styles["h1"]),
        HRFlowable(width="100%", thickness=1, color=C_LIGHT, spaceAfter=3*mm),
    ]
    advices = []
    if E_arr.min() < ecrit:
        advices.append("・エネルギーが危険水準を下回ります。補給量を増やすか、出発前に十分な食事を摂ってください。")
    if supply_plan and len(supply_plan) > 1:
        intervals = [supply_plan[i+1].time_min - supply_plan[i].time_min
                     for i in range(len(supply_plan)-1)]
        if min(intervals) < 20:
            advices.append("・補給間隔が20分未満の箇所があります。少量頻回を意識してください。")
    if profile.altitude_m > 3000:
        advices.append("・高標高（3000m超）では食欲不振が起きやすいため、ジェル・ドリンクを優先してください。")
    if weather_data and weather_data.temp_c < 5:
        advices.append("・低温環境では発汗が少なくても水分補給が必要です。温かい飲み物も持参してください。")
    if not advices:
        advices.append("・設定した補給計画で安全にゴールできます。計画通りに補給を実施してください。")
    advices.append("・このシミュレーションは出発前の計画用です。実際の体調・天候に合わせて柔軟に調整してください。")
    for adv in advices:
        story.append(Paragraph(adv, styles["body"]))

    story += [
        Spacer(1, 6*mm),
        HRFlowable(width="100%", thickness=0.5, color=C_GRAY),
        Paragraph("本レポートは Energy Tracker（PKモデルベース エネルギー残量予測ツール）により生成されました。",
                  styles["small"]),
    ]

    doc.build(story)
    return buf.getvalue()
