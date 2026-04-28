"""
models/route_fingerprint.py — ルートフィンガープリント生成
===========================================================
同一ルートを判定するための短いハッシュを生成する。

アルゴリズム:
  1. 座標列からサンプル点（最大12点）を選択
  2. 緯度・経度を小数点2桁に丸める（約1km 精度）
  3. JSON 化して SHA-256 → 先頭16文字を返す

これにより:
  - 同一ルートなら常に同じ文字列
  - 微妙に異なるルート（別道、逆方向）は別のハッシュ
  - GPS 誤差（数十m）は丸めで吸収
"""

from __future__ import annotations

import hashlib
import json
from typing import List, Tuple


def compute_gpx_fingerprint(
    coords: List[Tuple[float, float]],
    precision: int = 2,
) -> str:
    """
    座標列からルートフィンガープリントを生成する。

    Parameters
    ----------
    coords : list of (lat, lon)
    precision : int
        丸め精度。2 = 小数点2桁 ≈ 1.1km 精度。

    Returns
    -------
    str
        16文字の16進数ハッシュ文字列。
    """
    if not coords or len(coords) < 2:
        return ""

    # サンプル点を選択（始点・終点・等間隔の中間点）
    sample_indices = _sample_indices(len(coords), 12)
    sampled = [
        [round(float(coords[i][0]), precision), round(float(coords[i][1]), precision)]
        for i in sample_indices
    ]

    payload = json.dumps(sampled, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _sample_indices(n: int, k: int = 12) -> List[int]:
    """n 点から k 点を等間隔に選ぶ（始点と終点を必ず含む）。"""
    if n <= k:
        return list(range(n))
    indices = [0]
    step = (n - 1) / (k - 1)
    for i in range(1, k - 1):
        indices.append(round(i * step))
    indices.append(n - 1)
    return sorted(set(indices))
