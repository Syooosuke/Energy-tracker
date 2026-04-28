import axios from "axios";

const BASE      = "/api";
const AUTH_BASE = "/auth";

// ── axios インスタンス ────────────────────────────────────────────────
// withCredentials: true → HttpOnly Cookie を自動送受信
// JWT は localStorage ではなく HttpOnly Cookie に保管（XSS対策）
const client = axios.create({ withCredentials: true });

// 認証不要なパブリックエンドポイント用（login/register）
const publicClient = axios.create({ withCredentials: true });

// 401 が返ったら auth:expired イベントを発火してUIを強制ログアウト状態にする
client.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      window.dispatchEvent(new CustomEvent("auth:expired"));
    }
    return Promise.reject(err);
  },
);

// ── 認証 API ─────────────────────────────────────────────────────────
export const register = (email, password, displayName = "") =>
  publicClient.post(`${AUTH_BASE}/register`, { email, password, display_name: displayName })
    .then(r => r.data);

export const login = (email, password) =>
  publicClient.post(`${AUTH_BASE}/login`, { email, password })
    .then(r => r.data);

export const logout = () =>
  publicClient.post(`${AUTH_BASE}/logout`).then(r => r.data).catch(() => {});

export const getMe = () =>
  client.get(`${AUTH_BASE}/me`).then(r => r.data);

// ── アプリ API ────────────────────────────────────────────────────────
export const searchPlace   = (query, limit = 5) =>
  client.post(`${BASE}/search-place`, { query, limit }).then(r => r.data);

export const getRoute      = (payload) =>
  client.post(`${BASE}/route`, payload).then(r => r.data);

export const uploadGPX     = (file) => {
  const fd = new FormData();
  fd.append("file", file);
  return client.post(`${BASE}/gpx`, fd).then(r => r.data);
};

export const getWeather    = (payload) =>
  client.post(`${BASE}/weather`, payload).then(r => r.data);

export const runSimulation = (payload) =>
  client.post(`${BASE}/simulate`, payload).then(r => r.data);

export const getFoods      = () =>
  client.get(`${BASE}/foods`).then(r => r.data);

export const getActivities = () =>
  client.get(`${BASE}/activities`).then(r => r.data);

export const uploadHRLog   = (file) => {
  const fd = new FormData();
  fd.append("file", file);
  return client.post(`${BASE}/upload-hr`, fd).then(r => r.data);
};

export const getHRZones    = (hrMax = 180) =>
  client.get(`${BASE}/hr-zones`, { params: { hr_max: hrMax } }).then(r => r.data);

export const optimizeSupply = (payload) =>
  client.post(`${BASE}/optimize-supply`, payload).then(r => r.data);

// ── 個人化 ML API ──────────────────────────────────────────────────────
export const uploadGarminFit = (file, hrMax = 185, hrRest = 60) => {
  const fd = new FormData();
  fd.append("file", file);
  return client.post(`${BASE}/upload/garmin-fit`, fd, {
    params: { hr_max: hrMax, hr_rest: hrRest },
  }).then(r => r.data);
};

export const uploadAppleHealth = (file, hrMax = 185, hrRest = 60) => {
  const fd = new FormData();
  fd.append("file", file);
  return client.post(`${BASE}/upload/apple-health`, fd, {
    params: { hr_max: hrMax, hr_rest: hrRest },
  }).then(r => r.data);
};

export const getPersonalModels = () =>
  client.get(`${BASE}/ml/personal-models`).then(r => r.data);

export const getRecovery = (targetDate = null) =>
  client.get(`${BASE}/ml/recovery`, {
    params: targetDate ? { target_date: targetDate } : {},
  }).then(r => r.data);

export const getTrainingLoad = (days = 90) =>
  client.get(`${BASE}/ml/training-load`, { params: { days } }).then(r => r.data);

export const getVo2maxTrend = () =>
  client.get(`${BASE}/ml/vo2max`).then(r => r.data);

export const getActivityCalendar = (year) =>
  client.get(`${BASE}/ml/activity-calendar`, { params: year ? { year } : {} }).then(r => r.data);

export const getDashboard = () =>
  client.get(`${BASE}/dashboard`).then(r => r.data);

// ── 補給タイミングアラート ──────────────────────────────────────────────
export const getSupplyTimingAlerts = (simResult, raceStart, currentElapsedMin = 0) =>
  client.post(`${BASE}/alerts/supply-timing`, {
    sim_result: simResult,
    race_start: raceStart,
    current_elapsed_min: currentElapsedMin,
  }).then(r => r.data);

// ── 水分補給量計算 ────────────────────────────────────────────────────
export const calcHydration = (payload) =>
  client.post(`${BASE}/hydration`, payload).then(r => r.data);

// ── 週次/月次レポート ──────────────────────────────────────────────────
export const generateReport = (periodType, year, week = null, month = null) =>
  client.post(`${BASE}/reports/generate`,
    { period_type: periodType, year, week, month },
    { responseType: "blob" }
  ).then(r => r.data);

export const getAvailablePeriods = () =>
  client.get(`${BASE}/reports/available-periods`).then(r => r.data);

// ── ルート比較ダッシュボード ───────────────────────────────────────────
export const getMyRoutes = () =>
  client.get(`${BASE}/routes/my-routes`).then(r => r.data);

