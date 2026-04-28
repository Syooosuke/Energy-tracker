"""
異常状態検知モジュール
======================
シミュレーション結果から「危険な区間」を自動検出する。

検出する異常:
  1. エネルギー急落区間  : Ke（消費速度）が急激に高い区間
  2. ペース急落区間      : 速度が突然大きく低下する区間
  3. 心拍スパイク区間    : 心拍が LT を継続して超過する区間
  4. 低エネルギー接近区間: E(t) が危険水準に近づいている区間

手法:
  - ローリング Z スコア（指数移動平均 + 標準偏差）
  - 統計的に「外れ値」な点を異常として検出
  - 連続した異常点を「異常区間」にマージ

リアルタイム利用:
  シミュレーション段階では将来の危険区間を事前に可視化できる。
  実際の活動中は actual_* vs predicted_* の乖離で異常を検出できる。
"""

from __future__ import annotations
import numpy as np
from dataclasses import dataclass, field
from typing import List


# ══════════════════════════════════════════════════════
# 検出結果データクラス
# ══════════════════════════════════════════════════════

@dataclass
class AnomalySegment:
    """
    検出された異常区間。

    Attributes
    ----------
    start_min  : 区間開始時刻 [分]
    end_min    : 区間終了時刻 [分]
    anomaly_type : 異常の種類
    severity   : 深刻度スコア（標準偏差数）
    description: ユーザー向け説明文
    """
    start_min    : float
    end_min      : float
    anomaly_type : str    # "energy_drop" / "pace_drop" / "hr_spike" / "low_energy"
    severity     : float  # 何σ外れているか
    description  : str


# ══════════════════════════════════════════════════════
# 異常検知器
# ══════════════════════════════════════════════════════

