"""
models/ai_query.py — AI自然言語クエリエンジン
================================================
ユーザーの自然言語での質問を無料AIプロバイダーに送り、
DBデータのサマリーと組み合わせて回答を生成する。

対応プロバイダー（優先順）:
  1. Google Gemini  (GEMINI_API_KEY)  — 無料枠: 1,500 req/日
  2. Groq           (GROQ_API_KEY)    — 無料枠: 14,400 req/日・超高速
  3. Ollama         (ローカル)        — 完全無料（要 ollama 起動）
  4. 統計フォールバック              — 常時利用可能

環境変数:
  AI_PROVIDER      : "auto" | "gemini" | "groq" | "ollama" | "none"
                     (デフォルト: "auto" — APIキーを自動検出)
  GEMINI_API_KEY   : Google AI Studio で取得
  GROQ_API_KEY     : console.groq.com で取得
  OLLAMA_BASE_URL  : Ollamaのエンドポイント (デフォルト: http://localhost:11434)
  OLLAMA_MODEL     : 使用するモデル名 (デフォルト: llama3.2)

使用例:
  engine = AIQueryEngine()
  result = engine.query("先月一番疲労が高い日は？", context=ctx)
  print(result.answer)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False


# ══════════════════════════════════════════════════════
# データ構造
# ══════════════════════════════════════════════════════

@dataclass
class QueryContext:
    """DBから収集したユーザーデータのサマリー。"""
    recent_activities: list[dict]   # [{date, type, distance_km, duration_min, avg_hr, trimp}]
    daily_metrics: list[dict]       # [{date, atl, ctl, tsb, recovery_score, resting_hr, hrv_rmssd, sleep_hours}]
    stats: dict                     # {total_activities, total_distance_km, avg_weekly_trimp, vo2max_latest}


@dataclass
class QueryResult:
    answer: str
    data_used: list[str]
    confidence: str                  # "high" | "medium" | "low"
    follow_up_suggestions: list[str]
    provider: str                    # 実際に使用したプロバイダー名


# ══════════════════════════════════════════════════════
# プロンプト定義
# ══════════════════════════════════════════════════════

_SYSTEM_PROMPT = """あなたはアウトドアアスリート向けのパーソナルコーチAIです。
ユーザーのトレーニングデータ（心拍数・ペース・疲労度・リカバリースコアなど）を分析し、
具体的で実用的なインサイトを日本語で提供します。

回答のスタイル:
- 簡潔かつ具体的に（数値を交えて）
- 専門用語は必要に応じて括弧内に説明を追加
- アドバイスは必ず根拠となるデータに基づく
- 過度に楽観的・悲観的にならず、データが示す事実を中立的に伝える
- 回答は200〜400字程度にまとめる（長くなる場合は箇条書きを使用）"""

_USER_TEMPLATE = """## ユーザーのトレーニングデータ

### 統計サマリー
{stats_text}

### 直近のアクティビティ（最新30件）
{activities_text}

### 日次メトリクス（直近90日）
{metrics_text}

---
ユーザーの質問: {question}

上記データに基づいて回答してください。データに含まれない情報については「データが不足しています」と明示してください。"""


# ══════════════════════════════════════════════════════
# AIクエリエンジン
# ══════════════════════════════════════════════════════

class AIQueryEngine:
    """複数の無料AIプロバイダーを自動選択するNLクエリエンジン。"""

    def __init__(self):
        self._provider = os.environ.get("AI_PROVIDER", "auto").lower()

    def query(self, question: str, context: QueryContext) -> QueryResult:
        prompt = _USER_TEMPLATE.format(
            stats_text=_format_stats(context.stats),
            activities_text=_format_activities(context.recent_activities),
            metrics_text=_format_metrics(context.daily_metrics),
            question=question,
        )

        if not _REQUESTS_AVAILABLE:
            return _fallback_answer(question, context)

        providers = _resolve_providers(self._provider)

        for name, call_fn in providers:
            try:
                answer = call_fn(prompt)
                if answer:
                    return QueryResult(
                        answer=answer,
                        data_used=_detect_data_used(question),
                        confidence="high" if len(context.recent_activities) >= 10 else "medium",
                        follow_up_suggestions=_suggest_follow_ups(question),
                        provider=name,
                    )
            except Exception:
                continue  # 次のプロバイダーを試みる

        return _fallback_answer(question, context)


# ══════════════════════════════════════════════════════
# プロバイダー選択
# ══════════════════════════════════════════════════════

def _resolve_providers(mode: str) -> list[tuple[str, callable]]:
    """使用するプロバイダーのリストを返す。"""
    all_providers = {
        "anthropic": ("Claude",  _call_anthropic),
        "gemini":    ("Gemini",  _call_gemini),
        "groq":      ("Groq",    _call_groq),
        "ollama":    ("Ollama",  _call_ollama),
    }

    if mode == "none":
        return []

    if mode == "auto":
        candidates = []
        if os.environ.get("ANTHROPIC_API_KEY"):
            candidates.append(all_providers["anthropic"])
        if os.environ.get("GEMINI_API_KEY"):
            candidates.append(all_providers["gemini"])
        if os.environ.get("GROQ_API_KEY"):
            candidates.append(all_providers["groq"])
        # Ollama は常に候補に（起動していなければ失敗して次へ）
        candidates.append(all_providers["ollama"])
        return candidates

    if mode in all_providers:
        return [all_providers[mode]]

    return []


# ══════════════════════════════════════════════════════
# 各プロバイダーの呼び出し実装
# ══════════════════════════════════════════════════════

def _call_anthropic(prompt: str) -> str:
    """Anthropic Claude API (claude-haiku) — ANTHROPIC_API_KEY が必要"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": model,
        "max_tokens": 1024,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    resp = _requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["content"][0]["text"]


