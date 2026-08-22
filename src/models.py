"""검색 결과를 표현하는 공용 데이터 모델.

가격은 **항상 1인당 원화(KRW)**로 정규화해서 보관한다.
Google Flights 가 돌려주는 값은 전체 인원 합계이므로, 파싱 직후 인원수로 나눈다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime

from .clock import now
from typing import Literal


@dataclass(frozen=True)
class Segment:
    """실제 탑승 구간 하나 (경유가 있으면 여러 개)."""

    from_airport: str
    to_airport: str
    depart: datetime
    arrive: datetime
    duration_minutes: int = 0
    plane: str = ""


@dataclass
class Itinerary:
    """한 방향(편도) 또는 왕복 전체의 검색 결과 한 건."""

    price_per_person: int
    """1인당 KRW."""

    airlines: list[str]
    segments: list[Segment]
    source: str = "gflights"
    """gflights | serpapi"""

    deep_link: str = ""

    @property
    def stops(self) -> int:
        return max(len(self.segments) - 1, 0)

    @property
    def depart_at(self) -> datetime:
        return self.segments[0].depart

    @property
    def arrive_at(self) -> datetime:
        return self.segments[-1].arrive

    @property
    def origin(self) -> str:
        return self.segments[0].from_airport

    @property
    def destination(self) -> str:
        return self.segments[-1].to_airport

    @property
    def total_minutes(self) -> int:
        return int((self.arrive_at - self.depart_at).total_seconds() // 60)

    def airline_label(self) -> str:
        return ", ".join(self.airlines) if self.airlines else "?"

    def via(self) -> str:
        """경유지 공항 코드."""
        return ", ".join(s.to_airport for s in self.segments[:-1])


@dataclass
class Candidate:
    """(입국도시, 출국도시, 출발일, 귀국일) 하나에 대한 후보."""

    entry: str
    exit: str
    outbound_date: date
    inbound_date: date

    estimated_pp: int
    """편도 합산으로 추정한 1인당 왕복가."""

    outbound: Itinerary | None = None
    inbound: Itinerary | None = None

    @property
    def is_open_jaw(self) -> bool:
        return self.entry != self.exit

    @property
    def key(self) -> str:
        # 구분자는 "|". 날짜에 "-" 가 들어 있어 "-" 로는 되쪼갤 수 없다.
        return (
            f"{self.entry}|{self.exit}|"
            f"{self.outbound_date.isoformat()}|{self.inbound_date.isoformat()}"
        )


@dataclass
class Deal:
    """실제 확인된 왕복/오픈조 가격. 알림과 이력의 단위."""

    entry: str
    exit: str
    outbound_date: date
    inbound_date: date
    price_per_person: int
    airlines: list[str]
    stops: int
    deep_link: str
    source: str
    kind: Literal["round-trip", "open-jaw"] = "round-trip"
    korea_arrival: datetime | None = None
    total_minutes: int = 0
    found_at: datetime = field(default_factory=now)

    @property
    def route_key(self) -> str:
        # 구분자는 "|". 날짜에 "-" 가 들어 있어 "-" 로는 되쪼갤 수 없다.
        return (
            f"{self.entry}|{self.exit}|"
            f"{self.outbound_date.isoformat()}|{self.inbound_date.isoformat()}"
        )

    def fingerprint(self) -> str:
        """중복 알림 억제용. 가격은 5만원 버킷으로 뭉개 미세 변동을 무시한다."""
        bucket = self.price_per_person // 50_000
        raw = f"{self.route_key}|{','.join(sorted(self.airlines))}|{self.stops}|{bucket}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def to_row(self) -> dict:
        return {
            "found_at": self.found_at.isoformat(timespec="seconds"),
            "kind": self.kind,
            "entry": self.entry,
            "exit": self.exit,
            "outbound_date": self.outbound_date.isoformat(),
            "inbound_date": self.inbound_date.isoformat(),
            "price_per_person": self.price_per_person,
            "airlines": ",".join(self.airlines),
            "stops": self.stops,
            "source": self.source,
        }