class AnomalyDetector:
    """
    シミュレーション結果から異常区間を検出するクラス。

    使用例:
        detector = AnomalyDetector(sigma_threshold=2.5)
        anomalies = detector.detect(pk_result, engine_a, profile, alert_config)
        for seg in anomalies:
            print(f"{seg.start_min:.0f}分〜{seg.end_min:.0f}分: {seg.description}")
    """

    def __init__(self,
                 sigma_threshold : float = 2.5,   # 何σ以上で異常とみなすか
                 min_duration_min: float = 2.0,   # 最小異常区間長 [分]
                 window_min      : float = 10.0): # ローリング窓サイズ [分]
        self.sigma_threshold  = sigma_threshold
        self.min_duration_min = min_duration_min
        self.window_min       = window_min

    def detect(
        self,
        pk_result     : dict,
        engine_a      : dict,
        profile,               # UserProfile
        alert_config  = None,
    ) -> List[AnomalySegment]:
        """
        シミュレーション結果から全種類の異常区間を検出する。

        Parameters
        ----------
        pk_result    : run_pk_model() の戻り値
        engine_a     : _build_engine_a() の戻り値
        profile      : UserProfile
        alert_config : AlertConfig（e_crit の参照に使用）

        Returns
        -------
        検出された AnomalySegment のリスト（時刻順）
        """
        from pk_model_scipy import AlertConfig
        if alert_config is None:
            alert_config = AlertConfig()

        time_min  = pk_result["time_min"]
        e_kcal    = pk_result["E_kcal"]
        ke_series = pk_result["Ke_series"]
        speed_arr = engine_a["speed_curve"]
        hr_arr    = engine_a["hr_predicted"]
        e_crit    = alert_config.e_crit_kcal
        lt_hr     = profile.hr_max * 0.85

        anomalies: List[AnomalySegment] = []

        # ── 1. エネルギー急落区間（Ke が高すぎる） ───
        anomalies += self._detect_spike(
            time_min  = time_min,
            values    = ke_series,
            high_bad  = True,
            atype     = "energy_drop",
            desc_fn   = lambda s: f"エネルギー消費速度が急増（{s:.1f}σ外れ）: 補給を早めてください",
        )

        # ── 2. ペース急落区間（速度が急低下） ─────────
        anomalies += self._detect_spike(
            time_min  = time_min,
            values    = speed_arr[:len(time_min)],
            high_bad  = False,
            atype     = "pace_drop",
            desc_fn   = lambda s: f"予測ペースが急落（{s:.1f}σ低下）: 急坂または体力低下の可能性",
        )

        # ── 3. 心拍スパイク区間（LT を継続超過） ──────
        hr_trimmed = hr_arr[:len(time_min)]
        lt_excess  = np.maximum(hr_trimmed - lt_hr, 0.0)
        anomalies += self._detect_spike(
            time_min  = time_min,
            values    = lt_excess,
            high_bad  = True,
            atype     = "hr_spike",
            desc_fn   = lambda s: f"心拍数が乳酸閾値を継続超過（{s:.1f}σ): ペースを落としてください",
        )

        # ── 4. 低エネルギー接近区間（E(t) が e_crit 付近） ──
        anomalies += self._detect_low_energy(
            time_min  = time_min,
            e_kcal    = e_kcal,
            e_crit    = e_crit,
            warn_margin = 200.0,  # e_crit + 200 kcal 以下で警告
        )

        # 重複排除＋時刻順ソート
        anomalies = self._deduplicate(anomalies)
        anomalies.sort(key=lambda x: x.start_min)
        return anomalies

    # ── 内部ロジック ──────────────────────────────────

    def _detect_spike(
        self,
        time_min : np.ndarray,
        values   : np.ndarray,
        high_bad : bool,          # True: 高い値が異常 / False: 低い値が異常
        atype    : str,
        desc_fn,
    ) -> List[AnomalySegment]:
        """ローリング Z スコアで異常点を検出し、連続区間にまとめる。"""
        if len(values) == 0 or len(time_min) == 0:
            return []

        n = min(len(time_min), len(values))
        vals = np.array(values[:n], dtype=float)

        # ローリング平均・標準偏差（pandas 相当を numpy で実装）
        dt_min = float(time_min[1] - time_min[0]) if n > 1 else 1.0
        w = max(2, int(self.window_min / dt_min))

        roll_mean = np.array([vals[max(0, i-w):i+1].mean() for i in range(n)])
        roll_std  = np.array([vals[max(0, i-w):i+1].std()  for i in range(n)])
        roll_std  = np.maximum(roll_std, 1e-9)   # ゼロ除算防止

        z_scores = (vals - roll_mean) / roll_std
        if not high_bad:
            z_scores = -z_scores   # 低い値が異常 → 符号反転

        # 閾値以上の点を異常フラグとしてマーク
        flags = z_scores > self.sigma_threshold

        return self._flags_to_segments(time_min[:n], flags, z_scores, atype, desc_fn)

    def _detect_low_energy(
        self,
        time_min    : np.ndarray,
        e_kcal      : np.ndarray,
        e_crit      : float,
        warn_margin : float,
    ) -> List[AnomalySegment]:
        """E(t) が (e_crit + warn_margin) 以下の区間を検出する。"""
        n = min(len(time_min), len(e_kcal))
        if n == 0:
            return []

        warn_threshold = e_crit + warn_margin
        flags = np.array(e_kcal[:n]) < warn_threshold
        severity_arr = np.maximum(
            (warn_threshold - np.array(e_kcal[:n])) / max(warn_margin, 1.0),
            0.0
        )

        segments: List[AnomalySegment] = []
        in_seg = False
        seg_start = 0.0
        max_sev = 0.0

        for i, flag in enumerate(flags):
            if flag and not in_seg:
                in_seg    = True
                seg_start = float(time_min[i])
                max_sev   = severity_arr[i]
            elif flag and in_seg:
                max_sev = max(max_sev, severity_arr[i])
            elif not flag and in_seg:
                dur = float(time_min[i]) - seg_start
                if dur >= self.min_duration_min:
                    segments.append(AnomalySegment(
                        start_min    = seg_start,
                        end_min      = float(time_min[i]),
                        anomaly_type = "low_energy",
                        severity     = round(max_sev * self.sigma_threshold, 2),
                        description  = (
                            f"エネルギー残量が危険水準に接近（{e_crit:.0f}kcal まで"
                            f" {warn_margin:.0f}kcal 以内）: 即補給を検討してください"
                        ),
                    ))
                in_seg = False

        if in_seg:
            dur = float(time_min[-1]) - seg_start
            if dur >= self.min_duration_min:
                segments.append(AnomalySegment(
                    start_min    = seg_start,
                    end_min      = float(time_min[-1]),
                    anomaly_type = "low_energy",
                    severity     = round(max_sev * self.sigma_threshold, 2),
                    description  = (
                        f"エネルギー残量が危険水準に接近: 即補給を検討してください"
                    ),
                ))
        return segments

    def _flags_to_segments(
        self,
        time_min : np.ndarray,
        flags    : np.ndarray,
        z_scores : np.ndarray,
        atype    : str,
        desc_fn,
    ) -> List[AnomalySegment]:
        """異常フラグ配列を連続区間リストに変換する。"""
        segments: List[AnomalySegment] = []
        in_seg   = False
        seg_start = 0.0
        max_z     = 0.0

        for i, flag in enumerate(flags):
            if flag and not in_seg:
                in_seg    = True
                seg_start = float(time_min[i])
                max_z     = float(z_scores[i])
            elif flag and in_seg:
                max_z = max(max_z, float(z_scores[i]))
            elif not flag and in_seg:
                dur = float(time_min[i]) - seg_start
                if dur >= self.min_duration_min:
                    segments.append(AnomalySegment(
                        start_min    = seg_start,
                        end_min      = float(time_min[i]),
                        anomaly_type = atype,
                        severity     = round(max_z, 2),
                        description  = desc_fn(max_z),
                    ))
                in_seg = False

        if in_seg:
            dur = float(time_min[-1]) - seg_start
            if dur >= self.min_duration_min:
                segments.append(AnomalySegment(
                    start_min    = seg_start,
                    end_min      = float(time_min[-1]),
                    anomaly_type = atype,
                    severity     = round(max_z, 2),
                    description  = desc_fn(max_z),
                ))
        return segments

    @staticmethod
    def _deduplicate(segments: List[AnomalySegment]) -> List[AnomalySegment]:
        """同種・同区間の重複を除去する。"""
        seen: set[tuple] = set()
        result: List[AnomalySegment] = []
        for seg in segments:
            key = (seg.anomaly_type, round(seg.start_min, 1), round(seg.end_min, 1))
            if key not in seen:
                seen.add(key)
                result.append(seg)
        return result
