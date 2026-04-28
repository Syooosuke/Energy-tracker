/**
 * EnergyDashboard.jsx
 *
 * 依存パッケージ:
 *   npm install lucide-react
 *   (Tailwind CSS は tailwind.config.js で設定済みであること)
 *
 * 使用方法:
 *   import EnergyDashboard from "./EnergyDashboard";
 *   export default function App() { return <EnergyDashboard />; }
 */

import { useState, useEffect } from "react";
import {
  Zap, Coffee, Brain, Clock, CheckCircle2, Activity,
  Flame, Timer, TrendingUp, ChevronRight, MoreHorizontal,
  Target, User, BellRing,
} from "lucide-react";

/* ─────────────────────────────────────────────────────
   MOCK DATA
───────────────────────────────────────────────────── */
const ENERGY_PCT  = 72;
const FOCUS_PCT   = 84;
const BREAK_MIN   = 23;
const SESSIONS    = 4;
const STREAK      = 7;
const NOW_HOUR    = 11.67; // 11:40

const CHART_DATA = [
  { h: 0,     e: 40 }, { h: 1,     e: 38 }, { h: 2,     e: 36 },
  { h: 3,     e: 34 }, { h: 4,     e: 37 }, { h: 5,     e: 46 },
  { h: 6,     e: 57 }, { h: 7,     e: 68 }, { h: 8,     e: 76 },
  { h: 9,     e: 83 }, { h: 10,    e: 86 }, { h: 11,    e: 80 },
  { h: 11.67, e: 72 }, // ← 現在地
  { h: 12,    e: 66 }, { h: 13,    e: 57 }, { h: 14,    e: 64 },
  { h: 15,    e: 58 }, { h: 16,    e: 50 }, { h: 17,    e: 44 },
  { h: 18,    e: 40 }, { h: 19,    e: 37 }, { h: 20,    e: 34 },
  { h: 21,    e: 32 }, { h: 22,    e: 30 }, { h: 23,    e: 28 },
];

const WORK_LOG = [
  { id:1, time:"09:00", task:"プロジェクト計画レビュー", status:"done",     dur:"45分",    tag:"思考",  eLevel:"high"   },
  { id:2, time:"09:50", task:"デザインシステム構築",     status:"done",     dur:"60分",    tag:"創造",  eLevel:"high"   },
  { id:3, time:"11:00", task:"チームMTG",               status:"done",     dur:"30分",    tag:"会議",  eLevel:"medium" },
  { id:4, time:"11:40", task:"UIコンポーネント実装",     status:"active",   dur:"進行中",  tag:"実装",  eLevel:"high"   },
  { id:5, time:"13:30", task:"コードレビュー",           status:"upcoming", dur:"予定45分", tag:"分析", eLevel:"medium" },
  { id:6, time:"15:00", task:"ドキュメント整備",         status:"upcoming", dur:"予定60分", tag:"整理", eLevel:"low"    },
];

/* ─────────────────────────────────────────────────────
   SHARED STYLE TOKENS
───────────────────────────────────────────────────── */
const glass      = "backdrop-blur-xl bg-white/[0.04] border border-white/[0.07]";
const glassHover = "transition-all duration-200 hover:bg-white/[0.07] hover:border-white/[0.12]";

/* ─────────────────────────────────────────────────────
   SUB-COMPONENT: Circular Energy Ring
───────────────────────────────────────────────────── */
function CircularEnergyRing({ pct = 72, size = 210 }) {
  const sw   = 14;
  const r    = (size - sw * 2) / 2;
  const cx   = size / 2;
  const cy   = size / 2;
  const circ = 2 * Math.PI * r;
  const off  = circ * (1 - pct / 100);

  return (
    <svg
      width={size}
      height={size}
      aria-label={`エネルギー残量 ${pct}%`}
      role="img"
    >
      <defs>
        {/* Arc gradient: cyan → indigo → violet */}
        <linearGradient id="ringGrad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%"   stopColor="#22d3ee" />
          <stop offset="55%"  stopColor="#818cf8" />
          <stop offset="100%" stopColor="#c084fc" />
        </linearGradient>

        {/* Glow filter for the arc */}
        <filter id="arcGlow" x="-30%" y="-30%" width="160%" height="160%">
          <feGaussianBlur in="SourceGraphic" stdDeviation="5" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      {/* Outer decoration ring (dashed) */}
      <circle
        cx={cx} cy={cy} r={r + 14}
        fill="none"
        stroke="rgba(255,255,255,0.025)"
        strokeWidth="1"
        strokeDasharray="3 7"
      />

      {/* Track */}
      <circle
        cx={cx} cy={cy} r={r}
        fill="none"
        stroke="rgba(255,255,255,0.06)"
        strokeWidth={sw}
      />

      {/* Progress arc */}
      <circle
        cx={cx} cy={cy} r={r}
        fill="none"
        stroke="url(#ringGrad)"
        strokeWidth={sw}
        strokeLinecap="round"
        strokeDasharray={circ}
        strokeDashoffset={off}
        transform={`rotate(-90 ${cx} ${cy})`}
        filter="url(#arcGlow)"
        style={{ transition: "stroke-dashoffset 1.2s cubic-bezier(0.4,0,0.2,1)" }}
      />
    </svg>
  );
}

