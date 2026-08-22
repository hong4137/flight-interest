"""config/search.yaml 로드 및 검증."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "search.yaml"
DATA_DIR = ROOT / "data"


def _dates(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError(f"종료일({end})이 시작일({start})보다 빠릅니다")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


@dataclass
class Config:
    origin: str
    outbound_dates: list[date]
    arrive_korea: tuple[date, date]
    return_search_dates: list[date]
    entry_airports: list[str]
    exit_airports: list[str]
    allow_open_jaw: bool

    cabin: str
    max_stops: int
    passengers: int
    currency: str
    language: str
    hide_separate_and_self_transfer: bool

    threshold_pp: int
    ow_to_rt_ratio: float
    ow_to_openjaw_ratio: float
    t2_margin: float
    new_low_drop_pct: float

    cooldown_hours: int
    digest_top_n: int

    serpapi_monthly_cap: int
    serpapi_daily_cap: int
    serpapi_per_sweep_cap: int
    serpapi_warn_pct: int

    jitter: tuple[float, float]
    timeout: int
    max_retries: int
    hot_roundtrip_count: int
    hot_openjaw_count: int
    full_confirm_count: int
    fail_rate_alert_threshold: float

    raw: dict = field(default_factory=dict)

    # ── 시크릿 (환경변수에서만 읽는다) ──────────────────────────
    @property
    def telegram_token(self) -> str:
        return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

    @property
    def telegram_chat_id(self) -> str:
        return os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    @property
    def serpapi_key(self) -> str:
        return os.environ.get("SERPAPI_KEY", "").strip()

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @property
    def serpapi_enabled(self) -> bool:
        return bool(self.serpapi_key)

    def ratio_for(self, is_open_jaw: bool) -> float:
        return self.ow_to_openjaw_ratio if is_open_jaw else self.ow_to_rt_ratio

    def threshold_trigger_pp(self) -> int:
        """정밀 확인으로 승격시킬 추정가 상한 (1인당)."""
        return int(self.threshold_pp * self.t2_margin)


def _flatten_airports(value, label: str) -> list[str]:
    """{'IT': [...], 'ES': [...]} 또는 [...] 를 평평한 공항 목록으로."""
    if isinstance(value, dict):
        out: list[str] = []
        for codes in value.values():
            out.extend(codes)
    elif isinstance(value, list):
        out = list(value)
    else:
        raise ValueError(f"{label} 형식이 잘못되었습니다: {value!r}")

    seen, uniq = set(), []
    for code in out:
        code = str(code).strip().upper()
        if len(code) != 3:
            raise ValueError(f"{label}의 공항코드가 IATA 3자리가 아닙니다: {code!r}")
        if code not in seen:
            seen.add(code)
            uniq.append(code)
    if not uniq:
        raise ValueError(f"{label}가 비어 있습니다")
    return uniq


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG
    if not path.exists():
        raise FileNotFoundError(f"설정 파일이 없습니다: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    trip, search = raw["trip"], raw["search"]
    thr, alerting = raw["threshold"], raw["alerting"]
    budget, runtime = raw["budget"], raw["runtime"]

    ob = trip["outbound_dates"]
    outbound_dates = _dates(ob["start"], ob["end"])

    ak = trip["arrive_korea"]
    arrive_start, arrive_end = ak["start"], ak["end"]
    if arrive_end < arrive_start:
        raise ValueError("arrive_korea 종료일이 시작일보다 빠릅니다")
    if arrive_start <= outbound_dates[-1]:
        raise ValueError("인천 도착창이 출발창보다 빠르거나 겹칩니다")

    # 유럽 출발일은 도착창보다 최대 pad 일 앞설 수 있다. 최종 채택은 실제 도착일로 거른다.
    pad = int(trip.get("return_search_pad_days", 2))
    return_search_dates = _dates(arrive_start - timedelta(days=pad), arrive_end)

    entry = _flatten_airports(trip["entry_airports"], "entry_airports")
    exit_raw = trip.get("exit_airports", "same_as_entry")
    exits = entry if exit_raw == "same_as_entry" else _flatten_airports(exit_raw, "exit_airports")

    jitter = runtime.get("request_jitter_seconds", [2.5, 4.5])

    cabin = str(search["cabin"])
    if cabin not in {"economy", "premium-economy", "business", "first"}:
        raise ValueError(f"cabin 값이 잘못되었습니다: {cabin}")

    passengers = int(search.get("passengers", 1))
    if passengers < 1:
        raise ValueError("passengers 는 1 이상이어야 합니다")

    return Config(
        origin=str(trip["origin"]).upper(),
        outbound_dates=outbound_dates,
        arrive_korea=(arrive_start, arrive_end),
        return_search_dates=return_search_dates,
        entry_airports=entry,
        exit_airports=exits,
        allow_open_jaw=bool(trip.get("allow_open_jaw", True)),
        cabin=cabin,
        max_stops=int(search.get("max_stops", 1)),
        passengers=passengers,
        currency=str(search.get("currency", "KRW")),
        language=str(search.get("language", "ko")),
        hide_separate_and_self_transfer=bool(
            search.get("hide_separate_and_self_transfer", True)
        ),
        threshold_pp=int(thr["per_person_krw"]),
        ow_to_rt_ratio=float(thr.get("ow_to_rt_ratio", 0.70)),
        ow_to_openjaw_ratio=float(thr.get("ow_to_openjaw_ratio", 0.80)),
        t2_margin=float(thr.get("t2_margin", 1.25)),
        new_low_drop_pct=float(thr.get("new_low_alert_drop_pct", 5)),
        cooldown_hours=int(alerting.get("duplicate_cooldown_hours", 24)),
        digest_top_n=int(alerting.get("digest_top_n", 5)),
        serpapi_monthly_cap=int(budget.get("serpapi_monthly_cap", 240)),
        serpapi_daily_cap=int(budget.get("serpapi_daily_cap", 8)),
        serpapi_per_sweep_cap=int(budget.get("serpapi_per_sweep_cap", 2)),
        serpapi_warn_pct=int(budget.get("serpapi_warn_at_pct", 20)),
        jitter=(float(jitter[0]), float(jitter[1])),
        timeout=int(runtime.get("request_timeout_seconds", 30)),
        max_retries=int(runtime.get("max_retries", 3)),
        hot_roundtrip_count=int(runtime.get("hot_roundtrip_count", 8)),
        hot_openjaw_count=int(runtime.get("hot_openjaw_count", 4)),
        full_confirm_count=int(runtime.get("full_confirm_count", 30)),
        fail_rate_alert_threshold=float(runtime.get("fail_rate_alert_threshold", 0.35)),
        raw=raw,
    )
