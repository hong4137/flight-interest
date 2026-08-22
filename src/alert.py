"""알림 규칙과 텔레그램 발송.

알림은 두 경우에만 나간다.
  1. 1인당 가격이 목표 임계값 이하  → 즉시 (쿨다운을 무시하되, 더 싸졌을 때만 재발송)
  2. 해당 노선 역대 최저가를 일정 % 이상 갱신 → 참고용

그 외에는 조용히 이력만 쌓는다. 알림이 흔해지면 아무도 안 보게 된다.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

import requests

from .config import Config
from .gflights import deep_link_for
from .models import Deal
from .store import Store, read_history

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

AlertReason = Literal["threshold", "new_low"]


@dataclass
class AlertDecision:
    send: bool
    reason: AlertReason | None = None
    previous_best: int | None = None


def split_route_key(route_key: str) -> tuple[str, str, str, str] | None:
    """'FCO|BCN|2027-01-03|2027-01-17' 을 되쪼갠다.

    state.json 은 코드 변경을 넘어 살아남으므로, 옛 형식이나 손상된 키를 만나도
    다이제스트 전체가 죽지 않도록 None 을 돌려준다.
    """
    parts = route_key.split("|")
    if len(parts) != 4:
        return None
    return (parts[0], parts[1], parts[2], parts[3])


def route_label(origin: str, entry: str, exit_city: str) -> str:
    """FCO→FCO 는 읽기 나쁘다. 왕복이면 ICN↔FCO, 오픈조면 양쪽을 다 보여준다."""
    if entry == exit_city:
        return "{}↔{}".format(origin, entry)
    return "{o}→{e} / {x}→{o}".format(o=origin, e=entry, x=exit_city)


def booking_link(cfg: Config, entry: str, exit_city: str, out_d: date, in_d: date) -> str:
    """예약 화면 링크를 그때그때 만든다. 상태에 저장하지 않으므로 옛 기록에도 붙는다."""
    legs = [
        (cfg.origin, entry, out_d),
        (exit_city, cfg.origin, in_d),
    ]
    trip = "round-trip" if entry == exit_city else "multi-city"
    return deep_link_for(cfg, legs, trip)


def md(value) -> str:
    """1/3 형태의 짧은 날짜. strftime("%-m/%-d") 는 Windows 에서 동작하지 않는다."""
    return "{}/{}".format(value.month, value.day)


def mdhm(value) -> str:
    return "{}/{} {:02d}:{:02d}".format(value.month, value.day, value.hour, value.minute)


def won(value: int) -> str:
    """2875000 -> '287.5만원'"""
    man = value / 10_000
    if man >= 100:
        return "{:,.0f}만원".format(man)
    return "{:,.1f}만원".format(man)


def evaluate(deal: Deal, store: Store, cfg: Config) -> AlertDecision:
    """이 딜을 알릴지 판단한다. 상태를 갱신하는 부수효과가 있다."""
    improved, previous = store.record_price(deal)

    if deal.price_per_person <= cfg.threshold_pp:
        # 임계값 이하는 무조건 알린다. 단 같은 지문으로 이미 보냈고 더 싸지지도
        # 않았다면 침묵한다.
        cheaper_than_last = previous is None or deal.price_per_person < previous
        if store.should_alert(deal, cfg.cooldown_hours) or cheaper_than_last:
            return AlertDecision(True, "threshold", previous)
        return AlertDecision(False, None, previous)

    if improved and previous is not None:
        drop_pct = (previous - deal.price_per_person) / previous * 100
        if drop_pct >= cfg.new_low_drop_pct and store.should_alert(deal, cfg.cooldown_hours):
            return AlertDecision(True, "new_low", previous)

    return AlertDecision(False, None, previous)


# ── 메시지 조립 ──────────────────────────────────────────────

def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


# alert-test 가 쓰는 값. 진짜 딜과 헷갈리면 나중에 실제 특가를 무시하게 된다.
TEST_SOURCE = "테스트"


def format_deal(deal: Deal, cfg: Config, decision: AlertDecision) -> str:
    is_test = deal.source == TEST_SOURCE
    if is_test:
        head = "🧪 <b>[테스트] 실제 특가가 아닙니다</b>"
    elif decision.reason == "threshold":
        head = "🔥 <b>목표가 달성</b>"
    else:
        head = "📉 <b>역대 최저 갱신</b>"
    total = deal.price_per_person * cfg.passengers

    route = (
        "{o}→{e} · {x}→{o}".format(o=cfg.origin, e=deal.entry, x=deal.exit)
        if deal.kind == "open-jaw"
        else "{o}↔{e}".format(o=cfg.origin, e=deal.entry)
    )

    lines = [
        head,
        "",
        "<b>{pp}</b> / 1인  (총 {tot}, {n}명)".format(
            pp=won(deal.price_per_person), tot=won(total), n=cfg.passengers
        ),
        "<code>{:,}원</code> / 1인".format(deal.price_per_person),
        "",
        "{} {}".format(_esc(route), "(오픈조)" if deal.kind == "open-jaw" else ""),
        "{} 출발 · {} 귀국편".format(md(deal.outbound_date), md(deal.inbound_date)),
    ]
    if deal.korea_arrival:
        lines.append("인천 도착 {}".format(mdhm(deal.korea_arrival)))
    lines.append(
        "{} · 경유 {}회".format(_esc(", ".join(deal.airlines) or "?"), deal.stops)
    )

    if decision.previous_best:
        diff = decision.previous_best - deal.price_per_person
        if diff > 0:
            pct = diff / decision.previous_best * 100
            lines.append(
                "직전 최저 {} 대비 <b>-{:.0f}%</b>".format(won(decision.previous_best), pct)
            )

    if deal.price_per_person > cfg.threshold_pp:
        gap = deal.price_per_person - cfg.threshold_pp
        lines.append("목표({})까지 {} 남음".format(won(cfg.threshold_pp), won(gap)))

    if deal.deep_link:
        lines += ["", '<a href="{}">▶ Google Flights 에서 열기</a>'.format(_esc(deal.deep_link))]

    if is_test:
        lines += [
            "",
            "<i>알림 서식과 발송 경로를 점검하는 가짜 데이터입니다. "
            "위 가격·항공사·날짜는 실제 조회 결과가 아닙니다.</i>",
        ]
    else:
        lines.append("<i>출처: {}</i>".format(deal.source))
    return "\n".join(lines)


def format_digest(cfg: Config, store: Store) -> str:
    rows = read_history(days=7)
    best_map = store.state.get("best_by_route", {})

    ranked = sorted(best_map.items(), key=lambda kv: kv[1]["price"])[: cfg.digest_top_n]

    lines = ["📊 <b>일일 요약</b> · {}".format(md(datetime.now())), ""]

    if not ranked:
        lines.append("아직 확인된 왕복/오픈조 가격이 없습니다.")
    else:
        lines.append("<b>현재 최저가 TOP {}</b> (1인)".format(len(ranked)))
        shown = 0
        for route_key, info in ranked:
            parsed = split_route_key(route_key)
            if parsed is None:
                continue  # 옛 형식/손상된 키는 조용히 건너뛴다
            entry, exit_city, out_d, in_d = parsed
            shown += 1

            try:
                link = booking_link(
                    cfg, entry, exit_city, date.fromisoformat(out_d), date.fromisoformat(in_d)
                )
            except ValueError:
                link = ""

            tag = " · 오픈조" if entry != exit_city else ""
            lines.append(
                "{}. <b>{}</b> · {} · {} → {}{}".format(
                    shown,
                    won(info["price"]),
                    _esc(route_label(cfg.origin, entry, exit_city)),
                    out_d[5:], in_d[5:], tag,
                )
            )

            detail = _esc(info.get("airlines") or "?")
            stops = info.get("stops")
            if stops is not None:
                detail += " · 경유 {}회".format(stops)
            if link:
                detail += ' · <a href="{}">열기</a>'.format(_esc(link))
            lines.append("    {}".format(detail))

    gb = store.state.get("global_best")
    if gb:
        gap = gb["price"] - cfg.threshold_pp
        if gap <= 0:
            lines += ["", "🎯 목표 {} 달성 상태".format(won(cfg.threshold_pp))]
        else:
            pct = gap / cfg.threshold_pp * 100
            lines += [
                "",
                "목표 {} 까지 <b>{}</b> ({:.0f}%) 남음".format(
                    won(cfg.threshold_pp), won(gap), pct
                ),
            ]

    if rows:
        prices = [r["price_per_person"] for r in rows]
        lines += [
            "",
            "<b>최근 7일</b> · 관측 {}건 · 최저 {} · 중앙값 {}".format(
                len(prices), won(min(prices)), won(sorted(prices)[len(prices) // 2])
            ),
        ]

    labels = {"round-trip": "왕복", "open-jaw": "오픈조"}
    parts = []
    for kind, ratio, n in store.calibration_summary():
        shown = ratio if ratio else cfg.ratio_for(kind == "open-jaw")
        parts.append("{} {:.2f}(표본 {})".format(labels[kind], shown, n))
    lines.append("편도합산 환산비 — " + " · ".join(parts))

    month_left, day_left = store.serp_budget(cfg.serpapi_monthly_cap, cfg.serpapi_daily_cap)
    if cfg.serpapi_enabled:
        lines.append("SerpApi 잔여 {}콜/월".format(max(month_left, 0)))
        if month_left <= cfg.serpapi_monthly_cap * cfg.serpapi_warn_pct / 100:
            lines.append("⚠️ SerpApi 크레딧이 얼마 남지 않았습니다 (오픈조 확인 제한)")
    else:
        lines.append("<i>SerpApi 미설정 — 오픈조 실가 확인은 건너뜁니다</i>")

    last = store.state.get("last_sweep")
    if last:
        lines.append(
            "<i>마지막 스윕 {} · {} · 조회 {}건 · 실패율 {:.0%}</i>".format(
                last.get("at", "?")[5:16], last.get("mode", "?"),
                last.get("queries", 0), last.get("fail_rate", 0.0),
            )
        )
    return "\n".join(lines)


# ── 발송 ─────────────────────────────────────────────────────

def send(cfg: Config, text: str, *, silent: bool = False) -> bool:
    if not cfg.telegram_enabled:
        log.warning("텔레그램 미설정 — 아래 메시지를 보내지 못했습니다:\n%s", text)
        return False
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=cfg.telegram_token),
            json={
                "chat_id": cfg.telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silent,
            },
            timeout=cfg.timeout,
        )
        if resp.status_code != 200:
            log.error("텔레그램 발송 실패 %s: %s", resp.status_code, resp.text[:300])
            return False
        return True
    except requests.RequestException as exc:
        log.error("텔레그램 발송 중 네트워크 오류: %s", exc)
        return False


def notify_deal(cfg: Config, store: Store, deal: Deal, decision: AlertDecision) -> bool:
    ok = send(cfg, format_deal(deal, cfg, decision))
    if ok:
        store.mark_alerted(deal)
    return ok


def notify_health(cfg: Config, message: str) -> bool:
    return send(cfg, "⚠️ <b>감시 시스템 경고</b>\n\n{}".format(_esc(message)), silent=True)