/* ─────────────────────────────────────────────────────
   SUB-COMPONENT: Energy Forecast Chart (SVG)
───────────────────────────────────────────────────── */
function EnergyForecastChart() {
  const [tooltip, setTooltip] = useState(null); // { x, y, h, e }

  const VW = 600, VH = 130;
  const PL = 30, PR = 10, PT = 12, PB = 26;
  const W  = VW - PL - PR;
  const H  = VH - PT - PB;

  const tx  = (h) => PL + (h / 23) * W;
  const ty  = (e) => PT + H - (e / 100) * H;

  const past   = CHART_DATA.filter((d) => d.h <= NOW_HOUR);
  const future = CHART_DATA.filter((d) => d.h >= NOW_HOUR);

  // Find nearest data point to mouse x (in SVG coords)
  const handleMouseMove = (e) => {
    const svg   = e.currentTarget;
    const rect  = svg.getBoundingClientRect();
    const mx    = ((e.clientX - rect.left) / rect.width)  * VW;
    const hoverH = ((mx - PL) / W) * 23;
    // nearest data point
    const nearest = CHART_DATA.reduce((a, b) =>
      Math.abs(b.h - hoverH) < Math.abs(a.h - hoverH) ? b : a
    );
    setTooltip({ x: tx(nearest.h), y: ty(nearest.e), h: nearest.h, e: nearest.e });
  };

  const line = (pts) =>
    pts.map((d, i) => `${i === 0 ? "M" : "L"}${tx(d.h).toFixed(1)},${ty(d.e).toFixed(1)}`).join(" ");

  const area = (pts) => {
    const l  = line(pts);
    const lx = tx(pts.at(-1).h).toFixed(1);
    const fx = tx(pts[0].h).toFixed(1);
    const b  = (PT + H).toFixed(1);
    return `${l} L${lx},${b} L${fx},${b} Z`;
  };

  const nowX = tx(NOW_HOUR).toFixed(1);
  const nowY = ty(72).toFixed(1);

  return (
    <svg
      viewBox={`0 0 ${VW} ${VH}`}
      width="100%"
      preserveAspectRatio="xMidYMid meet"
      aria-hidden="true"
      onMouseMove={handleMouseMove}
      onMouseLeave={() => setTooltip(null)}
      style={{ cursor: "crosshair" }}
    >
      <defs>
        <linearGradient id="pastFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%"   stopColor="#22d3ee" stopOpacity="0.35" />
          <stop offset="100%" stopColor="#22d3ee" stopOpacity="0.01" />
        </linearGradient>
        <linearGradient id="futureFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%"   stopColor="#818cf8" stopOpacity="0.2"  />
          <stop offset="100%" stopColor="#818cf8" stopOpacity="0.01" />
        </linearGradient>
        <filter id="lineGlow" x="-20%" y="-50%" width="140%" height="200%">
          <feGaussianBlur stdDeviation="3" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
        <filter id="dotGlow" x="-100%" y="-100%" width="300%" height="300%">
          <feGaussianBlur stdDeviation="4" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      {/* Horizontal grid lines */}
      {[25, 50, 75, 100].map((g) => (
        <g key={g}>
          <line
            x1={PL} y1={ty(g)} x2={VW - PR} y2={ty(g)}
            stroke="rgba(255,255,255,0.05)"
            strokeWidth="1"
            strokeDasharray="3,5"
          />
          <text
            x={PL - 5} y={ty(g) + 3.5}
            fill="rgba(255,255,255,0.2)"
            fontSize="9"
            textAnchor="end"
            fontFamily="ui-monospace, monospace"
          >
            {g}
          </text>
        </g>
      ))}

      {/* Future: area then dashed line */}
      <path d={area(future)} fill="url(#futureFill)" />
      <path
        d={line(future)}
        fill="none"
        stroke="#818cf8"
        strokeWidth="1.8"
        strokeDasharray="6,3"
        opacity="0.6"
      />

      {/* Past: area then solid line (on top) */}
      <path d={area(past)} fill="url(#pastFill)" />
      <path
        d={line(past)}
        fill="none"
        stroke="#22d3ee"
        strokeWidth="2.5"
        filter="url(#lineGlow)"
      />

      {/* Now: vertical marker */}
      <line
        x1={nowX} y1={PT} x2={nowX} y2={PT + H}
        stroke="#f59e0b"
        strokeWidth="1.5"
        strokeDasharray="4,3"
        opacity="0.65"
      />

      {/* Now: animated dot */}
      <circle cx={nowX} cy={nowY} r="14" fill="#f59e0b" opacity="0.08" />
      <circle cx={nowX} cy={nowY} r="9"  fill="#f59e0b" opacity="0.18" />
      <circle cx={nowX} cy={nowY} r="5"  fill="#f59e0b" filter="url(#dotGlow)" />

      {/* "現在" label */}
      <text
        x={parseFloat(nowX) + 9} y={parseFloat(nowY) - 9}
        fill="#f59e0b"
        fontSize="9.5"
        fontWeight="600"
        fontFamily="ui-sans-serif, sans-serif"
      >
        現在
      </text>

      {/* X-axis hour labels */}
      {[0, 6, 12, 18, 23].map((h) => (
        <text
          key={h}
          x={tx(h)} y={VH - 5}
          fill="rgba(255,255,255,0.25)"
          fontSize="9"
          textAnchor="middle"
          fontFamily="ui-monospace, monospace"
        >
          {String(h).padStart(2, "0")}:00
        </text>
      ))}

      {/* ── Hover tooltip ── */}
      {tooltip && (() => {
        const tx2 = tooltip.x;
        const ty2 = tooltip.y;
        const isPast = tooltip.h <= NOW_HOUR;
        const bw = 72, bh = 34, br = 6;
        // Keep box inside viewBox
        const bx = Math.min(Math.max(tx2 - bw / 2, PL), VW - PR - bw);
        const by = ty2 - bh - 10 < PT ? ty2 + 10 : ty2 - bh - 10;
        const color = isPast ? "#22d3ee" : "#818cf8";
        const hStr = `${String(Math.floor(tooltip.h)).padStart(2,"0")}:${String(Math.round((tooltip.h % 1) * 60)).padStart(2,"0")}`;
        return (
          <g>
            {/* Vertical crosshair */}
            <line x1={tx2} y1={PT} x2={tx2} y2={PT + H}
              stroke={color} strokeWidth="1" opacity="0.3"/>
            {/* Dot highlight */}
            <circle cx={tx2} cy={ty2} r="4.5" fill={color} opacity="0.9"/>
            <circle cx={tx2} cy={ty2} r="8"   fill={color} opacity="0.15"/>
            {/* Tooltip box */}
            <rect x={bx} y={by} width={bw} height={bh} rx={br}
              fill="rgba(10,12,22,0.92)" stroke={color} strokeWidth="0.8" opacity="0.95"/>
            <text x={bx + bw/2} y={by + 13}
              fill={color} fontSize="11" fontWeight="700" textAnchor="middle"
              fontFamily="ui-monospace, monospace">
              {tooltip.e}%
            </text>
            <text x={bx + bw/2} y={by + 26}
              fill="rgba(255,255,255,0.4)" fontSize="9" textAnchor="middle"
              fontFamily="ui-monospace, monospace">
              {hStr}  {isPast ? "実績" : "予測"}
            </text>
          </g>
        );
      })()}
    </svg>
  );
}