def _call_gemini(prompt: str) -> str:
    """Google Gemini API (gemini-1.5-flash-8b) — 無料枠: 1,500 req/日"""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")

    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash-lite")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.3},
    }
    resp = _requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _call_groq(prompt: str) -> str:
    """Groq API (llama-3.1-8b-instant) — 無料枠: 14,400 req/日・超高速"""
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise ValueError("GROQ_API_KEY not set")

    model = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1024,
        "temperature": 0.3,
    }
    resp = _requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _call_ollama(prompt: str) -> str:
    """Ollama ローカル推論 — 完全無料（事前に `ollama run llama3.2` が必要）"""
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "llama3.2")
    url = f"{base_url}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 1024},
    }
    resp = _requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data["message"]["content"]


# ══════════════════════════════════════════════════════
# フォーマットヘルパー
# ══════════════════════════════════════════════════════

def _format_stats(stats: dict) -> str:
    lines = []
    if stats.get("total_activities"):
        lines.append(f"- 総アクティビティ数: {stats['total_activities']}件")
    if stats.get("total_distance_km"):
        lines.append(f"- 総距離: {stats['total_distance_km']:.1f} km")
    if stats.get("avg_weekly_trimp"):
        lines.append(f"- 週平均TRIMP: {stats['avg_weekly_trimp']:.1f}")
    if stats.get("vo2max_latest"):
        lines.append(f"- 最新VO2max推定値: {stats['vo2max_latest']:.1f} mL/kg/min")
    return "\n".join(lines) if lines else "データなし"


def _format_activities(activities: list[dict]) -> str:
    if not activities:
        return "アクティビティデータなし"
    lines = []
    for a in activities[:20]:
        parts = [a.get("date", "?"), a.get("type", "?")]
        if a.get("distance_km"):
            parts.append(f"{a['distance_km']:.1f}km")
        if a.get("duration_min"):
            parts.append(f"{a['duration_min']:.0f}分")
        if a.get("avg_hr"):
            parts.append(f"HR:{a['avg_hr']:.0f}bpm")
        if a.get("trimp"):
            parts.append(f"TRIMP:{a['trimp']:.0f}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _format_metrics(metrics: list[dict]) -> str:
    if not metrics:
        return "日次データなし"
    lines = []
    for m in metrics[-10:]:
        parts = [m.get("date", "?")]
        if m.get("tsb") is not None:
            parts.append(f"TSB:{m['tsb']:.1f}")
        if m.get("atl") is not None:
            parts.append(f"ATL:{m['atl']:.1f}")
        if m.get("recovery_score") is not None:
            parts.append(f"回復:{m['recovery_score']:.0f}")
        if m.get("hrv_rmssd") is not None:
            parts.append(f"HRV:{m['hrv_rmssd']:.0f}ms")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _detect_data_used(question: str) -> list[str]:
    used = []
    q = question.lower()
    keywords = {
        "アクティビティ・距離": ["距離", "活動", "km", "走", "登山", "ハイキング"],
        "疲労・ATL":            ["疲労", "atl", "負荷", "きつ"],
        "フィットネス・CTL":    ["ctl", "フィットネス", "体力"],
        "コンディション・TSB":  ["tsb", "コンディション", "調子"],
        "リカバリー":           ["回復", "リカバリ", "休養"],
        "HRV・心拍":            ["hrv", "心拍", "hr"],
        "VO2max":               ["vo2max", "有酸素"],
        "睡眠":                 ["睡眠", "寝", "sleep"],
    }
    for label, words in keywords.items():
        if any(w in q for w in words):
            used.append(label)
    return used or ["一般データ"]


def _suggest_follow_ups(question: str) -> list[str]:
    candidates = [
        "直近4週間のトレーニング量の傾向を教えて",
        "今のコンディションで重要な山行に出ても大丈夫？",
        "VO2maxはこの3ヶ月で改善している？",
        "過去最高のリカバリースコアはいつ？",
        "週あたりのTRIMPが最も高かった週はいつ？",
    ]
    q = question.lower()
    return [s for s in candidates if not any(w in q for w in s[:8].lower().split())][:3]


def _fallback_answer(question: str, context: QueryContext) -> QueryResult:
    """APIなし・全プロバイダー失敗時の統計ベース回答。"""
    metrics = context.daily_metrics
    activities = context.recent_activities
    parts = []

    if metrics:
        latest = metrics[-1]
        tsb = latest.get("tsb")
        atl = latest.get("atl")
        ctl = latest.get("ctl")
        rec = latest.get("recovery_score")
        if tsb is not None:
            status = "フレッシュ" if tsb >= 10 else ("良好" if tsb >= 0 else ("疲労気味" if tsb >= -10 else "要休養"))
            parts.append(f"現在のコンディション: TSB={tsb:.1f} ({status})")
        if atl is not None and ctl is not None:
            parts.append(f"ATL={atl:.1f} / CTL={ctl:.1f}")
        if rec is not None:
            parts.append(f"リカバリースコア: {rec:.0f}/100")

    if activities:
        total = len(activities)
        avg_dist = sum(a.get("distance_km", 0) for a in activities) / max(total, 1)
        parts.append(f"直近{total}件の平均距離: {avg_dist:.1f}km")

    if not parts:
        parts.append("データが不足しています。アクティビティをアップロードしてください。")

    parts.append(
        "\n※ AI回答を有効にするには ANTHROPIC_API_KEY（Claude）または"
        " GEMINI_API_KEY / GROQ_API_KEY を Render の環境変数に設定してください。"
    )

    return QueryResult(
        answer="\n".join(parts),
        data_used=["統計サマリー"],
        confidence="medium",
        follow_up_suggestions=[],
        provider="fallback",
    )
