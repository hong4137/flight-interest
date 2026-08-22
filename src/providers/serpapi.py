"""SerpApi 백업 제공자.

두 가지 일만 한다.

1. **오픈조(멀티시티) 실가 확인** — fast-flights 로는 불가능하다. Google 은 멀티시티
   검색 결과를 첫 응답의 ds:1 스크립트에 싣지 않고(payload[3] 이 None), 구간을
   하나씩 선택해야 내려준다. SerpApi 는 그 과정을 대신해 준다.
2. **사각지대 스캔** — Deals 엔진으로 우리가 열거하지 않은 도시/항공사의 특가를 훑는다.

무료 플랜은 월 250콜뿐이므로 호출 전에 반드시 예산을 확인한다.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime

import requests

from ..config import Config
from ..models import Itinerary, Segment
from ..store import Store

log = logging.getLogger(__name__)

ENDPOINT = "https://serpapi.com/search.json"

_CABIN_CODE = {"economy": 1, "premium-economy": 2, "business": 3, "first": 4}
# SerpApi: 0=제한없음, 1=직항만, 2=경유 1회 이하, 3=경유 2회 이하
_STOPS_CODE = {0: 1, 1: 2, 2: 3}


class BudgetExhausted(RuntimeError):
    """월/일 크레딧 상한에 도달."""


class SerpApiClient:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store

    @property
    def enabled(self) -> bool:
        return self.cfg.serpapi_enabled

    def remaining(self) -> tuple[int, int]:
        return self.store.serp_budget(
            self.cfg.serpapi_monthly_cap, self.cfg.serpapi_daily_cap
        )

    def _call(self, params: dict) -> dict:
        if not self.enabled:
            raise BudgetExhausted("SERPAPI_KEY 가 설정되지 않았습니다")
        month_left, day_left = self.remaining()
        if month_left <= 0:
            raise BudgetExhausted("이번 달 SerpApi 크레딧을 모두 사용했습니다")
        if day_left <= 0:
            raise BudgetExhausted("오늘 SerpApi 호출 한도에 도달했습니다")

        params = {**params, "api_key": self.cfg.serpapi_key, "no_cache": "false"}
        resp = requests.get(ENDPOINT, params=params, timeout=self.cfg.timeout)
        # 크레딧은 요청을 보낸 시점에 소모된 것으로 간주한다(실패해도 보수적으로 차감).
        self.store.spend_serp(1)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError("SerpApi 오류: {}".format(data["error"]))
        return data

    # ── 응답 정규화 ──────────────────────────────────────────
    def _to_itineraries(self, data: dict, deep_link: str) -> list[Itinerary]:
        """SerpApi 응답을 Itinerary 목록으로. 가격은 1인당으로 환산한다.

        SerpApi 의 price 는 **전체 인원 합계**다. gflights 계층과 같은 규칙이므로
        동일하게 인원수로 나눈다.

        2026-08-23 실측 확인: 같은 오픈조 질의(ICN→FCO / OPO→ICN)에서
        adults=1 은 4,229,000, adults=2 는 8,458,000 을 돌려줬다. 정확히 2배다.
        (여기서 틀리면 모든 판단에 2배 오차가 나므로 추측하지 않고 확인했다.)
        """
        found: list[Itinerary] = []
        for bucket in ("best_flights", "other_flights"):
            for offer in data.get(bucket, []) or []:
                price = offer.get("price")
                if not isinstance(price, (int, float)) or price <= 0:
                    continue
                segments: list[Segment] = []
                for leg in offer.get("flights", []) or []:
                    try:
                        segments.append(
                            Segment(
                                from_airport=leg["departure_airport"]["id"],
                                to_airport=leg["arrival_airport"]["id"],
                                depart=datetime.fromisoformat(leg["departure_airport"]["time"]),
                                arrive=datetime.fromisoformat(leg["arrival_airport"]["time"]),
                                duration_minutes=int(leg.get("duration") or 0),
                                plane=str(leg.get("airplane") or ""),
                            )
                        )
                    except (KeyError, ValueError, TypeError):
                        continue
                if not segments:
                    continue
                airlines = sorted({str(f.get("airline")) for f in offer.get("flights", []) if f.get("airline")})
                found.append(
                    Itinerary(
                        price_per_person=int(round(price / max(self.cfg.passengers, 1))),
                        airlines=airlines,
                        segments=segments,
                        source="serpapi",
                        deep_link=deep_link,
                    )
                )
        found.sort(key=lambda it: it.price_per_person)
        return found

    # ── 오픈조 ───────────────────────────────────────────────
    def open_jaw(
        self, entry: str, exit_city: str, outbound: date, inbound: date, deep_link: str = ""
    ) -> list[Itinerary]:
        """ICN -> entry (outbound), exit_city -> ICN (inbound) 멀티시티 실가."""
        legs = [
            {"departure_id": self.cfg.origin, "arrival_id": entry, "date": outbound.isoformat()},
            {"departure_id": exit_city, "arrival_id": self.cfg.origin, "date": inbound.isoformat()},
        ]
        data = self._call(
            {
                "engine": "google_flights",
                "type": 3,  # multi-city
                "multi_city_json": json.dumps(legs),
                "travel_class": _CABIN_CODE.get(self.cfg.cabin, 3),
                "stops": _STOPS_CODE.get(self.cfg.max_stops, 0),
                "adults": self.cfg.passengers,
                "currency": self.cfg.currency,
                "gl": "kr",
                "hl": self.cfg.language,
            }
        )
        return self._to_itineraries(data, deep_link)

    # ── 사각지대 스캔 ────────────────────────────────────────
    def deals(self) -> list[dict]:
        """출발지 하나 기준으로 날짜창 전체의 특가 목록. 1콜로 넓게 훑는다."""
        out = self.cfg.outbound_dates
        ret = self.cfg.return_search_dates
        data = self._call(
            {
                "engine": "google_flights_deals",
                "departure_id": self.cfg.origin,
                "outbound_date": "{},{}".format(out[0].isoformat(), out[-1].isoformat()),
                "return_date": "{},{}".format(ret[0].isoformat(), ret[-1].isoformat()),
                "travel_class": _CABIN_CODE.get(self.cfg.cabin, 3),
                "stops": _STOPS_CODE.get(self.cfg.max_stops, 0),
                "adults": self.cfg.passengers,
                "currency": self.cfg.currency,
                "gl": "kr",
                "hl": self.cfg.language,
            }
        )
        results = []
        for deal in data.get("deals", []) or []:
            price = deal.get("price")
            if not isinstance(price, (int, float)):
                continue
            results.append(
                {
                    "destination": deal.get("destination_id") or deal.get("name"),
                    "name": deal.get("name"),
                    "country": deal.get("country"),
                    "price_per_person": int(round(price / max(self.cfg.passengers, 1))),
                    "outbound_date": deal.get("outbound_date"),
                    "return_date": deal.get("return_date"),
                    "airline": deal.get("airline"),
                    "stops": deal.get("stops"),
                    "link": deal.get("flight_link") or "",
                }
            )
        results.sort(key=lambda d: d["price_per_person"])
        return results
