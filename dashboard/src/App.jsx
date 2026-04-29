/**
 * App.jsx — Energy Tracker (React + FastAPI)
 * アクティビティ別エネルギー補給予測アプリ
 */

import { useState, useEffect, useRef, useCallback, createContext, useContext, Component } from "react";
import { MapContainer, TileLayer, Polyline, Marker, Popup, useMap } from "react-leaflet";
import "leaflet/dist/leaflet.css";
import L from "leaflet";
import {
  Mountain, Zap, Clock, TrendingUp, AlertTriangle, CheckCircle2,
  ChevronRight, ChevronLeft, Search, Upload, Plus, Minus,
  Thermometer, Wind, Droplets, MapPin, Activity, Coffee,
  BarChart2, Route, Settings, Play, Loader2,
  Shield, ShieldAlert, TrendingDown, HeartPulse, Gauge, Brain,
  Moon, Dumbbell, Award, RefreshCw, Heart, Watch, Smartphone,
  Bot, Flag, Calendar, Target, ChevronDown, Sun,
  Mic, MicOff, Share2, BookOpen, Layers, Crosshair, Stethoscope,
} from "lucide-react";
import {
  uploadGarminFit, uploadAppleHealth,
  getPersonalModels, getRecovery, getTrainingLoad, getVo2maxTrend,
  getActivityCalendar, getDashboard,
  // 新機能
  getSupplyTimingAlerts, calcHydration,
  generateReport, getAvailablePeriods,
  getMyRoutes, getRouteHistory, compareRoutes,
  getPeerComparison,
  getFeatureImportance,
  createFeedback, getFeedbackHistory, getFeedbackSummary, deleteFeedback,
  // 新機能
  aiQuery, simulateRace, createTrainingPlan, listTrainingPlans, getTrainingPlan, deleteTrainingPlan,
  // 追加機能
  compareRouteCandidates, adjustTrainingWeek, submitRaceResult,
  getInjuryRisk, getWeatherPerformance, getGoalTimeProbability, getTimeOfDayPerformance,
  saveRouteToLibrary, listRouteLibrary, getRouteFromLibrary, deleteRouteFromLibrary,
  getRoutePOI, createSharedRoute, getSharedRoute,
  generateRaceStrategy, parseActivityText, getWeeklyCoaching,
} from "./api.js";
import {
  searchPlace, getRoute, uploadGPX, runSimulation, getFoods,
  register, login, logout, getMe, optimizeSupply,
} from "./api.js";

// Fix Leaflet default icon
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon-2x.png",
  iconUrl:       "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon.png",
  shadowUrl:     "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-shadow.png",
});

/* ─────────────────────────────────────────────────────
   STYLE TOKENS
───────────────────────────────────────────────────── */
const glass      = "backdrop-blur-xl bg-white/[0.04] border border-white/[0.07]";
const glassHover = "transition-all duration-200 hover:bg-white/[0.07] hover:border-white/[0.12]";

/** FastAPI のエラーレスポンス (detail が string | object[] のどちらでも) を表示可能な文字列に変換する */
const apiErr = (e, fallback = "エラーが発生しました") => {
  const detail = e?.response?.data?.detail;
  if (!detail) return e?.message || fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(d => d.msg ?? JSON.stringify(d)).join(" / ");
  return fallback;
};

/** パネル単位のエラーバウンダリー — クラッシュしても画面全体は真っ暗にならない */
class PanelErrorBoundary extends Component {
  constructor(props) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  render() {
    if (this.state.error) {
      return (
        <div className="backdrop-blur-xl bg-white/[0.04] border border-red-500/30 rounded-2xl p-5">
          <p className="text-xs text-red-400 font-semibold mb-1">表示エラー</p>
          <p className="text-[11px] text-white/40">{this.state.error?.message || "予期しないエラーが発生しました"}</p>
          <button onClick={() => this.setState({ error: null })}
            className="mt-3 text-[11px] text-white/50 underline">再試行</button>
        </div>
      );
    }
    return this.props.children;
  }
}

// バックエンド再起動を検知して自動リロードする hook
function useBackendReloadDetector(intervalMs = 3000) {
  const bootTimeRef = useRef(null);
  useEffect(() => {
    const check = async () => {
      try {
        const res = await fetch("/api/boot-time");
        if (!res.ok) return;
        const { boot_time } = await res.json();
        if (bootTimeRef.current === null) {
          bootTimeRef.current = boot_time;
        } else if (bootTimeRef.current !== boot_time) {
          window.location.reload();
        }
      } catch {
        // バックエンド停止中は無視
      }
    };
    const id = setInterval(check, intervalMs);
    check();
    return () => clearInterval(id);
  }, [intervalMs]);
}

// テーマ切替 hook（localStorage 永続化）
function useTheme() {
  const [theme, setTheme] = useState(() => localStorage.getItem("et_theme") || "dark");
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("et_theme", theme);
  }, [theme]);
  const toggle = () => setTheme(t => t === "dark" ? "light" : "dark");
  return [theme, toggle];
}

const ACTIVITIES = [
  { key: "🏔️ 登山・ハイキング",   icon: "🏔️", label: "登山・ハイキング",   desc: "Pandolf式でエネルギー計算" },
  { key: "🏃 ランニング",           icon: "🏃", label: "ランニング",           desc: "Minetti式でエネルギー計算" },
  { key: "🌲 トレイルランニング",   icon: "🌲", label: "トレイルランニング",   desc: "山道ランニング" },
  { key: "🚴 サイクリング",         icon: "🚴", label: "サイクリング",         desc: "空気抵抗+転がり抵抗モデル" },
  { key: "🚵 マウンテンバイク",     icon: "🚵", label: "マウンテンバイク",     desc: "MTB向け補正あり" },
];

const FOOD_DEFAULTS = [
  { time_min: 30,  food: "エネルギージェル", qty: 1 },
  { time_min: 90,  food: "おにぎり (梅)",    qty: 1 },
  { time_min: 150, food: "バナナ",           qty: 1 },
];

const DEFAULT_PROFILE = {
  body_weight: 65, load_weight: 8, e_initial: 1800,
  hr_max: 180, terrain_factor: 1.2, lt_ratio: 0.85,
  e_crit: 300, t_warn: 25,
};

