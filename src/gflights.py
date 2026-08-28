"""Google Flights 조회 계층.

fast-flights 는 **protobuf 쿼리 빌더로만** 쓴다. 응답 파싱은 직접 한다.

이유: fast-flights 3.1.0 의 파서는 Google 이 "가격 미제공"으로 내려보내는 항목
(k[1][0] == [])을 만나면 IndexError 로 결과 **전체**를 버린다. 실측 결과 33건 중
4건이 그런 항목이었고, 그 때문에 정상적인 29건까지 함께 날아갔다.
여기서는 그런 항목만 건너뛴다.
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import date, datetime
from typing import Iterable

from fast_flights import FlightQuery, Passengers, create_query, fetch_flights_html
from fast_flights.querying import Query
from selectolax.lexbor import LexborHTMLParser

from .config import Config
from .models import Itinerary, Segment

log = logging.getLogger(__name__)


class BlockedError(RuntimeError):
    """Google 이 결과 대신 차단/동의 페이지를 돌려준 경우."""


class ParseError(RuntimeError):
    """HTML 은 받았지만 예상한 데이터 구조를 찾지 못한 경우."""


# ── 응답 파싱 ────────────────────────────────────────────────

def _expand_time(value) -> tuple[int, int]:
    """Google 은 0인 시/분 성분을 생략하거나 None 으로 보낸다. [8] -> 08:00, [None,31] -> 00:31."""
    padded = [*(value or []), None, None]
    return (padded[0] or 0, padded[1] or 0)


def _as_datetime(date_parts, time_parts) -> datetime:
    y, m, d = (list(date_parts) + [1, 1, 1])[:3]
    hh, mm = _expand_time(time_parts)
    return datetime(int(y), int(m), int(d), int(hh), int(mm))


def _extract_payload(html: str) -> list:
    node = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if node is None:
        lowered = html[:200_000].lower()
        if "unusual traffic" in lowered or "/sorry/" in lowered:
            raise BlockedError("Google 이 비정상 트래픽으로 차단했습니다")
        if "before you continue" in lowered or "consent.google" in lowered:
            raise BlockedError("동의(consent) 페이지가 반환되었습니다")
        raise ParseError("ds:1 스크립트를 찾지 못했습니다 (레이아웃 변경 가능성)")

    text = node.text()
    if "data:" not in text:
        raise ParseError("ds:1 안에 data: 블록이 없습니다")

    raw = text.split("data:", 1)[1].rsplit(",", 1)[0]
    if raw.rstrip().endswith("errorHasStatus: true"):
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError("data: 블록 JSON 파싱 실패: {}".format(exc)) from exc


def parse_itineraries(html: str, passengers: int, deep_link: str = "") -> list[Itinerary]:
    """검색 결과 HTML을 Itinerary 목록으로. 1인당 가격 기준, 저렴한 순."""
    payload = _extract_payload(html)
    if not payload:
        return []

    # 결과는 두 묶음으로 온다 — payload[2][0] "인기 출발 항공편",
    # payload[3][0] "기타 출발 항공편". 어느 쪽이 싼지는 그때그때 다르므로
    # 반드시 둘 다 읽어야 한다.
    #
    # 2026-08-23 실측 (왕복 ICN-BCN 12/30~1/8):
    #   payload[2][0] 최저 6,722,200 / payload[3][0] 최저 7,031,600
    # [3] 만 읽던 동안 왕복 최저가를 1인 15만원씩 놓치고 있었고,
    # 그래서 알림이 실제 최저가가 아닌 항공편을 가리켰다.
    entries: list = []
    for slot in (2, 3):
        try:
            group = payload[slot][0]
        except (IndexError, TypeError):
            continue
        if isinstance(group, list):
            entries.extend(group)

    if not entries:
        return []

    out: list[Itinerary] = []
    skipped = 0
    seen: set[tuple] = set()

    for entry in entries:
        try:
            price_total = entry[1][0][1]
        except (IndexError, TypeError, KeyError):
            skipped += 1  # 가격 미제공 항목
            continue
        if not isinstance(price_total, (int, float)) or price_total <= 0:
            skipped += 1
            continue

        try:
            flight = entry[0]
            airlines = [str(a) for a in (flight[1] or [])]
            segments = [
                Segment(
                    from_airport=str(seg[3]),
                    to_airport=str(seg[6]),
                    depart=_as_datetime(seg[20], seg[8]),
                    arrive=_as_datetime(seg[21], seg[10]),
                    duration_minutes=int(seg[11] or 0),
                    plane=str(seg[17] or ""),
                )
                for seg in flight[2]
            ]
        except (IndexError, TypeError, ValueError) as exc:
            log.debug("항목 건너뜀: %s", exc)
            skipped += 1
            continue

        if not segments:
            skipped += 1
            continue

        # 같은 여정이 두 묶음에 모두 실릴 수 있다.
        fingerprint = (
            int(price_total),
            tuple((s.from_airport, s.to_airport, s.depart, s.arrive) for s in segments),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        out.append(
            Itinerary(
                price_per_person=int(round(price_total / max(passengers, 1))),
                airlines=airlines,
                segments=segments,
                deep_link=deep_link,
            )
        )

    if skipped:
        log.debug("가격/구조 불량으로 %d건 건너뜀 (정상 %d건)", skipped, len(out))

    out.sort(key=lambda it: it.price_per_person)
    return out


# ── 쿼리 생성 ────────────────────────────────────────────────

def build_query(cfg: Config, legs: Iterable[tuple[str, str, date]], trip: str) -> Query:
    """legs = [(출발공항, 도착공항, 날짜), ...]"""
    flights = [
        FlightQuery(
            date=d.isoformat(),
            from_airport=frm,
            to_airport=to,
            max_stops=cfg.max_stops,
        )
        for frm, to, d in legs
    ]
    return create_query(
        flights=flights,
        seat=cfg.cabin,
        trip=trip,
        passengers=Passengers(adults=cfg.passengers),
        language=cfg.language,
        currency=cfg.currency,
        hide_separate_and_self_transfer=cfg.hide_separate_and_self_transfer,
    )


def deep_link_for(cfg: Config, legs: list[tuple[str, str, date]], trip: str) -> str:
    """예약 화면으로 바로 가는 Google Flights 링크."""
    return build_query(cfg, legs, trip).url()


# ── 페치 (재시도 + 지터) ──────────────────────────────────────

class Fetcher:
    """요청 간 지터를 넣고 실패율을 집계한다."""

    def __init__(self, cfg: Config, proxy: str | None = None):
        self.cfg = cfg
        self.proxy = proxy
        self.attempts = 0
        self.failures = 0
        self.blocked = 0
        self.empty_recovered = 0
        """재시도로 살려낸 빈 응답 수."""
        self.empty_final = 0
        """재시도해도 끝내 0건이었던 수 (진짜 노선이 없는 경우)."""
        self._last_request_at = 0.0

    @property
    def fail_rate(self) -> float:
        return self.failures / self.attempts if self.attempts else 0.0

    def _sleep_jitter(self) -> None:
        low, high = self.cfg.jitter
        elapsed = time.monotonic() - self._last_request_at
        wait = random.uniform(low, high) - elapsed
        if wait > 0:
            time.sleep(wait)

    def search(self, legs: list[tuple[str, str, date]], trip: str) -> list[Itinerary]:
        """실패하면 예외 대신 빈 목록을 돌려주고 통계에만 반영한다.

        빈 결과도 재시도한다. Google 은 오류가 아닌 정상 응답 안에 결과를 0건으로
        담아 보낼 때가 있는데, 그러면 그 구간이 걸린 조합이 해당 스윕에서 통째로
        사라진다. 실측(2026-08-23)에서 0건이 나왔던 9개 구간을 다시 조회하니
        전부 3~12건을 반환했다 — 지속적으로 빈 구간은 하나도 없었다.
        """
        query = build_query(self.cfg, legs, trip)
        link = query.url()
        self.attempts += 1
        empty_tries = 0

        while True:
            for attempt in range(1, self.cfg.max_retries + 1):
                self._sleep_jitter()
                self._last_request_at = time.monotonic()
                try:
                    html = fetch_flights_html(query, proxy=self.proxy)
                    found = parse_itineraries(html, self.cfg.passengers, link)
                    break
                except BlockedError as exc:
                    self.blocked += 1
                    self.failures += 1
                    log.warning("차단 감지 (%s): %s", legs, exc)
                    return []  # 차단은 재시도해도 소용없다
                except Exception as exc:  # noqa: BLE001 - 어떤 실패든 스윕은 계속돼야 한다
                    if attempt == self.cfg.max_retries:
                        log.warning("조회 실패 %s (%d회 시도): %s", legs, attempt, exc)
                        self.failures += 1
                        return []
                    backoff = 2 ** attempt + random.uniform(0, 1.5)
                    log.debug(
                        "재시도 %d/%d, %.1fs 대기: %s",
                        attempt, self.cfg.max_retries, backoff, exc,
                    )
                    time.sleep(backoff)

            if found:
                if empty_tries:
                    self.empty_recovered += 1
                    log.info("빈 응답을 %d회 재시도해 %d건 회복: %s", empty_tries, len(found), legs)
                return found

            if empty_tries >= self.cfg.empty_retries:
                if empty_tries:
                    self.empty_final += 1
                return []

            empty_tries += 1
            time.sleep(random.uniform(2.0, 4.0))

    # 편의 래퍼 ------------------------------------------------
    def one_way(self, frm: str, to: str, when: date) -> list[Itinerary]:
        return self.search([(frm, to, when)], "one-way")

    def round_trip(self, origin: str, city: str, out: date, back: date) -> list[Itinerary]:
        return self.search([(origin, city, out), (city, origin, back)], "round-trip")