/* ─────────────────────────────────────────────────────
   SUB-COMPONENT: Small Stat Card
───────────────────────────────────────────────────── */
function StatCard({ icon: Icon, label, value, unit, iconBg, iconGlow }) {
  return (
    <div className={`${glass} ${glassHover} rounded-2xl p-4 flex items-center gap-3.5 cursor-default`}>
      <div
        className={`w-10 h-10 rounded-xl flex items-center justify-center shrink-0 ${iconBg}`}
        style={{ boxShadow: iconGlow }}
      >
        <Icon size={17} className="text-white" />
      </div>
      <div className="min-w-0">
        <p className="text-[11px] text-slate-500 leading-none truncate">{label}</p>
        <p className="text-2xl font-bold text-white leading-none mt-1 tabular-nums">
          {value}
          <span className="text-xs font-normal text-slate-500 ml-0.5">{unit}</span>
        </p>
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB-COMPONENT: Work Log Row
───────────────────────────────────────────────────── */
function WorkLogRow({ item }) {
  const statusConfig = {
    done:     { Icon: CheckCircle2, dot: "bg-emerald-400", text: "text-slate-400 line-through decoration-slate-600/60", row: "" },
    active:   { Icon: Activity,     dot: "bg-cyan-400 animate-pulse", text: "text-white font-medium", row: "bg-cyan-500/[0.06] border !border-cyan-500/20" },
    upcoming: { Icon: Clock,        dot: "bg-slate-600", text: "text-slate-300", row: "" },
  };
  const eLevelConfig = {
    high:   "bg-emerald-500/15 text-emerald-400 ring-1 ring-emerald-500/20",
    medium: "bg-amber-500/15   text-amber-400   ring-1 ring-amber-500/20",
    low:    "bg-rose-500/15    text-rose-400    ring-1 ring-rose-500/20",
  };
  const { Icon, dot, text, row } = statusConfig[item.status];

  return (
    <div
      className={`flex items-center gap-4 px-4 py-3.5 rounded-2xl border border-transparent
        ${row} hover:bg-white/[0.03] hover:border-white/[0.06] transition-all duration-150 cursor-pointer group`}
    >
      {/* Timestamp */}
      <span className="text-xs text-slate-600 w-12 shrink-0 font-mono tabular-nums">{item.time}</span>

      {/* Status dot + icon */}
      <div className="flex items-center gap-2 shrink-0">
        <div className={`w-1.5 h-1.5 rounded-full ${dot}`} />
        <Icon size={14} className={
          item.status === "done" ? "text-emerald-500/60" :
          item.status === "active" ? "text-cyan-400" : "text-slate-600"
        } />
      </div>

      {/* Task name */}
      <p className={`flex-1 text-sm truncate ${text}`}>{item.task}</p>

      {/* Energy tag */}
      <span className={`text-[10px] px-2.5 py-1 rounded-full shrink-0 font-medium ${eLevelConfig[item.eLevel]}`}>
        {item.tag}
      </span>

      {/* Duration */}
      <span className={`text-xs shrink-0 font-mono tabular-nums ${
        item.status === "active" ? "text-cyan-400 font-semibold" : "text-slate-600"
      }`}>
        {item.dur}
      </span>

      {/* Hover arrow */}
      <ChevronRight
        size={13}
        className="text-slate-700 opacity-0 group-hover:opacity-100 group-hover:translate-x-0.5 transition-all duration-150 shrink-0"
      />
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   MAIN DASHBOARD COMPONENT
───────────────────────────────────────────────────── */
export default function EnergyDashboard() {
  const [now, setNow] = useState(new Date());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  const timeStr = now.toLocaleTimeString("ja-JP", {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const dateStr = now.toLocaleDateString("ja-JP", {
    year: "numeric", month: "long", day: "numeric", weekday: "short",
  });

  return (
    <div
      className="min-h-screen bg-[#060912] text-white antialiased"
      style={{ fontFamily: "'Inter', 'Noto Sans JP', ui-sans-serif, sans-serif" }}
    >
      {/* ── Ambient background blobs ── */}
      <div
        className="fixed inset-0 overflow-hidden pointer-events-none select-none"
        aria-hidden="true"
      >
        <div
          className="absolute -top-40 -left-40 w-[700px] h-[700px] rounded-full"
          style={{ background: "radial-gradient(circle, rgba(34,211,238,0.045) 0%, transparent 65%)" }}
        />
        <div
          className="absolute top-1/2 -right-56 w-[600px] h-[600px] rounded-full"
          style={{ background: "radial-gradient(circle, rgba(129,140,248,0.04) 0%, transparent 65%)" }}
        />
        <div
          className="absolute -bottom-40 left-[30%] w-[500px] h-[500px] rounded-full"
          style={{ background: "radial-gradient(circle, rgba(192,132,252,0.03) 0%, transparent 65%)" }}
        />
      </div>

      {/* ── Page content ── */}
      <div className="relative z-10 max-w-[1280px] mx-auto px-6 py-8">

        {/* ════ HEADER ════ */}
        <header className="flex items-center justify-between mb-10">
          {/* Logo + title */}
          <div className="flex items-center gap-3">
            <div
              className="w-10 h-10 rounded-2xl bg-gradient-to-br from-cyan-500 to-indigo-500 flex items-center justify-center shrink-0"
              style={{ boxShadow: "0 0 24px rgba(34,211,238,0.35)" }}
            >
              <Zap size={19} className="text-white" />
            </div>
            <div>
              <h1 className="text-base font-bold text-white leading-none tracking-tight">Energy OS</h1>
              <p className="text-[11px] text-slate-600 leading-none mt-0.5">
                パーソナル・パフォーマンスダッシュボード
              </p>
            </div>
          </div>

          {/* Right side controls */}
          <div className="flex items-center gap-3">
            {/* Live clock */}
            <div className="text-right hidden sm:block mr-1">
              <p className="text-[11px] text-slate-600">{dateStr}</p>
              <p className="text-base font-mono font-semibold text-slate-200 tracking-widest leading-none mt-0.5">
                {timeStr}
              </p>
            </div>

            {/* Status badge */}
            <div
              className={`${glass} rounded-2xl px-4 py-2 flex items-center gap-2`}
            >
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
              <span className="text-xs text-slate-400">モニタリング中</span>
            </div>

            {/* Alert bell */}
            <button
              className={`${glass} ${glassHover} w-9 h-9 rounded-xl flex items-center justify-center relative`}
              aria-label="通知"
            >
              <BellRing size={15} className="text-slate-400" />
              <span className="absolute top-1.5 right-1.5 w-1.5 h-1.5 rounded-full bg-amber-400" />
            </button>

            {/* Avatar */}
            <button
              className={`${glass} ${glassHover} w-9 h-9 rounded-xl flex items-center justify-center`}
              aria-label="プロフィール"
            >
              <User size={15} className="text-slate-400" />
            </button>
          </div>
        </header>

        {/* ════ MAIN 3-COLUMN GRID ════ */}
        <div className="grid grid-cols-12 gap-5 lg:gap-6">

          {/* ── LEFT PANEL (col 1–4): Energy Ring + Stats ── */}
          <div className="col-span-12 lg:col-span-4 flex flex-col gap-5">

            {/* Energy Ring Card */}
            <div className={`${glass} rounded-3xl p-6 relative overflow-hidden flex-1`}>
              {/* Top shimmer line */}
              <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-cyan-400/30 to-transparent" />

              {/* Card header */}
              <div className="flex items-center justify-between mb-4">
                <div>
                  <p className="text-[10px] font-semibold text-slate-500 uppercase tracking-widest">
                    エネルギー残量
                  </p>
                  <p className="text-sm text-slate-400 mt-0.5">推定現在値</p>
                </div>
                <span
                  className="text-xs px-3 py-1.5 rounded-xl font-semibold"
                  style={{
                    background: "rgba(34,211,238,0.12)",
                    color: "#22d3ee",
                    border: "1px solid rgba(34,211,238,0.2)",
                  }}
                >
                  良好
                </span>
              </div>

              {/* Ring + center overlay */}
              <div className="relative flex items-center justify-center py-2">
                <CircularEnergyRing pct={ENERGY_PCT} size={210} />

                {/* Center text (absolute over ring) */}
                <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
                  <span
                    className="text-[52px] font-black leading-none tabular-nums"
                    style={{
                      background: "linear-gradient(135deg, #e0f7ff 30%, #a5b4fc)",
                      WebkitBackgroundClip: "text",
                      WebkitTextFillColor: "transparent",
                    }}
                  >
                    {ENERGY_PCT}
                  </span>
                  <span className="text-base text-cyan-400/80 font-semibold -mt-1">%</span>
                  <span className="text-[11px] text-slate-600 mt-2 tracking-wide">推定エネルギー残量</span>
                </div>
              </div>

              {/* Sub-stats row */}
              <div className="mt-4 grid grid-cols-3 gap-1 pt-4 border-t border-white/[0.06]">
                {[
                  { label: "本日ピーク", val: "86%", color: "text-emerald-400" },
                  { label: "現在",       val: `${ENERGY_PCT}%`, color: "text-cyan-400",   bold: true },
                  { label: "深夜予測",   val: "28%", color: "text-slate-500" },
                ].map((s) => (
                  <div key={s.label} className="text-center">
                    <p className={`text-lg font-bold tabular-nums ${s.color} ${s.bold ? "drop-shadow-[0_0_8px_rgba(34,211,238,0.5)]" : ""}`}>
                      {s.val}
                    </p>
                    <p className="text-[10px] text-slate-600 mt-0.5">{s.label}</p>
                  </div>
                ))}
              </div>
            </div>

            {/* 2×2 Stat Cards */}
            <div className="grid grid-cols-2 gap-3">
              <StatCard
                icon={Brain} label="集中スコア" value={FOCUS_PCT} unit="%"
                iconBg="bg-indigo-500/25"
                iconGlow="0 0 16px rgba(129,140,248,0.3)"
              />
              <StatCard
                icon={Flame} label="継続ストリーク" value={STREAK} unit="日"
                iconBg="bg-orange-500/25"
                iconGlow="0 0 16px rgba(251,146,60,0.3)"
              />
              <StatCard
                icon={Target} label="本日セッション" value={SESSIONS} unit="回"
                iconBg="bg-emerald-500/25"
                iconGlow="0 0 16px rgba(52,211,153,0.3)"
              />
              <StatCard
                icon={TrendingUp} label="効率スコア" value="91" unit="%"
                iconBg="bg-purple-500/25"
                iconGlow="0 0 16px rgba(192,132,252,0.3)"
              />
            </div>
          </div>

          {/* ── RIGHT PANEL (col 5–12): Alert + Chart ── */}
          <div className="col-span-12 lg:col-span-8 flex flex-col gap-5">

            {/* ── Break Alert Card ── */}
            <div
              className="rounded-3xl p-6 relative overflow-hidden"
              style={{
                background: "linear-gradient(135deg, rgba(245,158,11,0.11) 0%, rgba(251,191,36,0.05) 100%)",
                border: "1px solid rgba(245,158,11,0.22)",
                boxShadow: "0 0 50px rgba(245,158,11,0.07), inset 0 1px 0 rgba(255,255,255,0.05)",
              }}
            >
              {/* Top shimmer */}
              <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-amber-400/50 to-transparent" />

              <div className="flex items-center gap-5">
                {/* Icon */}
                <div
                  className="w-16 h-16 rounded-2xl flex items-center justify-center shrink-0"
                  style={{
                    background: "rgba(245,158,11,0.18)",
                    boxShadow: "0 0 24px rgba(245,158,11,0.25)",
                  }}
                >
                  <Coffee size={28} className="text-amber-400" />
                </div>

                {/* Text */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1.5">
                    <span className="text-[10px] font-bold uppercase tracking-widest text-amber-500">
                      休憩アラート
                    </span>
                    <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
                  </div>
                  <p className="text-white font-semibold text-lg leading-snug">
                    最適な休憩タイミングまで
                  </p>
                  <p className="text-slate-400 text-sm mt-0.5 truncate">
                    集中セッション開始から 85分 経過 · 認知パフォーマンスの低下が予測されます
                  </p>
                </div>

                {/* Countdown */}
                <div className="text-right shrink-0">
                  <div className="flex items-baseline gap-1 justify-end">
                    <span
                      className="text-[56px] font-black leading-none tabular-nums"
                      style={{
                        background: "linear-gradient(135deg, #fbbf24, #f97316)",
                        WebkitBackgroundClip: "text",
                        WebkitTextFillColor: "transparent",
                        filter: "drop-shadow(0 0 12px rgba(251,191,36,0.4))",
                      }}
                    >
                      {BREAK_MIN}
                    </span>
                    <span className="text-xl text-amber-500 font-semibold">分後</span>
                  </div>
                  <div className="flex items-center gap-1.5 justify-end mt-0.5">
                    <Timer size={11} className="text-amber-600" />
                    <span className="text-xs text-amber-600">12:03 推奨休憩</span>
                  </div>
                </div>
              </div>

              {/* Progress bar: session elapsed */}
              <div className="mt-5">
                <div className="h-1.5 w-full rounded-full overflow-hidden bg-white/[0.06]">
                  <div
                    className="h-full rounded-full"
                    style={{
                      width: `${(85 / 108) * 100}%`,
                      background: "linear-gradient(90deg, #f59e0b, #fbbf24)",
                      boxShadow: "0 0 10px rgba(251,191,36,0.5)",
                    }}
                  />
                </div>
                <div className="flex justify-between mt-1.5">
                  <span className="text-[10px] text-slate-700">セッション開始</span>
                  <span className="text-[10px] text-amber-700">85 / 108 分</span>
                </div>
              </div>
            </div>

            {/* ── Energy Forecast Chart Card ── */}
            <div className={`${glass} rounded-3xl p-6 relative overflow-hidden flex-1`}>
              {/* Top shimmer */}
              <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-indigo-400/20 to-transparent" />

              {/* Chart header */}
              <div className="flex items-center justify-between mb-5">
                <div>
                  <p className="text-[10px] font-semibold text-slate-500 uppercase tracking-widest">
                    エネルギー予測推移
                  </p>
                  <p className="text-sm text-slate-300 mt-0.5">本日 0:00 – 24:00</p>
                </div>

                {/* Legend */}
                <div className="flex items-center gap-5 text-[11px] text-slate-500">
                  <span className="flex items-center gap-1.5">
                    <span className="w-5 h-[2px] rounded-full bg-cyan-400 inline-block" />
                    実績
                  </span>
                  <span className="flex items-center gap-1.5">
                    {/* SVG dashed line for legend */}
                    <svg width="20" height="2" aria-hidden="true">
                      <line x1="0" y1="1" x2="20" y2="1"
                        stroke="#818cf8" strokeWidth="2"
                        strokeDasharray="4,3" opacity="0.7"/>
                    </svg>
                    予測
                  </span>
                  <span className="flex items-center gap-1.5">
                    <span className="w-2.5 h-2.5 rounded-full bg-amber-400 inline-block" />
                    現在
                  </span>
                </div>
              </div>

              {/* SVG Chart */}
              <EnergyForecastChart />
            </div>
          </div>

          {/* ════ WORK LOG (full width) ════ */}
          <div className="col-span-12">
            <div className={`${glass} rounded-3xl p-6 relative overflow-hidden`}>
              {/* Top shimmer */}
              <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-white/10 to-transparent" />

              {/* Log header */}
              <div className="flex items-center justify-between mb-5">
                <div>
                  <p className="text-[10px] font-semibold text-slate-500 uppercase tracking-widest">
                    本日の作業ログ
                  </p>
                  <p className="text-sm text-slate-300 mt-0.5">タスクとエネルギー消費の記録</p>
                </div>

                <div className="flex items-center gap-2.5">
                  {/* Progress badge */}
                  <div
                    className={`${glass} rounded-xl px-3.5 py-2 flex items-center gap-2`}
                  >
                    <Activity size={13} className="text-cyan-400" />
                    <span className="text-xs text-slate-400">
                      <span className="text-white font-semibold">4</span>
                      <span className="text-slate-600"> / 6</span>
                      <span className="ml-1">完了</span>
                    </span>
                  </div>

                  {/* More button */}
                  <button
                    className={`${glass} ${glassHover} w-9 h-9 rounded-xl flex items-center justify-center`}
                    aria-label="その他のオプション"
                  >
                    <MoreHorizontal size={15} className="text-slate-500" />
                  </button>
                </div>
              </div>

              {/* Log rows */}
              <div className="space-y-1">
                {WORK_LOG.map((item) => (
                  <WorkLogRow key={item.id} item={item} />
                ))}
              </div>
            </div>
          </div>
        </div>

        {/* ── Footer ── */}
        <footer className="mt-8 flex items-center justify-between text-[11px] text-slate-800">
          <span>Energy OS v2.0 · リアルタイム更新</span>
          <span>© 2025 Performance Analytics</span>
        </footer>
      </div>
    </div>
  );
}