/* ─────────────────────────────────────────────────────
   SUB: AuthScreen  (ログイン / 新規登録)
───────────────────────────────────────────────────── */
function AuthScreen({ onAuth }) {
  const [mode,        setMode]        = useState("login");   // "login" | "register"
  const [email,       setEmail]       = useState("");
  const [password,    setPassword]    = useState("");
  const [displayName, setDisplayName] = useState("");
  const [loading,     setLoading]     = useState(false);
  const [error,       setError]       = useState("");

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(""); setLoading(true);
    try {
      let data;
      if (mode === "login") {
        data = await login(email, password);
      } else {
        if (password.length < 8) { setError("パスワードは8文字以上にしてください"); return; }
        data = await register(email, password, displayName);
      }
      onAuth(data.user);
    } catch (err) {
      const msg = err.response?.data?.detail || err.message;
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen font-sans flex items-center justify-center relative overflow-hidden" style={{ backgroundColor: "var(--bg-primary)", color: "var(--text-primary)" }}>
      {/* Ambient blobs */}
      <div className="pointer-events-none fixed inset-0 overflow-hidden">
        <div className="absolute top-[-15%] left-[-5%] w-[45vw] h-[45vw] rounded-full bg-indigo-600/[0.06] blur-[100px]" />
        <div className="absolute bottom-[-10%] right-[-5%] w-[40vw] h-[40vw] rounded-full bg-violet-600/[0.05] blur-[80px]" />
      </div>

      <div className="relative z-10 w-full max-w-sm px-4">
        {/* ロゴ */}
        <div className="text-center mb-8">
          <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-cyan-500/30 to-indigo-500/30 border border-white/10 flex items-center justify-center mx-auto mb-4">
            <Zap size={24} className="text-cyan-400" />
          </div>
          <h1 className="text-2xl font-bold tracking-tight">Energy Tracker</h1>
          <p className="text-xs text-white/40 mt-1">エネルギー補給予測アプリ</p>
        </div>

        {/* タブ */}
        <div className={`${glass} flex rounded-xl p-1 gap-1 mb-5`}>
          {[["login", "ログイン"], ["register", "新規登録"]].map(([m, label]) => (
            <button key={m} onClick={() => { setMode(m); setError(""); }}
              className={`flex-1 py-2 rounded-lg text-sm font-medium transition-all ${
                mode === m
                  ? "bg-indigo-500/25 border border-indigo-400/40 text-indigo-300"
                  : "text-white/40 hover:text-white/60"
              }`}>
              {label}
            </button>
          ))}
        </div>

        {/* フォーム */}
        <form onSubmit={handleSubmit} className={`${glass} rounded-2xl p-6 space-y-4`}>
          {mode === "register" && (
            <div>
              <label className="text-xs text-white/50 block mb-1.5">表示名（任意）</label>
              <input
                type="text" value={displayName} onChange={e => setDisplayName(e.target.value)}
                placeholder="山田 太郎"
                className={`${glass} w-full px-3 py-2.5 rounded-xl text-sm text-white/80 placeholder-white/25 outline-none focus:border-indigo-400/50`}
              />
            </div>
          )}
          <div>
            <label className="text-xs text-white/50 block mb-1.5">メールアドレス</label>
            <input
              type="email" value={email} onChange={e => setEmail(e.target.value)}
              placeholder="you@example.com" required
              className={`${glass} w-full px-3 py-2.5 rounded-xl text-sm text-white/80 placeholder-white/25 outline-none focus:border-indigo-400/50`}
            />
          </div>
          <div>
            <label className="text-xs text-white/50 block mb-1.5">
              パスワード{mode === "register" && <span className="text-white/30 ml-1">（8文字以上）</span>}
            </label>
            <input
              type="password" value={password} onChange={e => setPassword(e.target.value)}
              placeholder="••••••••" required minLength={mode === "register" ? 8 : 1}
              className={`${glass} w-full px-3 py-2.5 rounded-xl text-sm text-white/80 placeholder-white/25 outline-none focus:border-indigo-400/50`}
            />
          </div>

          {error && (
            <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">
              {error}
            </div>
          )}

          <button
            type="submit" disabled={loading}
            className="w-full py-2.5 rounded-xl bg-gradient-to-r from-cyan-500/25 to-indigo-500/25 border border-cyan-400/30 text-cyan-300 hover:from-cyan-500/35 hover:to-indigo-500/35 transition-all font-semibold text-sm flex items-center justify-center gap-2 disabled:opacity-50"
          >
            {loading
              ? <><Loader2 size={14} className="animate-spin" />処理中...</>
              : mode === "login" ? "ログイン" : "アカウントを作成"
            }
          </button>
        </form>
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: StepIndicator
───────────────────────────────────────────────────── */
function StepIndicator({ current }) {
  const steps = ["アクティビティ", "ルート", "プロフィール", "結果"];
  return (
    <div className="flex items-center justify-center gap-0 mb-6 sm:mb-8">
      {steps.map((s, i) => {
        const n = i + 1;
        const done = n < current;
        const active = n === current;
        return (
          <div key={n} className="flex items-center">
            <div className="flex flex-col items-center">
              <div className={`w-7 h-7 sm:w-8 sm:h-8 rounded-full flex items-center justify-center text-xs font-bold transition-all ${
                done   ? "bg-cyan-500/30 border border-cyan-500/60 text-cyan-400" :
                active ? "bg-indigo-500/30 border border-indigo-400/60 text-indigo-300" :
                         "bg-white/[0.04] border border-white/10 text-white/30"
              }`}>
                {done ? "✓" : n}
              </div>
              <span className={`hidden sm:block text-[10px] mt-1 font-medium ${
                active ? "text-indigo-300" : done ? "text-cyan-400/70" : "text-white/25"
              }`}>{s}</span>
            </div>
            {i < steps.length - 1 && (
              <div className={`w-5 sm:w-12 h-px mb-0 sm:mb-4 mx-0.5 sm:mx-1 ${done ? "bg-cyan-500/40" : "bg-white/[0.08]"}`} />
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: ActivityCard
───────────────────────────────────────────────────── */
function ActivityCard({ act, selected, onClick }) {
  return (
    <button
      onClick={onClick}
      className={`${glass} ${glassHover} rounded-2xl p-4 text-left transition-all ${
        selected
          ? "border-indigo-400/50 bg-indigo-500/10 shadow-[0_0_20px_rgba(99,102,241,0.15)]"
          : ""
      }`}
    >
      <div className="text-2xl mb-2">{act.icon}</div>
      <div className="text-sm font-semibold text-white/90">{act.label}</div>
      <div className="text-[11px] text-white/40 mt-0.5">{act.desc}</div>
      {selected && (
        <div className="mt-2 w-2 h-2 rounded-full bg-indigo-400 shadow-[0_0_6px_#818cf8]" />
      )}
    </button>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: PlaceSearch
───────────────────────────────────────────────────── */
function PlaceSearch({ label, icon: Icon, value, onSelect }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);

  const doSearch = async () => {
    if (!query.trim()) return;
    setLoading(true);
    try {
      const rs = await searchPlace(query.trim());
      setResults(rs);
    } finally {
      setLoading(false);
    }
  };

  const handleKey = (e) => { if (e.key === "Enter") doSearch(); };

  return (
    <div className="space-y-2">
      <label className="text-xs text-white/50 font-medium flex items-center gap-1.5">
        <Icon size={12} /> {label}
      </label>
      {value ? (
        <div className="flex items-center justify-between px-3 py-2 rounded-xl bg-cyan-500/10 border border-cyan-500/30">
          <span className="text-sm text-cyan-300 truncate">{value.name}</span>
          <button onClick={() => { onSelect(null); setResults([]); }} className="text-white/30 hover:text-white/70 ml-2 shrink-0">✕</button>
        </div>
      ) : (
        <>
          <div className="flex gap-2">
            <input
              value={query}
              onChange={e => setQuery(e.target.value)}
              onKeyDown={handleKey}
              placeholder="例: 富士山五合目"
              className={`${glass} flex-1 px-3 py-2 rounded-xl text-sm text-white/80 placeholder-white/25 outline-none focus:border-indigo-400/50 focus:bg-white/[0.06]`}
            />
            <button
              onClick={doSearch}
              disabled={loading}
              className="px-3 py-2 rounded-xl bg-indigo-500/20 border border-indigo-400/30 text-indigo-300 hover:bg-indigo-500/30 transition-colors disabled:opacity-50"
            >
              {loading ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}
            </button>
          </div>
          {results.length > 0 && (
            <div className={`${glass} rounded-xl overflow-hidden`}>
              {results.map((r, i) => (
                <button
                  key={i}
                  onClick={() => { onSelect(r); setResults([]); setQuery(""); }}
                  className="w-full text-left px-3 py-2 text-xs text-white/70 hover:bg-white/[0.06] hover:text-white/90 transition-colors border-b border-white/[0.04] last:border-0 truncate"
                >
                  📍 {r.name}
                </button>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: SupplyRow
───────────────────────────────────────────────────── */
function SupplyRow({ supply, index, foods, onChange, onRemove }) {
  return (
    <div className="flex items-center gap-2">
      <div className={`${glass} flex items-center gap-1 px-2 py-1.5 rounded-lg`}>
        <span className="text-[10px] text-white/40">分</span>
        <input
          type="number"
          value={supply.time_min}
          onChange={e => onChange(index, "time_min", Number(e.target.value))}
          className="w-14 bg-transparent text-sm text-white/80 text-center outline-none"
        />
      </div>
      <select
        value={supply.food}
        onChange={e => onChange(index, "food", e.target.value)}
        className={`${glass} flex-1 px-2 py-1.5 rounded-lg text-xs text-white/80 outline-none bg-transparent cursor-pointer`}
      >
        {foods.map(f => <option key={f} value={f} className="bg-[#0e0e1a]">{f}</option>)}
      </select>
      <div className={`${glass} flex items-center gap-1 px-2 py-1.5 rounded-lg`}>
        <button onClick={() => onChange(index, "qty", Math.max(1, supply.qty - 1))} className="text-white/40 hover:text-white/80"><Minus size={10} /></button>
        <span className="text-xs text-white/80 w-4 text-center">{supply.qty}</span>
        <button onClick={() => onChange(index, "qty", Math.min(5, supply.qty + 1))} className="text-white/40 hover:text-white/80"><Plus size={10} /></button>
      </div>
      <button onClick={() => onRemove(index)} className="text-white/25 hover:text-red-400/70 transition-colors">
        <Minus size={14} />
      </button>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: EnergyResultChart (SVG)
───────────────────────────────────────────────────── */
function EnergyResultChart({ timeMin, eKcal, supplyLog, eCrit, eInitial, alertTimes, hoverFraction, onHover }) {
  const [tooltip, setTooltip] = useState(null);
  const VW = 600, VH = 160;
  const PL = 48, PR = 16, PT = 12, PB = 28;
  const W = VW - PL - PR, H = VH - PT - PB;

  if (!timeMin || timeMin.length === 0) return null;

  const maxT = Math.max(...timeMin);
  const maxE = Math.max(...eKcal, eInitial);

  const tx = (t) => PL + (t / maxT) * W;
  const ty = (e) => PT + H - (e / maxE) * H;

  const linePath = timeMin.map((t, i) =>
    `${i === 0 ? "M" : "L"}${tx(t).toFixed(1)},${ty(eKcal[i]).toFixed(1)}`
  ).join(" ");

  const areaPath = `${linePath} L${tx(maxT).toFixed(1)},${(PT + H).toFixed(1)} L${PL},${(PT + H).toFixed(1)} Z`;

  const handleMove = (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const mx   = ((e.clientX - rect.left) / rect.width) * VW;
    const hovT = ((mx - PL) / W) * maxT;
    const nearest = timeMin.reduce((best, t, i) =>
      Math.abs(t - hovT) < Math.abs(timeMin[best] - hovT) ? i : best, 0);
    setTooltip({ x: tx(timeMin[nearest]), y: ty(eKcal[nearest]), t: timeMin[nearest], e: eKcal[nearest] });
    onHover?.(timeMin[nearest] / maxT);
  };
  const handleLeave = () => { setTooltip(null); onHover?.(null); };

  // External hover dot (driven by elevation chart hover)
  const extNearest = hoverFraction != null && !tooltip
    ? timeMin.reduce((best, t, i) =>
        Math.abs(t - hoverFraction * maxT) < Math.abs(timeMin[best] - hoverFraction * maxT) ? i : best, 0)
    : null;

  const critY = ty(eCrit);

  return (
    <svg
      viewBox={`0 0 ${VW} ${VH}`}
      width="100%"
      preserveAspectRatio="xMidYMid meet"
      onMouseMove={handleMove}
      onMouseLeave={handleLeave}
      style={{ cursor: "crosshair" }}
    >
      <defs>
        <linearGradient id="eGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%"   stopColor="#22d3ee" stopOpacity="0.4" />
          <stop offset="100%" stopColor="#22d3ee" stopOpacity="0.02" />
        </linearGradient>
        <filter id="eGlow" x="-20%" y="-50%" width="140%" height="200%">
          <feGaussianBlur stdDeviation="3" result="b" />
          <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
      </defs>

      {/* Grid */}
      {[25, 50, 75, 100].map(pct => {
        const e = eInitial * pct / 100;
        return (
          <g key={pct}>
            <line x1={PL} y1={ty(e)} x2={VW - PR} y2={ty(e)}
              stroke="rgba(255,255,255,0.05)" strokeWidth="1" strokeDasharray="3,5" />
            <text x={PL - 6} y={ty(e) + 3.5}
              fill="rgba(255,255,255,0.2)" fontSize="9" textAnchor="end" fontFamily="monospace">
              {Math.round(e)}
            </text>
          </g>
        );
      })}

      {/* Critical threshold */}
      <line x1={PL} y1={critY} x2={VW - PR} y2={critY}
        stroke="#f87171" strokeWidth="1.5" strokeDasharray="6,3" opacity="0.7" />
      <text x={PL + 4} y={critY - 4} fill="#f87171" fontSize="9" fontWeight="600">危険水準 {eCrit}kcal</text>

      {/* Energy area + line */}
      <path d={areaPath} fill="url(#eGrad)" />
      <path d={linePath} fill="none" stroke="#22d3ee" strokeWidth="2.5" filter="url(#eGlow)" />

      {/* Supply events */}
      {supplyLog.map((s, i) => {
        const x = tx(s.time_min).toFixed(1);
        return (
          <g key={i}>
            <line x1={x} y1={PT} x2={x} y2={PT + H}
              stroke="#a78bfa" strokeWidth="1.5" strokeDasharray="4,3" opacity="0.6" />
            <text x={parseFloat(x) + 3} y={PT + 12}
              fill="#a78bfa" fontSize="8.5" fontWeight="600">+{s.kcal}kcal</text>
          </g>
        );
      })}

      {/* Alert times */}
      {alertTimes.map((at, i) => (
        <g key={i}>
          <line x1={tx(at).toFixed(1)} y1={PT} x2={tx(at).toFixed(1)} y2={PT + H}
            stroke="#f87171" strokeWidth="1.5" strokeDasharray="3,3" opacity="0.6" />
        </g>
      ))}

      {/* X-axis labels */}
      {[0, 0.25, 0.5, 0.75, 1].map(f => {
        const t = f * maxT;
        const h = Math.floor(t / 60), m = Math.round(t % 60);
        return (
          <text key={f} x={tx(t)} y={VH - 5}
            fill="rgba(255,255,255,0.25)" fontSize="9"
            textAnchor="middle" fontFamily="monospace">
            {h > 0 ? `${h}h${String(m).padStart(2,"0")}m` : `${m}m`}
          </text>
        );
      })}

      {/* Y-axis label */}
      <text x={14} y={VH / 2} fill="rgba(255,255,255,0.2)" fontSize="9"
        textAnchor="middle" transform={`rotate(-90,14,${VH/2})`}>kcal</text>

      {/* External hover dot */}
      {extNearest != null && (
        <circle cx={tx(timeMin[extNearest])} cy={ty(eKcal[extNearest])} r="4.5" fill="#ef4444" filter="url(#eGlow)" />
      )}

      {/* Tooltip */}
      {tooltip && (
        <g>
          <circle cx={tooltip.x} cy={tooltip.y} r="4.5" fill="#ef4444" filter="url(#eGlow)" />
          <rect x={tooltip.x + 6} y={tooltip.y - 16} width="78" height="22" rx="4"
            fill="rgba(10,10,20,0.9)" stroke="rgba(239,68,68,0.3)" strokeWidth="1" />
          <text x={tooltip.x + 45} y={tooltip.y - 1}
            fill="#e2e8f0" fontSize="10" textAnchor="middle" fontFamily="monospace">
            {Math.round(tooltip.t)}m / {Math.round(tooltip.e)}kcal
          </text>
        </g>
      )}
    </svg>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: ElevationChart (SVG)
───────────────────────────────────────────────────── */
function ElevationChart({ elevations, coords, hoverFraction, onHover }) {
  const [tooltip, setTooltip] = useState(null);
  const VW = 600, VH = 100;
  const PL = 44, PR = 12, PT = 8, PB = 20;
  const W = VW - PL - PR, H = VH - PT - PB;

  if (!elevations || elevations.length === 0) return null;

  const minE = Math.min(...elevations);
  const maxE = Math.max(...elevations);
  const rng  = maxE - minE || 1;
  const n    = elevations.length;

  const tx = (i) => PL + (i / (n - 1)) * W;
  const ty = (e) => PT + H - ((e - minE) / rng) * H;

  const line = elevations.map((e, i) => `${i === 0 ? "M" : "L"}${tx(i).toFixed(1)},${ty(e).toFixed(1)}`).join(" ");
  const area = `${line} L${tx(n - 1).toFixed(1)},${(PT + H).toFixed(1)} L${PL},${(PT + H).toFixed(1)} Z`;

  const handleMove = (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const mx   = ((e.clientX - rect.left) / rect.width) * VW;
    const frac = Math.max(0, Math.min(1, (mx - PL) / W));
    const idx  = Math.min(n - 1, Math.round(frac * (n - 1)));
    setTooltip({ x: tx(idx), y: ty(elevations[idx]), e: elevations[idx] });
    onHover?.(frac);
  };
  const handleLeave = () => { setTooltip(null); onHover?.(null); };

  // External hover dot (driven by energy chart hover)
  const extIdx = hoverFraction != null ? Math.min(n - 1, Math.round(hoverFraction * (n - 1))) : null;

  return (
    <svg
      viewBox={`0 0 ${VW} ${VH}`} width="100%" preserveAspectRatio="xMidYMid meet"
      onMouseMove={handleMove} onMouseLeave={handleLeave} style={{ cursor: "crosshair" }}
    >
      <defs>
        <linearGradient id="elevGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%"   stopColor="#818cf8" stopOpacity="0.5" />
          <stop offset="100%" stopColor="#818cf8" stopOpacity="0.05" />
        </linearGradient>
        <filter id="elevGlow" x="-20%" y="-50%" width="140%" height="200%">
          <feGaussianBlur stdDeviation="2.5" result="b" />
          <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
      </defs>
      {[0, 0.5, 1].map(f => {
        const e = minE + f * rng;
        return (
          <g key={f}>
            <line x1={PL} y1={ty(e)} x2={VW - PR} y2={ty(e)}
              stroke="rgba(255,255,255,0.05)" strokeWidth="1" strokeDasharray="3,5" />
            <text x={PL - 6} y={ty(e) + 3.5}
              fill="rgba(255,255,255,0.2)" fontSize="9" textAnchor="end" fontFamily="monospace">
              {Math.round(e)}m
            </text>
          </g>
        );
      })}
      <path d={area} fill="url(#elevGrad)" />
      <path d={line} fill="none" stroke="#818cf8" strokeWidth="2" />
      <circle cx={tx(0)} cy={ty(elevations[0])} r="4" fill="#4ade80" />
      <circle cx={tx(n - 1)} cy={ty(elevations[n - 1])} r="4" fill="#f87171" />

      {/* External hover dot */}
      {extIdx != null && !tooltip && (
        <circle cx={tx(extIdx)} cy={ty(elevations[extIdx])} r="4.5" fill="#ef4444" filter="url(#elevGlow)" />
      )}

      {/* Self tooltip dot */}
      {tooltip && (
        <g>
          <circle cx={tooltip.x} cy={tooltip.y} r="4.5" fill="#ef4444" filter="url(#elevGlow)" />
          <rect x={tooltip.x + 6} y={tooltip.y - 16} width="60" height="20" rx="4"
            fill="rgba(10,10,20,0.9)" stroke="rgba(239,68,68,0.3)" strokeWidth="1" />
          <text x={tooltip.x + 36} y={tooltip.y - 2}
            fill="#e2e8f0" fontSize="10" textAnchor="middle" fontFamily="monospace">
            {Math.round(tooltip.e)}m
          </text>
        </g>
      )}
    </svg>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: RouteMap (Leaflet)
───────────────────────────────────────────────────── */
function FitBounds({ coords }) {
  const map = useMap();
  useEffect(() => {
    if (coords && coords.length > 0) {
      map.fitBounds(coords, { padding: [20, 20] });
    }
  }, [coords, map]);
  return null;
}

function MapUpdater() {
  const map = useMap();
  useEffect(() => {
    // タイルが確実に描画されるよう複数タイミングでサイズ再計算
    const t1 = setTimeout(() => map.invalidateSize(), 50);
    const t2 = setTimeout(() => map.invalidateSize(), 300);
    const t3 = setTimeout(() => map.invalidateSize(), 800);
    return () => { clearTimeout(t1); clearTimeout(t2); clearTimeout(t3); };
  }, [map]);
  return null;
}

function RouteMap({ coords, hoverFraction, onHover }) {
  if (!coords || coords.length === 0) return null;

  const startIcon = new L.DivIcon({
    html: `<div style="background:#4ade80;width:14px;height:14px;border-radius:50%;border:2px solid white;box-shadow:0 0 8px #4ade80"></div>`,
    className: "", iconSize: [14, 14], iconAnchor: [7, 7],
  });
  const goalIcon = new L.DivIcon({
    html: `<div style="background:#f87171;width:14px;height:14px;border-radius:50%;border:2px solid white;box-shadow:0 0 8px #f87171"></div>`,
    className: "", iconSize: [14, 14], iconAnchor: [7, 7],
  });
  const hoverIcon = new L.DivIcon({
    html: `<div style="background:#ef4444;width:14px;height:14px;border-radius:50%;border:2px solid white;box-shadow:0 0 10px #ef4444,0 0 20px rgba(239,68,68,0.5)"></div>`,
    className: "", iconSize: [14, 14], iconAnchor: [7, 7],
  });

  const hoverPos = hoverFraction != null
    ? coords[Math.min(coords.length - 1, Math.round(hoverFraction * (coords.length - 1)))]
    : null;

  const mapKey = coords[0]?.join(",");

  return (
    <div className="rounded-xl" style={{ height: 280, overflow: "hidden", position: "relative" }}>
      <MapContainer
        key={mapKey}
        center={coords[Math.floor(coords.length / 2)]}
        zoom={12}
        style={{ height: "100%", width: "100%", zIndex: 0 }}
        zoomControl={true}
      >
        <TileLayer
          url="/api/tiles/{z}/{x}/{y}"
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          maxZoom={19}
        />
        <MapUpdater />
        <FitBounds coords={coords} />
        <Polyline positions={coords} color="#22d3ee" weight={3} opacity={0.9} />
        <Marker position={coords[0]}                   icon={startIcon}><Popup>スタート</Popup></Marker>
        <Marker position={coords[coords.length - 1]}   icon={goalIcon}><Popup>ゴール</Popup></Marker>
        {hoverPos && <Marker position={hoverPos} icon={hoverIcon} />}
      </MapContainer>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: RoutePreview (step 2 — elevation + map synced)
───────────────────────────────────────────────────── */
function RoutePreview({ route }) {
  const [hoverFrac, setHoverFrac] = useState(null);
  const diff = route.difficulty;
  const diffColor = diff
    ? (diff.score <= 2.5 ? "text-green-400 bg-green-500/15 border-green-500/25"
     : diff.score <= 5.0 ? "text-yellow-400 bg-yellow-500/15 border-yellow-500/25"
     : diff.score <= 7.5 ? "text-orange-400 bg-orange-500/15 border-orange-500/25"
     :                     "text-red-400 bg-red-500/15 border-red-500/25")
    : "";
  return (
    <div className={`${glass} rounded-2xl p-4 space-y-3`}>
      <div className="flex items-center gap-2 text-xs text-green-400">
        <CheckCircle2 size={14} /> ルート取得完了
        {diff && (
          <span className={`ml-2 px-2 py-0.5 rounded-full border text-[10px] font-semibold ${diffColor}`}>
            難易度 {diff.score} / 10 · {diff.label}
          </span>
        )}
        <span className="text-white/30 ml-auto">{route.method}</span>
      </div>
      <div className="grid grid-cols-3 gap-3">
        {[
          ["距離",    (route.distance_m / 1000).toFixed(1), "km"],
          ["獲得標高", Math.round(route.ascent_m || 0),      "m"],
          ["座標数",   route.coords.length,                   "点"],
        ].map(([l, v, u]) => (
          <div key={l} className="text-center">
            <div className="text-lg font-bold text-white/90">{v}<span className="text-xs text-white/40 ml-1">{u}</span></div>
            <div className="text-[10px] text-white/40">{l}</div>
          </div>
        ))}
      </div>
      <ElevationChart
        elevations={route.elevations}
        coords={route.coords}
        hoverFraction={hoverFrac}
        onHover={setHoverFrac}
      />
      <RouteMap
        coords={route.coords}
        hoverFraction={hoverFrac}
        onHover={setHoverFrac}
      />
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: ResultChartsAndMap (step 4 — all three synced)
───────────────────────────────────────────────────── */
function ResultChartsAndMap({ result }) {
  const [hoverFrac, setHoverFrac] = useState(null);
  return (
    <>
      {/* Energy chart */}
      <div className={`${glass} rounded-2xl p-5`}>
        <div className="flex items-center gap-2 mb-3 text-xs text-white/50 font-medium uppercase tracking-wider">
          <BarChart2 size={12} /> エネルギー推移
          <span className="text-white/25 normal-case ml-auto">破線: ハンガーノック警告</span>
        </div>
        <EnergyResultChart
          timeMin      = {result.time_min}
          eKcal        = {result.e_kcal}
          supplyLog    = {result.supply_log}
          eCrit        = {result.e_crit}
          eInitial     = {result.e_initial}
          alertTimes   = {result.alert_times}
          hoverFraction= {hoverFrac}
          onHover      = {setHoverFrac}
        />
      </div>

      {/* Elevation */}
      <div className={`${glass} rounded-2xl p-5`}>
        <div className="flex items-center gap-2 mb-3 text-xs text-white/50 font-medium uppercase tracking-wider">
          <Mountain size={12} /> 標高プロフィール
        </div>
        <ElevationChart
          elevations   = {result.elevations}
          coords       = {result.coords}
          hoverFraction= {hoverFrac}
          onHover      = {setHoverFrac}
        />
      </div>

      {/* Map */}
      <div className={`${glass} rounded-2xl p-5`}>
        <div className="flex items-center gap-2 mb-3 text-xs text-white/50 font-medium uppercase tracking-wider">
          <MapPin size={12} /> ルートマップ
        </div>
        <RouteMap
          coords       = {result.coords}
          hoverFraction= {hoverFrac}
          onHover      = {setHoverFrac}
        />
      </div>
    </>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: KPICard
───────────────────────────────────────────────────── */
function KPICard({ icon: Icon, label, value, unit, color = "cyan", alert = false }) {
  const colors = {
    cyan:   "text-cyan-400   bg-cyan-500/10   border-cyan-500/20",
    indigo: "text-indigo-400 bg-indigo-500/10 border-indigo-500/20",
    violet: "text-violet-400 bg-violet-500/10 border-violet-500/20",
    amber:  "text-amber-400  bg-amber-500/10  border-amber-500/20",
    red:    "text-red-400    bg-red-500/10    border-red-500/20",
    green:  "text-green-400  bg-green-500/10  border-green-500/20",
  };
  const c = alert ? colors.red : (colors[color] || colors.cyan);
  return (
    <div className={`${glass} rounded-2xl p-4 flex flex-col gap-2`}>
      <div className={`w-8 h-8 rounded-xl flex items-center justify-center border ${c}`}>
        <Icon size={16} />
      </div>
      <div>
        <div className="text-xs text-white/40 font-medium">{label}</div>
        <div className="flex items-baseline gap-1 mt-0.5">
          <span className={`text-xl font-bold ${c.split(" ")[0]}`}>{value}</span>
          <span className="text-xs text-white/40">{unit}</span>
        </div>
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: CompletionProbabilityCard
───────────────────────────────────────────────────── */
function CompletionProbabilityCard({ completion }) {
  if (!completion || completion.probability == null) return null;

  const prob = completion.probability;

  // リスクレベルに応じた色定義
  const riskConfig = {
    low:      { color: "#4ade80", bg: "bg-green-500/10",  border: "border-green-500/25",  text: "text-green-400",  label: "低リスク",    Icon: Shield      },
    moderate: { color: "#fbbf24", bg: "bg-amber-500/10",  border: "border-amber-500/25",  text: "text-amber-400",  label: "中程度",      Icon: ShieldAlert },
    high:     { color: "#f97316", bg: "bg-orange-500/10", border: "border-orange-500/25", text: "text-orange-400", label: "高リスク",    Icon: ShieldAlert },
    critical: { color: "#f87171", bg: "bg-red-500/10",    border: "border-red-500/25",    text: "text-red-400",    label: "要注意",      Icon: AlertTriangle },
  };
  const cfg  = riskConfig[completion.risk_level] || riskConfig.moderate;
  const Icon = cfg.Icon;

  // SVG 円弧ゲージのパラメータ
  const R   = 44;
  const CX  = 56, CY = 56;
  const circ = 2 * Math.PI * R;
  const arc  = circ * (prob / 100);
  const gap  = circ - arc;

  return (
    <div className={`${glass} rounded-2xl p-5`}>
      <div className="flex items-center gap-2 mb-4 text-xs text-white/50 font-medium uppercase tracking-wider">
        <Brain size={12} /> 完走確率予測
        <span className="text-white/25 normal-case ml-auto">Monte Carlo n=100</span>
      </div>

      <div className="flex items-center gap-6">
        {/* 円弧ゲージ */}
        <div className="shrink-0">
          <svg width="112" height="112" viewBox="0 0 112 112">
            {/* 背景トラック */}
            <circle cx={CX} cy={CY} r={R}
              fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth="10" />
            {/* プログレスアーク */}
            <circle cx={CX} cy={CY} r={R}
              fill="none"
              stroke={cfg.color}
              strokeWidth="10"
              strokeLinecap="round"
              strokeDasharray={`${arc} ${gap}`}
              strokeDashoffset={circ * 0.25}   /* 12時スタート */
              style={{ filter: `drop-shadow(0 0 6px ${cfg.color}88)`, transition: "stroke-dasharray 0.8s ease" }}
            />
            {/* 中央テキスト */}
            <text x={CX} y={CY - 6} textAnchor="middle" fill="white" fontSize="20" fontWeight="700" fontFamily="monospace">
              {prob.toFixed(0)}
            </text>
            <text x={CX} y={CY + 10} textAnchor="middle" fill="rgba(255,255,255,0.4)" fontSize="11">
              %
            </text>
          </svg>
        </div>

        {/* 右側テキスト情報 */}
        <div className="flex-1 space-y-3">
          {/* リスクバッジ */}
          <div className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-xs font-semibold ${cfg.bg} ${cfg.border} ${cfg.text}`}>
            <Icon size={11} /> {cfg.label}
          </div>

          {/* 信頼区間 */}
          <div>
            <div className="text-[10px] text-white/35 mb-1">95% 信頼区間</div>
            <div className="flex items-center gap-1.5">
              <div className="h-1.5 rounded-full bg-white/10 flex-1 relative overflow-hidden">
                <div
                  className="absolute top-0 h-full rounded-full"
                  style={{
                    left:  `${completion.confidence_low}%`,
                    right: `${100 - completion.confidence_high}%`,
                    backgroundColor: cfg.color,
                    opacity: 0.6,
                  }}
                />
                <div
                  className="absolute top-0 w-1 h-full rounded-full"
                  style={{ left: `${prob}%`, backgroundColor: cfg.color }}
                />
              </div>
              <span className="text-[10px] text-white/40 font-mono shrink-0">
                {completion.confidence_low?.toFixed(0)}〜{completion.confidence_high?.toFixed(0)}%
              </span>
            </div>
          </div>

          {/* メッセージ */}
          <p className="text-xs text-white/55 leading-relaxed">{completion.message}</p>
        </div>
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: AnomalyList
───────────────────────────────────────────────────── */
function AnomalyList({ anomalies, fmtMin }) {
  if (!anomalies || anomalies.length === 0) return null;

  const typeConfig = {
    energy_drop: {
      Icon: Zap,         color: "text-orange-400", bg: "bg-orange-500/10", border: "border-orange-500/20",
      label: "エネルギー急落",
    },
    pace_drop: {
      Icon: TrendingDown, color: "text-amber-400",  bg: "bg-amber-500/10",  border: "border-amber-500/20",
      label: "ペース急落",
    },
    hr_spike: {
      Icon: Activity,    color: "text-red-400",    bg: "bg-red-500/10",    border: "border-red-500/20",
      label: "心拍スパイク",
    },
    low_energy: {
      Icon: AlertTriangle, color: "text-red-400",  bg: "bg-red-500/10",    border: "border-red-500/20",
      label: "低エネルギー",
    },
  };

  const severityLabel = (s) => {
    if (s >= 4) return { text: "重大",   cls: "bg-red-500/20 text-red-300 border-red-500/30" };
    if (s >= 3) return { text: "高",     cls: "bg-orange-500/20 text-orange-300 border-orange-500/30" };
    return              { text: "中",    cls: "bg-amber-500/20 text-amber-300 border-amber-500/30" };
  };

  return (
    <div className={`${glass} rounded-2xl p-5`}>
      <div className="flex items-center gap-2 mb-3 text-xs text-white/50 font-medium uppercase tracking-wider">
        <Gauge size={12} /> 異常区間検知
        <span className={`ml-auto text-[10px] px-2 py-0.5 rounded-full border
          ${anomalies.length > 0 ? "bg-orange-500/15 border-orange-500/30 text-orange-400" : "bg-green-500/10 border-green-500/20 text-green-400"}`}>
          {anomalies.length} 件
        </span>
      </div>

      <div className="space-y-2">
        {anomalies.map((seg, i) => {
          const cfg = typeConfig[seg.type] || typeConfig.energy_drop;
          const Icon = cfg.Icon;
          const sev  = severityLabel(seg.severity);
          return (
            <div key={i} className={`flex items-start gap-3 px-3 py-2.5 rounded-xl border ${cfg.bg} ${cfg.border}`}>
              {/* アイコン */}
              <div className={`shrink-0 mt-0.5 ${cfg.color}`}>
                <Icon size={14} />
              </div>
              {/* 内容 */}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-0.5">
                  <span className={`text-xs font-semibold ${cfg.color}`}>{cfg.label}</span>
                  <span className="text-[10px] text-white/35 font-mono">
                    {fmtMin(seg.start_min)} 〜 {fmtMin(seg.end_min)}
                  </span>
                  <span className={`ml-auto text-[10px] px-1.5 py-0.5 rounded border shrink-0 ${sev.cls}`}>
                    {sev.text}
                  </span>
                </div>
                <p className="text-[11px] text-white/50 leading-relaxed truncate">{seg.description}</p>
              </div>
            </div>
          );
        })}
      </div>

      <p className="text-[10px] text-white/25 mt-3">
        ※ ローリングZスコアによる統計的外れ値検出（2.5σ閾値）
      </p>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: RecoveryRing — リカバリースコアの円形ゲージ
───────────────────────────────────────────────────── */
function RecoveryRing({ score, status, size = 160 }) {
  const r      = size * 0.38;
  const cx     = size / 2;
  const cy     = size / 2;
  const circ   = 2 * Math.PI * r;
  const pct    = Math.max(0, Math.min(100, score ?? 0));
  const dash   = (pct / 100) * circ;
  const gap    = circ - dash;

  const colorMap = {
    excellent: { stroke: "#34d399", glow: "#34d399", label: "絶好調", text: "text-emerald-400" },
    good:      { stroke: "#22d3ee", glow: "#22d3ee", label: "良好",   text: "text-cyan-400"    },
    moderate:  { stroke: "#fbbf24", glow: "#fbbf24", label: "普通",   text: "text-amber-400"   },
    poor:      { stroke: "#f87171", glow: "#f87171", label: "疲労",   text: "text-red-400"     },
    rest:      { stroke: "#94a3b8", glow: "#94a3b8", label: "要休息", text: "text-slate-400"   },
  };
  const c = colorMap[status] ?? colorMap.moderate;

  return (
    <div className="flex flex-col items-center gap-2">
      <svg width={size} height={size} style={{ transform: "rotate(-90deg)" }}>
        <defs>
          <filter id="rGlow">
            <feGaussianBlur stdDeviation="3" result="blur" />
            <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
        </defs>
        {/* トラック */}
        <circle cx={cx} cy={cy} r={r} fill="none" stroke="rgba(255,255,255,0.05)" strokeWidth="10" />
        {/* プログレス */}
        <circle
          cx={cx} cy={cy} r={r}
          fill="none"
          stroke={c.stroke}
          strokeWidth="10"
          strokeLinecap="round"
          strokeDasharray={`${dash} ${gap}`}
          filter="url(#rGlow)"
          style={{ transition: "stroke-dasharray 0.8s ease" }}
        />
        {/* 中央テキスト（回転戻す） */}
        <text x={cx} y={cy - 8} textAnchor="middle" dominantBaseline="middle"
          fill="white" fontSize={size * 0.22} fontWeight="bold" fontFamily="sans-serif"
          style={{ transform: `rotate(90deg) translate(0, ${-size}px)`, transformOrigin: `${cx}px ${cy}px` }}
        >
          {score != null ? Math.round(score) : "--"}
        </text>
        <text x={cx} y={cy + size * 0.12} textAnchor="middle"
          fill="rgba(255,255,255,0.4)" fontSize={size * 0.08} fontFamily="sans-serif"
          style={{ transform: `rotate(90deg) translate(0, ${-size}px)`, transformOrigin: `${cx}px ${cy}px` }}
        >
          / 100
        </text>
      </svg>
      <span className={`text-sm font-semibold ${c.text}`}>{c.label}</span>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: PMCChart — ATL/CTL/TSB 折れ線グラフ
───────────────────────────────────────────────────── */
function PMCChart({ data }) {
  if (!data || data.length === 0) return null;
  const W = 600, H = 180, PAD = { t: 12, r: 8, b: 28, l: 36 };
  const iW = W - PAD.l - PAD.r;
  const iH = H - PAD.t - PAD.b;

  const vals = data.flatMap(d => [d.atl ?? 0, d.ctl ?? 0, d.tsb ?? 0]);
  const yMin = Math.min(...vals, -15);
  const yMax = Math.max(...vals, 15);
  const yRng = yMax - yMin || 1;

  const tx = i => PAD.l + (i / Math.max(data.length - 1, 1)) * iW;
  const ty = v => PAD.t + iH - ((v - yMin) / yRng) * iH;

  const line = (key, clr) => {
    const pts = data
      .map((d, i) => d[key] != null ? `${tx(i).toFixed(1)},${ty(d[key]).toFixed(1)}` : null)
      .filter(Boolean);
    if (pts.length < 2) return null;
    return <polyline key={key} points={pts.join(" ")} fill="none" stroke={clr} strokeWidth="2" strokeLinejoin="round" />;
  };

  // TSBゼロライン
  const zeroY = ty(0);

  // X軸ラベル（月初のみ）
  const xLabels = [];
  let lastMonth = null;
  data.forEach((d, i) => {
    if (!d.date) return;
    const m = d.date.slice(0, 7);
    if (m !== lastMonth) { xLabels.push({ i, label: d.date.slice(5, 7) + "月" }); lastMonth = m; }
  });

  // Y軸目盛り
  const yTicks = [-20, -10, 0, 10, 20, 30].filter(v => v >= yMin - 5 && v <= yMax + 5);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" preserveAspectRatio="none" style={{ height: 180 }}>
      {/* Y軸グリッド */}
      {yTicks.map(v => (
        <g key={v}>
          <line x1={PAD.l} y1={ty(v)} x2={W - PAD.r} y2={ty(v)}
            stroke={v === 0 ? "rgba(255,255,255,0.15)" : "rgba(255,255,255,0.05)"} strokeWidth={v === 0 ? 1.5 : 1} />
          <text x={PAD.l - 4} y={ty(v)} textAnchor="end" dominantBaseline="middle"
            fill="rgba(255,255,255,0.3)" fontSize="9">{v}</text>
        </g>
      ))}
      {/* TSB 0ライン強調 */}
      <line x1={PAD.l} y1={zeroY} x2={W - PAD.r} y2={zeroY}
        stroke="rgba(255,255,255,0.2)" strokeWidth="1" strokeDasharray="4,3" />
      {/* データライン */}
      {line("ctl", "#22d3ee")}   {/* CTL: 水色 */}
      {line("atl", "#f87171")}   {/* ATL: 赤 */}
      {line("tsb", "#a78bfa")}   {/* TSB: 紫 */}
      {/* X軸ラベル */}
      {xLabels.map(({ i, label }) => (
        <text key={i} x={tx(i)} y={H - 4} textAnchor="middle"
          fill="rgba(255,255,255,0.3)" fontSize="9">{label}</text>
      ))}
    </svg>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: SparkLine — 小さい折れ線（VO2max / 精度トレンド用）
───────────────────────────────────────────────────── */
function SparkLine({ values, color = "#22d3ee", height = 60 }) {
  if (!values || values.length < 2) return null;
  const W = 300, H = height;
  const PAD = 8;
  const iW = W - PAD * 2, iH = H - PAD * 2;
  const vMin = Math.min(...values);
  const vMax = Math.max(...values);
  const rng = vMax - vMin || 1;
  const tx = i => PAD + (i / (values.length - 1)) * iW;
  const ty = v => PAD + iH - ((v - vMin) / rng) * iH;
  const pts = values.map((v, i) => `${tx(i).toFixed(1)},${ty(v).toFixed(1)}`).join(" ");
  const areaBot = `${tx(values.length - 1)},${PAD + iH} ${tx(0)},${PAD + iH}`;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" preserveAspectRatio="none" style={{ height }}>
      <defs>
        <linearGradient id="spGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.25" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <polygon points={`${pts} ${areaBot}`} fill="url(#spGrad)" />
      <polyline points={pts} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" />
      <circle cx={tx(values.length - 1)} cy={ty(values[values.length - 1])} r="3" fill={color} />
    </svg>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: FileDropZone — ファイルアップロードエリア
───────────────────────────────────────────────────── */
function FileDropZone({ accept, label, hint, icon: Icon, onFile, loading, result, error }) {
  const ref = useRef(null);
  const [dragging, setDragging] = useState(false);

  const handleFile = (f) => { if (f) onFile(f); };
  const onDrop = (e) => {
    e.preventDefault(); setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) handleFile(f);
  };

  return (
    <div
      className={`relative rounded-2xl border-2 border-dashed transition-all cursor-pointer
        ${dragging ? "border-indigo-400/60 bg-indigo-500/10" : "border-white/10 hover:border-white/20"}
        ${result ? "border-green-500/40 bg-green-500/5" : ""}
        ${error ? "border-red-500/40 bg-red-500/5" : ""}
      `}
      onDragOver={e => { e.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      onClick={() => ref.current?.click()}
    >
      <input ref={ref} type="file" accept={accept} className="hidden"
        onChange={e => handleFile(e.target.files[0])} />
      <div className="flex flex-col items-center gap-2 px-4 py-6 text-center select-none">
        {loading
          ? <Loader2 size={24} className="text-indigo-400 animate-spin" />
          : result
          ? <CheckCircle2 size={24} className="text-green-400" />
          : error
          ? <AlertTriangle size={24} className="text-red-400" />
          : <Icon size={24} className="text-white/30" />
        }
        <div>
          <p className="text-sm font-medium text-white/70">{label}</p>
          <p className="text-[11px] text-white/30 mt-0.5">{hint}</p>
        </div>
        {result && (
          <div className="text-[11px] text-green-400 bg-green-500/10 border border-green-500/20 rounded-xl px-3 py-1.5 max-w-full">
            {typeof result === "string" ? result : result.message || "取り込み完了"}
          </div>
        )}
        {error && (
          <div className="text-[11px] text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-1.5 max-w-full">
            {error}
          </div>
        )}
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: ModelProgressBar — アクティビティ数のプログレス
───────────────────────────────────────────────────── */
function ModelProgressBar({ n, threshold = 5 }) {
  const pct = Math.min((n / threshold) * 100, 100);
  const reached = n >= threshold;
  return (
    <div className="space-y-1.5">
      <div className="flex justify-between text-[11px]">
        <span className="text-white/40">
          {reached ? "高精度モデル (GradientBoosting)" : `高精度モデルまで あと ${threshold - n} 件`}
        </span>
        <span className={reached ? "text-cyan-400" : "text-white/40"}>{n} / {threshold}</span>
      </div>
      <div className="h-1.5 rounded-full bg-white/[0.05] overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-700 ${reached ? "bg-cyan-400" : "bg-indigo-400"}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: TSBStatusBadge
───────────────────────────────────────────────────── */
function TSBStatusBadge({ status, tsb }) {
  const cfg = {
    "フレッシュ":  { bg: "bg-emerald-500/15", border: "border-emerald-500/30", text: "text-emerald-400" },
    "通常":       { bg: "bg-cyan-500/15",     border: "border-cyan-500/30",    text: "text-cyan-400"    },
    "軽度疲労":   { bg: "bg-amber-500/15",    border: "border-amber-500/30",   text: "text-amber-400"   },
    "高負荷":     { bg: "bg-orange-500/15",   border: "border-orange-500/30",  text: "text-orange-400"  },
    "過負荷":     { bg: "bg-red-500/15",      border: "border-red-500/30",     text: "text-red-400"     },
  }[status] ?? { bg: "bg-white/5", border: "border-white/10", text: "text-white/50" };

  return (
    <span className={`inline-flex items-center gap-1 px-3 py-1 rounded-full border text-xs font-semibold ${cfg.bg} ${cfg.border} ${cfg.text}`}>
      {status}
      {tsb != null && <span className="text-[10px] font-normal opacity-70 ml-1">TSB {tsb > 0 ? "+" : ""}{tsb}</span>}
    </span>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: ActivityHeatmap — GitHub草スタイルカレンダー
───────────────────────────────────────────────────── */
const ACTIVITY_LABELS = {
  hiking:       "登山・ハイキング",
  running:      "ランニング",
  trail_running:"トレイルラン",
  cycling:      "サイクリング",
};
const DOW_LABELS = ["日", "月", "火", "水", "木", "金", "土"];
const MONTH_LABELS = ["1月","2月","3月","4月","5月","6月","7月","8月","9月","10月","11月","12月"];

function heatColor(value, max, mode) {
  if (!value || value === 0 || !max) return "rgba(255,255,255,0.05)";
  const pct = Math.min(value / max, 1);
  // indigo → cyan のグラデーション（4段階）
  if (pct < 0.15) return "rgba(99,102,241,0.30)";
  if (pct < 0.35) return "rgba(99,102,241,0.55)";
  if (pct < 0.60) return "rgba(34,211,238,0.50)";
  if (pct < 0.80) return "rgba(34,211,238,0.75)";
  return "rgba(34,211,238,1.00)";
}

function buildGrid(year, dayMap) {
  // 年の1月1日の曜日（0=日）
  const jan1Dow = new Date(year, 0, 1).getDay();
  // グリッド開始: Jan1 を含む週の日曜
  const gridStart = new Date(year, 0, 1 - jan1Dow);
  // グリッド終了: Dec31 を含む週の土曜
  const dec31 = new Date(year, 11, 31);
  const dec31Dow = dec31.getDay();
  const gridEnd = new Date(year, 11, 31 + (6 - dec31Dow));

  const weeks = [];
  const cur = new Date(gridStart);
  while (cur <= gridEnd) {
    const week = [];
    for (let d = 0; d < 7; d++) {
      const iso = cur.toISOString().slice(0, 10);
      week.push({
        date:    iso,
        data:    dayMap[iso] || null,
        inYear:  cur.getFullYear() === year,
        month:   cur.getMonth(),
        dom:     cur.getDate(),
      });
      cur.setDate(cur.getDate() + 1);
    }
    weeks.push(week);
  }

  // 月ラベル: 各週の最初の日が変月するとき
  const monthCols = [];
  weeks.forEach((week, wi) => {
    const firstInYear = week.find(c => c.inYear);
    if (!firstInYear) return;
    if (wi === 0 || firstInYear.dom <= 7) {
      monthCols.push({ wi, month: firstInYear.month });
    }
  });

  return { weeks, monthCols };
}

function ActivityHeatmap({ calData, mode = "trimp" }) {
  const [tooltip, setTooltip] = useState(null); // { x, y, cell }
  const [heatMode, setHeatMode] = useState(mode);

  if (!calData) return null;

  const dayMap = {};
  (calData.days || []).forEach(d => { dayMap[d.date] = d; });
  const { weeks, monthCols } = buildGrid(calData.year, dayMap);
  const maxVal = heatMode === "trimp" ? calData.max_trimp : calData.max_duration;

  const CELL = 13, GAP = 2, STEP = CELL + GAP;

  return (
    <div className="space-y-2">
      {/* ヘッダー：モード切替 + 統計 */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex gap-1">
          {[["trimp", "負荷 (TRIMP)"], ["duration", "時間"]].map(([m, lbl]) => (
            <button key={m} onClick={() => setHeatMode(m)}
              className={`px-2.5 py-1 rounded-lg text-[11px] font-medium transition-all border ${
                heatMode === m
                  ? "bg-cyan-500/20 border-cyan-500/30 text-cyan-300"
                  : "border-white/10 text-white/40 hover:text-white/60"
              }`}>
              {lbl}
            </button>
          ))}
        </div>
        <div className="flex gap-3 text-[11px] text-white/40">
          <span>{calData.total_days} 日</span>
          <span>{calData.total_duration_h} h</span>
          {calData.total_elevation_gain_m > 0 && <span>↑{calData.total_elevation_gain_m.toLocaleString()} m</span>}
        </div>
      </div>

      {/* グリッド */}
      <div className="overflow-x-auto pb-1">
        <div style={{ position: "relative", display: "inline-block", paddingTop: 20, paddingLeft: 20 }}>

          {/* 曜日ラベル（左） */}
          {[0, 2, 4, 6].map(dow => (
            <div key={dow} style={{
              position: "absolute", left: 0,
              top: 20 + dow * STEP + (CELL / 2),
              fontSize: 9, color: "rgba(255,255,255,0.3)",
              lineHeight: 1, transform: "translateY(-50%)",
            }}>
              {DOW_LABELS[dow]}
            </div>
          ))}

          {/* 月ラベル（上） */}
          {monthCols.map(({ wi, month }) => (
            <div key={`m${wi}`} style={{
              position: "absolute",
              left: 20 + wi * STEP,
              top: 2,
              fontSize: 9, color: "rgba(255,255,255,0.35)",
              whiteSpace: "nowrap",
            }}>
              {MONTH_LABELS[month]}
            </div>
          ))}

          {/* セルグリッド */}
          <div style={{ display: "flex", gap: GAP }}>
            {weeks.map((week, wi) => (
              <div key={wi} style={{ display: "flex", flexDirection: "column", gap: GAP }}>
                {week.map((cell) => {
                  const val = heatMode === "trimp"
                    ? (cell.data?.trimp || 0)
                    : (cell.data?.duration_min || 0);
                  const bg = cell.inYear ? heatColor(val, maxVal, heatMode) : "transparent";
                  return (
                    <div key={cell.date}
                      style={{ width: CELL, height: CELL, borderRadius: 2, backgroundColor: bg, cursor: cell.data ? "pointer" : "default", transition: "opacity 0.1s" }}
                      onMouseEnter={e => {
                        if (!cell.data && !cell.inYear) return;
                        const rect = e.currentTarget.getBoundingClientRect();
                        setTooltip({ x: rect.left + rect.width / 2, y: rect.top, cell });
                      }}
                      onMouseLeave={() => setTooltip(null)}
                    />
                  );
                })}
              </div>
            ))}
          </div>

          {/* カラースケール凡例 */}
          <div style={{ display: "flex", alignItems: "center", gap: 4, marginTop: 8, justifyContent: "flex-end" }}>
            <span style={{ fontSize: 9, color: "rgba(255,255,255,0.3)" }}>少</span>
            {[0, 0.2, 0.4, 0.65, 1].map(pct => (
              <div key={pct} style={{
                width: CELL, height: CELL, borderRadius: 2,
                backgroundColor: pct === 0 ? "rgba(255,255,255,0.05)" : heatColor(pct * 100, 100),
              }} />
            ))}
            <span style={{ fontSize: 9, color: "rgba(255,255,255,0.3)" }}>多</span>
          </div>
        </div>
      </div>

      {/* ツールチップ（Portal的に固定表示） */}
      {tooltip && tooltip.cell.inYear && (
        <div style={{
          position: "fixed",
          left: Math.min(tooltip.x, window.innerWidth - 200),
          top:  tooltip.y - 8,
          transform: "translate(-50%, -100%)",
          zIndex: 9999, pointerEvents: "none",
        }}
          className={`${glass} rounded-xl px-3 py-2 text-[11px] space-y-0.5 shadow-xl`}
        >
          <div className="font-semibold text-white/80">{tooltip.cell.date}</div>
          {tooltip.cell.data ? (
            <>
              <div className="text-white/50">{tooltip.cell.data.n_activities}件のアクティビティ</div>
              <div className="text-white/50">{tooltip.cell.data.activity_types.map(t => ACTIVITY_LABELS[t] || t).join(" · ")}</div>
              <div className="flex gap-2 text-white/40">
                <span>⏱ {tooltip.cell.data.duration_min}分</span>
                {tooltip.cell.data.elevation_gain_m > 0 && <span>↑{tooltip.cell.data.elevation_gain_m}m</span>}
                {tooltip.cell.data.avg_hr && <span>♥ {tooltip.cell.data.avg_hr}bpm</span>}
              </div>
              {tooltip.cell.data.trimp > 0 && (
                <div className="text-indigo-300">負荷 {tooltip.cell.data.trimp} TRIMP</div>
              )}
            </>
          ) : (
            <div className="text-white/30">活動なし</div>
          )}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: BodyMetricCard — 体の状態指標カード
───────────────────────────────────────────────────── */
function BodyMetricCard({ icon: Icon, label, value, unit, sub, color = "cyan", alert = false }) {
  const clr = {
    cyan:    "text-cyan-400   bg-cyan-500/10   border-cyan-500/20",
    indigo:  "text-indigo-400 bg-indigo-500/10 border-indigo-500/20",
    violet:  "text-violet-400 bg-violet-500/10 border-violet-500/20",
    emerald: "text-emerald-400 bg-emerald-500/10 border-emerald-500/20",
    amber:   "text-amber-400  bg-amber-500/10  border-amber-500/20",
    red:     "text-red-400    bg-red-500/10    border-red-500/20",
  }[alert ? "red" : color] ?? "";

  return (
    <div className={`${glass} rounded-2xl p-4 border ${clr.split(" ").slice(1).join(" ")}`}>
      <div className="flex items-center justify-between mb-2">
        <span className="text-[10px] text-white/40 font-medium uppercase tracking-wider">{label}</span>
        <Icon size={14} className={clr.split(" ")[0]} />
      </div>
      <div className="flex items-end gap-1">
        <span className="text-2xl font-bold text-white/90">{value ?? "--"}</span>
        {unit && <span className="text-xs text-white/40 mb-0.5">{unit}</span>}
      </div>
      {sub && <p className="text-[11px] text-white/35 mt-1">{sub}</p>}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: MiniTrendLine — 7日スパークライン（インライン）
───────────────────────────────────────────────────── */
function MiniTrendLine({ values, color = "#22d3ee", height = 32 }) {
  const cleaned = values.filter(v => v != null);
  if (cleaned.length < 2) return <div style={{ height }} className="flex items-center justify-center text-[10px] text-white/20">データなし</div>;
  const W = 120, H = height, P = 4;
  const vMin = Math.min(...cleaned), vMax = Math.max(...cleaned);
  const rng = vMax - vMin || 1;
  // 元の values 配列のindexで、nullを除いた点のみ表示
  const points = values.map((v, i) => v != null ? { x: P + (i / (values.length - 1)) * (W - P * 2), y: P + H - P * 2 - ((v - vMin) / rng) * (H - P * 2) } : null).filter(Boolean);
  const pts = points.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  const last = points[points.length - 1];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H}>
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" opacity="0.7" />
      {last && <circle cx={last.x} cy={last.y} r="2.5" fill={color} />}
    </svg>
  );
}

/* ─────────────────────────────────────────────────────
   MAIN: DashboardTab — 体の状態ダッシュボード
───────────────────────────────────────────────────── */
function DashboardTab() {
  const [dash,     setDash]     = useState(null);
  const [calData,  setCalData]  = useState(null);
  const [calYear,  setCalYear]  = useState(new Date().getFullYear());
  const [loading,  setLoading]  = useState(true);
  const [error,    setError]    = useState(null);

  const fetchAll = useCallback(async (year) => {
    setLoading(true); setError(null);
    try {
      const [d, c] = await Promise.all([
        getDashboard(),
        getActivityCalendar(year),
      ]);
      setDash(d);
      setCalData(c);
    } catch (e) {
      setError("データの取得に失敗しました。バックエンドが起動しているか確認してください。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchAll(calYear); }, [calYear, fetchAll]);

  const today    = dash?.today;
  const rec      = today?.recovery_score;
  const trend    = dash?.trend_7d;
  const recent   = dash?.recent_activities ?? [];
  const stats    = dash?.stats;
  const availYears = calData?.available_years ?? [new Date().getFullYear()];

  const recColor = {
    excellent: "emerald", good: "cyan", moderate: "amber", poor: "amber", rest: "red",
  }[rec?.status] ?? "cyan";

  return (
    <div className="space-y-5">

      {/* ── 週次AIコーチング ── */}
      <WeeklyCoachingCard />

      {/* ── ヘッダー ── */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-bold text-white/90">ダッシュボード</h2>
          <p className="text-xs text-white/40 mt-0.5">
            {today?.date ?? "今日"}<span className="hidden sm:inline"> · ログイン中はデータが継続して蓄積されます</span>
          </p>
        </div>
        <button onClick={() => fetchAll(calYear)} disabled={loading}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl border border-white/10 text-white/40 hover:text-white/70 hover:border-white/20 transition-all text-xs">
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} /> 更新
        </button>
      </div>

      {error && (
        <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-4 py-2.5">{error}</div>
      )}

      {loading && !dash ? (
        <div className="flex items-center justify-center py-20">
          <Loader2 size={32} className="animate-spin text-indigo-400" />
        </div>
      ) : (
      <>

      {/* ── 体の状態 カード4枚 ── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <BodyMetricCard
          icon={Heart} label="リカバリー" color={recColor}
          value={rec?.score != null ? Math.round(rec.score) : "--"} unit="pt"
          sub={rec?.status === "excellent" ? "絶好調" : rec?.status === "good" ? "良好" : rec?.status === "moderate" ? "普通" : rec?.status === "poor" ? "疲労" : rec?.status === "rest" ? "要休息" : "データなし"}
          alert={rec?.status === "rest"}
        />
        <BodyMetricCard
          icon={Dumbbell} label="フォーム (TSB)" color={today?.tsb >= 10 ? "emerald" : today?.tsb < -10 ? "red" : "indigo"}
          value={today?.tsb != null ? (today.tsb > 0 ? "+" : "") + today.tsb.toFixed(1) : "--"} unit=""
          sub={today?.tsb_status ?? "データなし"}
          alert={today?.tsb < -30}
        />
        <BodyMetricCard
          icon={Award} label="VO2max" color="violet"
          value={today?.vo2max?.toFixed(1) ?? "--"} unit="mL/kg/min"
          sub={today?.vo2max_interp}
        />
        <BodyMetricCard
          icon={Moon} label="睡眠" color="indigo"
          value={today?.sleep_hours?.toFixed(1) ?? "--"} unit="h"
          sub={today?.sleep_quality != null ? `品質スコア ${Math.round(today.sleep_quality)}` : "データなし"}
          alert={today?.sleep_hours != null && today.sleep_hours < 5}
        />
      </div>

      {/* ── 怪我リスク + 時間帯別パフォーマンス ── */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <InjuryRiskCard />
        <TimeOfDayCard />
      </div>

      {/* ── メインレイアウト: 左=体の詳細、右=ヒートマップ ── */}
      <div className="grid grid-cols-1 lg:grid-cols-[300px_1fr] gap-5">

        {/* 左カラム: 詳細ステータス + 最近のアクティビティ */}
        <div className="space-y-4">

          {/* 7日トレンド */}
          <div className={`${glass} rounded-2xl p-4 space-y-3`}>
            <div className="flex items-center gap-2 text-xs text-white/50 font-medium uppercase tracking-wider">
              <TrendingUp size={12} /> 直近7日のトレンド
            </div>
            {[
              ["HRV (RMSSD)", trend?.hrv,        "#34d399", "ms"],
              ["安静時心拍",  trend?.resting_hr,  "#f87171", "bpm"],
              ["ATL (疲労)",  trend?.atl,         "#fbbf24", ""],
              ["リカバリー",  trend?.recovery,    "#22d3ee", "pt"],
            ].map(([label, vals, color, unit]) => {
              const cleaned = (vals || []).filter(v => v != null);
              const latest  = cleaned[cleaned.length - 1];
              return (
                <div key={label} className="flex items-center gap-3">
                  <div className="w-16 text-[10px] text-white/40 shrink-0">{label}</div>
                  <div className="flex-1"><MiniTrendLine values={vals || []} color={color} /></div>
                  <div className="w-12 text-right text-xs font-mono text-white/60 shrink-0">
                    {latest != null ? latest.toFixed(unit === "pt" ? 0 : 1) : "--"}
                    <span className="text-[9px] text-white/30 ml-0.5">{unit}</span>
                  </div>
                </div>
              );
            })}
          </div>

          {/* 累計統計 */}
          {stats && (
            <div className={`${glass} rounded-2xl p-4 space-y-2`}>
              <div className="flex items-center gap-2 text-xs text-white/50 font-medium uppercase tracking-wider">
                <BarChart2 size={12} /> 累計実績
              </div>
              <div className="grid grid-cols-2 gap-2">
                {[
                  ["活動回数",   stats.total_activities,                   "回"],
                  ["総時間",     stats.total_duration_h,                   "h"],
                  ["総距離",     stats.total_distance_km?.toFixed(0),      "km"],
                  ["獲得標高",   stats.total_elevation_gain_m?.toLocaleString(), "m"],
                ].map(([l, v, u]) => (
                  <div key={l} className="bg-white/[0.02] border border-white/[0.04] rounded-xl px-3 py-2">
                    <div className="text-sm font-bold text-white/80">{v ?? "--"}<span className="text-[10px] text-white/30 ml-1">{u}</span></div>
                    <div className="text-[10px] text-white/35">{l}</div>
                  </div>
                ))}
              </div>
              {/* 活動種別 */}
              {Object.entries(stats.by_type || {}).length > 0 && (
                <div className="flex flex-wrap gap-1.5 pt-1">
                  {Object.entries(stats.by_type).map(([type, cnt]) => (
                    <span key={type} className="text-[10px] px-2 py-0.5 rounded-full bg-indigo-500/15 border border-indigo-500/20 text-indigo-300">
                      {ACTIVITY_LABELS[type] || type} {cnt}回
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* 最近のアクティビティ */}
          <div className={`${glass} rounded-2xl p-4 space-y-2`}>
            <div className="flex items-center gap-2 text-xs text-white/50 font-medium uppercase tracking-wider">
              <Activity size={12} /> 最近のアクティビティ
            </div>
            {recent.length === 0 ? (
              <p className="text-sm text-white/30 py-2 text-center">アクティビティがありません</p>
            ) : (
              <div className="space-y-1.5">
                {recent.slice(0, 6).map((a) => {
                  const typeColors = {
                    hiking:       "text-emerald-400", running: "text-cyan-400",
                    trail_running:"text-amber-400",   cycling: "text-violet-400",
                  };
                  return (
                    <div key={a.id} className="flex items-center gap-2 px-2 py-1.5 rounded-xl bg-white/[0.02] border border-white/[0.04]">
                      <div className="shrink-0 w-1.5 h-1.5 rounded-full" style={{ backgroundColor: { hiking: "#34d399", running: "#22d3ee", trail_running: "#fbbf24", cycling: "#a78bfa" }[a.activity_type] || "#fff" }} />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-1.5">
                          <span className={`text-[11px] font-medium ${typeColors[a.activity_type] ?? "text-white/60"}`}>
                            {ACTIVITY_LABELS[a.activity_type] || a.activity_type}
                          </span>
                          <span className="text-[10px] text-white/30">{a.date}</span>
                        </div>
                        <div className="flex gap-2 text-[10px] text-white/35 mt-0.5">
                          <span>⏱ {a.duration_min}分</span>
                          {a.elevation_gain_m > 0 && <span>↑{a.elevation_gain_m}m</span>}
                          {a.avg_hr && <span>♥ {a.avg_hr}bpm</span>}
                          {a.trimp && <span className="text-indigo-300/60">TRIMP {a.trimp}</span>}
                        </div>
                      </div>
                      <span className="text-[9px] text-white/20 shrink-0">{a.source}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* モデル状態 */}
          {dash?.model && (
            <div className={`${glass} rounded-2xl p-4 space-y-2`}>
              <div className="flex items-center gap-2 text-xs text-white/50 font-medium uppercase tracking-wider">
                <Brain size={12} /> 個人化モデル
              </div>
              <ModelProgressBar n={dash.model.n_activities ?? 0} threshold={5} />
              {dash.model.hr_mae != null && (
                <p className="text-[11px] text-white/40">
                  現在のHR予測精度: ±{dash.model.hr_mae} bpm
                  {dash.model.model_type && <span className="text-indigo-300/60 ml-1">({dash.model.model_type})</span>}
                </p>
              )}
            </div>
          )}
        </div>

        {/* 右カラム: アクティビティヒートマップ */}
        <div className={`${glass} rounded-2xl p-4 space-y-3`}>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-xs text-white/50 font-medium uppercase tracking-wider">
              <Activity size={12} /> アクティビティカレンダー
            </div>
            {/* 年切り替え */}
            <div className="flex items-center gap-1">
              {availYears.map(y => (
                <button key={y} onClick={() => setCalYear(y)}
                  className={`px-2 py-0.5 rounded-lg text-[11px] font-medium transition-all border ${
                    calYear === y
                      ? "bg-indigo-500/25 border-indigo-400/40 text-indigo-300"
                      : "border-white/10 text-white/40 hover:text-white/60"
                  }`}>
                  {y}
                </button>
              ))}
            </div>
          </div>

          {calData ? (
            <ActivityHeatmap calData={calData} />
          ) : (
            <div className="flex items-center justify-center py-16 text-white/30 text-sm">
              {loading ? <Loader2 size={20} className="animate-spin" /> : "アクティビティデータをアップロードするとカレンダーが表示されます"}
            </div>
          )}

          {/* データなし時のガイド */}
          {calData && calData.total_days === 0 && (
            <div className="text-center py-6 space-y-2">
              <p className="text-sm text-white/40">まだアクティビティが記録されていません</p>
              <p className="text-xs text-white/25">「コンディション管理」タブから Garmin FIT または Apple Health をアップロードしてください</p>
            </div>
          )}

          {/* 推奨メッセージ（リカバリーがある場合） */}
          {rec?.recommendation && (
            <div className={`px-4 py-2.5 rounded-xl text-sm border ${
              rec.status === "excellent" ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-300" :
              rec.status === "good"      ? "bg-cyan-500/10 border-cyan-500/20 text-cyan-300"         :
              rec.status === "moderate"  ? "bg-amber-500/10 border-amber-500/20 text-amber-300"      :
                                           "bg-red-500/10 border-red-500/20 text-red-300"
            }`}>
              {rec.recommendation}
            </div>
          )}
        </div>
      </div>

      </>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: InjuryRiskCard — 怪我リスク予測
───────────────────────────────────────────────────── */
function InjuryRiskCard() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    setLoading(true);
    getInjuryRisk().then(setData).catch(() => {}).finally(() => setLoading(false));
  }, []);
  if (loading) return <div className={`${glass} rounded-2xl p-4 text-center text-white/30 text-sm`}><Loader2 size={14} className="animate-spin inline mr-1" />怪我リスク分析中...</div>;
  if (!data) return null;
  const color = data.risk_level === "低" ? "emerald" : data.risk_level === "中" ? "amber" : "red";
  const colorMap = { emerald: "text-emerald-400 bg-emerald-500/10 border-emerald-500/20", amber: "text-amber-400 bg-amber-500/10 border-amber-500/20", red: "text-red-400 bg-red-500/10 border-red-500/20" };
  return (
    <div className={`${glass} rounded-2xl p-4 space-y-3`}>
      <div className="flex items-center gap-2">
        <Stethoscope size={15} className="text-rose-400" />
        <span className="text-sm font-semibold text-white/80">怪我リスク予測</span>
        <span className={`ml-auto text-xs px-2 py-0.5 rounded-full border font-bold ${colorMap[color]}`}>{data.risk_level} {data.risk_pct}%</span>
      </div>
      <div className="w-full bg-white/[0.06] rounded-full h-1.5">
        <div className={`h-1.5 rounded-full ${color === "emerald" ? "bg-emerald-400" : color === "amber" ? "bg-amber-400" : "bg-red-400"}`} style={{ width: `${data.risk_pct}%` }} />
      </div>
      <p className="text-xs text-white/50">{data.advice}</p>
      {data.factors && (
        <div className="flex flex-wrap gap-2 text-[10px] text-white/30">
          {data.factors.tsb_now != null && <span>TSB {data.factors.tsb_now}</span>}
          {data.factors.atl_7d_change_pct != null && <span>ATL変化 {data.factors.atl_7d_change_pct}%</span>}
          {data.factors.hrv_drop_pct != null && <span>HRV低下 {data.factors.hrv_drop_pct}%</span>}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: TimeOfDayCard — 時間帯別パフォーマンス
───────────────────────────────────────────────────── */
function TimeOfDayCard() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    setLoading(true);
    getTimeOfDayPerformance().then(setData).catch(() => {}).finally(() => setLoading(false));
  }, []);
  if (loading) return <div className={`${glass} rounded-2xl p-4 text-center text-white/30 text-sm`}><Loader2 size={14} className="animate-spin inline mr-1" />分析中...</div>;
  if (!data || !data.slots) return null;
  const active = data.slots.filter(s => s.n > 0);
  if (active.length === 0) return null;
  const maxRel = Math.max(...active.map(s => Math.abs(s.relative_pct)));
  return (
    <div className={`${glass} rounded-2xl p-4 space-y-3`}>
      <div className="flex items-center gap-2">
        <Clock size={15} className="text-violet-400" />
        <span className="text-sm font-semibold text-white/80">時間帯別パフォーマンス</span>
        {data.best_slot && <span className="ml-auto text-xs text-violet-300 bg-violet-500/10 px-2 py-0.5 rounded-full">最適: {data.best_slot}</span>}
      </div>
      <div className="space-y-1.5">
        {data.slots.filter(s => s.n > 0).map(s => (
          <div key={s.label} className="flex items-center gap-2 text-xs">
            <span className="w-14 text-white/40 shrink-0">{s.icon} {s.label}</span>
            <div className="flex-1 bg-white/[0.04] rounded-full h-1.5 relative">
              <div className={`h-1.5 rounded-full ${s.relative_pct >= 0 ? "bg-cyan-400" : "bg-white/20"}`}
                style={{ width: `${Math.abs(s.relative_pct) / Math.max(maxRel, 1) * 100}%`, marginLeft: s.relative_pct < 0 ? "0" : "0" }} />
            </div>
            <span className={`w-14 text-right font-mono ${s.relative_pct >= 0 ? "text-cyan-400" : "text-white/30"}`}>
              {s.relative_pct > 0 ? "+" : ""}{s.relative_pct}%
            </span>
            <span className="text-white/20 w-8 text-right">{s.n}回</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: WeeklyCoachingCard — 週次AIコーチング
───────────────────────────────────────────────────── */
function WeeklyCoachingCard() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(true);
  useEffect(() => {
    setLoading(true);
    getWeeklyCoaching().then(setData).catch(() => {}).finally(() => setLoading(false));
  }, []);
  if (loading) return <div className={`${glass} rounded-2xl p-4 text-xs text-white/30 flex items-center gap-2`}><Loader2 size={12} className="animate-spin" />週次コーチング生成中...</div>;
  if (!data) return null;
  return (
    <div className={`${glass} rounded-2xl p-4 space-y-2 border border-cyan-400/10`}>
      <button onClick={() => setOpen(o => !o)} className="w-full flex items-center gap-2">
        <Bot size={15} className="text-cyan-400 shrink-0" />
        <span className="text-sm font-semibold text-white/80 text-left flex-1">週次AIコーチング</span>
        <span className="text-[10px] text-white/20">{data.summary?.n_activities}回 / {data.summary?.total_km}km</span>
        <ChevronDown size={13} className={`text-white/30 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <>
          <p className="text-xs text-white/70 leading-relaxed whitespace-pre-wrap">{data.message}</p>
          <div className="flex items-center justify-between text-[10px] text-white/20 pt-1 border-t border-white/[0.05]">
            <span>CTL {data.metrics?.ctl} / TSB {data.metrics?.tsb}</span>
            <span>{data.has_ai ? "✨ AI生成" : "自動生成"}</span>
          </div>
        </>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   MAIN: ConditioningTab — コンディション管理タブ
───────────────────────────────────────────────────── */
function ConditioningTab() {
  // ── State ─────────────────────────────────────────
  const [recovery,   setRecovery]   = useState(null);
  const [loadData,   setLoadData]   = useState(null);
  const [vo2max,     setVo2max]     = useState(null);
  const [modelData,  setModelData]  = useState(null);
  const [fetchErr,   setFetchErr]   = useState(null);
  const [fetching,   setFetching]   = useState(false);

  const [fitLoading,  setFitLoading]  = useState(false);
  const [fitResult,   setFitResult]   = useState(null);
  const [fitError,    setFitError]    = useState(null);
  const [ahLoading,   setAhLoading]   = useState(false);
  const [ahResult,    setAhResult]    = useState(null);
  const [ahError,     setAhError]     = useState(null);

  const [hrMax,  setHrMax]  = useState(185);
  const [hrRest, setHrRest] = useState(60);

  // ── データ取得 ────────────────────────────────────
  const fetchAll = useCallback(async () => {
    setFetching(true); setFetchErr(null);
    try {
      const [rec, load, vo2, mdl] = await Promise.allSettled([
        getRecovery(),
        getTrainingLoad(90),
        getVo2maxTrend(),
        getPersonalModels(),
      ]);
      if (rec.status   === "fulfilled") setRecovery(rec.value);
      if (load.status  === "fulfilled") setLoadData(load.value);
      if (vo2.status   === "fulfilled") setVo2max(vo2.value);
      if (mdl.status   === "fulfilled") setModelData(mdl.value);

      // 認証エラーが含まれていたらエラー表示（401/403）
      const authFailed = [rec, load, vo2, mdl].some(
        r => r.status === "rejected" && [401, 403].includes(r.reason?.response?.status)
      );
      if (authFailed) setFetchErr("認証エラーが発生しました。再ログインしてください。");
    } catch (e) {
      setFetchErr("データの取得に失敗しました。バックエンドが起動しているか確認してください。");
    } finally {
      setFetching(false);
    }
  }, []);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  // ── FIT アップロード ──────────────────────────────
  const handleFitUpload = async (file) => {
    setFitLoading(true); setFitResult(null); setFitError(null);
    try {
      const r = await uploadGarminFit(file, hrMax, hrRest);
      setFitResult(r);
      fetchAll();
    } catch (e) {
      const status = e.response?.status;
      if (status === 401 || status === 403) {
        setFitError("認証エラーです。再ログインしてから再試行してください。");
      } else {
        setFitError(apiErr(e, "アップロードに失敗しました"));
      }
    } finally {
      setFitLoading(false);
    }
  };

  // ── Apple Health アップロード ────────────────────
  const handleAhUpload = async (file) => {
    setAhLoading(true); setAhResult(null); setAhError(null);
    const MAX_MB = 50;
    if (file.size > MAX_MB * 1024 * 1024) {
      setAhError(
        `ファイルサイズが ${MAX_MB}MB を超えています（${(file.size / 1024 / 1024).toFixed(0)}MB）。` +
        `export.zip のまま（解凍せず）アップロードするか、Apple Health アプリで期間を絞ってエクスポートしてください。`
      );
      setAhLoading(false);
      return;
    }
    try {
      const r = await uploadAppleHealth(file, hrMax, hrRest);
      setAhResult(r);
      if (r.status !== "processing") fetchAll();
    } catch (e) {
      const status = e.response?.status;
      if (status === 401 || status === 403) {
        setAhError("認証エラーです。再ログインしてから再試行してください。");
      } else {
        setAhError(apiErr(e, "アップロードに失敗しました"));
      }
    } finally {
      setAhLoading(false);
    }
  };

  // ── Render ────────────────────────────────────────
  return (
    <div className="space-y-6">

      {/* ── ヘッダー ── */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-bold text-white/90">コンディション管理</h2>
          <p className="text-xs text-white/40 mt-0.5 hidden sm:block">
            Garmin / Apple Health のデータを蓄積して個人に最適化されたモデルを構築
          </p>
        </div>
        <button
          onClick={fetchAll}
          disabled={fetching}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl border border-white/10 text-white/40 hover:text-white/70 hover:border-white/20 transition-all text-xs"
        >
          <RefreshCw size={12} className={fetching ? "animate-spin" : ""} /> 更新
        </button>
      </div>

      {fetchErr && (
        <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-4 py-2.5">
          {fetchErr}
        </div>
      )}

      {/* ── セクション1: データアップロード ── */}
      <div className={`${glass} rounded-2xl p-5 space-y-4`}>
        <div className="flex items-center gap-2 text-xs text-white/50 font-medium uppercase tracking-wider">
          <Upload size={12} /> データを取り込む
        </div>

        {/* HR Max / Rest 設定 */}
        <div className="grid grid-cols-2 gap-3">
          {[
            ["最大心拍数 (bpm)", hrMax, setHrMax, 140, 220],
            ["安静時心拍数 (bpm)", hrRest, setHrRest, 30, 80],
          ].map(([label, val, setter, min, max]) => (
            <div key={label}>
              <label className="text-[10px] text-white/35 block mb-1">{label}</label>
              <input
                type="number" min={min} max={max} value={val}
                onChange={e => setter(Number(e.target.value))}
                className={`${glass} w-full px-3 py-1.5 rounded-xl text-sm text-white/80 outline-none bg-transparent`}
              />
            </div>
          ))}
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <FileDropZone
            accept=".fit"
            label="Garmin FIT ファイル"
            hint="Garmin Connect → アクティビティ → エクスポート → 元のファイル"
            icon={Watch}
            onFile={handleFitUpload}
            loading={fitLoading}
            result={fitResult ? `${fitResult.activity_type} / ${fitResult.duration_min}分 / HR avg ${fitResult.avg_hr ?? "--"} bpm` : null}
            error={fitError}
          />
          <FileDropZone
            accept=".xml,.zip"
            label="Apple Health エクスポート"
            hint="ヘルスケアApp → 右上アイコン → ヘルスケアデータを書き出す"
            icon={Smartphone}
            onFile={handleAhUpload}
            loading={ahLoading}
            result={ahResult ? (ahResult.status === "processing" ? ahResult.message : `${ahResult.n_workouts}件のワークアウト / ${ahResult.n_daily_metrics}日分のデータを取り込みました`) : null}
            error={ahError}
          />
        </div>

        {/* アップロード後の再学習結果 */}
        {(fitResult?.model_retrained || (ahResult?.model_retrained && ahResult?.status !== "processing")) && (() => {
          const r = fitResult?.model_retrained ?? ahResult?.model_retrained;
          return (
            <div className="text-[11px] bg-indigo-500/10 border border-indigo-500/20 rounded-xl px-3 py-2 text-indigo-300">
              モデル再学習完了 — {r.n_activities}件 / {r.n_points?.toLocaleString()}ポイント
              {r.hr_mae != null && ` · HR誤差: ±${r.hr_mae} bpm`}
              {r.pace_mae != null && ` · ペース誤差: ±${(r.pace_mae * 1000 / 60).toFixed(1)} sec/km`}
            </div>
          );
        })()}
      </div>

      {/* ── セクション2: 今日のコンディション ── */}
      <div className={`${glass} rounded-2xl p-5`}>
        <div className="flex items-center gap-2 mb-4 text-xs text-white/50 font-medium uppercase tracking-wider">
          <Heart size={12} /> 今日のコンディション
        </div>

        {recovery == null ? (
          <div className="text-center py-8 text-white/30 text-sm">
            {fetching ? <Loader2 size={20} className="animate-spin mx-auto" /> : "Garmin / Apple Health データをアップロードするとコンディションが表示されます"}
          </div>
        ) : recovery.recovery_score == null ? (
          <p className="text-sm text-white/40 py-4 text-center">{recovery.message}</p>
        ) : (
          <div className="space-y-5">
            {/* スコアリング */}
            <div className="flex flex-col sm:flex-row items-center gap-6">
              <RecoveryRing score={recovery.recovery_score} status={recovery.status} size={160} />
              <div className="flex-1 space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-white/50">パフォーマンス準備度</span>
                  <span className="text-sm font-bold text-indigo-300">{recovery.readiness_score} / 100</span>
                </div>
                {/* コンポーネント内訳 */}
                {recovery.components && Object.entries({
                  hrv:   ["HRV（自律神経）",  "#34d399"],
                  sleep: ["睡眠",             "#818cf8"],
                  rhr:   ["安静時心拍",        "#22d3ee"],
                  load:  ["トレーニング負荷",  "#fbbf24"],
                }).map(([key, [label, color]]) => {
                  const val = recovery.components[key] ?? 50;
                  return (
                    <div key={key} className="space-y-0.5">
                      <div className="flex justify-between text-[11px]">
                        <span className="text-white/40">{label}</span>
                        <span className="text-white/60">{Math.round(val)}</span>
                      </div>
                      <div className="h-1 rounded-full bg-white/[0.05] overflow-hidden">
                        <div className="h-full rounded-full" style={{ width: `${val}%`, backgroundColor: color, opacity: 0.7 }} />
                      </div>
                    </div>
                  );
                })}
                {/* 信頼度 */}
                {recovery.confidence != null && (
                  <p className="text-[10px] text-white/25">
                    モデル信頼度: {Math.round(recovery.confidence * 100)}%
                    （{recovery.n_baseline_days ?? 0}日分のベースラインで個人最適化済み）
                  </p>
                )}
              </div>
            </div>

            {/* 推奨メッセージ */}
            {recovery.recommendation && (
              <div className={`px-4 py-3 rounded-xl text-sm leading-relaxed border ${
                recovery.status === "excellent" ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-300" :
                recovery.status === "good"      ? "bg-cyan-500/10 border-cyan-500/20 text-cyan-300"         :
                recovery.status === "moderate"  ? "bg-amber-500/10 border-amber-500/20 text-amber-300"      :
                                                  "bg-red-500/10 border-red-500/20 text-red-300"
              }`}>
                {recovery.recommendation}
              </div>
            )}

            {/* 生データ */}
            {recovery.raw && (
              <div className="grid grid-cols-3 sm:grid-cols-4 gap-2">
                {[
                  ["HRV",     recovery.raw.hrv_rmssd != null    ? `${recovery.raw.hrv_rmssd?.toFixed(0)} ms`    : "--", HeartPulse],
                  ["安静HR",  recovery.raw.resting_hr != null   ? `${recovery.raw.resting_hr?.toFixed(0)} bpm`  : "--", Activity  ],
                  ["睡眠",    recovery.raw.sleep_hours != null  ? `${recovery.raw.sleep_hours?.toFixed(1)} h`   : "--", Moon      ],
                  ["SpO2",    recovery.raw.spo2_pct != null     ? `${recovery.raw.spo2_pct?.toFixed(0)} %`      : "--", Gauge     ],
                  ["ATL",     recovery.raw.atl != null          ? recovery.raw.atl?.toFixed(1)                  : "--", TrendingUp],
                  ["CTL",     recovery.raw.ctl != null          ? recovery.raw.ctl?.toFixed(1)                  : "--", TrendingUp],
                  ["TSB",     recovery.raw.tsb != null          ? (recovery.raw.tsb > 0 ? "+" : "") + recovery.raw.tsb?.toFixed(1) : "--", Dumbbell],
                ].map(([label, val, Icon]) => (
                  <div key={label} className="bg-white/[0.02] border border-white/[0.05] rounded-xl px-3 py-2 text-center">
                    <div className="text-xs font-semibold text-white/70">{val}</div>
                    <div className="text-[10px] text-white/30 mt-0.5">{label}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* ── セクション3: トレーニング負荷 PMC ── */}
      <div className={`${glass} rounded-2xl p-5 space-y-3`}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-xs text-white/50 font-medium uppercase tracking-wider">
            <Dumbbell size={12} /> トレーニング負荷（ATL / CTL / TSB）
          </div>
          {loadData?.current_status && (
            <TSBStatusBadge status={loadData.current_status} tsb={loadData.current_tsb} />
          )}
        </div>

        {/* 凡例 */}
        <div className="flex gap-4 text-[11px]">
          {[["CTL (フィットネス)", "#22d3ee"], ["ATL (疲労)", "#f87171"], ["TSB (コンディション)", "#a78bfa"]].map(([lbl, clr]) => (
            <div key={lbl} className="flex items-center gap-1.5">
              <div className="w-3 h-0.5 rounded" style={{ backgroundColor: clr }} />
              <span className="text-white/40">{lbl}</span>
            </div>
          ))}
        </div>

        {!loadData || loadData.n_data_points === 0 ? (
          <div className="py-8 text-center text-white/30 text-sm">
            {fetching ? <Loader2 size={20} className="animate-spin mx-auto" /> : "アクティビティデータをアップロードするとグラフが表示されます"}
          </div>
        ) : (
          <>
            <div className={`${glass} rounded-xl p-2`}>
              <PMCChart data={loadData.chart} />
            </div>

            {/* TSBガイド */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[10px]">
              {[
                ["TSB > +10",   "フレッシュ",   "text-emerald-400"],
                ["TSB 0〜+10",  "通常",         "text-cyan-400"   ],
                ["TSB -10〜0",  "軽度疲労",     "text-amber-400"  ],
                ["TSB < -30",   "過負荷 ⚠",   "text-red-400"    ],
              ].map(([range, label, clr]) => (
                <div key={range} className="bg-white/[0.02] border border-white/[0.04] rounded-lg px-2 py-1.5 text-center">
                  <div className={`font-mono font-bold ${clr}`}>{range}</div>
                  <div className="text-white/30 mt-0.5">{label}</div>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {/* ── セクション4: VO2max トレンド ── */}
      <div className={`${glass} rounded-2xl p-5 space-y-3`}>
        <div className="flex items-center gap-2 text-xs text-white/50 font-medium uppercase tracking-wider">
          <Award size={12} /> VO2max トレンド
        </div>

        {!vo2max || vo2max.trend?.length === 0 ? (
          <p className="py-4 text-center text-sm text-white/30">
            {fetching ? <Loader2 size={20} className="animate-spin inline mr-2" /> : ""}
            {vo2max?.message || "Garmin FIT データをアップロードするとVO2max推定が表示されます"}
          </p>
        ) : (
          <div className="space-y-3">
            <div className="flex items-end gap-4">
              <div>
                <div className="text-3xl font-bold text-indigo-300">{vo2max.latest_vo2max}</div>
                <div className="text-xs text-white/40">mL/kg/min</div>
              </div>
              <div className="text-xs text-white/40 pb-1">{vo2max.interpretation}</div>
            </div>
            <div className={`${glass} rounded-xl p-2`}>
              <SparkLine values={vo2max.trend.map(d => d.vo2max)} color="#818cf8" height={70} />
            </div>
            <p className="text-[10px] text-white/25">
              ※ 活動時の平均心拍・速度・標高から Daniels-Gilbert 式で推定。精度は活動強度が高いほど向上します。
            </p>
          </div>
        )}
      </div>

      {/* ── セクション5: 個人化モデルの状態 ── */}
      <div className={`${glass} rounded-2xl p-5 space-y-4`}>
        <div className="flex items-center gap-2 text-xs text-white/50 font-medium uppercase tracking-wider">
          <Brain size={12} /> 個人化モデルの学習状態
        </div>

        {!modelData ? (
          <p className="py-4 text-center text-sm text-white/30">
            {fetching ? <Loader2 size={20} className="animate-spin inline mr-2" /> : "モデル情報を取得できませんでした"}
          </p>
        ) : (
          <div className="space-y-4">
            {/* メッセージ */}
            <p className="text-sm text-white/60">{modelData.message}</p>

            {/* アクティビティ数プログレス */}
            <ModelProgressBar n={modelData.n_activities ?? 0} threshold={modelData.gb_threshold ?? 5} />

            {/* 最新精度 */}
            {modelData.latest_metrics && (
              <div className="grid grid-cols-2 gap-3">
                {[
                  ["HR予測精度", `±${modelData.latest_metrics.hr_mae} bpm`, modelData.latest_metrics.hr_r2, "#22d3ee"],
                  ["ペース予測精度", `±${(modelData.latest_metrics.pace_mae * 1000 / 60).toFixed(1)} sec/km`, modelData.latest_metrics.pace_r2, "#818cf8"],
                ].map(([label, err, r2, clr]) => (
                  <div key={label} className="bg-white/[0.02] border border-white/[0.04] rounded-xl p-3">
                    <div className="text-[10px] text-white/40 mb-1">{label}</div>
                    <div className="text-sm font-bold" style={{ color: clr }}>{err}</div>
                    {r2 != null && !isNaN(r2) && (
                      <div className="text-[10px] text-white/30 mt-0.5">R² = {r2.toFixed(3)}</div>
                    )}
                  </div>
                ))}
              </div>
            )}

            {/* 精度推移グラフ */}
            {modelData.accuracy_trend?.hr_mae?.length > 1 && (
              <div className="space-y-1">
                <p className="text-[11px] text-white/40">HR誤差の推移（データが増えるほど低下）</p>
                <div className={`${glass} rounded-xl p-2`}>
                  <SparkLine
                    values={modelData.accuracy_trend.hr_mae.map(v => -v)}
                    color="#22d3ee"
                    height={50}
                  />
                </div>
                <p className="text-[10px] text-white/25">グラフ右上ほど精度が高い（誤差が小さい）</p>
              </div>
            )}

            {/* モデル切替タイミング案内 */}
            <div className="bg-white/[0.02] border border-white/[0.04] rounded-xl px-4 py-3 text-[11px] text-white/40 leading-relaxed">
              <span className="text-white/60 font-medium">モデル自動切替:</span>{" "}
              1〜{(modelData.gb_threshold ?? 5) - 1}件は BayesianRidge（少量データで安定）→
              {modelData.gb_threshold ?? 5}件以上で GradientBoosting（高精度・非線形）に自動切替。
              データが増えるほどあなた固有の心拍・疲労パターンに適合します。
            </div>
          </div>
        )}

        {/* Feature Importance */}
        <FeatureImportanceCard />

      </div>

      {/* ── セクション6: 音声入力でアクティビティ記録 ── */}
      <VoiceInputPanel />

    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: VoiceInputButton — 音声入力でアクティビティ記録
───────────────────────────────────────────────────── */
function VoiceInputPanel() {
  const [listening, setListening] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [parsed, setParsed] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const recognitionRef = useRef(null);
  const supported = typeof window !== "undefined" && ("SpeechRecognition" in window || "webkitSpeechRecognition" in window);

  const startListen = () => {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return;
    const r = new SR();
    r.lang = "ja-JP";
    r.continuous = false;
    r.interimResults = false;
    r.onresult = (e) => {
      const text = e.results[0][0].transcript;
      setTranscript(text);
      setListening(false);
    };
    r.onerror = () => setListening(false);
    r.onend = () => setListening(false);
    recognitionRef.current = r;
    r.start();
    setListening(true);
    setError(null);
    setParsed(null);
  };

  const stopListen = () => {
    recognitionRef.current?.stop();
    setListening(false);
  };

  const handleParse = async () => {
    if (!transcript.trim()) return;
    setLoading(true); setError(null); setParsed(null);
    try {
      const r = await parseActivityText(transcript);
      setParsed(r);
    } catch (e) {
      setError(apiErr(e, "解析に失敗しました"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className={`${glass} rounded-2xl p-5 space-y-4`}>
      <div className="flex items-center gap-2 text-cyan-300 font-semibold text-sm">
        <Mic size={16} /> 音声でアクティビティを記録
        <span className="ml-auto text-[10px] text-white/30 font-normal">日本語対応</span>
      </div>

      {!supported ? (
        <p className="text-xs text-white/30">このブラウザは音声入力に対応していません（Chrome推奨）</p>
      ) : (
        <div className="space-y-3">
          <div className="flex gap-2">
            <button
              onClick={listening ? stopListen : startListen}
              className={`flex items-center gap-2 px-4 py-2.5 rounded-xl font-medium text-sm transition-all ${
                listening
                  ? "bg-red-500/20 border border-red-400/30 text-red-300 animate-pulse"
                  : "bg-cyan-500/15 border border-cyan-400/25 text-cyan-300 hover:bg-cyan-500/25"
              }`}>
              {listening ? <MicOff size={14} /> : <Mic size={14} />}
              {listening ? "録音停止" : "録音開始"}
            </button>
            {transcript && !loading && (
              <button onClick={handleParse}
                className="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-indigo-500/15 border border-indigo-400/25 text-indigo-300 hover:bg-indigo-500/25 text-sm transition-all">
                <Brain size={14} /> 解析
              </button>
            )}
          </div>

          {transcript && (
            <div className="bg-white/[0.03] border border-white/[0.07] rounded-xl px-3 py-2">
              <p className="text-xs text-white/30 mb-1">認識されたテキスト:</p>
              <p className="text-sm text-white/70">{transcript}</p>
            </div>
          )}

          {loading && <div className="text-xs text-white/30 flex items-center gap-2"><Loader2 size={12} className="animate-spin" />AIが解析中...</div>}

          {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">{error}</div>}

          {parsed && (
            <div className="bg-emerald-500/10 border border-emerald-500/20 rounded-xl p-3 space-y-2">
              <p className="text-xs text-emerald-300 font-semibold">解析結果</p>
              <div className="grid grid-cols-2 gap-2 text-xs">
                {parsed.activity_type && <div className="text-white/50">種別: <span className="text-white/80">{parsed.activity_type}</span></div>}
                {parsed.duration_min && <div className="text-white/50">時間: <span className="text-white/80">{parsed.duration_min}分</span></div>}
                {parsed.distance_km && <div className="text-white/50">距離: <span className="text-white/80">{parsed.distance_km}km</span></div>}
                {parsed.avg_hr && <div className="text-white/50">平均HR: <span className="text-white/80">{parsed.avg_hr}bpm</span></div>}
                {parsed.elevation_gain_m && <div className="text-white/50">獲得標高: <span className="text-white/80">{parsed.elevation_gain_m}m</span></div>}
                {parsed.notes && <div className="col-span-2 text-white/50">メモ: <span className="text-white/70">{parsed.notes}</span></div>}
              </div>
              {parsed.confidence && (
                <p className="text-[10px] text-white/25">信頼度: {Math.round(parsed.confidence * 100)}%</p>
              )}
            </div>
          )}

          <p className="text-[10px] text-white/25">例:「昨日10km走って1時間かかった、心拍140くらい」</p>
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   AIQueryPanel — AI自然言語クエリパネル
───────────────────────────────────────────────────── */
function AIQueryPanel() {
  const [question, setQuestion] = useState("");
  const [loading,  setLoading]  = useState(false);
  const [result,   setResult]   = useState(null);
  const [error,    setError]    = useState(null);

  const SUGGESTIONS = [
    "先月一番疲労が高い日はいつ？",
    "直近4週間のトレーニング量の傾向を教えて",
    "今のコンディションで重要な山行に出ても大丈夫？",
    "VO2maxはこの3ヶ月で改善している？",
    "週あたりのTRIMPが最も高かった週はいつ？",
  ];

  const handleQuery = async () => {
    if (!question.trim()) return;
    setLoading(true); setError(null); setResult(null);
    try {
      const r = await aiQuery(question);
      setResult(r);
    } catch (e) {
      setError(apiErr(e, "クエリの実行に失敗しました"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className={`${glass} rounded-2xl p-5 space-y-4`}>
      <div className="flex items-center gap-2 text-cyan-300 font-semibold text-sm">
        <Bot size={16} /> AI自然言語クエリ
        <span className="ml-auto text-[10px] text-white/30 font-normal">Claude API</span>
      </div>

      {/* テキスト入力 */}
      <div className="flex gap-2">
        <input
          type="text"
          value={question}
          onChange={e => setQuestion(e.target.value)}
          onKeyDown={e => e.key === "Enter" && handleQuery()}
          placeholder="例: 先月一番疲労が高い日はいつ？"
          className={`${glass} flex-1 px-3 py-2 rounded-xl text-white/80 text-sm outline-none placeholder:text-white/25`}
        />
        <button
          onClick={handleQuery}
          disabled={loading || !question.trim()}
          className="px-4 py-2 rounded-xl bg-cyan-500/20 border border-cyan-400/30 text-cyan-300 text-sm font-medium disabled:opacity-40 hover:bg-cyan-500/30 transition-all flex items-center gap-1.5"
        >
          {loading ? <Loader2 size={13} className="animate-spin" /> : <Bot size={13} />}
          聞く
        </button>
      </div>

      {/* サジェスト */}
      <div className="flex flex-wrap gap-1.5">
        {SUGGESTIONS.map(s => (
          <button key={s} onClick={() => setQuestion(s)}
            className="text-[10px] text-white/40 hover:text-white/70 bg-white/[0.03] hover:bg-white/[0.06] border border-white/[0.06] rounded-lg px-2 py-1 transition-all">
            {s}
          </button>
        ))}
      </div>

      {/* エラー */}
      {error && (
        <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">
          ⚠ {error}
        </div>
      )}

      {/* 回答 */}
      {result && (
        <div className="space-y-3 border-t border-white/[0.06] pt-3">
          <div className="text-sm text-white/80 leading-relaxed whitespace-pre-wrap">
            {result.answer}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {result.data_used?.map(d => (
              <span key={d} className="text-[10px] text-indigo-300/60 bg-indigo-500/10 border border-indigo-400/20 rounded-md px-1.5 py-0.5">{d}</span>
            ))}
            <span className={`text-[10px] rounded-md px-1.5 py-0.5 ${
              result.confidence === "high"
                ? "text-green-300/60 bg-green-500/10 border border-green-400/20"
                : "text-amber-300/60 bg-amber-500/10 border border-amber-400/20"
            }`}>
              {result.confidence === "high" ? "高信頼度" : "中信頼度"}
            </span>
            {!result.has_ai && (
              <span className="text-[10px] text-white/30 bg-white/5 border border-white/10 rounded-md px-1.5 py-0.5">統計モード（AIキー未設定）</span>
            )}
          </div>
          {result.follow_up_suggestions?.length > 0 && (
            <div>
              <div className="text-[10px] text-white/30 mb-1.5">フォローアップ</div>
              <div className="flex flex-wrap gap-1.5">
                {result.follow_up_suggestions.map(s => (
                  <button key={s} onClick={() => setQuestion(s)}
                    className="text-[10px] text-cyan-300/50 hover:text-cyan-300 bg-cyan-500/5 hover:bg-cyan-500/10 border border-cyan-400/15 rounded-lg px-2 py-1 transition-all">
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   RaceSimulatorPanel — レースシミュレーターパネル
───────────────────────────────────────────────────── */
function RaceSimulatorPanel() {
  const [form, setForm] = useState({
    target_hours:   4, target_mins: 0,
    distance_km:    20, elevation_gain_m: 1000,
    activity_type:  "hiking",
    body_weight:    65, load_weight: 8,
  });
  const [loading, setLoading] = useState(false);
  const [result,  setResult]  = useState(null);
  const [error,   setError]   = useState(null);
  const [showSplits, setShowSplits] = useState(false);

  // ルートポイントを距離・標高プロファイルから近似生成
  const buildRoutePoints = () => {
    const pts = [];
    const n = Math.max(20, form.distance_km * 2);
    for (let i = 0; i <= n; i++) {
      const t = i / n;
      const d = t * form.distance_km * 1000;
      // 前半登り・後半下りの単純プロファイル
      const elev = t < 0.5
        ? t * 2 * form.elevation_gain_m
        : (1 - (t - 0.5) * 2) * form.elevation_gain_m;
      pts.push({ distance_m: d, elevation_m: elev });
    }
    return pts;
  };

  const handleSim = async () => {
    setLoading(true); setError(null); setResult(null);
    const target_min = form.target_hours * 60 + form.target_mins;
    try {
      const r = await simulateRace({
        route_points:   buildRoutePoints(),
        target_time_min: target_min,
        activity_type:  form.activity_type,
        body_weight:    form.body_weight,
        load_weight:    form.load_weight,
        segment_km:     1.0,
        use_personal_model: true,
      });
      setResult(r);
    } catch (e) {
      setError(apiErr(e, "シミュレーション失敗"));
    } finally {
      setLoading(false);
    }
  };

  const fmtMin = (m) => {
    const h = Math.floor(m / 60), min = Math.round(m % 60);
    return h > 0 ? `${h}h${min}m` : `${min}m`;
  };

  const feasColor = (pct) =>
    pct >= 80 ? "text-green-300" : pct >= 60 ? "text-amber-300" : "text-red-300";

  return (
    <div className={`${glass} rounded-2xl p-5 space-y-4`}>
      <div className="flex items-center gap-2 text-violet-300 font-semibold text-sm">
        <Flag size={16} /> レースシミュレーター
      </div>

      <div className="grid grid-cols-2 gap-3 text-xs">
        {/* アクティビティ */}
        <div className="col-span-2">
          <label className="text-white/40 block mb-1">アクティビティ</label>
          <select value={form.activity_type}
            onChange={e => setForm(f => ({ ...f, activity_type: e.target.value }))}
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 text-xs outline-none`}>
            <option value="hiking">登山・ハイキング</option>
            <option value="trail_running">トレイルランニング</option>
            <option value="running">ロードランニング</option>
            <option value="cycling">サイクリング</option>
          </select>
        </div>

        {/* 目標タイム */}
        <div>
          <label className="text-white/40 block mb-1">目標タイム（時間）</label>
          <input type="number" min={0} max={72} value={form.target_hours}
            onChange={e => setForm(f => ({ ...f, target_hours: +e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">目標タイム（分）</label>
          <input type="number" min={0} max={59} value={form.target_mins}
            onChange={e => setForm(f => ({ ...f, target_mins: +e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
        </div>

        {/* コース */}
        <div>
          <label className="text-white/40 block mb-1">距離 (km)</label>
          <input type="number" min={1} max={500} value={form.distance_km}
            onChange={e => setForm(f => ({ ...f, distance_km: +e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">獲得標高 (m)</label>
          <input type="number" min={0} max={10000} value={form.elevation_gain_m}
            onChange={e => setForm(f => ({ ...f, elevation_gain_m: +e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
        </div>

        {/* 体重 */}
        <div>
          <label className="text-white/40 block mb-1">体重 (kg)</label>
          <input type="number" min={20} max={150} value={form.body_weight}
            onChange={e => setForm(f => ({ ...f, body_weight: +e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">荷物 (kg)</label>
          <input type="number" min={0} max={50} value={form.load_weight}
            onChange={e => setForm(f => ({ ...f, load_weight: +e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
        </div>
      </div>

      <button onClick={handleSim} disabled={loading}
        className="w-full py-2 rounded-xl bg-violet-500/20 border border-violet-400/30 text-violet-300 text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-40 hover:bg-violet-500/30 transition-all">
        {loading ? <><Loader2 size={14} className="animate-spin" />シミュレーション中</> : <><Flag size={14} />シミュレーション実行</>}
      </button>

      {error && (
        <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">⚠ {error}</div>
      )}

      {result && (
        <div className="space-y-3 border-t border-white/[0.06] pt-3">
          {/* KPI カード */}
          <div className="grid grid-cols-2 gap-2 text-xs">
            {[
              ["目標タイム",   fmtMin(result.target_time_min),    "text-white/70"],
              ["予測タイム",   fmtMin(result.predicted_time_min), "text-cyan-300"],
              ["総消費Cal",   `${result.total_energy_kcal} kcal`, "text-amber-300"],
              ["実現可能性",   `${result.feasibility_pct}%`,       feasColor(result.feasibility_pct)],
            ].map(([k, v, cls]) => (
              <div key={k} className={`${glass} rounded-xl p-2.5`}>
                <div className="text-white/40">{k}</div>
                <div className={`font-semibold mt-0.5 ${cls}`}>{v}</div>
              </div>
            ))}
          </div>

          {result.avg_hr && (
            <div className="flex items-center gap-2 text-xs text-white/50">
              <Heart size={12} className="text-red-400" />
              平均予測心拍: <span className="text-red-300 font-semibold">{result.avg_hr} bpm</span>
              <span className="text-white/30 ml-1">(個人化モデル)</span>
            </div>
          )}

          {/* 警告 */}
          {result.warnings?.length > 0 && (
            <div className="space-y-1">
              {result.warnings.map((w, i) => (
                <div key={i} className="text-xs text-amber-300 bg-amber-500/10 border border-amber-400/20 rounded-xl px-3 py-2">
                  ⚠ {w}
                </div>
              ))}
            </div>
          )}

          {/* 補給推奨 */}
          {result.supply_recommendations?.length > 0 && (
            <div>
              <div className="text-[10px] text-white/30 uppercase tracking-wider mb-1.5">補給推奨タイミング</div>
              <div className="space-y-1">
                {result.supply_recommendations.map((r, i) => (
                  <div key={i} className="flex items-center gap-2 text-xs text-white/60">
                    <span className="font-mono text-white/40 w-12">{fmtMin(r.at_min)}</span>
                    <span className="text-cyan-300 font-semibold">{r.kcal}kcal</span>
                    <span className="text-white/30">{r.reason}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* スプリット展開 */}
          <button onClick={() => setShowSplits(!showSplits)}
            className="w-full flex items-center justify-between text-xs text-white/40 hover:text-white/60 py-1 transition-colors">
            <span>スプリット詳細 ({result.splits?.length}件)</span>
            <ChevronDown size={13} className={`transition-transform ${showSplits ? "rotate-180" : ""}`} />
          </button>
          {showSplits && (
            <div className="max-h-48 overflow-y-auto space-y-0.5">
              <div className="grid grid-cols-5 text-[10px] text-white/30 px-2 mb-1">
                <span>km</span><span>経過</span><span>ペース</span><span>勾配</span><span>累計cal</span>
              </div>
              {result.splits?.map((s, i) => (
                <div key={i} className="grid grid-cols-5 text-[10px] text-white/60 bg-white/[0.02] rounded-lg px-2 py-1">
                  <span>{s.km}</span>
                  <span>{fmtMin(s.elapsed_min)}</span>
                  <span>{s.pace_min_per_km.toFixed(1)}/km</span>
                  <span className={s.grade_pct > 10 ? "text-red-300" : s.grade_pct < -5 ? "text-blue-300" : ""}>{s.grade_pct}%</span>
                  <span>{s.energy_kcal_cum.toFixed(0)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   TrainingPlanPanel — 自動トレーニングプランパネル
───────────────────────────────────────────────────── */
function TrainingPlanPanel() {
  const [form, setForm] = useState({
    race_name:        "",
    race_date:        "",
    race_distance_km: 42,
    activity_type:    "running",
  });
  const [loading,    setLoading]    = useState(false);
  const [plan,       setPlan]       = useState(null);
  const [savedPlans, setSavedPlans] = useState([]);
  const [error,      setError]      = useState(null);
  const [activeWeek, setActiveWeek] = useState(0);

  useEffect(() => {
    listTrainingPlans().then(setSavedPlans).catch(() => {});
  }, []);

  const handleGenerate = async () => {
    if (!form.race_name.trim() || !form.race_date) return;
    setLoading(true); setError(null);
    try {
      const r = await createTrainingPlan(form);
      setPlan(r);
      setActiveWeek(0);
      listTrainingPlans().then(setSavedPlans).catch(() => {});
    } catch (e) {
      setError(apiErr(e, "プラン生成失敗"));
    } finally {
      setLoading(false);
    }
  };

  const handleLoad = async (id) => {
    try {
      const r = await getTrainingPlan(id);
      setPlan(r); setActiveWeek(0);
    } catch(e) {}
  };

  const handleDelete = async (id) => {
    try {
      await deleteTrainingPlan(id);
      setSavedPlans(p => p.filter(x => x.id !== id));
      if (plan?.plan_id === id) setPlan(null);
    } catch(e) {}
  };

  const PHASE_COLORS = {
    base:  "bg-blue-500/20 border-blue-400/30 text-blue-300",
    build: "bg-orange-500/20 border-orange-400/30 text-orange-300",
    peak:  "bg-red-500/20 border-red-400/30 text-red-300",
    taper: "bg-green-500/20 border-green-400/30 text-green-300",
  };

  const WORKOUT_ICONS = {
    REST: "😴", EASY: "🚶", LONG: "🏃", TEMPO: "💨",
    INTERVAL: "⚡", RACE: "🏆", CROSS: "🏊",
  };

  const WORKOUT_COLORS = {
    REST:     "text-white/20",
    EASY:     "text-blue-300",
    LONG:     "text-indigo-300",
    TEMPO:    "text-orange-300",
    INTERVAL: "text-red-300",
    RACE:     "text-yellow-300",
    CROSS:    "text-teal-300",
  };

  const WORKOUT_LABELS = {
    REST:     "完全休養",
    EASY:     "有酸素ランニング（イージー）",
    LONG:     "ロング走（LSD）",
    TEMPO:    "テンポ走（乳酸閾値）",
    INTERVAL: "インターバルトレーニング",
    RACE:     "目標レース",
    CROSS:    "クロストレーニング",
  };

  return (
    <div className={`${glass} rounded-2xl p-5 space-y-4`}>
      <div className="flex items-center gap-2 text-emerald-300 font-semibold text-sm">
        <Target size={16} /> 自動トレーニングプラン
      </div>

      {/* フォーム */}
      <div className="grid grid-cols-2 gap-3 text-xs">
        <div className="col-span-2">
          <label className="text-white/40 block mb-1">レース名</label>
          <input type="text" value={form.race_name} placeholder="例: 富士登山競走"
            onChange={e => setForm(f => ({ ...f, race_name: e.target.value }))}
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 text-xs outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">レース日</label>
          <input type="date" value={form.race_date}
            onChange={e => setForm(f => ({ ...f, race_date: e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">距離 (km)</label>
          <input type="number" min={1} max={5000} value={form.race_distance_km}
            onChange={e => setForm(f => ({ ...f, race_distance_km: +e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
        </div>
        <div className="col-span-2">
          <label className="text-white/40 block mb-1">アクティビティ</label>
          <select value={form.activity_type}
            onChange={e => setForm(f => ({ ...f, activity_type: e.target.value }))}
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 text-xs outline-none`}>
            <option value="running">ロードランニング</option>
            <option value="trail_running">トレイルランニング</option>
            <option value="hiking">登山・ハイキング</option>
            <option value="cycling">サイクリング</option>
          </select>
        </div>
      </div>

      <button onClick={handleGenerate} disabled={loading || !form.race_name.trim() || !form.race_date}
        className="w-full py-2 rounded-xl bg-emerald-500/20 border border-emerald-400/30 text-emerald-300 text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-40 hover:bg-emerald-500/30 transition-all">
        {loading ? <><Loader2 size={14} className="animate-spin" />生成中</> : <><Calendar size={14} />プランを生成</>}
      </button>

      {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">⚠ {error}</div>}

      {/* 保存済みプラン一覧 */}
      {savedPlans.length > 0 && !plan && (
        <div>
          <div className="text-[10px] text-white/30 uppercase tracking-wider mb-1.5">保存済みプラン</div>
          <div className="space-y-1.5">
            {savedPlans.map(p => (
              <div key={p.id} className="flex items-center justify-between bg-white/[0.03] rounded-xl px-3 py-2 text-xs">
                <button onClick={() => handleLoad(p.id)} className="flex-1 text-left hover:text-white/80 text-white/60 transition-colors">
                  <span className="font-medium">{p.race_name}</span>
                  <span className="text-white/30 ml-2">{p.race_date} · {p.race_distance_km}km</span>
                </button>
                <button onClick={() => handleDelete(p.id)} className="text-white/20 hover:text-red-400 transition-colors ml-2">×</button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* プラン表示 */}
      {plan && (
        <div className="space-y-3 border-t border-white/[0.06] pt-3">
          {/* サマリー */}
          <div className="text-xs text-white/60 bg-white/[0.02] rounded-xl p-3 leading-relaxed border border-white/[0.05]">
            {plan.summary}
          </div>

          {/* CTL 進捗 */}
          <div className="flex items-center gap-2 text-xs bg-white/[0.02] rounded-xl px-3 py-2 border border-white/[0.05]">
            <span className="text-white/40">現在 CTL</span>
            <span className="text-white/80 font-semibold">{plan.current_ctl}</span>
            <span className="text-white/20 mx-0.5">→</span>
            <span className="text-emerald-300 font-semibold">{plan.target_ctl}</span>
            <span className="text-white/40">目標 CTL</span>
            <span className="ml-auto text-white/25 text-[10px]">{plan.total_weeks}週間</span>
          </div>

          {/* 週タブ */}
          <div className="flex gap-1 overflow-x-auto pb-1 scrollbar-hide">
            {plan.weeks?.map((w, i) => (
              <button key={i} onClick={() => setActiveWeek(i)}
                className={`flex-shrink-0 text-[10px] px-2.5 py-1.5 rounded-lg border transition-all ${
                  activeWeek === i
                    ? PHASE_COLORS[w.phase] || "bg-white/10 border-white/20 text-white/80"
                    : "bg-white/[0.02] border-white/[0.06] text-white/30 hover:text-white/50"
                }`}>
                {w.week_number}週
              </button>
            ))}
          </div>

          {/* 週詳細 */}
          {plan.weeks?.[activeWeek] && (() => {
            const week = plan.weeks[activeWeek];
            const getJpDow = (dateStr) => {
              if (!dateStr) return "";
              const d = new Date(dateStr + "T00:00:00");
              return ["日", "月", "火", "水", "木", "金", "土"][d.getDay()];
            };
            return (
              <div className="space-y-2">
                {/* 週ヘッダー */}
                <div className="flex items-center justify-between text-xs">
                  <div className="flex items-center gap-2">
                    <span className={`px-2 py-0.5 rounded-lg border text-[10px] font-medium ${PHASE_COLORS[week.phase] || ""}`}>
                      {week.phase_label}
                    </span>
                    <span className="text-white/25 text-[10px]">
                      {week.start_date?.slice(5).replace("-", "/")} 〜 {week.end_date?.slice(5).replace("-", "/")}
                    </span>
                  </div>
                  <span className="text-[10px] text-white/30">週TSS <span className="text-white/55 font-medium">{week.weekly_tss}</span></span>
                </div>

                {/* 今週のフォーカス */}
                <div className="bg-white/[0.03] rounded-xl px-3 py-2 border border-white/[0.05]">
                  <span className="text-[9px] text-white/25 uppercase tracking-wider mr-1.5">今週の目標</span>
                  <span className="text-[11px] text-white/60 leading-relaxed">{week.focus}</span>
                </div>

                {/* ワークアウト一覧（カード形式） */}
                <div className="space-y-1.5">
                  {week.workouts?.map((workout, di) => {
                    const dow = getJpDow(workout.date);
                    const isRest = workout.workout_type === "REST";
                    return (
                      <div key={di} className={`rounded-xl border ${
                        isRest
                          ? "bg-white/[0.01] border-white/[0.04]"
                          : "bg-white/[0.03] border-white/[0.07]"
                      }`}>
                        <div className="flex items-start gap-2.5 px-3 py-2.5">
                          {/* 曜日・アイコン */}
                          <div className="flex-shrink-0 w-9 text-center">
                            <div className="text-[10px] text-white/25 mb-0.5">{dow}</div>
                            <div className="text-xl leading-none">{WORKOUT_ICONS[workout.workout_type] || "?"}</div>
                          </div>
                          {/* 内容 */}
                          {isRest ? (
                            <div className="flex-1 py-1">
                              <span className="text-[11px] text-white/30">完全休養 — 軽いストレッチのみ</span>
                            </div>
                          ) : (
                            <div className="flex-1 min-w-0">
                              {/* ワークアウト名・時間・ゾーン */}
                              <div className="flex items-center gap-1.5 flex-wrap mb-1">
                                <span className={`text-xs font-semibold ${WORKOUT_COLORS[workout.workout_type] || "text-white/70"}`}>
                                  {WORKOUT_LABELS[workout.workout_type] || workout.workout_type}
                                </span>
                                {workout.duration_min > 0 && (
                                  <span className="text-[10px] text-white/50 font-medium">{workout.duration_min}分</span>
                                )}
                                <span className="ml-auto text-[9px] text-white/30 bg-white/[0.05] px-1.5 py-0.5 rounded border border-white/[0.06]">
                                  {workout.target_zone}
                                </span>
                              </div>
                              {/* 説明 */}
                              <p className="text-[11px] text-white/55 leading-relaxed">{workout.description}</p>
                              {/* 補足メモ */}
                              {workout.notes && (
                                <p className="text-[10px] text-emerald-300/50 mt-1 leading-relaxed">
                                  💡 {workout.notes}
                                </p>
                              )}
                              {/* TSS */}
                              {workout.tss_estimate > 0 && (
                                <div className="text-[9px] text-white/20 mt-1">推定TSS: {workout.tss_estimate}</div>
                              )}
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })()}

          {/* AI調整パネル */}
          <AIPlanAdjustPanel plan={plan} activeWeek={activeWeek} onAdjust={(adjusted) => {
            setPlan(prev => ({
              ...prev,
              weeks: prev.weeks.map((w, i) => i === activeWeek ? adjusted : w),
            }));
          }} />

          <button onClick={() => setPlan(null)}
            className="w-full py-1.5 text-xs text-white/30 hover:text-white/50 transition-colors">
            ← 一覧に戻る
          </button>
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   SUB: AIPlanAdjustPanel — トレーニングプランAI調整
───────────────────────────────────────────────────── */
function AIPlanAdjustPanel({ plan, activeWeek, onAdjust }) {
  const [message,     setMessage]     = useState("");
  const [loading,     setLoading]     = useState(false);
  const [explanation, setExplanation] = useState(null);
  const [error,       setError]       = useState(null);

  const handleAdjust = async () => {
    if (!message.trim() || !plan?.plan_id) return;
    setLoading(true); setError(null); setExplanation(null);
    try {
      const r = await adjustTrainingWeek(plan.plan_id, activeWeek, message);
      onAdjust(r.adjusted_week);
      setExplanation(r.explanation);
      setMessage("");
    } catch (e) {
      setError(apiErr(e, "調整に失敗しました"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="border-t border-white/[0.06] pt-3 space-y-2">
      <div className="flex items-center gap-2 text-[10px] text-white/30 uppercase tracking-wider">
        <Brain size={11} /> 今週のプランをAIに相談
      </div>
      <div className="flex gap-2">
        <input
          type="text"
          value={message}
          onChange={e => setMessage(e.target.value)}
          onKeyDown={e => e.key === "Enter" && handleAdjust()}
          placeholder="例: 今週疲れてる、少し負荷を下げて"
          className={`${glass} flex-1 px-3 py-1.5 rounded-xl text-white/80 text-xs outline-none placeholder:text-white/25`}
        />
        <button
          onClick={handleAdjust}
          disabled={loading || !message.trim()}
          className="px-3 py-1.5 rounded-xl bg-emerald-500/20 border border-emerald-400/30 text-emerald-300 text-xs disabled:opacity-40 hover:bg-emerald-500/30 transition-all"
        >
          {loading ? <Loader2 size={11} className="animate-spin" /> : "調整"}
        </button>
      </div>
      {explanation && (
        <div className="text-[10px] text-emerald-300/70 bg-emerald-500/10 border border-emerald-500/20 rounded-xl px-3 py-1.5">
          ✓ {explanation}
        </div>
      )}
      {error && (
        <div className="text-[10px] text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-1.5">
          ⚠ {error}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: WeatherPerformancePanel — 気象×パフォーマンス相関
───────────────────────────────────────────────────── */
function WeatherPerformancePanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    setLoading(true);
    getWeatherPerformance().then(setData).catch(() => {}).finally(() => setLoading(false));
  }, []);

  return (
    <div className={`${glass} rounded-2xl p-5 space-y-4`}>
      <div className="flex items-center gap-2 text-sky-300 font-semibold text-sm">
        <Wind size={16} /> 気象×パフォーマンス相関
      </div>
      {loading && <div className="text-xs text-white/30 flex items-center gap-2"><Loader2 size={12} className="animate-spin" />分析中...</div>}
      {!loading && !data && <p className="text-xs text-white/30">アクティビティデータが不足しています（気象情報付きの活動が必要）</p>}
      {data && (
        <div className="space-y-4">
          {data.message && <p className="text-xs text-white/50">{data.message}</p>}
          {data.correlations && (
            <div className="space-y-2">
              <p className="text-[10px] text-white/40 uppercase tracking-wider">気象要素との相関係数（ペース）</p>
              {Object.entries(data.correlations).map(([key, val]) => {
                const label = { temp: "気温", humidity: "湿度", wind_speed: "風速" }[key] || key;
                const pct = Math.abs(val) * 100;
                const positive = val >= 0;
                return (
                  <div key={key} className="flex items-center gap-3 text-xs">
                    <span className="w-16 text-white/40 shrink-0">{label}</span>
                    <div className="flex-1 bg-white/[0.04] rounded-full h-1.5">
                      <div className={`h-1.5 rounded-full ${positive ? "bg-sky-400" : "bg-rose-400"}`} style={{ width: `${pct}%` }} />
                    </div>
                    <span className={`w-12 text-right font-mono ${positive ? "text-sky-400" : "text-rose-400"}`}>
                      {val > 0 ? "+" : ""}{val.toFixed(2)}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
          {data.optimal_temp_range && (
            <div className="bg-sky-500/10 border border-sky-500/20 rounded-xl px-3 py-2 text-xs text-sky-300">
              最適気温帯: {data.optimal_temp_range[0]}°C 〜 {data.optimal_temp_range[1]}°C
            </div>
          )}
          {data.best_conditions && (
            <div className="text-xs text-white/50 space-y-1">
              {data.best_conditions.map((c, i) => <p key={i}>• {c}</p>)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: GoalTimeProbPanel — 目標タイム達成確率
───────────────────────────────────────────────────── */
function GoalTimeProbPanel() {
  const [form, setForm] = useState({ activity_type: "running", race_distance_km: 42.195, target_time_min: 240, elevation_gain_m: 0 });
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const handleCalc = async () => {
    setLoading(true); setError(null); setResult(null);
    try {
      const r = await getGoalTimeProbability(form);
      setResult(r);
    } catch (e) {
      setError(apiErr(e, "計算に失敗しました"));
    } finally {
      setLoading(false);
    }
  };

  const probColor = result?.probability_pct >= 70 ? "emerald" : result?.probability_pct >= 40 ? "amber" : "red";

  return (
    <div className={`${glass} rounded-2xl p-5 space-y-4`}>
      <div className="flex items-center gap-2 text-violet-300 font-semibold text-sm">
        <Target size={16} /> 目標タイム達成確率
      </div>

      <div className="grid grid-cols-2 gap-3 text-xs">
        <div>
          <label className="text-white/40 block mb-1">種目</label>
          <select value={form.activity_type} onChange={e => setForm(f => ({ ...f, activity_type: e.target.value }))}
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 outline-none`}>
            <option value="running">ロードランニング</option>
            <option value="trail_running">トレイルランニング</option>
            <option value="hiking">ハイキング</option>
            <option value="cycling">サイクリング</option>
          </select>
        </div>
        <div>
          <label className="text-white/40 block mb-1">距離 (km)</label>
          <input type="number" value={form.race_distance_km} onChange={e => setForm(f => ({ ...f, race_distance_km: Number(e.target.value) }))}
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">目標タイム (分)</label>
          <input type="number" value={form.target_time_min} onChange={e => setForm(f => ({ ...f, target_time_min: Number(e.target.value) }))}
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">獲得標高 (m)</label>
          <input type="number" value={form.elevation_gain_m} onChange={e => setForm(f => ({ ...f, elevation_gain_m: Number(e.target.value) }))}
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 outline-none`} />
        </div>
      </div>

      <button onClick={handleCalc} disabled={loading}
        className="w-full py-2.5 rounded-xl bg-violet-500/20 border border-violet-400/30 text-violet-300 text-sm hover:bg-violet-500/30 transition-all disabled:opacity-40 flex items-center justify-center gap-2">
        {loading ? <Loader2 size={14} className="animate-spin" /> : <Target size={14} />}
        達成確率を計算
      </button>

      {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">{error}</div>}

      {result && (
        <div className="space-y-3">
          <div className={`text-center py-4 rounded-xl border ${
            probColor === "emerald" ? "bg-emerald-500/10 border-emerald-500/20" :
            probColor === "amber"   ? "bg-amber-500/10 border-amber-500/20" :
                                     "bg-red-500/10 border-red-500/20"
          }`}>
            <div className={`text-4xl font-bold ${probColor === "emerald" ? "text-emerald-300" : probColor === "amber" ? "text-amber-300" : "text-red-300"}`}>
              {result.probability_pct}%
            </div>
            <div className="text-xs text-white/50 mt-1">目標達成確率</div>
          </div>
          {result.estimated_time_min && (
            <div className="text-center text-xs text-white/50">
              予測タイム: <span className="text-white/80 font-mono">{Math.floor(result.estimated_time_min / 60)}h{Math.round(result.estimated_time_min % 60)}m</span>
            </div>
          )}
          {result.advice && <p className="text-xs text-white/60">{result.advice}</p>}
          {result.required_improvements && result.required_improvements.length > 0 && (
            <div className="space-y-1">
              <p className="text-[10px] text-white/40">改善ポイント:</p>
              {result.required_improvements.map((imp, i) => (
                <p key={i} className="text-[11px] text-white/50">• {imp}</p>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: RaceStrategyPanel — レース戦略レポート生成
───────────────────────────────────────────────────── */
function RaceStrategyPanel() {
  const [form, setForm] = useState({
    race_name: "",
    race_distance_km: 42.195,
    elevation_gain_m: 0,
    target_time_min: 240,
    activity_type: "running",
    race_date: new Date().toISOString().slice(0, 10),
  });
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const handleGenerate = async () => {
    setLoading(true); setError(null); setResult(null);
    try {
      const r = await generateRaceStrategy(form);
      setResult(r);
    } catch (e) {
      setError(apiErr(e, "生成に失敗しました"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className={`${glass} rounded-2xl p-5 space-y-4`}>
      <div className="flex items-center gap-2 text-amber-300 font-semibold text-sm">
        <Flag size={16} /> レース戦略レポート生成
      </div>

      <div className="grid grid-cols-2 gap-3 text-xs">
        <div className="col-span-2">
          <label className="text-white/40 block mb-1">レース名</label>
          <input value={form.race_name} onChange={e => setForm(f => ({ ...f, race_name: e.target.value }))}
            placeholder="例: UTMF 2025"
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 outline-none placeholder:text-white/20`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">種目</label>
          <select value={form.activity_type} onChange={e => setForm(f => ({ ...f, activity_type: e.target.value }))}
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 outline-none`}>
            <option value="running">ロードランニング</option>
            <option value="trail_running">トレイルランニング</option>
            <option value="hiking">ハイキング</option>
            <option value="cycling">サイクリング</option>
          </select>
        </div>
        <div>
          <label className="text-white/40 block mb-1">距離 (km)</label>
          <input type="number" value={form.race_distance_km} onChange={e => setForm(f => ({ ...f, race_distance_km: Number(e.target.value) }))}
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">獲得標高 (m)</label>
          <input type="number" value={form.elevation_gain_m} onChange={e => setForm(f => ({ ...f, elevation_gain_m: Number(e.target.value) }))}
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">目標タイム (分)</label>
          <input type="number" value={form.target_time_min} onChange={e => setForm(f => ({ ...f, target_time_min: Number(e.target.value) }))}
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">レース日</label>
          <input type="date" value={form.race_date} onChange={e => setForm(f => ({ ...f, race_date: e.target.value }))}
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 outline-none`} />
        </div>
      </div>

      <button onClick={handleGenerate} disabled={loading || !form.race_name.trim()}
        className="w-full py-2.5 rounded-xl bg-amber-500/20 border border-amber-400/30 text-amber-300 text-sm hover:bg-amber-500/30 transition-all disabled:opacity-40 flex items-center justify-center gap-2">
        {loading ? <Loader2 size={14} className="animate-spin" /> : <Bot size={14} />}
        AIが戦略レポートを生成
      </button>

      {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">{error}</div>}

      {result && (
        <div className="space-y-3">
          <div className="bg-amber-500/10 border border-amber-500/20 rounded-xl p-4 space-y-2">
            <p className="text-xs font-semibold text-amber-300">{result.race_name || form.race_name} 戦略レポート</p>
            <p className="text-xs text-white/70 leading-relaxed whitespace-pre-wrap">{result.strategy_text}</p>
          </div>
          {result.pace_plan && result.pace_plan.length > 0 && (
            <div className="space-y-1.5">
              <p className="text-[10px] text-white/40 uppercase tracking-wider">ペース配分</p>
              {result.pace_plan.map((seg, i) => (
                <div key={i} className="flex items-center justify-between text-xs px-3 py-1.5 bg-white/[0.02] border border-white/[0.04] rounded-xl">
                  <span className="text-white/50">{seg.segment}</span>
                  <span className="font-mono text-white/70">{seg.target_pace}</span>
                </div>
              ))}
            </div>
          )}
          {result.key_checkpoints && result.key_checkpoints.length > 0 && (
            <div className="space-y-1.5">
              <p className="text-[10px] text-white/40 uppercase tracking-wider">補給ポイント</p>
              {result.key_checkpoints.map((cp, i) => (
                <div key={i} className="text-xs px-3 py-1.5 bg-white/[0.02] border border-white/[0.04] rounded-xl text-white/50">
                  <span className="text-white/70 font-medium">{cp.name}</span>
                  {cp.action && <span className="ml-2 text-white/35">{cp.action}</span>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: RouteLibraryPanel — ルートライブラリ
───────────────────────────────────────────────────── */
function RouteLibraryPanel() {
  const [routes, setRoutes] = useState([]);
  const [loading, setLoading] = useState(false);
  const [tagFilter, setTagFilter] = useState("");
  const [error, setError] = useState(null);

  const fetchRoutes = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const r = await listRouteLibrary(tagFilter || null);
      setRoutes(r.routes || []);
    } catch (e) {
      setError("ルートの取得に失敗しました");
    } finally {
      setLoading(false);
    }
  }, [tagFilter]);

  useEffect(() => { fetchRoutes(); }, [fetchRoutes]);

  const handleDelete = async (id) => {
    if (!window.confirm("このルートを削除しますか？")) return;
    try {
      await deleteRouteFromLibrary(id);
      setRoutes(rs => rs.filter(r => r.id !== id));
    } catch {
      setError("削除に失敗しました");
    }
  };

  return (
    <div className={`${glass} rounded-2xl p-5 space-y-4`}>
      <div className="flex items-center gap-2 text-emerald-300 font-semibold text-sm">
        <BookOpen size={16} /> ルートライブラリ
        <button onClick={fetchRoutes} disabled={loading} className="ml-auto text-white/30 hover:text-white/60 transition-colors">
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      <div className="flex gap-2">
        <input value={tagFilter} onChange={e => setTagFilter(e.target.value)}
          placeholder="タグでフィルタ（例: 富士山）"
          className={`${glass} flex-1 px-3 py-2 rounded-xl text-white/80 text-xs outline-none placeholder:text-white/20`} />
        <button onClick={fetchRoutes}
          className="px-3 py-2 rounded-xl bg-emerald-500/15 border border-emerald-400/25 text-emerald-300 text-xs hover:bg-emerald-500/25 transition-all">
          <Search size={12} />
        </button>
      </div>

      {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">{error}</div>}

      {loading && <div className="text-xs text-white/30 flex items-center gap-2"><Loader2 size={12} className="animate-spin" />読み込み中...</div>}

      {!loading && routes.length === 0 && (
        <p className="text-xs text-white/30 text-center py-4">保存済みルートがありません。<br/>ルート計画後に「ライブラリに保存」から追加できます。</p>
      )}

      <div className="space-y-2">
        {routes.map(r => (
          <div key={r.id} className="flex items-start gap-3 px-3 py-2.5 bg-white/[0.02] border border-white/[0.04] rounded-xl">
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-white/80">{r.name}</p>
              <div className="flex flex-wrap gap-2 mt-0.5 text-[10px] text-white/35">
                {r.distance_m && <span>{(r.distance_m / 1000).toFixed(1)}km</span>}
                {r.elevation_gain_m && <span>↑{Math.round(r.elevation_gain_m)}m</span>}
                {r.activity_type && <span>{r.activity_type}</span>}
              </div>
              {r.tags && (
                <div className="flex flex-wrap gap-1 mt-1">
                  {r.tags.split(",").filter(Boolean).map(t => (
                    <span key={t} onClick={() => setTagFilter(t.trim())}
                      className="text-[9px] px-1.5 py-0.5 rounded-full bg-emerald-500/10 border border-emerald-500/15 text-emerald-400 cursor-pointer hover:bg-emerald-500/20">
                      #{t.trim()}
                    </span>
                  ))}
                </div>
              )}
            </div>
            <button onClick={() => handleDelete(r.id)}
              className="text-white/20 hover:text-red-400 transition-colors mt-0.5 shrink-0">
              <Minus size={13} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   AIRaceTab — レース/AIタブ（統合ビュー）
───────────────────────────────────────────────────── */
function AIRaceTab() {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-bold text-white/90 mb-1">レース / AI分析</h2>
        <p className="text-xs text-white/40">AI自然言語クエリ・レースシミュレーター・トレーニングプラン生成・実績記録</p>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="space-y-4">
          <PanelErrorBoundary><AIQueryPanel /></PanelErrorBoundary>
          <PanelErrorBoundary><GoalTimeProbPanel /></PanelErrorBoundary>
          <PanelErrorBoundary><RaceStrategyPanel /></PanelErrorBoundary>
          <PanelErrorBoundary><RaceResultPanel /></PanelErrorBoundary>
        </div>
        <div className="space-y-4">
          <PanelErrorBoundary><TrainingPlanPanel /></PanelErrorBoundary>
          <PanelErrorBoundary><RaceSimulatorPanel /></PanelErrorBoundary>
        </div>
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: ToolsTab — 分析ツール統合タブ
───────────────────────────────────────────────────── */
function ToolsTab() {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-bold text-white/90 mb-1">分析ツール</h2>
        <p className="text-xs text-white/40">補給計画・ルート比較・気象分析・精度改善ツール</p>
      </div>

      <RouteCandidateComparePanel />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="space-y-4">
          <WeatherPerformancePanel />
          <RouteLibraryPanel />
          <ReportPanel />
        </div>
        <div className="space-y-4">
          <RouteComparePanel />
          <FeedbackPanel />
          <FeatureImportanceCard />
        </div>
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: RaceResultPanel — レース実績入力 → モデル更新
───────────────────────────────────────────────────── */
function RaceResultPanel() {
  const [form, setForm] = useState({
    activity_type:    "running",
    race_date:        new Date().toISOString().slice(0, 10),
    race_distance_km: 42,
    actual_time_min:  240,
    avg_hr:           "",
    max_hr:           "",
    elevation_gain_m: "",
    notes:            "",
  });
  const [loading, setLoading] = useState(false);
  const [result,  setResult]  = useState(null);
  const [error,   setError]   = useState(null);

  const handleSubmit = async () => {
    setLoading(true); setError(null); setResult(null);
    try {
      const payload = {
        ...form,
        avg_hr:           form.avg_hr           ? Number(form.avg_hr)           : null,
        max_hr:           form.max_hr           ? Number(form.max_hr)           : null,
        elevation_gain_m: form.elevation_gain_m ? Number(form.elevation_gain_m) : null,
      };
      const r = await submitRaceResult(payload);
      setResult(r);
    } catch (e) {
      setError(apiErr(e, "保存に失敗しました"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className={`${glass} rounded-2xl p-5 space-y-4`}>
      <div className="flex items-center gap-2 text-amber-300 font-semibold text-sm">
        <Award size={16} /> レース実績を記録
        <span className="ml-auto text-[10px] text-white/30 font-normal">モデル自動更新</span>
      </div>

      <div className="grid grid-cols-2 gap-3 text-xs">
        <div className="col-span-2">
          <label className="text-white/40 block mb-1">アクティビティ</label>
          <select value={form.activity_type}
            onChange={e => setForm(f => ({ ...f, activity_type: e.target.value }))}
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 text-xs outline-none`}>
            <option value="running">ロードランニング</option>
            <option value="trail_running">トレイルランニング</option>
            <option value="hiking">登山・ハイキング</option>
            <option value="cycling">サイクリング</option>
          </select>
        </div>
        <div>
          <label className="text-white/40 block mb-1">レース日</label>
          <input type="date" value={form.race_date}
            onChange={e => setForm(f => ({ ...f, race_date: e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">距離 (km)</label>
          <input type="number" min={1} max={500} value={form.race_distance_km}
            onChange={e => setForm(f => ({ ...f, race_distance_km: +e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">実際のタイム (分)</label>
          <input type="number" min={5} max={10080} value={form.actual_time_min}
            onChange={e => setForm(f => ({ ...f, actual_time_min: +e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">平均HR (bpm・任意)</label>
          <input type="number" min={40} max={230} value={form.avg_hr} placeholder="--"
            onChange={e => setForm(f => ({ ...f, avg_hr: e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">最大HR (bpm・任意)</label>
          <input type="number" min={40} max={230} value={form.max_hr} placeholder="--"
            onChange={e => setForm(f => ({ ...f, max_hr: e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
        </div>
        <div>
          <label className="text-white/40 block mb-1">獲得標高 (m・任意)</label>
          <input type="number" min={0} value={form.elevation_gain_m} placeholder="--"
            onChange={e => setForm(f => ({ ...f, elevation_gain_m: e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
        </div>
        <div className="col-span-2">
          <label className="text-white/40 block mb-1">メモ（任意）</label>
          <input type="text" value={form.notes} placeholder="天気、コンディションなど"
            onChange={e => setForm(f => ({ ...f, notes: e.target.value }))}
            className={`${glass} w-full px-3 py-2 rounded-xl text-white/80 text-xs outline-none`} />
        </div>
      </div>

      <button onClick={handleSubmit} disabled={loading}
        className="w-full py-2 rounded-xl bg-amber-500/20 border border-amber-400/30 text-amber-300 text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-40 hover:bg-amber-500/30 transition-all">
        {loading ? <><Loader2 size={14} className="animate-spin" />保存中...</> : <><Award size={14} />結果を記録してモデルを更新</>}
      </button>

      {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">⚠ {error}</div>}

      {result && (
        <div className="bg-amber-500/10 border border-amber-500/20 rounded-xl p-3 space-y-1.5 text-xs">
          <div className="text-amber-300 font-semibold">✓ {result.message}</div>
          {result.pace_min_per_km && (
            <div className="text-white/50">平均ペース: <span className="text-white/80">{result.pace_min_per_km} 分/km</span></div>
          )}
          {result.trimp && (
            <div className="text-white/50">推定TRIMP: <span className="text-white/80">{result.trimp}</span></div>
          )}
          {result.retrain_result && (
            <div className="text-indigo-300/70 text-[10px] mt-1">
              モデル更新: {result.retrain_result.n_activities}件 / ±{result.retrain_result.hr_mae} bpm
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: RouteCandidateComparePanel — A/Bルート比較
───────────────────────────────────────────────────── */
const DIFF_BADGE = (score) => {
  if (!score) return "text-white/30";
  if (score <= 2.5) return "text-green-400";
  if (score <= 5.0) return "text-yellow-400";
  if (score <= 7.5) return "text-orange-400";
  return "text-red-400";
};

function RouteCandidateComparePanel() {
  const ACTIVITIES_COMPARE = [
    { key: "🏔️ 登山・ハイキング", label: "登山・ハイキング" },
    { key: "🏃 ランニング",         label: "ランニング" },
    { key: "🌲 トレイルランニング", label: "トレイルランニング" },
    { key: "🚴 サイクリング",       label: "サイクリング" },
  ];
  const DEFAULT_PROFILE = { body_weight: 65, load_weight: 8, e_initial: 1800, hr_max: 180, terrain_factor: 1.2, lt_ratio: 0.85, e_crit: 300, t_warn: 25 };

  const [candidates, setCandidates] = useState([
    { label: "ルートA", route: null, activity: "🏔️ 登山・ハイキング" },
    { label: "ルートB", route: null, activity: "🏔️ 登山・ハイキング" },
  ]);
  const [loading,  setLoading]  = useState(false);
  const [results,  setResults]  = useState(null);
  const [error,    setError]    = useState(null);
  const gpxRef0 = useRef(null);
  const gpxRef1 = useRef(null);
  const gpxRef2 = useRef(null);
  const gpxRefs = [gpxRef0, gpxRef1, gpxRef2];

  const handleGpxUpload = async (idx, file) => {
    try {
      const rt = await uploadGPX(file);
      setCandidates(prev => prev.map((c, i) => i === idx ? { ...c, route: rt } : c));
    } catch (e) {
      setError(`ルート${idx + 1}: ${apiErr(e, "GPX読み込みに失敗しました")}`);
    }
  };

  const addCandidate = () => {
    if (candidates.length >= 3) return;
    setCandidates(prev => [...prev, { label: `ルート${String.fromCharCode(65 + prev.length)}`, route: null, activity: "🏔️ 登山・ハイキング" }]);
  };

  const handleCompare = async () => {
    const ready = candidates.filter(c => c.route);
    if (ready.length < 2) { setError("ルートを2つ以上読み込んでください"); return; }
    setLoading(true); setError(null); setResults(null);
    try {
      const payload = {
        candidates: ready.map(c => ({
          label:    c.label,
          route:    c.route,
          activity: c.activity,
          profile:  DEFAULT_PROFILE,
        })),
        supplies: [],
      };
      const r = await compareRouteCandidates(payload);
      setResults(r.results);
    } catch (e) {
      setError(apiErr(e, "比較に失敗しました"));
    } finally {
      setLoading(false);
    }
  };

  const fmtMin = (m) => {
    const h = Math.floor(m / 60), mn = Math.round(m % 60);
    return h > 0 ? `${h}h${String(mn).padStart(2,"0")}m` : `${mn}m`;
  };

  return (
    <div className={`${glass} rounded-2xl p-5 space-y-4`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-cyan-300 font-semibold text-sm">
          <Route size={16} /> ルート候補比較
        </div>
        {candidates.length < 3 && (
          <button onClick={addCandidate}
            className="text-xs text-white/40 hover:text-white/70 flex items-center gap-1 transition-colors">
            <Plus size={12} /> ルートを追加
          </button>
        )}
      </div>

      {/* ルート設定 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {candidates.map((cand, idx) => (
          <div key={idx} className={`${glass} rounded-xl p-3 space-y-2`}>
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={cand.label}
                onChange={e => setCandidates(prev => prev.map((c, i) => i === idx ? { ...c, label: e.target.value } : c))}
                className="bg-transparent text-sm font-semibold text-white/80 outline-none w-20 border-b border-white/10 focus:border-cyan-400/40"
              />
              {candidates.length > 2 && (
                <button onClick={() => setCandidates(prev => prev.filter((_, i) => i !== idx))}
                  className="ml-auto text-white/20 hover:text-red-400 transition-colors text-xs">×</button>
              )}
            </div>
            <select value={cand.activity}
              onChange={e => setCandidates(prev => prev.map((c, i) => i === idx ? { ...c, activity: e.target.value } : c))}
              className={`${glass} w-full px-2 py-1 rounded-lg text-white/70 text-xs outline-none`}>
              {ACTIVITIES_COMPARE.map(a => (
                <option key={a.key} value={a.key} className="bg-[#0e0e1a]">{a.label}</option>
              ))}
            </select>
            {cand.route ? (
              <div className="text-[10px] text-green-400 flex items-center gap-1">
                <CheckCircle2 size={10} />
                {(cand.route.distance_m / 1000).toFixed(1)}km · ↑{Math.round(cand.route.ascent_m || 0)}m
                {cand.route.difficulty && (
                  <span className={`ml-1 ${DIFF_BADGE(cand.route.difficulty.score)}`}>
                    難{cand.route.difficulty.score}
                  </span>
                )}
                <button onClick={() => setCandidates(prev => prev.map((c, i) => i === idx ? { ...c, route: null } : c))}
                  className="ml-auto text-white/25 hover:text-white/50">✕</button>
              </div>
            ) : (
              <div>
                <input ref={gpxRefs[idx]} type="file" accept=".gpx" className="hidden"
                  onChange={e => e.target.files[0] && handleGpxUpload(idx, e.target.files[0])} />
                <button onClick={() => gpxRefs[idx].current?.click()}
                  className="w-full py-1.5 border border-dashed border-white/15 rounded-lg text-[10px] text-white/30 hover:text-white/50 hover:border-white/25 transition-all flex items-center justify-center gap-1">
                  <Upload size={10} /> GPXファイルを読み込む
                </button>
              </div>
            )}
          </div>
        ))}
      </div>

      <button onClick={handleCompare}
        disabled={loading || candidates.filter(c => c.route).length < 2}
        className="w-full py-2 rounded-xl bg-cyan-500/20 border border-cyan-400/30 text-cyan-300 text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-40 hover:bg-cyan-500/30 transition-all">
        {loading ? <><Loader2 size={14} className="animate-spin" />比較中...</> : <><BarChart2 size={14} />シミュレーション比較を実行</>}
      </button>

      {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">⚠ {error}</div>}

      {/* 比較結果 */}
      {results && (
        <div className="space-y-3">
          <div className="text-[10px] text-white/30 uppercase tracking-wider">比較結果</div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {results.map((r, i) => r.error ? (
              <div key={i} className={`${glass} rounded-xl p-3 text-xs text-red-400`}>{r.label}: {r.error}</div>
            ) : (
              <div key={i} className={`${glass} rounded-xl p-4 space-y-3`}>
                <div className="flex items-center justify-between">
                  <span className="text-sm font-semibold text-white/90">{r.label}</span>
                  {r.difficulty && (
                    <span className={`text-[10px] font-semibold ${DIFF_BADGE(r.difficulty.score)}`}>
                      難易度 {r.difficulty.score} · {r.difficulty.label}
                    </span>
                  )}
                </div>
                <div className="grid grid-cols-2 gap-2">
                  {[
                    ["距離",       `${r.dist_km} km`,          "text-cyan-300"],
                    ["予測時間",   fmtMin(r.eta_min),           "text-indigo-300"],
                    ["総消費",     `${r.total_kcal} kcal`,      "text-amber-300"],
                    ["獲得標高",   `${r.ascent_m} m`,           "text-violet-300"],
                    ["残エネルギー", `${r.final_kcal} kcal`,    r.final_kcal < 500 ? "text-red-400" : "text-green-400"],
                    ["警告数",     `${r.alert_count} 件`,        r.alert_count > 0 ? "text-red-400" : "text-green-400"],
                  ].map(([label, val, cls]) => (
                    <div key={label} className="bg-white/[0.02] rounded-lg px-2 py-1.5">
                      <div className={`text-sm font-semibold ${cls}`}>{val}</div>
                      <div className="text-[9px] text-white/30">{label}</div>
                    </div>
                  ))}
                </div>
                {r.difficulty?.factors && (
                  <div className="text-[10px] text-white/30">
                    最大斜度 {r.difficulty.factors.max_gradient_pct}% · 標高/km {r.difficulty.factors.ascent_per_km}m
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: HydrationModal — 水分補給量計算
───────────────────────────────────────────────────── */
function HydrationModal({ onClose, defaultTempC = 20, defaultDurationMin = 180, defaultWeight = 65, activityType = "hiking" }) {
  const [loading, setLoading] = useState(false);
  const [result,  setResult]  = useState(null);
  const [form, setForm] = useState({
    temp_c: defaultTempC, humidity_pct: 60, duration_min: defaultDurationMin,
    intensity_factor: 1.0, body_weight_kg: defaultWeight, activity_type: activityType,
  });

  const handleCalc = async () => {
    setLoading(true);
    try {
      const r = await calcHydration(form);
      setResult(r);
    } catch (e) {
      alert(apiErr(e, "計算エラー"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div className={`${glass} rounded-2xl p-6 w-full max-w-md mx-4 space-y-4`} onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-cyan-300 font-semibold">
            <Droplets size={16} /> 水分補給量計算
          </div>
          <button onClick={onClose} className="text-white/30 hover:text-white/60 text-lg">×</button>
        </div>

        <div className="grid grid-cols-2 gap-3 text-xs">
          {[
            ["気温 (℃)", "temp_c", "number", -20, 60],
            ["湿度 (%)",  "humidity_pct", "number", 0, 100],
            ["運動時間 (分)", "duration_min", "number", 10, 2880],
            ["体重 (kg)",   "body_weight_kg", "number", 20, 300],
          ].map(([label, key, type, min, max]) => (
            <div key={key}>
              <label className="text-white/40 block mb-1">{label}</label>
              <input type={type} min={min} max={max} value={form[key]}
                onChange={e => setForm(f => ({ ...f, [key]: parseFloat(e.target.value) }))}
                className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
            </div>
          ))}
          <div>
            <label className="text-white/40 block mb-1">強度係数</label>
            <select value={form.intensity_factor}
              onChange={e => setForm(f => ({ ...f, intensity_factor: parseFloat(e.target.value) }))}
              className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`}>
              <option value={0.7}>低強度 (0.7)</option>
              <option value={1.0}>中強度 (1.0)</option>
              <option value={1.3}>高強度 (1.3)</option>
            </select>
          </div>
        </div>

        <button onClick={handleCalc} disabled={loading}
          className="w-full py-2 rounded-xl bg-cyan-500/20 border border-cyan-400/30 text-cyan-300 text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-50">
          {loading ? <><Loader2 size={14} className="animate-spin" />計算中</> : "計算する"}
        </button>

        {result && (
          <div className="space-y-3 border-t border-white/[0.06] pt-3">
            <div className="grid grid-cols-2 gap-2 text-xs">
              {[
                ["必要水分量合計", `${result.total_ml} mL`],
                ["時間あたり",     `${result.rate_ml_per_hour} mL/h`],
                ["推定発汗率",     `${result.sweat_rate_ml_per_hour} mL/h`],
                ["Na 必要量",      `${result.electrolyte_na_mg} mg`],
              ].map(([k, v]) => (
                <div key={k} className={`${glass} rounded-xl p-2`}>
                  <div className="text-white/40">{k}</div>
                  <div className="text-white/90 font-semibold mt-0.5">{v}</div>
                </div>
              ))}
            </div>
            {result.warning && (
              <div className="text-xs text-amber-300 bg-amber-500/10 border border-amber-500/20 rounded-xl px-3 py-2">
                ⚠ {result.warning}
              </div>
            )}
            <div className="text-[10px] text-white/40 font-medium uppercase tracking-wider">補給スケジュール</div>
            <div className="space-y-1 max-h-40 overflow-y-auto">
              {result.schedule.map((s, i) => (
                <div key={i} className="flex items-center gap-2 text-xs text-white/60">
                  <span className="w-12 font-mono text-white/40">{s.at_min}分</span>
                  <span className="text-cyan-300 font-semibold">{s.amount_ml}mL</span>
                  <span className="text-white/30">{s.note}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: SupplyCountdown — レースモード補給カウントダウン
───────────────────────────────────────────────────── */
function SupplyCountdown({ simResult, raceStart }) {
  const [elapsed,  setElapsed]  = useState(0);
  const [data,     setData]     = useState(null);
  const [running,  setRunning]  = useState(false);
  const timerRef = useRef(null);

  const fetch = async (el) => {
    try {
      const r = await getSupplyTimingAlerts(simResult, raceStart, el);
      setData(r);
    } catch (_) {}
  };

  useEffect(() => {
    if (running) {
      fetch(elapsed);
      timerRef.current = setInterval(() => {
        setElapsed(e => {
          const next = e + 1;
          fetch(next);
          return next;
        });
      }, 60000); // 1分ごと更新
    } else {
      clearInterval(timerRef.current);
    }
    return () => clearInterval(timerRef.current);
  }, [running]);

  const ns = data?.next_supply;
  return (
    <div className={`${glass} rounded-2xl p-4 space-y-3`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs font-semibold text-emerald-300">
          <Play size={12} /> レースモード
        </div>
        <button onClick={() => setRunning(r => !r)}
          className={`text-xs px-3 py-1 rounded-lg font-medium transition-all ${
            running
              ? "bg-red-500/20 border border-red-400/30 text-red-300"
              : "bg-emerald-500/20 border border-emerald-400/30 text-emerald-300"
          }`}>
          {running ? "停止" : "スタート"}
        </button>
      </div>

      {running && (
        <div className="space-y-2 text-xs">
          <div className="text-white/40">経過: <span className="text-white/80 font-mono">{elapsed}分</span></div>
          {ns ? (
            <div className="bg-emerald-500/10 border border-emerald-500/20 rounded-xl p-3">
              <div className="text-emerald-300 font-semibold">次の補給: {ns.at_clock}</div>
              <div className="text-white/60 mt-1">あと {ns.in_min} 分 — {ns.food} ({ns.kcal}kcal)</div>
            </div>
          ) : (
            <div className="text-white/30">補給イベントなし</div>
          )}
          {data?.current_e_kcal != null && (
            <div className="text-white/40">現在エネルギー残量: <span className="text-white/70 font-mono">{data.current_e_kcal} kcal</span></div>
          )}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: ReportPanel — 週次/月次レポートダウンロード
───────────────────────────────────────────────────── */
function ReportPanel() {
  const [periods,  setPeriods]  = useState(null);
  const [loading,  setLoading]  = useState(false);
  const [selType,  setSelType]  = useState("monthly");
  const [selYear,  setSelYear]  = useState(new Date().getFullYear());
  const [selWeek,  setSelWeek]  = useState(null);
  const [selMonth, setSelMonth] = useState(new Date().getMonth() + 1);

  useEffect(() => {
    getAvailablePeriods().then(setPeriods).catch(() => {});
  }, []);

  const handleDownload = async () => {
    setLoading(true);
    try {
      const blob = await generateReport(selType, selYear, selType === "weekly" ? selWeek : null, selType === "monthly" ? selMonth : null);
      const url  = URL.createObjectURL(new Blob([blob], { type: "application/pdf" }));
      const a    = document.createElement("a");
      a.href = url;
      a.download = `report_${selType}_${selYear}${selType === "monthly" ? selMonth.toString().padStart(2,"0") : `W${String(selWeek).padStart(2,"0")}`}.pdf`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      alert(apiErr(e, "レポート生成エラー"));
    } finally {
      setLoading(false);
    }
  };

  const monthOptions = periods?.months || [];
  const weekOptions  = periods?.weeks  || [];
  const curOpts = selType === "monthly" ? monthOptions : weekOptions;

  return (
    <div className={`${glass} rounded-2xl p-5 space-y-4`}>
      <div className="flex items-center gap-2 text-xs font-semibold text-white/60 uppercase tracking-wider">
        <BarChart2 size={13} /> レポート生成
      </div>

      <div className="grid grid-cols-2 gap-3 text-xs">
        <div>
          <label className="text-white/40 block mb-1">種別</label>
          <select value={selType} onChange={e => setSelType(e.target.value)}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 outline-none`}>
            <option value="monthly">月次</option>
            <option value="weekly">週次</option>
          </select>
        </div>
        <div>
          <label className="text-white/40 block mb-1">期間</label>
          <select
            value={selType === "monthly" ? `${selYear}-${selMonth}` : `${selYear}-W${selWeek}`}
            onChange={e => {
              const v = e.target.value;
              if (selType === "monthly") {
                const [y, m] = v.split("-");
                setSelYear(parseInt(y)); setSelMonth(parseInt(m));
              } else {
                const [y, w] = v.split("-W");
                setSelYear(parseInt(y)); setSelWeek(parseInt(w));
              }
            }}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 outline-none`}>
            {curOpts.length === 0 && <option value="">データなし</option>}
            {curOpts.map(p => {
              const key = selType === "monthly" ? `${p.year}-${p.month}` : `${p.year}-W${p.week}`;
              const label = selType === "monthly" ? `${p.year}年${p.month}月` : `${p.year}年第${p.week}週`;
              return <option key={key} value={key}>{label}</option>;
            })}
          </select>
        </div>
      </div>

      <button onClick={handleDownload} disabled={loading || curOpts.length === 0}
        className="w-full py-2 rounded-xl bg-indigo-500/20 border border-indigo-400/30 text-indigo-300 text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-40">
        {loading ? <><Loader2 size={13} className="animate-spin" />生成中...</> : "PDFダウンロード"}
      </button>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: FeatureImportanceCard — 特徴量重要度
───────────────────────────────────────────────────── */
function FeatureImportanceCard() {
  const [data,    setData]    = useState(null);
  const [loading, setLoading] = useState(true);

  const FEATURE_LABELS = {
    slope_pct:        "斜度",
    speed_mps:        "速度",
    elevation_norm:   "標高",
    fatigue_index:    "疲労度",
    cum_gain_norm:    "累積獲得標高",
    hr:               "心拍数",
    resting_hr_ratio: "安静時HR比率",
    hrv_zscore:       "HRV Zスコア",
    sleep_score_norm: "睡眠スコア",
  };

  useEffect(() => {
    getFeatureImportance()
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="text-white/30 text-xs py-4 text-center"><Loader2 size={14} className="animate-spin inline mr-1" />読み込み中...</div>;
  if (!data)   return null;

  const renderBar = (info, title) => {
    if (!info) return null;
    return (
      <div>
        <div className="text-xs text-white/50 font-medium mb-2">{title} ({info.model_type})</div>
        <div className="space-y-1.5">
          {info.features.map((f, i) => (
            <div key={f} className="flex items-center gap-2">
              <div className="text-[11px] text-white/50 w-28 shrink-0">{FEATURE_LABELS[f] || f}</div>
              <div className="flex-1 h-2 bg-white/[0.05] rounded-full overflow-hidden">
                <div className="h-full bg-gradient-to-r from-cyan-500/60 to-indigo-500/60 rounded-full"
                  style={{ width: `${(info.importances[i] * 100).toFixed(1)}%` }} />
              </div>
              <div className="text-[11px] text-white/40 w-10 text-right">{(info.importances[i] * 100).toFixed(1)}%</div>
            </div>
          ))}
        </div>
      </div>
    );
  };

  return (
    <div className={`${glass} rounded-2xl p-5 space-y-5`}>
      <div className="flex items-center gap-2 text-xs font-semibold text-white/60 uppercase tracking-wider">
        <Brain size={13} /> 予測に影響する要因ランキング
      </div>
      {renderBar(data.hr,   "心拍数予測モデル")}
      {renderBar(data.pace, "ペース予測モデル")}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: FeedbackPanel — 予測精度フィードバック
───────────────────────────────────────────────────── */
function FeedbackPanel() {
  const [summary, setSummary] = useState(null);
  const [history, setHistory] = useState([]);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({
    prediction_type: "eta_min",
    predicted_value: "",
    actual_value: "",
    predicted_at: new Date().toISOString().slice(0, 16),
    notes: "",
  });
  const [loading, setLoading] = useState(false);

  const LABELS = { hr: "心拍数 (bpm)", pace: "ペース (m/s)", completion: "完走確率 (%)", total_kcal: "消費kcal", eta_min: "所要時間 (分)" };

  const load = () => {
    Promise.all([getFeedbackSummary(), getFeedbackHistory(null, 20)])
      .then(([s, h]) => { setSummary(s); setHistory(h.records || []); })
      .catch(() => {});
  };

  useEffect(() => { load(); }, []);

  const handleSubmit = async () => {
    if (!form.predicted_value || !form.actual_value) return;
    setLoading(true);
    try {
      await createFeedback({
        prediction_type:  form.prediction_type,
        predicted_value:  parseFloat(form.predicted_value),
        actual_value:     parseFloat(form.actual_value),
        predicted_at:     form.predicted_at + ":00",
        notes:            form.notes || undefined,
      });
      setShowForm(false);
      load();
    } catch (e) {
      alert(apiErr(e, "送信エラー"));
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (id) => {
    await deleteFeedback(id).catch(() => {});
    load();
  };

  return (
    <div className={`${glass} rounded-2xl p-5 space-y-4`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs font-semibold text-white/60 uppercase tracking-wider">
          <RefreshCw size={13} /> 予測精度フィードバック
        </div>
        <button onClick={() => setShowForm(v => !v)}
          className="text-xs px-3 py-1 rounded-lg bg-cyan-500/20 border border-cyan-400/30 text-cyan-300">
          {showForm ? "閉じる" : "+ 実績を記録"}
        </button>
      </div>

      {showForm && (
        <div className="space-y-3 border border-white/[0.06] rounded-xl p-3">
          <div className="grid grid-cols-2 gap-2 text-xs">
            <div>
              <label className="text-white/40 block mb-1">種別</label>
              <select value={form.prediction_type} onChange={e => setForm(f => ({ ...f, prediction_type: e.target.value }))}
                className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 outline-none`}>
                {Object.entries(LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </div>
            <div>
              <label className="text-white/40 block mb-1">予測日時</label>
              <input type="datetime-local" value={form.predicted_at}
                onChange={e => setForm(f => ({ ...f, predicted_at: e.target.value }))}
                className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
            </div>
            <div>
              <label className="text-white/40 block mb-1">予測値</label>
              <input type="number" value={form.predicted_value} onChange={e => setForm(f => ({ ...f, predicted_value: e.target.value }))}
                className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
            </div>
            <div>
              <label className="text-white/40 block mb-1">実績値</label>
              <input type="number" value={form.actual_value} onChange={e => setForm(f => ({ ...f, actual_value: e.target.value }))}
                className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
            </div>
          </div>
          <input type="text" placeholder="メモ（任意）" value={form.notes}
            onChange={e => setForm(f => ({ ...f, notes: e.target.value }))}
            className={`${glass} w-full px-2 py-1.5 rounded-lg text-white/80 text-xs outline-none`} />
          <button onClick={handleSubmit} disabled={loading}
            className="w-full py-1.5 rounded-lg bg-cyan-500/20 border border-cyan-400/30 text-cyan-300 text-xs font-medium disabled:opacity-50">
            {loading ? "送信中..." : "記録する"}
          </button>
        </div>
      )}

      {summary && Object.keys(summary.by_type || {}).length > 0 && (
        <div className="space-y-2">
          <div className="text-[10px] text-white/40 uppercase tracking-wider">精度サマリー</div>
          {Object.entries(summary.by_type).map(([type, s]) => (
            <div key={type} className="flex items-center gap-2 text-xs">
              <span className="text-white/50 w-24">{LABELS[type] || type}</span>
              <span className="text-white/30">{s.count}件</span>
              <span className="text-amber-300 ml-auto">MAE: {s.mae?.toFixed(2)}</span>
              <span className="text-white/40">({s.mape?.toFixed(1)}%)</span>
            </div>
          ))}
        </div>
      )}

      {history.length > 0 && (
        <div className="space-y-1 max-h-48 overflow-y-auto">
          <div className="text-[10px] text-white/40 uppercase tracking-wider">履歴</div>
          {history.map(r => (
            <div key={r.id} className="flex items-center gap-2 text-[11px] text-white/50 py-1 border-b border-white/[0.04]">
              <span className="text-white/30 w-12">{r.date}</span>
              <span className="w-16">{LABELS[r.prediction_type] || r.prediction_type}</span>
              <span>予測:{r.predicted_value?.toFixed(1)} → 実績:{r.actual_value?.toFixed(1)}</span>
              <span className={`ml-auto ${r.error_pct > 20 ? "text-red-400" : "text-emerald-400"}`}>
                {r.error_pct?.toFixed(1)}%
              </span>
              <button onClick={() => handleDelete(r.id)} className="text-white/20 hover:text-red-400 transition-colors">×</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: RouteComparePanel — ルート比較ダッシュボード
───────────────────────────────────────────────────── */
function RouteComparePanel() {
  const [routes,   setRoutes]   = useState([]);
  const [selected, setSelected] = useState(null);
  const [history,  setHistory]  = useState([]);
  const [checkedIds, setCheckedIds] = useState([]);
  const [series,   setSeries]   = useState(null);
  const [loading,  setLoading]  = useState(false);

  useEffect(() => {
    getMyRoutes().then(r => setRoutes(r.routes || [])).catch(() => {});
  }, []);

  const handleSelectRoute = async (fp) => {
    setSelected(fp); setSeries(null); setCheckedIds([]);
    const h = await getRouteHistory(fp).catch(() => ({ activities: [] }));
    setHistory(h.activities || []);
  };

  const handleCompare = async () => {
    if (checkedIds.length < 2) { alert("2件以上選択してください"); return; }
    setLoading(true);
    try {
      const r = await compareRoutes(selected, checkedIds);
      setSeries(r.series || []);
    } catch (e) {
      alert(apiErr(e, "比較エラー"));
    } finally {
      setLoading(false);
    }
  };

  const COLORS = ["#22d3ee", "#f59e0b", "#a78bfa", "#34d399", "#f87171"];

  return (
    <div className={`${glass} rounded-2xl p-5 space-y-4`}>
      <div className="flex items-center gap-2 text-xs font-semibold text-white/60 uppercase tracking-wider">
        <Route size={13} /> ルート比較ダッシュボード
      </div>

      {routes.length === 0 ? (
        <p className="text-xs text-white/30">Garmin FIT をアップロードするとルート比較ができます。</p>
      ) : (
        <div className="space-y-2">
          <div className="text-[10px] text-white/40">ルートを選択</div>
          <div className="space-y-1 max-h-40 overflow-y-auto">
            {routes.map(r => (
              <button key={r.fingerprint} onClick={() => handleSelectRoute(r.fingerprint)}
                className={`w-full text-left text-xs px-3 py-2 rounded-xl transition-all ${
                  selected === r.fingerprint
                    ? "bg-cyan-500/20 border border-cyan-400/30 text-cyan-300"
                    : `${glass} text-white/60 ${glassHover}`
                }`}>
                {r.activity_type} — {r.n_activities}回 / 最終: {r.last_date || "—"}
              </button>
            ))}
          </div>
        </div>
      )}

      {history.length > 0 && (
        <div className="space-y-2">
          <div className="text-[10px] text-white/40">比較する記録を選択（2〜5件）</div>
          <div className="space-y-1 max-h-40 overflow-y-auto">
            {history.map((a, idx) => (
              <label key={a.id} className="flex items-center gap-2 text-xs cursor-pointer hover:text-white/80 transition-colors">
                <input type="checkbox" checked={checkedIds.includes(a.id)}
                  onChange={e => setCheckedIds(ids =>
                    e.target.checked ? [...ids, a.id] : ids.filter(id => id !== a.id)
                  )} className="accent-cyan-400" />
                <span style={{ color: COLORS[idx % COLORS.length] }}>●</span>
                <span className="text-white/60">{a.date}</span>
                <span className="text-white/40 ml-auto">{a.duration_min}分 / 平均HR:{a.avg_hr?.toFixed(0) || "—"}</span>
              </label>
            ))}
          </div>
          <button onClick={handleCompare} disabled={loading || checkedIds.length < 2}
            className="w-full py-1.5 rounded-xl bg-cyan-500/20 border border-cyan-400/30 text-cyan-300 text-xs font-medium disabled:opacity-40">
            {loading ? "読み込み中..." : "比較グラフを表示"}
          </button>
        </div>
      )}

      {series && series.length > 0 && (
        <div className="space-y-3">
          <div className="text-[10px] text-white/40 uppercase tracking-wider">心拍数推移 比較</div>
          <div className="relative h-36 bg-white/[0.02] rounded-xl p-2 overflow-hidden">
            <svg viewBox="0 0 400 120" className="w-full h-full">
              {series.map((s, si) => {
                const pts = s.points.filter(p => p.hr);
                if (pts.length < 2) return null;
                const maxElapsed = Math.max(...pts.map(p => p.elapsed_min));
                const hrs = pts.map(p => p.hr);
                const minHR = Math.min(...hrs) - 5;
                const maxHR = Math.max(...hrs) + 5;
                const d = pts.map(p => {
                  const x = (p.elapsed_min / maxElapsed) * 390 + 5;
                  const y = 115 - ((p.hr - minHR) / (maxHR - minHR)) * 110;
                  return `${x},${y}`;
                }).join(" ");
                return <polyline key={si} points={d} fill="none" stroke={COLORS[si]} strokeWidth="1.5" opacity="0.8" />;
              })}
            </svg>
          </div>
          <div className="flex flex-wrap gap-2">
            {series.map((s, si) => (
              <div key={si} className="flex items-center gap-1 text-[11px]">
                <span style={{ color: COLORS[si] }}>●</span>
                <span className="text-white/50">{s.date} ({s.duration_min}分)</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   NEW: PeerCompareModal — ピア比較
───────────────────────────────────────────────────── */
function PeerCompareModal({ activityType, distanceKm, elevationGainM, onClose }) {
  const [data,    setData]    = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getPeerComparison(activityType, distanceKm, elevationGainM)
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div className={`${glass} rounded-2xl p-6 w-full max-w-sm mx-4 space-y-4`} onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-indigo-300 font-semibold text-sm">
            <Award size={15} /> ピア比較
          </div>
          <button onClick={onClose} className="text-white/30 hover:text-white/60 text-lg">×</button>
        </div>

        {loading && <div className="text-white/30 text-xs text-center py-4"><Loader2 size={14} className="animate-spin inline mr-1" />分析中...</div>}

        {data && !loading && (
          data.peer_stats ? (
            <div className="space-y-4">
              <div className="text-xs text-white/40">{data.segment} — {data.n_peers}人のデータ</div>
              <div className="relative">
                <div className="text-[10px] text-white/40 mb-2">所要時間の分布</div>
                <div className="flex items-center h-8 gap-0.5">
                  {["p25_duration","p50_duration","p75_duration"].map((k, i) => {
                    const val = data.peer_stats[k];
                    const isMe = i === 1;
                    return (
                      <div key={k} className={`flex-1 rounded text-[9px] text-center py-1 ${
                        isMe ? "bg-indigo-500/30 border border-indigo-400/30 text-indigo-300"
                             : "bg-white/[0.04] text-white/30"
                      }`}>
                        {val ? `${Math.round(val)}分` : "—"}
                        <div>{["P25","中央","P75"][i]}</div>
                      </div>
                    );
                  })}
                </div>
                {data.your_stats?.avg_duration_min && (
                  <div className="text-xs text-cyan-300 mt-2">
                    あなたの平均: {Math.round(data.your_stats.avg_duration_min)}分
                    {data.percentile?.duration != null && (
                      <span className="ml-2 text-white/40">（上位 {100 - data.percentile.duration}%）</span>
                    )}
                  </div>
                )}
              </div>
              {data.interpretation && (
                <div className="text-xs text-emerald-300 bg-emerald-500/10 border border-emerald-500/20 rounded-xl px-3 py-2">
                  {data.interpretation}
                </div>
              )}
            </div>
          ) : (
            <div className="text-xs text-white/40 text-center py-4">
              {data.message || "比較データなし"}
            </div>
          )
        )}
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────
   MAIN APP
───────────────────────────────────────────────────── */
export default function App() {
  useBackendReloadDetector(); // バックエンド再起動時に自動リロード
  const [theme, toggleTheme] = useTheme(); // ダーク/ライトモード

  // ── 認証状態 ──────────────────────────────────────────────────────
  const [user,      setUser]     = useState(null);    // null = 未ログイン
  const [authReady, setAuthReady] = useState(false);  // 初回トークン検証完了フラグ
  const [appTab,    setAppTab]   = useState("route"); // "route" | "conditioning" | "tools"

  // 起動時: Cookie が有効なら自動ログイン（HttpOnly Cookie は JS から読めないため常にAPIで確認）
  useEffect(() => {
    getMe()
      .then(u => setUser(u))
      .catch(() => {})   // Cookie なし or 期限切れ → 未ログイン状態のまま
      .finally(() => setAuthReady(true));
  }, []);

  const handleAuth   = (u) => setUser(u);
  const handleLogout = () => { logout().finally(() => { setUser(null); setResult(null); setStep(1); }); };

  // 401 によるトークン期限切れ → 強制ログアウト
  useEffect(() => {
    const onExpired = () => { setUser(null); };
    window.addEventListener("auth:expired", onExpired);
    return () => window.removeEventListener("auth:expired", onExpired);
  }, []);

  // ── アプリ状態 ────────────────────────────────────────────────────
  const [step,      setStep]     = useState(1);
  const [activity,  setActivity] = useState("🏔️ 登山・ハイキング");
  const [routeMode, setMode]     = useState("search"); // "search" | "gpx"
  const [start,     setStart]    = useState(null);
  const [goal,      setGoal]     = useState(null);
  const [vias,      setVias]     = useState([]);
  const [route,     setRoute]    = useState(null);
  const [gpxFile,   setGpxFile]  = useState(null);
  const [engine,    setEngine]   = useState("ORS");
  const [planDt,    setPlanDt]   = useState("");
  const [profile,   setProfile]  = useState(DEFAULT_PROFILE);
  const [supplies,  setSupplies] = useState(FOOD_DEFAULTS);
  const [foods,     setFoods]    = useState([]);
  const [result,    setResult]   = useState(null);
  const [loading,   setLoading]  = useState(false);
  const [error,     setError]    = useState("");
  const [optLoading,    setOptLoading]    = useState(false);
  const [optResult,     setOptResult]     = useState(null);
  const [showHydration, setShowHydration] = useState(false);
  const [showRaceMode,  setShowRaceMode]  = useState(false);
  const [peerModal,     setPeerModal]     = useState(null); // {activityType, distanceKm, elevationGainM}
  const [shareLoading,  setShareLoading]  = useState(false);
  const [shareUrl,      setShareUrl]      = useState(null);

  const gpxInputRef = useRef(null);

  useEffect(() => {
    getFoods().then(d => setFoods(Object.keys(d))).catch(() => {});
  }, []);

  // 未ログイン → ログイン画面
  if (!authReady) return null;  // 初回検証中は何も表示しない
  if (!user) return <AuthScreen onAuth={handleAuth} />;

  const foodList = foods.length > 0 ? foods : FOOD_DEFAULTS.map(s => s.food);

  /* ── Helpers ── */
  const canAdvance = () => {
    if (step === 2) return !!(start || gpxFile || route);
    return true;
  };

  const handleSupplyChange = (i, key, val) => {
    setSupplies(prev => prev.map((s, idx) => idx === i ? { ...s, [key]: val } : s));
  };

  const handleAddSupply = () => {
    const lastT = supplies.length > 0 ? supplies[supplies.length - 1].time_min + 60 : 30;
    setSupplies(prev => [...prev, { time_min: lastT, food: foodList[0] || "エネルギージェル", qty: 1 }]);
  };

  const handleRemoveSupply = (i) => {
    setSupplies(prev => prev.filter((_, idx) => idx !== i));
  };

  /* ── Error helper ── */
  const apiErrMsg = (e) => {
    if (!e.response || (e.response.status >= 500 && !e.response?.data?.detail))
      return "バックエンドサーバーに接続できません。uvicorn を起動してください。";
    return apiErr(e);
  };

  /* ── Route fetch ── */
  const handleGetRoute = async () => {
    if (!start || !goal) return;
    setLoading(true); setError("");
    try {
      const rt = await getRoute({
        start: { lat: start.lat, lon: start.lon, name: start.name },
        goal:  { lat: goal.lat,  lon: goal.lon,  name: goal.name  },
        vias:  vias.filter(v => v?.lat).map(v => ({ lat: v.lat, lon: v.lon, name: v.name })),
        activity,
        engine,
      });
      setRoute(rt);
    } catch (e) {
      setError("ルート取得エラー: " + apiErrMsg(e));
    } finally {
      setLoading(false);
    }
  };

  const handleGpxUpload = async (file) => {
    setGpxFile(file);
    setLoading(true); setError("");
    try {
      const rt = await uploadGPX(file);
      setRoute(rt);
      const c = rt.coords;
      if (c.length > 0) {
        setStart({ lat: c[0][0],                lon: c[0][1],                name: "GPXスタート" });
        setGoal({  lat: c[c.length - 1][0],     lon: c[c.length - 1][1],    name: "GPXゴール"   });
      }
    } catch (e) {
      setError("GPX解析エラー: " + apiErrMsg(e));
    } finally {
      setLoading(false);
    }
  };

  /* ── Optimize Supply ── */
  const handleOptimizeSupply = async () => {
    if (!route) { setError("先にルートを確定してください"); return; }
    setOptLoading(true); setError(""); setOptResult(null);
    try {
      const res = await optimizeSupply({ route, activity, profile });
      setOptResult(res);
      if (res.supply_plan && res.supply_plan.length > 0) {
        setSupplies(res.supply_plan.map(s => ({
          time_min: s.time_min,
          food:     s.food,
          qty:      s.qty,
        })));
      }
    } catch (e) {
      setError("補給最適化エラー: " + apiErrMsg(e));
    } finally {
      setOptLoading(false);
    }
  };

  /* ── Simulate ── */
  const handleShare = async () => {
    if (!route || !result) return;
    setShareLoading(true); setShareUrl(null);
    try {
      const r = await createSharedRoute({
        title: route.name || "ルート",
        route_json: JSON.stringify(route),
        supply_plan_json: JSON.stringify(supplies),
        expires_hours: 168,
      });
      const url = `${window.location.origin}/shared/${r.token}`;
      setShareUrl(url);
      try { await navigator.clipboard.writeText(url); } catch {}
    } catch {
      // ignore
    } finally {
      setShareLoading(false);
    }
  };

  const handleSimulate = async () => {
    if (!route) { setError("先にルートを確定してください"); return; }
    setLoading(true); setError(""); setResult(null);
    try {
      const res = await runSimulation({
        route,
        activity,
        profile,
        supplies,
        planned_dt: planDt || null,
      });
      setResult(res);
      setStep(4);
    } catch (e) {
      setError("シミュレーションエラー: " + apiErrMsg(e));
    } finally {
      setLoading(false);
    }
  };

  /* ── Derived ── */
  const fmtMin = (m) => {
    const h = Math.floor(m / 60), mn = Math.round(m % 60);
    return h > 0 ? `${h}時間${String(mn).padStart(2,"0")}分` : `${mn}分`;
  };

  const alertCount = result ? result.alert_times.length : 0;

  /* ─────────────────────────────────────────────────────
     RENDER
  ───────────────────────────────────────────────────── */
  return (
    <div className={`min-h-screen font-sans relative overflow-hidden transition-colors duration-300 ${
      theme === "light" ? "bg-[#dde6f0]" : "bg-[#060912] text-white"
    }`}>

      {/* Ambient blobs */}
      <div className="pointer-events-none fixed inset-0 overflow-hidden">
        <div className="absolute top-[-15%] left-[-5%] w-[45vw] h-[45vw] rounded-full bg-indigo-600/[0.06] blur-[100px]" />
        <div className="absolute bottom-[-10%] right-[-5%] w-[40vw] h-[40vw] rounded-full bg-violet-600/[0.05] blur-[80px]" />
        <div className="absolute top-[40%] left-[55%] w-[30vw] h-[30vw] rounded-full bg-cyan-500/[0.04] blur-[90px]" />
      </div>

      {/* Header */}
      <header className={`${glass} sticky top-0 z-50 px-3 sm:px-6 py-2.5 sm:py-3 flex items-center justify-between gap-2`}>
        <div className="flex items-center gap-2 sm:gap-3 min-w-0">
          <div className="w-7 h-7 sm:w-8 sm:h-8 flex-shrink-0 rounded-xl bg-gradient-to-br from-cyan-500/30 to-indigo-500/30 border border-white/10 flex items-center justify-center">
            <Zap size={14} className="text-cyan-400" />
          </div>
          <span className="text-sm font-bold tracking-tight whitespace-nowrap">Energy Tracker</span>
        </div>
        <div className="flex items-center gap-1.5 sm:gap-2 flex-shrink-0">
          {result && (
            <button
              onClick={() => { setStep(1); setResult(null); setRoute(null); setStart(null); setGoal(null); }}
              className="hidden sm:block text-xs text-white/40 hover:text-white/70 transition-colors px-3 py-1.5 rounded-lg border border-white/[0.06] hover:border-white/20"
            >
              リセット
            </button>
          )}
          <div className={`px-2 py-0.5 sm:px-2.5 sm:py-1 rounded-full text-[10px] font-medium ${
            alertCount > 0 ? "bg-red-500/20 border border-red-500/30 text-red-400" : "bg-green-500/15 border border-green-500/25 text-green-400"
          }`}>
            {alertCount > 0 ? `⚠️ ${alertCount}` : "●"}
          </div>
          {/* テーマ切替ボタン */}
          <button
            onClick={toggleTheme}
            className={`${glass} p-1.5 rounded-xl text-white/40 hover:text-white/70 transition-colors`}
            title={theme === "dark" ? "ライトモードに切替" : "ダークモードに切替"}
          >
            {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
          </button>
          {/* ユーザーバッジ */}
          <div className={`${glass} flex items-center gap-1.5 px-2 sm:px-3 py-1.5 rounded-xl`}>
            <div className="w-5 h-5 rounded-full bg-indigo-500/30 border border-indigo-400/30 flex items-center justify-center text-[10px] text-indigo-300 font-bold">
              {(user.display_name || user.email)[0].toUpperCase()}
            </div>
            <span className="hidden sm:block text-xs text-white/60 max-w-[80px] truncate">{user.display_name || user.email}</span>
            <button onClick={handleLogout} className="text-white/25 hover:text-white/60 transition-colors text-[10px]">
              ログアウト
            </button>
          </div>
        </div>
      </header>

      {/* タブナビ */}
      <div className="relative z-40 border-b border-white/[0.06]">
        <div className="max-w-4xl mx-auto flex">
          {[
            ["dashboard",    <Gauge size={14} />,       "ダッシュボード", "DB"    ],
            ["route",        <Route size={14} />,       "ルート計画",     "ルート" ],
            ["conditioning", <HeartPulse size={14} />,  "コンディション", "状態"   ],
            ["tools",        <Settings size={14} />,    "分析ツール",     "ツール" ],
            ["ai",           <Bot size={14} />,          "レース/AI",      "AI"    ],
          ].map(([tab, ico, label, shortLabel]) => (
            <button
              key={tab}
              onClick={() => setAppTab(tab)}
              className={`flex-1 flex flex-col sm:flex-row items-center justify-center gap-0.5 sm:gap-1.5 px-1 sm:px-3 py-2 sm:py-2.5 transition-all border-b-2 -mb-px ${
                appTab === tab
                  ? "text-cyan-300 border-cyan-400 bg-white/[0.03]"
                  : "text-white/40 border-transparent hover:text-white/60"
              }`}
            >
              {ico}
              <span className="text-[9px] sm:hidden font-medium">{shortLabel}</span>
              <span className="hidden sm:inline text-xs font-medium">{label}</span>
            </button>
          ))}
        </div>
      </div>

      <main className="relative z-10 max-w-4xl mx-auto px-3 sm:px-4 py-4 sm:py-8">
        {appTab === "dashboard" ? (
          <DashboardTab />
        ) : appTab === "conditioning" ? (
          <ConditioningTab />
        ) : appTab === "tools" ? (
          <ToolsTab />
        ) : appTab === "ai" ? (
          <AIRaceTab />
        ) : (
        <>
        <StepIndicator current={step} />

        {/* ─── STEP 1: Activity ─── */}
        {step === 1 && (
          <div className="space-y-6">
            <div className="text-center">
              <h2 className="text-lg font-bold text-white/90">アクティビティを選択</h2>
              <p className="text-xs text-white/40 mt-1">エネルギー計算モデルが変わります</p>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              {ACTIVITIES.map(a => (
                <ActivityCard key={a.key} act={a} selected={activity === a.key} onClick={() => setActivity(a.key)} />
              ))}
            </div>
            <div className="flex justify-end">
              <button onClick={() => setStep(2)} className="flex items-center gap-2 px-6 py-2.5 rounded-xl bg-indigo-500/20 border border-indigo-400/40 text-indigo-300 hover:bg-indigo-500/30 transition-all font-medium text-sm">
                次へ <ChevronRight size={16} />
              </button>
            </div>
          </div>
        )}

        {/* ─── STEP 2: Route ─── */}
        {step === 2 && (
          <div className="space-y-5">
            <div className="text-center">
              <h2 className="text-lg font-bold text-white/90">ルートを設定</h2>
              <p className="text-xs text-white/40 mt-1">地名検索またはGPXファイルで入力</p>
            </div>

            {/* Mode tabs */}
            <div className={`${glass} flex rounded-xl p-1 gap-1`}>
              {[["search", <Search size={13} />, "地名で検索"], ["gpx", <Upload size={13} />, "GPXファイル"]].map(([m, ico, lbl]) => (
                <button key={m} onClick={() => setMode(m)}
                  className={`flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg text-xs font-medium transition-all ${
                    routeMode === m ? "bg-indigo-500/25 border border-indigo-400/40 text-indigo-300" : "text-white/40 hover:text-white/60"
                  }`}>
                  {ico}{lbl}
                </button>
              ))}
            </div>

            {routeMode === "search" && (
              <div className={`${glass} rounded-2xl p-5 space-y-4`}>
                <PlaceSearch label="スタート地点" icon={MapPin} value={start} onSelect={setStart} />

                {/* Vias */}
                {vias.map((v, i) => (
                  <div key={i} className="flex items-end gap-2">
                    <div className="flex-1">
                      <PlaceSearch label={`経由地 ${i + 1}`} icon={Route} value={v} onSelect={(val) => setVias(prev => prev.map((x, idx) => idx === i ? val : x))} />
                    </div>
                    <button onClick={() => setVias(prev => prev.filter((_, idx) => idx !== i))} className="mb-0.5 text-white/30 hover:text-red-400/70 transition-colors">
                      <Minus size={14} />
                    </button>
                  </div>
                ))}
                <button onClick={() => setVias(prev => [...prev, null])} className="text-xs text-white/40 hover:text-white/70 flex items-center gap-1 transition-colors">
                  <Plus size={12} /> 経由地を追加
                </button>

                <PlaceSearch label="ゴール地点" icon={MapPin} value={goal} onSelect={setGoal} />

                {/* Engine */}
                <div className="flex gap-2">
                  {[["ORS", "ORS"], ["GraphHopper", "GraphHopper"]].map(([k, l]) => (
                    <button key={k} onClick={() => setEngine(k)}
                      className={`px-3 py-1.5 rounded-lg text-xs transition-all ${
                        engine === k ? "bg-cyan-500/20 border border-cyan-400/30 text-cyan-300" : `${glass} text-white/40 hover:text-white/60`
                      }`}>
                      {l}
                    </button>
                  ))}
                </div>

                {/* Date/time */}
                <div>
                  <label className="text-xs text-white/50 font-medium block mb-1.5">出発予定日時（天気予報取得に使用）</label>
                  <input
                    type="datetime-local"
                    value={planDt}
                    onChange={e => setPlanDt(e.target.value)}
                    className={`${glass} px-3 py-2 rounded-xl text-sm text-white/70 outline-none focus:border-indigo-400/50 bg-transparent`}
                  />
                </div>

                <button
                  onClick={handleGetRoute}
                  disabled={!start || !goal || loading}
                  className="w-full py-2.5 rounded-xl bg-cyan-500/20 border border-cyan-400/30 text-cyan-300 hover:bg-cyan-500/30 transition-all disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2 font-medium text-sm"
                >
                  {loading ? <><Loader2 size={14} className="animate-spin" /> 取得中...</> : <><Route size={14} /> ルートを取得</>}
                </button>
              </div>
            )}

            {routeMode === "gpx" && (
              <div className={`${glass} rounded-2xl p-5 space-y-4`}>
                <div
                  onClick={() => gpxInputRef.current?.click()}
                  className={`border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-all ${
                    gpxFile ? "border-cyan-500/40 bg-cyan-500/5" : "border-white/10 hover:border-white/20 hover:bg-white/[0.02]"
                  }`}
                >
                  {gpxFile ? (
                    <div className="text-sm text-cyan-300"><CheckCircle2 size={20} className="mx-auto mb-2" />{gpxFile.name}</div>
                  ) : (
                    <div className="text-white/30">
                      <Upload size={28} className="mx-auto mb-2 opacity-50" />
                      <div className="text-sm">GPXファイルをクリックして選択</div>
                      <div className="text-xs mt-1 opacity-60">YAMAP / ヤマレコ / Garmin / Strava</div>
                    </div>
                  )}
                </div>
                <input ref={gpxInputRef} type="file" accept=".gpx" className="hidden"
                  onChange={e => e.target.files[0] && handleGpxUpload(e.target.files[0])} />

                <div>
                  <label className="text-xs text-white/50 font-medium block mb-1.5">出発予定日時（天気予報取得に使用）</label>
                  <input
                    type="datetime-local"
                    value={planDt}
                    onChange={e => setPlanDt(e.target.value)}
                    className={`${glass} px-3 py-2 rounded-xl text-sm text-white/70 outline-none focus:border-indigo-400/50 bg-transparent`}
                  />
                </div>
              </div>
            )}

            {/* Route preview */}
            {route && (
              <RoutePreview route={route} />
            )}

            {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-4 py-2.5">{error}</div>}

            <div className="flex justify-between">
              <button onClick={() => setStep(1)} className="flex items-center gap-2 px-4 py-2 rounded-xl text-white/40 hover:text-white/70 transition-colors text-sm">
                <ChevronLeft size={16} /> 戻る
              </button>
              <button
                onClick={() => setStep(3)}
                disabled={!route}
                className="flex items-center gap-2 px-6 py-2.5 rounded-xl bg-indigo-500/20 border border-indigo-400/40 text-indigo-300 hover:bg-indigo-500/30 transition-all disabled:opacity-40 disabled:cursor-not-allowed font-medium text-sm"
              >
                次へ <ChevronRight size={16} />
              </button>
            </div>
          </div>
        )}

        {/* ─── STEP 3: Profile + Supply ─── */}
        {step === 3 && (
          <div className="space-y-5">
            <div className="text-center">
              <h2 className="text-lg font-bold text-white/90">プロフィール・補給設定</h2>
              <p className="text-xs text-white/40 mt-1">任意。精度を高めたい場合に設定してください</p>
            </div>

            {/* Profile */}
            <div className={`${glass} rounded-2xl p-5 space-y-4`}>
              <div className="flex items-center gap-2 text-xs text-white/50 font-medium uppercase tracking-wider">
                <Settings size={12} /> プロフィール
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                {[
                  ["体重 (kg)",        "body_weight",    30,   150, 0.5],
                  ["荷物 (kg)",        "load_weight",     0,    50, 0.5],
                  ["初期エネルギー (kcal)", "e_initial", 500,  3500,  50],
                ].map(([label, key, min, max, step]) => (
                  <div key={key}>
                    <label className="text-[11px] text-white/40 block mb-1">{label}</label>
                    <input
                      type="number"
                      min={min} max={max} step={step}
                      value={profile[key]}
                      onChange={e => setProfile(p => ({ ...p, [key]: Number(e.target.value) }))}
                      className={`${glass} w-full px-3 py-2 rounded-xl text-sm text-white/80 outline-none focus:border-indigo-400/50 bg-transparent`}
                    />
                  </div>
                ))}
              </div>
            </div>

            {/* Supply */}
            <div className={`${glass} rounded-2xl p-5 space-y-3`}>
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2 text-xs text-white/50 font-medium uppercase tracking-wider">
                  <Coffee size={12} /> 補給計画
                  <span className="text-[10px] text-white/25 normal-case hidden sm:inline">時刻(分) / 食品 / 個数</span>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={handleOptimizeSupply}
                    disabled={optLoading || !route}
                    title={!route ? "先にルートを確定してください" : ""}
                    className="flex items-center gap-1 px-2.5 py-1 rounded-lg bg-violet-500/15 border border-violet-400/25 text-violet-300 hover:bg-violet-500/25 hover:border-violet-400/40 transition-all text-[11px] font-medium disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {optLoading
                      ? <><Loader2 size={11} className="animate-spin" /> 生成中...</>
                      : <><Brain size={11} /> AI自動生成</>
                    }
                  </button>
                </div>
              </div>

              {/* 最適化結果バッジ */}
              {optResult && (
                <div className={`flex flex-col gap-1 px-3 py-2 rounded-xl border text-[11px]
                  ${optResult.is_feasible
                    ? "bg-green-500/10 border-green-500/20 text-green-400"
                    : "bg-amber-500/10 border-amber-500/20 text-amber-400"}`}>
                  <div className="flex items-center gap-2">
                    {optResult.is_feasible
                      ? <CheckCircle2 size={12} />
                      : <AlertTriangle size={12} />}
                    <span>{optResult.is_feasible ? "最適化成功" : "補給不足の可能性あり"}</span>
                  </div>
                  <span className="text-white/30 text-[10px]">
                    スコア {optResult.score}点 · 最小E {Math.round(optResult.min_e_kcal)} kcal · 計{Math.round(optResult.total_kcal)} kcal補給
                  </span>
                </div>
              )}

              {supplies.map((s, i) => (
                <SupplyRow key={i} supply={s} index={i} foods={foodList}
                  onChange={handleSupplyChange} onRemove={handleRemoveSupply} />
              ))}

              <button
                onClick={handleAddSupply}
                className="text-xs text-white/40 hover:text-white/70 flex items-center gap-1 transition-colors mt-1"
              >
                <Plus size={12} /> 補給を追加
              </button>
            </div>

            {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-4 py-2.5">{error}</div>}

            {/* 水分補給計算ボタン */}
            <button onClick={() => setShowHydration(true)}
              className="w-full py-2 rounded-xl border border-cyan-400/20 text-cyan-400/70 hover:text-cyan-300 hover:border-cyan-400/40 transition-all text-xs flex items-center justify-center gap-1.5">
              <Droplets size={12} /> 水分補給量を計算する
            </button>

            <div className="flex justify-between">
              <button onClick={() => setStep(2)} className="flex items-center gap-2 px-4 py-2 rounded-xl text-white/40 hover:text-white/70 transition-colors text-sm">
                <ChevronLeft size={16} /> 戻る
              </button>
              <button
                onClick={handleSimulate}
                disabled={loading}
                className="flex items-center gap-2 px-7 py-2.5 rounded-xl bg-gradient-to-r from-cyan-500/25 to-indigo-500/25 border border-cyan-400/30 text-cyan-300 hover:from-cyan-500/35 hover:to-indigo-500/35 transition-all font-semibold text-sm shadow-[0_0_20px_rgba(34,211,238,0.1)]"
              >
                {loading ? <><Loader2 size={14} className="animate-spin" /> 計算中...</> : <><Play size={14} /> シミュレーション実行</>}
              </button>
            </div>
          </div>
        )}

        {/* ─── STEP 4: Results ─── */}
        {step === 4 && result && (
          <div className="space-y-5">
            <div className="text-center">
              <h2 className="text-lg font-bold text-white/90">シミュレーション結果</h2>
              {result.route_method && <p className="text-xs text-white/30 mt-1">{result.route_method}</p>}
            </div>

            {/* KPI Grid */}
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              <KPICard icon={Route}       label="距離"       value={(result.dist_km || 0).toFixed(1)} unit="km"   color="cyan"   />
              <KPICard icon={Clock}       label="予測時間"   value={fmtMin(result.eta_min)}            unit=""     color="indigo" />
              <KPICard icon={TrendingUp}  label="獲得標高"   value={Math.round(result.ascent_m || 0)}  unit="m"    color="violet" />
              <KPICard icon={Zap}         label="総消費"     value={Math.round(result.total_kcal)}     unit="kcal" color="amber"  />
              <KPICard icon={Activity}    label="残エネルギー" value={Math.round(result.final_kcal)}   unit="kcal"
                color={result.final_kcal < result.e_crit * 1.2 ? "red" : "green"}
                alert={result.final_kcal < result.e_crit} />
              <KPICard icon={alertCount > 0 ? AlertTriangle : CheckCircle2}
                label="ハンガーノック" value={alertCount > 0 ? `${alertCount}件の警告` : "安全"} unit=""
                color={alertCount > 0 ? "red" : "green"} alert={alertCount > 0} />
            </div>
            {/* 難易度バッジ */}
            {route?.difficulty && (() => {
              const d = route.difficulty;
              const c = d.score <= 2.5 ? "text-green-400 bg-green-500/10 border-green-500/20"
                      : d.score <= 5.0 ? "text-yellow-400 bg-yellow-500/10 border-yellow-500/20"
                      : d.score <= 7.5 ? "text-orange-400 bg-orange-500/10 border-orange-500/20"
                      :                  "text-red-400 bg-red-500/10 border-red-500/20";
              return (
                <div className={`flex items-center gap-3 px-4 py-2.5 rounded-xl border text-xs ${c}`}>
                  <Mountain size={13} />
                  <span className="font-semibold">難易度 {d.score} / 10 — {d.label}</span>
                  <span className="text-white/30 text-[10px] ml-auto">
                    最大斜度 {d.factors?.max_gradient_pct}% · 累計獲得 {d.factors?.total_ascent_m}m
                    · 距離 {d.factors?.dist_km}km
                  </span>
                </div>
              );
            })()}

            {/* Weather */}
            {result.weather && result.weather.temp_c != null && (
              <div className={`${glass} rounded-2xl p-4 space-y-2`}>
                <div className="flex items-center gap-2 text-xs text-white/50 font-medium uppercase tracking-wider">
                  <Activity size={12} /> 気象情報
                  {result.weather.location && <span className="text-white/30 normal-case">({result.weather.location})</span>}
                </div>
                <div className="flex flex-wrap gap-3 text-sm">
                  <span className="flex items-center gap-1 text-amber-400"><Thermometer size={13} />{result.weather.temp_c?.toFixed(1)}℃</span>
                  <span className="flex items-center gap-1 text-cyan-400"><Droplets size={13} />{result.weather.humidity_pct}%</span>
                  <span className="flex items-center gap-1 text-indigo-400"><Wind size={13} />{result.weather.wind_speed_ms?.toFixed(1)}m/s</span>
                  <span className="text-white/50 text-xs">補正×{result.weather.weather_factor?.toFixed(2)}</span>
                </div>
              </div>
            )}

            {/* Energy chart + Elevation + Map (synchronized hover) */}
            <ResultChartsAndMap result={result} />

            {/* Supply log */}
            {result.supply_log.length > 0 && (
              <div className={`${glass} rounded-2xl p-5`}>
                <div className="flex items-center gap-2 mb-3 text-xs text-white/50 font-medium uppercase tracking-wider">
                  <Coffee size={12} /> 補給計画
                </div>
                <div className="space-y-2">
                  {result.supply_log.map((s, i) => {
                    const speed = s.ka >= 1.5 ? "高速" : s.ka >= 0.9 ? "中速" : "低速";
                    const sc    = s.ka >= 1.5 ? "text-green-400 bg-green-500/10" : s.ka >= 0.9 ? "text-amber-400 bg-amber-500/10" : "text-blue-400 bg-blue-500/10";
                    return (
                      <div key={i} className="flex items-center gap-3 px-3 py-2 rounded-xl bg-white/[0.02] border border-white/[0.04]">
                        <span className="text-xs text-white/40 w-12 font-mono">{Math.round(s.time_min)}分</span>
                        <span className="text-sm text-white/80 flex-1">{s.kcal}kcal</span>
                        <span className={`text-[11px] px-2 py-0.5 rounded-full ${sc}`}>{speed}吸収</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Alert detail */}
            {alertCount > 0 && (
              <div className="bg-red-500/5 border border-red-500/20 rounded-2xl p-4">
                <div className="flex items-center gap-2 text-red-400 text-sm font-semibold mb-2">
                  <AlertTriangle size={14} /> ハンガーノック警告
                </div>
                <p className="text-xs text-white/50 mb-2">以下の時刻に危険水準({result.e_crit}kcal)を下回る可能性があります：</p>
                <div className="flex flex-wrap gap-2">
                  {result.alert_times.map((t, i) => (
                    <span key={i} className="text-xs bg-red-500/10 border border-red-500/20 rounded-lg px-2 py-1 text-red-300 font-mono">
                      {fmtMin(t)}
                    </span>
                  ))}
                </div>
                <p className="text-xs text-white/40 mt-2">→ 補給タイミングを早めるか、補給量を増やしてください</p>
              </div>
            )}

            {/* 完走確率 (案4) */}
            <CompletionProbabilityCard completion={result.completion} />

            {/* 異常区間検知 (案5) */}
            <AnomalyList anomalies={result.anomalies} fmtMin={fmtMin} />

            {/* レースモード補給カウントダウン */}
            {planDt && (
              <SupplyCountdown simResult={result} raceStart={planDt} />
            )}

            {/* ピア比較ボタン */}
            {route && (
              <button onClick={() => setPeerModal({
                activityType: ["🏔️ 登山・ハイキング","🌲 トレイルランニング"].includes(activity) ? "hiking" :
                               activity === "🏃 ランニング" ? "running" : "cycling",
                distanceKm:   (route.distance_m || 0) / 1000,
                elevationGainM: route.ascent_m || 0,
              })}
                className={`w-full py-2 rounded-xl border border-indigo-400/20 text-indigo-400/70 hover:text-indigo-300 hover:border-indigo-400/40 transition-all text-xs flex items-center justify-center gap-1.5`}>
                <Award size={12} /> 同条件ユーザーと比較する
              </button>
            )}

            {/* ルート共有ボタン */}
            {route && result && (
              <div className="space-y-2">
                <button onClick={handleShare} disabled={shareLoading}
                  className="w-full py-2 rounded-xl border border-cyan-400/20 text-cyan-400/70 hover:text-cyan-300 hover:border-cyan-400/40 transition-all text-xs flex items-center justify-center gap-1.5 disabled:opacity-40">
                  {shareLoading ? <Loader2 size={12} className="animate-spin" /> : <Share2 size={12} />}
                  ルートを共有する
                </button>
                {shareUrl && (
                  <div className="bg-cyan-500/10 border border-cyan-500/20 rounded-xl px-3 py-2 space-y-1">
                    <p className="text-[10px] text-cyan-300">共有URLをコピーしました (7日間有効)</p>
                    <p className="text-[10px] text-white/40 font-mono break-all">{shareUrl}</p>
                  </div>
                )}
              </div>
            )}

            <div className="flex justify-between">
              <button onClick={() => setStep(3)} className="flex items-center gap-2 px-4 py-2 rounded-xl text-white/40 hover:text-white/70 transition-colors text-sm">
                <ChevronLeft size={16} /> 設定を変更
              </button>
            </div>
          </div>
        )}
        </>
        )}
      </main>

      {/* Footer */}
      <footer className="relative z-10 text-center py-4 text-[10px] text-white/15">
        Energy Tracker v2.0 — React + FastAPI
      </footer>

      {/* 水分補給モーダル */}
      {showHydration && (
        <HydrationModal
          onClose={() => setShowHydration(false)}
          defaultTempC={20}
          defaultDurationMin={route?.duration_sec ? Math.round(route.duration_sec / 60) : 180}
          defaultWeight={profile.body_weight}
          activityType={["🏔️ 登山・ハイキング","🌲 トレイルランニング"].includes(activity) ? "hiking" :
                         activity === "🏃 ランニング" ? "running" : "cycling"}
        />
      )}

      {/* ピア比較モーダル */}
      {peerModal && (
        <PeerCompareModal
          activityType={peerModal.activityType}
          distanceKm={peerModal.distanceKm}
          elevationGainM={peerModal.elevationGainM}
          onClose={() => setPeerModal(null)}
        />
      )}
    </div>
  );
}
