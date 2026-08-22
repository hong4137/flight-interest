"""상태(state.json)와 가격 이력(history.csv) 저장소.

GitHub Actions 러너는 매 실행마다 새 컨테이너이므로, 스윕 간에 남겨야 하는 것은
모두 여기에 기록하고 레포에 커밋한다.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .clock import now, today
from .config import DATA_DIR
from .models import Deal

log = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "state.json"
HISTORY_PATH = DATA_DIR / "history.csv"

HISTORY_FIELDS = [
    "found_at",
    "kind",
    "entry",
    "exit",
    "outbound_date",
    "inbound_date",
    "price_per_person",
    "airlines",
    "stops",
    "source",
]

_EMPTY: dict[str, Any] = {
    "version": 1,
    "best_by_route": {},   # route_key -> {price, at, airlines, stops}
    "global_best": None,   # {price, at, route_key}
    "alerts": {},          # fingerprint -> 마지막 발송 시각(iso)
    # 왕복과 오픈조는 운임 구조가 다르다. 하나로 뭉치면 오픈조를 과소평가한다.
    "calibration": {
        "round-trip": {"samples": [], "ratio": None},
        "open-jaw": {"samples": [], "ratio": None},
    },
    "serpapi": {"month": "", "month_used": 0, "day": "", "day_used": 0},
    "hot": [],             # 시간당 스윕이 재확인할 조합 목록
    "last_sweep": None,
}


@dataclass
class Store:
    state: dict[str, Any]

    # ── 로드/저장 ────────────────────────────────────────────
    @classmethod
    def load(cls) -> "Store":
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning("state.json 이 손상되어 초기화합니다")
                data = {}
        else:
            data = {}
        merged = {**json.loads(json.dumps(_EMPTY)), **data}
        return cls(state=merged)

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # ── 최저가 추적 ──────────────────────────────────────────
    def best_for(self, route_key: str) -> int | None:
        entry = self.state["best_by_route"].get(route_key)
        return entry["price"] if entry else None

    def global_best(self) -> int | None:
        gb = self.state.get("global_best")
        return gb["price"] if gb else None

    def record_price(self, deal: Deal) -> tuple[bool, int | None]:
        """가격을 기록하고 (해당 노선 최저가 갱신 여부, 직전 최저가)를 돌려준다."""
        prev = self.best_for(deal.route_key)
        improved = prev is None or deal.price_per_person < prev
        if improved:
            self.state["best_by_route"][deal.route_key] = {
                "price": deal.price_per_person,
                "at": deal.found_at.isoformat(timespec="seconds"),
                "airlines": ",".join(deal.airlines),
                "stops": deal.stops,
            }
        gb = self.state.get("global_best")
        if gb is None or deal.price_per_person < gb["price"]:
            self.state["global_best"] = {
                "price": deal.price_per_person,
                "at": deal.found_at.isoformat(timespec="seconds"),
                "route_key": deal.route_key,
            }
        return improved, prev

    # ── 알림 중복 억제 ───────────────────────────────────────
    def should_alert(self, deal: Deal, cooldown_hours: int) -> bool:
        """쿨다운 내에 같은 지문으로 이미 보냈으면 False."""
        last = self.state["alerts"].get(deal.fingerprint())
        if not last:
            return True
        try:
            sent_at = datetime.fromisoformat(last)
        except ValueError:
            return True
        return now() - sent_at >= timedelta(hours=cooldown_hours)

    def mark_alerted(self, deal: Deal) -> None:
        self.state["alerts"][deal.fingerprint()] = now().isoformat(timespec="seconds")

    def prune_alerts(self, keep_days: int = 14) -> None:
        cutoff = now() - timedelta(days=keep_days)
        kept = {}
        for fp, ts in self.state["alerts"].items():
            try:
                if datetime.fromisoformat(ts) >= cutoff:
                    kept[fp] = ts
            except ValueError:
                continue
        self.state["alerts"] = kept

    # ── 편도합산 → 실가 환산비 자가보정 ───────────────────────
    #
    # 왕복과 오픈조는 운임 구조가 다르다. 항공사는 왕복에만 공격적으로 할인을
    # 붙이므로 왕복은 편도합산의 ~0.71, 오픈조는 ~0.80 근처다. 하나로 뭉치면
    # 오픈조를 실제보다 싸게 보고, 오픈조가 후보 상위를 점령해 확인 예산을
    # 낭비한다. (2026-08-23 실측: 오픈조 추정 336만 vs 실제 401만)
    def _calib_bucket(self, kind: str) -> dict:
        calib = self.state["calibration"]
        # 예전 단일 형식에서 올라온 상태 파일을 이관한다.
        if "samples" in calib:
            legacy = {"samples": calib.get("samples", []), "ratio": calib.get("ratio")}
            self.state["calibration"] = calib = {
                "round-trip": legacy,  # 옛 표본은 전부 같은 도시 왕복이었다
                "open-jaw": {"samples": [], "ratio": None},
            }
        return calib.setdefault(kind, {"samples": [], "ratio": None})

    def add_calibration(self, ow_sum: int, actual: int, kind: str = "round-trip") -> None:
        """실가를 확인할 때마다 표본을 쌓아 비율을 재추정한다."""
        if ow_sum <= 0 or actual <= 0:
            return
        bucket = self._calib_bucket(kind)
        samples = bucket["samples"]
        samples.append([int(ow_sum), int(actual)])
        del samples[:-200]  # 최근 200개만 유지
        ratios = sorted(rt / ow for ow, rt in samples if ow > 0)
        if ratios:
            mid = len(ratios) // 2
            median = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2
            bucket["ratio"] = round(median, 4)

    def calibrated_ratio(self, fallback: float, kind: str = "round-trip") -> float:
        bucket = self._calib_bucket(kind)
        ratio = bucket.get("ratio")
        # 표본이 적으면 설정값을 믿는다.
        if ratio and len(bucket.get("samples", [])) >= 5:
            return float(ratio)
        return fallback

    def calibration_summary(self) -> list[tuple[str, float | None, int]]:
        out = []
        for kind in ("round-trip", "open-jaw"):
            bucket = self._calib_bucket(kind)
            out.append((kind, bucket.get("ratio"), len(bucket.get("samples", []))))
        return out

    # ── SerpApi 크레딧 예산 ──────────────────────────────────
    def serp_budget(self, monthly_cap: int, daily_cap: int) -> tuple[int, int]:
        """(이번 달 남은 콜, 오늘 남은 콜)."""
        s = self.state["serpapi"]
        # 한국 자정 기준. UTC 자정이면 한국시간 오전 9시에 한도가 풀린다.
        day = today()
        today_s, month = day.isoformat(), day.strftime("%Y-%m")
        if s.get("month") != month:
            s["month"], s["month_used"] = month, 0
        if s.get("day") != today_s:
            s["day"], s["day_used"] = today_s, 0
        return (monthly_cap - s["month_used"], daily_cap - s["day_used"])

    def spend_serp(self, n: int = 1) -> None:
        s = self.state["serpapi"]
        s["month_used"] = s.get("month_used", 0) + n
        s["day_used"] = s.get("day_used", 0) + n

    # ── 시간당 스윕이 볼 hot 조합 ────────────────────────────
    def set_hot(self, combos: list[dict]) -> None:
        self.state["hot"] = combos

    def get_hot(self) -> list[dict]:
        return self.state.get("hot", [])

    def touch_sweep(self, mode: str, stats: dict) -> None:
        self.state["last_sweep"] = {
            "at": now().isoformat(timespec="seconds"),
            "mode": mode,
            **stats,
        }


# ── 가격 이력 ────────────────────────────────────────────────

def append_history(deals: list[Deal]) -> None:
    if not deals:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not HISTORY_PATH.exists()
    with HISTORY_PATH.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        if is_new:
            writer.writeheader()
        for deal in deals:
            writer.writerow(deal.to_row())


def read_history(days: int | None = None) -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    cutoff = now() - timedelta(days=days) if days else None
    rows: list[dict] = []
    with HISTORY_PATH.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if cutoff:
                try:
                    if datetime.fromisoformat(row["found_at"]) < cutoff:
                        continue
                except (ValueError, KeyError):
                    continue
            try:
                row["price_per_person"] = int(row["price_per_person"])
            except (ValueError, KeyError):
                continue
            rows.append(row)
    return rows
