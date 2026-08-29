"""스윕 오케스트레이션.

full 모드
  1. 편도를 양방향으로 훑는다 (인천→입국도시, 출국도시→인천).
     11개 도시 × 5일 + 11개 도시 × 7일 = 132회 조회로
     11 × 11 × 5 × 7 = 4,235개 조합의 가격 상한을 얻는다.
  2. 편도 합산 × 환산비로 왕복가를 추정하고, 임계값 근처만 골라
  3. 실제 가격을 확인한다 (같은 도시 왕복은 무료, 오픈조는 SerpApi).

hot 모드
  직전 full 스윕이 남긴 유망 조합만 매시간 재확인한다. 12회 안팎.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from .alert import evaluate, notify_deal, notify_health
from .config import Config
from .gflights import Fetcher, deep_link_for
from .models import Candidate, Deal, Itinerary
from .providers.serpapi import BudgetExhausted, SerpApiClient
from .store import Store, append_history

log = logging.getLogger(__name__)


@dataclass
class SweepResult:
    mode: str
    queries: int = 0
    failures: int = 0
    fail_rate: float = 0.0
    candidates: int = 0
    confirmed: list[Deal] = field(default_factory=list)
    alerted: int = 0
    serp_used: int = 0
    skipped_open_jaw: int = 0
    empty_recovered: int = 0
    empty_final: int = 0
    cheapest: Deal | None = None

    def summary(self) -> str:
        parts = [
            "{} 스윕".format(self.mode),
            "조회 {}건".format(self.queries),
            "실패 {}건 ({:.0%})".format(self.failures, self.fail_rate),
            "후보 {}개".format(self.candidates),
            "확인 {}건".format(len(self.confirmed)),
            "알림 {}건".format(self.alerted),
        ]
        if self.serp_used:
            parts.append("SerpApi {}콜".format(self.serp_used))
        if self.empty_recovered:
            parts.append("빈응답 회복 {}건".format(self.empty_recovered))
        if self.empty_final:
            parts.append("결과없음 {}건".format(self.empty_final))
        if self.skipped_open_jaw:
            parts.append("오픈조 미확인 {}개".format(self.skipped_open_jaw))
        if self.cheapest:
            parts.append("최저 {:,}원/인".format(self.cheapest.price_per_person))
        return " · ".join(parts)


# ── 1단계: 편도 스크리닝 ─────────────────────────────────────

def _scan_one_ways(
    cfg: Config, fetcher: Fetcher
) -> tuple[dict[tuple[str, date], Itinerary], dict[tuple[str, date], Itinerary]]:
    """(출국편 최저, 귀국편 최저). 키는 (공항, 해당 구간 출발일)."""
    outbound: dict[tuple[str, date], Itinerary] = {}
    inbound: dict[tuple[str, date], Itinerary] = {}

    total = len(cfg.entry_airports) * len(cfg.outbound_dates)
    done = 0
    for city in cfg.entry_airports:
        for when in cfg.outbound_dates:
            done += 1
            results = fetcher.one_way(cfg.origin, city, when)
            if results:
                outbound[(city, when)] = results[0]
                log.info(
                    "[%d/%d] %s→%s %s  최저 %s원/인",
                    done, total, cfg.origin, city, when, format(results[0].price_per_person, ","),
                )
            else:
                log.info("[%d/%d] %s→%s %s  결과 없음", done, total, cfg.origin, city, when)

    arrive_start, arrive_end = cfg.arrive_korea
    total = len(cfg.exit_airports) * len(cfg.return_search_dates)
    done = 0
    for city in cfg.exit_airports:
        for when in cfg.return_search_dates:
            done += 1
            results = fetcher.one_way(city, cfg.origin, when)
            # 인천 도착일이 희망 창에 드는 것만 채택한다. 유럽 출발일 기준으로
            # 조회했으므로 하루 밀리는 경우가 흔하다.
            valid = [
                it for it in results if arrive_start <= it.arrive_at.date() <= arrive_end
            ]
            if valid:
                inbound[(city, when)] = valid[0]
                log.info(
                    "[%d/%d] %s→%s %s  최저 %s원/인 (인천 %s 도착)",
                    done, total, city, cfg.origin, when,
                    format(valid[0].price_per_person, ","), valid[0].arrive_at.date(),
                )
            else:
                log.info(
                    "[%d/%d] %s→%s %s  도착창 밖 (%d건 중 0건 채택)",
                    done, total, city, cfg.origin, when, len(results),
                )

    return outbound, inbound


def _same_city_targets(
    cfg: Config,
    store: Store,
    outbound: dict[tuple[str, date], Itinerary],
    inbound: dict[tuple[str, date], Itinerary],
) -> list[Candidate]:
    """같은 도시 왕복은 **추정하지 않고 전부 직접 조회**한다.

    편도 합산은 왕복가를 예측하는 힘이 약하다. 실측 환산비가 0.563~0.941 로
    1.67배 벌어져, 추정으로 거르면 실제로 싼 조합이 탈락했다 (전수 감사에서
    80개 중 10개 이상). 조합 수가 `도시 × 출발일 × 귀국일` 이라 감당 가능하다.

    다만 도시를 늘리면 조회량이 폭발하므로 상한을 둔다. 상한을 넘으면 유망한
    것부터 채우고 나머지는 스윕마다 순환하며 훑어, 몇 번에 걸쳐 전체를 덮는다.
    """
    ratio = store.calibrated_ratio(cfg.ow_to_rt_ratio, "round-trip")
    targets: list[Candidate] = []

    for (city, out_date), ob in outbound.items():
        for (exit_city, in_date), ib in inbound.items():
            if exit_city != city:
                continue
            targets.append(
                Candidate(
                    entry=city,
                    exit=city,
                    outbound_date=out_date,
                    inbound_date=in_date,
                    # 순위용 참고값일 뿐, 거르는 데는 쓰지 않는다.
                    estimated_pp=int(round((ob.price_per_person + ib.price_per_person) * ratio)),
                    outbound=ob,
                    inbound=ib,
                )
            )

    targets.sort(key=lambda c: c.estimated_pp)
    cap = cfg.direct_roundtrip_cap
    if len(targets) <= cap:
        log.info("같은 도시 왕복 %d개 전수 조회 (상한 %d)", len(targets), cap)
        return targets

    # 상한 초과 — 상위 60% 는 늘 보고, 나머지 40% 는 순환한다.
    head = int(cap * 0.6)
    tail_slots = cap - head
    rest = targets[head:]
    start = store.rotation_offset("same_city", len(rest), tail_slots)
    rotated = [rest[(start + i) % len(rest)] for i in range(tail_slots)]
    log.info(
        "같은 도시 왕복 %d개 중 %d개 조회 (상위 %d + 순환 %d, 순환 위치 %d)",
        len(targets), cap, head, tail_slots, start,
    )
    return targets[:head] + rotated


def _open_jaw_candidates(
    cfg: Config,
    store: Store,
    outbound: dict[tuple[str, date], Itinerary],
    inbound: dict[tuple[str, date], Itinerary],
) -> list[Candidate]:
    """오픈조는 조합 수가 커서(도시² × 날짜²) 여전히 추정으로 좁힌다.

    다만 중앙값 대신 낮은 백분위를 써서, 유난히 싼 조합이 과대 추정으로
    탈락하는 일을 줄인다. 확인 예산은 SerpApi 크레딧으로 어차피 막혀 있다.
    """
    if not cfg.allow_open_jaw:
        return []

    ratio = store.screening_ratio(
        cfg.ow_to_openjaw_ratio, "open-jaw", cfg.screening_percentile
    )
    trigger = cfg.threshold_trigger_pp()
    log.info("오픈조 스크리닝 환산비 %.3f (하위 %d%%), 임계 %s원",
             ratio, cfg.screening_percentile, format(trigger, ","))

    out: list[Candidate] = []
    for (entry, out_date), ob in outbound.items():
        for (exit_city, in_date), ib in inbound.items():
            if exit_city == entry:
                continue
            estimate = int(round((ob.price_per_person + ib.price_per_person) * ratio))
            if estimate > trigger:
                continue
            out.append(
                Candidate(
                    entry=entry,
                    exit=exit_city,
                    outbound_date=out_date,
                    inbound_date=in_date,
                    estimated_pp=estimate,
                    outbound=ob,
                    inbound=ib,
                )
            )
    out.sort(key=lambda c: c.estimated_pp)
    return out


# ── 2단계: 실가 확인 ─────────────────────────────────────────

def _confirm(
    cfg: Config,
    store: Store,
    fetcher: Fetcher,
    serp: SerpApiClient,
    cand: Candidate,
    result: SweepResult,
) -> Deal | None:
    """후보 하나의 실제 가격을 확인한다."""
    korea_arrival = cand.inbound.arrive_at if cand.inbound else None
    inbound_stops = cand.inbound.stops if cand.inbound else 0

    if not cand.is_open_jaw:
        legs = [
            (cfg.origin, cand.entry, cand.outbound_date),
            (cand.entry, cfg.origin, cand.inbound_date),
        ]
        found = fetcher.round_trip(
            cfg.origin, cand.entry, cand.outbound_date, cand.inbound_date
        )
        if not found:
            return None
        best = found[0]
        # 편도합산 대비 실제 왕복가로 환산비를 보정한다.
        if cand.outbound and cand.inbound:
            store.add_calibration(
                cand.outbound.price_per_person + cand.inbound.price_per_person,
                best.price_per_person,
                "round-trip",
            )
        return Deal(
            entry=cand.entry,
            exit=cand.exit,
            outbound_date=cand.outbound_date,
            inbound_date=cand.inbound_date,
            price_per_person=best.price_per_person,
            airlines=best.airlines,
            # 왕복 조회 결과의 구간은 가는 편만 담겨 있다. 오는 편 경유 수는
            # 편도 조회에서 가져와 둘 중 큰 값을 쓴다.
            stops=max(best.stops, inbound_stops),
            deep_link=best.deep_link or deep_link_for(cfg, legs, "round-trip"),
            source=best.source,
            kind="round-trip",
            korea_arrival=korea_arrival,
            total_minutes=best.total_minutes,
            outbound_depart=best.depart_at,
            outbound_arrive=best.arrive_at,
            outbound_via=best.via(),
        )

    # 오픈조 — fast-flights 로는 불가능하므로 SerpApi 를 쓴다.
    if not serp.enabled:
        result.skipped_open_jaw += 1
        return None
    link = deep_link_for(
        cfg,
        [
            (cfg.origin, cand.entry, cand.outbound_date),
            (cand.exit, cfg.origin, cand.inbound_date),
        ],
        "multi-city",
    )
    try:
        found = serp.open_jaw(
            cand.entry, cand.exit, cand.outbound_date, cand.inbound_date, link
        )
        result.serp_used += 1
    except BudgetExhausted as exc:
        log.info("오픈조 확인 건너뜀: %s", exc)
        result.skipped_open_jaw += 1
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("SerpApi 오픈조 조회 실패 (%s→%s): %s", cand.entry, cand.exit, exc)
        result.skipped_open_jaw += 1
        return None

    if not found:
        return None
    best = found[0]
    # 오픈조 환산비도 같은 방식으로 보정한다. 크레딧이 비싸 표본이 느리게 쌓인다.
    if cand.outbound and cand.inbound:
        store.add_calibration(
            cand.outbound.price_per_person + cand.inbound.price_per_person,
            best.price_per_person,
            "open-jaw",
        )
    return Deal(
        entry=cand.entry,
        exit=cand.exit,
        outbound_date=cand.outbound_date,
        inbound_date=cand.inbound_date,
        price_per_person=best.price_per_person,
        airlines=best.airlines,
        stops=max(best.stops, inbound_stops),
        deep_link=best.deep_link or link,
        source="serpapi",
        kind="open-jaw",
        korea_arrival=korea_arrival,
        total_minutes=best.total_minutes,
        outbound_depart=best.depart_at,
        outbound_arrive=best.arrive_at,
        outbound_via=best.via(),
    )


def _process(
    cfg: Config,
    store: Store,
    candidates: list[Candidate],
    fetcher: Fetcher,
    serp: SerpApiClient,
    result: SweepResult,
    *,
    dry_run: bool,
) -> None:
    for cand in candidates:
        deal = _confirm(cfg, store, fetcher, serp, cand, result)
        if deal is None:
            continue
        result.confirmed.append(deal)
        log.info(
            "확인: %s %s→%s  %s 출발 / %s 귀국  %s원/인 (추정 %s)",
            deal.kind, deal.entry, deal.exit,
            deal.outbound_date, deal.inbound_date,
            format(deal.price_per_person, ","), format(cand.estimated_pp, ","),
        )
        decision = evaluate(deal, store, cfg)
        if decision.send:
            if dry_run:
                log.info("[dry-run] 알림 생략: %s", deal.route_key)
            elif notify_deal(cfg, store, deal, decision):
                result.alerted += 1

    if result.confirmed:
        result.cheapest = min(result.confirmed, key=lambda d: d.price_per_person)


# ── 진입점 ───────────────────────────────────────────────────

def run_sweep(
    cfg: Config, store: Store, mode: str = "full", *, dry_run: bool = False
) -> SweepResult:
    result = SweepResult(mode=mode)
    fetcher = Fetcher(cfg)
    serp = SerpApiClient(cfg, store)

    # 조건이 바뀌었으면 이제 못 사는 조합의 기록을 먼저 버린다.
    stale = store.prune_stale(cfg)
    if stale:
        log.info("조건에 맞지 않는 옛 기록 %d건을 정리했습니다", stale)

    if mode == "full":
        outbound, inbound = _scan_one_ways(cfg, fetcher)
        log.info(
            "편도 스크리닝 완료: 출국 %d개, 귀국 %d개 (도착창 통과)",
            len(outbound), len(inbound),
        )
        # 같은 도시 왕복은 무료(fast-flights)이므로 추정 없이 직접 조회한다.
        same_city = _same_city_targets(cfg, store, outbound, inbound)

        # 오픈조는 SerpApi 크레딧을 쓰므로 추정으로 좁히고 예산만큼만 확인한다.
        open_jaw = _open_jaw_candidates(cfg, store, outbound, inbound)
        open_jaw = _select_for_confirmation(
            cfg, open_jaw, serp, result, cfg.full_confirm_count
        )

        result.candidates = len(same_city) + len(open_jaw)
        log.info("확인 대상 — 같은 도시 %d개 · 오픈조 %d개", len(same_city), len(open_jaw))

        selected = same_city + open_jaw
        _process(cfg, store, selected, fetcher, serp, result, dry_run=dry_run)
        store.set_hot(_hot_list(cfg, selected, result))

    elif mode == "hot":
        candidates = _hot_candidates(cfg, store)
        result.candidates = len(candidates)
        if not candidates:
            log.info("hot 목록이 비어 있습니다. full 스윕을 먼저 돌리세요.")
        limit = cfg.hot_roundtrip_count + cfg.hot_openjaw_count
        selected = _select_for_confirmation(cfg, candidates, serp, result, limit)
        _process(cfg, store, selected, fetcher, serp, result, dry_run=dry_run)

    else:
        raise ValueError("알 수 없는 모드: {}".format(mode))

    result.queries = fetcher.attempts
    result.failures = fetcher.failures
    result.fail_rate = fetcher.fail_rate
    result.empty_recovered = fetcher.empty_recovered
    result.empty_final = fetcher.empty_final

    store.prune_alerts()
    store.record_config(cfg)
    store.touch_sweep(
        mode,
        {
            "queries": result.queries,
            "failures": result.failures,
            "fail_rate": round(result.fail_rate, 3),
            "candidates": result.candidates,
            "confirmed": len(result.confirmed),
            "empty_recovered": result.empty_recovered,
            "empty_final": result.empty_final,
            "cheapest": result.cheapest.price_per_person if result.cheapest else None,
        },
    )

    if not dry_run:
        append_history(result.confirmed)
        _health_check(cfg, fetcher, result)

    return result


def _select_for_confirmation(
    cfg: Config,
    candidates: list[Candidate],
    serp: SerpApiClient,
    result: SweepResult,
    limit: int,
) -> list[Candidate]:
    """확인 슬롯을 실제로 가격을 알아낼 수 있는 후보에만 배분한다.

    오픈조는 SerpApi 로만 확인되고 크레딧은 무료 플랜 기준 월 250콜뿐이다.
    키가 없거나 일일 한도를 넘긴 상태에서 오픈조를 상위에 두면, 확인 슬롯이
    전부 "확인 불가"로 소진되고 정작 무료로 알 수 있는 같은 도시 왕복은
    한 건도 못 본다. 그래서 오픈조 배정은 남은 크레딧만큼만 허용한다.
    """
    if serp.enabled:
        month_left, day_left = serp.remaining()
        open_jaw_budget = max(0, min(month_left, day_left, cfg.serpapi_per_sweep_cap))
    else:
        open_jaw_budget = 0

    selected: list[Candidate] = []
    open_jaw_used = 0

    for cand in candidates:
        if len(selected) >= limit:
            break
        if cand.is_open_jaw:
            if open_jaw_used >= open_jaw_budget:
                result.skipped_open_jaw += 1
                continue
            open_jaw_used += 1
        selected.append(cand)

    if open_jaw_budget == 0 and result.skipped_open_jaw:
        log.info(
            "오픈조 후보 %d개는 확인할 수단이 없어 건너뜁니다 (SERPAPI_KEY 미설정 또는 크레딧 소진)",
            result.skipped_open_jaw,
        )
    return selected


def _health_check(cfg: Config, fetcher: Fetcher, result: SweepResult) -> None:
    if fetcher.attempts < 10:
        return
    if fetcher.blocked:
        notify_health(
            cfg,
            "Google Flights 가 요청을 차단했습니다 ({}건). 스크레이핑 축이 막혔을 수 있습니다.\n"
            "SerpApi 유료 전환을 검토하세요.".format(fetcher.blocked),
        )
    elif fetcher.fail_rate >= cfg.fail_rate_alert_threshold:
        notify_health(
            cfg,
            "조회 실패율이 {:.0%} 입니다 ({}/{}). 파서가 깨졌거나 네트워크 문제일 수 있습니다.".format(
                fetcher.fail_rate, fetcher.failures, fetcher.attempts
            ),
        )


def _hot_list(cfg: Config, selected: list[Candidate], result: SweepResult) -> list[dict]:
    """다음 시간당 스윕이 재확인할 조합. 실제 확인된 것을 우선한다."""
    ranked = sorted(result.confirmed, key=lambda d: d.price_per_person)
    hot: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(entry: str, exit_city: str, out_d, in_d, price: int) -> bool:
        """중복이면 False. 조합 자체로 판별한다 — 가격은 확인 여부에 따라 달라지므로
        dict 통째로 비교하면 같은 조합이 두 번 들어간다."""
        ident = (entry, exit_city, out_d.isoformat(), in_d.isoformat())
        if ident in seen:
            return False
        seen.add(ident)
        hot.append(
            {
                "entry": entry,
                "exit": exit_city,
                "outbound_date": ident[2],
                "inbound_date": ident[3],
                "estimated_pp": price,
            }
        )
        return True

    n_rt = n_oj = 0
    for deal in ranked:
        is_oj = deal.entry != deal.exit
        if is_oj and n_oj >= cfg.hot_openjaw_count:
            continue
        if not is_oj and n_rt >= cfg.hot_roundtrip_count:
            continue
        if add(deal.entry, deal.exit, deal.outbound_date, deal.inbound_date,
               deal.price_per_person):
            n_oj += is_oj
            n_rt += not is_oj

    # 확인된 게 부족하면 추정 상위 후보로 채운다.
    limit = cfg.hot_roundtrip_count + cfg.hot_openjaw_count
    for cand in selected:
        if len(hot) >= limit:
            break
        add(cand.entry, cand.exit, cand.outbound_date, cand.inbound_date, cand.estimated_pp)
    return hot


def _hot_candidates(cfg: Config, store: Store) -> list[Candidate]:
    out: list[Candidate] = []
    for item in store.get_hot():
        try:
            out.append(
                Candidate(
                    entry=item["entry"],
                    exit=item["exit"],
                    outbound_date=date.fromisoformat(item["outbound_date"]),
                    inbound_date=date.fromisoformat(item["inbound_date"]),
                    estimated_pp=int(item.get("estimated_pp", 0)),
                )
            )
        except (KeyError, ValueError) as exc:
            log.debug("hot 항목 무시: %s (%s)", item, exc)
    return out