export const getRouteHistory = (fingerprint) =>
  client.get(`${BASE}/routes/${fingerprint}/history`).then(r => r.data);

export const compareRoutes = (fingerprint, ids) =>
  client.get(`${BASE}/routes/${fingerprint}/compare`, { params: { ids: ids.join(",") } }).then(r => r.data);

// ── ピア比較 ─────────────────────────────────────────────────────────
export const getPeerComparison = (activityType, distanceKm, elevationGainM) =>
  client.get(`${BASE}/peers/compare`, {
    params: { activity_type: activityType, distance_km: distanceKm, elevation_gain_m: elevationGainM },
  }).then(r => r.data);

// ── Feature importance ────────────────────────────────────────────────
export const getFeatureImportance = () =>
  client.get(`${BASE}/ml/feature-importance`).then(r => r.data);

// ── 予測精度フィードバック ─────────────────────────────────────────────
export const createFeedback = (payload) =>
  client.post(`${BASE}/ml/feedback`, payload).then(r => r.data);

export const getFeedbackHistory = (predictionType = null, limit = 50) =>
  client.get(`${BASE}/ml/feedback`, {
    params: predictionType ? { prediction_type: predictionType, limit } : { limit },
  }).then(r => r.data);

export const getFeedbackSummary = () =>
  client.get(`${BASE}/ml/feedback/summary`).then(r => r.data);

export const deleteFeedback = (feedbackId) =>
  client.delete(`${BASE}/ml/feedback/${feedbackId}`).then(r => r.data);

// ── AI自然言語クエリ ─────────────────────────────────────────────────
export const aiQuery = (question) =>
  client.post(`${BASE}/ai/query`, { question }).then(r => r.data);

// ── レースシミュレーター ──────────────────────────────────────────────
export const simulateRace = (payload) =>
  client.post(`${BASE}/race/simulate`, payload).then(r => r.data);

// ── トレーニングプラン ────────────────────────────────────────────────
export const createTrainingPlan = (payload) =>
  client.post(`${BASE}/training/plan`, payload).then(r => r.data);

export const listTrainingPlans = () =>
  client.get(`${BASE}/training/plan`).then(r => r.data);

export const getTrainingPlan = (planId) =>
  client.get(`${BASE}/training/plan/${planId}`).then(r => r.data);

export const deleteTrainingPlan = (planId) =>
  client.delete(`${BASE}/training/plan/${planId}`).then(r => r.data);

// ── ルート候補比較 ────────────────────────────────────────
export const compareRouteCandidates = (payload) =>
  client.post(`${BASE}/routes/compare-candidates`, payload).then(r => r.data);

// ── トレーニングプランAI調整 ──────────────────────────────
export const adjustTrainingWeek = (planId, weekIndex, message) =>
  client.post(`${BASE}/training/plan/${planId}/adjust`, {
    plan_id: planId, week_index: weekIndex, message,
  }).then(r => r.data);

// ── レース実績入力 ────────────────────────────────────────
export const submitRaceResult = (payload) =>
  client.post(`${BASE}/race/result`, payload).then(r => r.data);

// ── 新機能 API ────────────────────────────────────────────────────────

// 機能1: 怪我リスク予測
export const getInjuryRisk = () =>
  client.get(`${BASE}/ml/injury-risk`).then(r => r.data);

// 機能2: 気象×パフォーマンス相関
export const getWeatherPerformance = () =>
  client.get(`${BASE}/analytics/weather-performance`).then(r => r.data);

// 機能3: 目標タイム達成確率
export const getGoalTimeProbability = (payload) =>
  client.post(`${BASE}/ml/goal-time-probability`, payload).then(r => r.data);

// 機能4: 時間帯別パフォーマンス
export const getTimeOfDayPerformance = () =>
  client.get(`${BASE}/analytics/time-of-day`).then(r => r.data);

// 機能5: ルートライブラリ
export const saveRouteToLibrary = (payload) =>
  client.post(`${BASE}/routes/library`, payload).then(r => r.data);
export const listRouteLibrary = (tag = null) =>
  client.get(`${BASE}/routes/library`, { params: tag ? { tag } : {} }).then(r => r.data);
export const getRouteFromLibrary = (id) =>
  client.get(`${BASE}/routes/library/${id}`).then(r => r.data);
export const deleteRouteFromLibrary = (id) =>
  client.delete(`${BASE}/routes/library/${id}`).then(r => r.data);

// 機能6: ルート上のPOI
export const getRoutePOI = (bbox, categories) =>
  client.post(`${BASE}/route/poi`, { ...bbox, categories }).then(r => r.data);

// 機能7: ルート共有URL
export const createSharedRoute = (payload) =>
  client.post(`${BASE}/share/route`, payload).then(r => r.data);
export const getSharedRoute = (token) =>
  publicClient.get(`${BASE}/share/${token}`).then(r => r.data);

// 機能8: レース戦略レポート
export const generateRaceStrategy = (payload) =>
  client.post(`${BASE}/ai/race-strategy`, payload).then(r => r.data);

// 機能9: 音声テキスト解析
export const parseActivityText = (text) =>
  client.post(`${BASE}/ai/parse-activity-text`, { text }).then(r => r.data);

// 機能10: 週次コーチング
export const getWeeklyCoaching = () =>
  client.get(`${BASE}/ai/weekly-coaching`).then(r => r.data);

// publicClient を api.js 外部でも使えるよう export
export { publicClient };
