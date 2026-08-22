"""네트워크 없이 도는 회귀 테스트.

이 시스템은 4개월간 무인으로 돈다. 파싱과 알림 판단이 조용히 망가지면
"특가가 안 뜬 것"과 구분이 안 되므로, 순수 로직만이라도 빠르게 검증한다.

    ./.venv/Scripts/python.exe -m pytest tests/ -q
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from src.alert import evaluate, format_deal, format_digest, md, won
from src.config import load_config
from src.gflights import parse_itineraries
from src.models import Deal, Itinerary, Segment
from src.store import Store


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def store(tmp_path, monkeypatch):
    import src.store as store_mod

    monkeypatch.setattr(store_mod, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(store_mod, "HISTORY_PATH", tmp_path / "history.csv")
    monkeypatch.setattr(store_mod, "DATA_DIR", tmp_path)
    return Store.load()


def make_deal(price: int, *, entry="FCO", exit_city="FCO", airlines=("LH",)) -> Deal:
    return Deal(
        entry=entry,
        exit=exit_city,
        outbound_date=date(2027, 1, 3),
        inbound_date=date(2027, 1, 17),
        price_per_person=price,
        airlines=list(airlines),
        stops=1,
        deep_link="https://example.test",
        source="test",
        kind="open-jaw" if entry != exit_city else "round-trip",
    )


# ── 파서 ─────────────────────────────────────────────────────

def _payload(entries: list) -> str:
    """ds:1 스크립트를 흉내낸 최소 HTML."""
    doc = [None, None, None, [entries], None, None, None, [None, [[], []]]]
    return (
        '<html><body><script class="ds:1">AF_initDataCallback({key: 1, data:'
        + json.dumps(doc)
        + ", sideChannel: {}});</script></body></html>"
    )


def _entry(price, dep=(2027, 1, 3), dep_t=(23, 0), arr=(2027, 1, 4), arr_t=(17, 55)):
    seg = [None] * 23
    seg[3], seg[4] = "ICN", "인천"
    seg[5], seg[6] = "피우미치노", "FCO"
    seg[8], seg[10] = list(dep_t), list(arr_t)
    seg[11] = 780
    seg[17] = "Boeing 777"
    seg[20], seg[21] = list(dep), list(arr)
    flight = [None] * 23
    flight[0] = "1"
    flight[1] = ["루프트한자"]
    flight[2] = [seg]
    flight[22] = [None] * 9
    price_block = [[None, price], "token"] if price is not None else [[], "token"]
    return [flight, price_block] + [None] * 9


def test_가격없는_항목은_건너뛰고_나머지는_살린다():
    """fast-flights 3.1.0 이 여기서 IndexError 로 결과 전체를 버린다."""
    html = _payload([_entry(1000), _entry(None), _entry(800), _entry(None)])
    got = parse_itineraries(html, passengers=1)
    assert [it.price_per_person for it in got] == [800, 1000]


def test_가격은_인원수로_나눠_1인당으로_정규화된다():
    """Google 표시가는 전체 인원 합계다. 2명이면 절반이 1인가."""
    html = _payload([_entry(5_000_000)])
    assert parse_itineraries(html, passengers=2)[0].price_per_person == 2_500_000


def test_시간성분_생략을_복원한다():
    # [8] -> 08:00, [None, 31] -> 00:31
    html = _payload([_entry(1000, dep_t=(8,), arr_t=(None, 31))])
    it = parse_itineraries(html, passengers=1)[0]
    assert (it.depart_at.hour, it.depart_at.minute) == (8, 0)
    assert (it.arrive_at.hour, it.arrive_at.minute) == (0, 31)


def test_결과가_없으면_빈_목록():
    assert parse_itineraries(_payload([]), passengers=1) == []


# ── 알림 판단 ────────────────────────────────────────────────

def test_임계값_이하면_알린다(cfg, store):
    decision = evaluate(make_deal(cfg.threshold_pp - 1), store, cfg)
    assert decision.send and decision.reason == "threshold"


def test_임계값_초과_최초관측은_알리지_않는다(cfg, store):
    assert not evaluate(make_deal(cfg.threshold_pp + 500_000), store, cfg).send


def test_충분히_떨어진_최저가_갱신은_알린다(cfg, store):
    base = cfg.threshold_pp + 1_000_000
    evaluate(make_deal(base), store, cfg)
    decision = evaluate(make_deal(int(base * 0.90)), store, cfg)
    assert decision.send and decision.reason == "new_low"


def test_미미한_하락은_알리지_않는다(cfg, store):
    base = cfg.threshold_pp + 1_000_000
    evaluate(make_deal(base), store, cfg)
    assert not evaluate(make_deal(base - 1_000), store, cfg).send


def test_같은_지문은_쿨다운_안에_다시_알리지_않는다(cfg, store):
    deal = make_deal(cfg.threshold_pp - 1)
    assert evaluate(deal, store, cfg).send
    store.mark_alerted(deal)
    # 같은 가격 재관측 — 더 싸지지 않았으므로 침묵해야 한다.
    assert not evaluate(make_deal(cfg.threshold_pp - 1), store, cfg).send


def test_쿨다운_중이라도_더_싸지면_다시_알린다(cfg, store):
    deal = make_deal(cfg.threshold_pp - 1)
    evaluate(deal, store, cfg)
    store.mark_alerted(deal)
    assert evaluate(make_deal(cfg.threshold_pp - 300_000), store, cfg).send


# ── 상태 저장소 ──────────────────────────────────────────────

def test_route_key_는_날짜의_하이픈과_충돌하지_않는다():
    """route_key 를 '-' 로 나누면 날짜가 쪼개진다. 다이제스트가 이걸 되쪼갠다."""
    key = make_deal(1, entry="FCO", exit_city="BCN").route_key
    assert key.split("|") == ["FCO", "BCN", "2027-01-03", "2027-01-17"]


def test_환산비는_표본이_쌓이면_보정된다(cfg, store):
    assert store.calibrated_ratio(0.70) == 0.70  # 표본 부족 -> 설정값
    for _ in range(6):
        store.add_calibration(ow_sum=5_000_000, actual_rt=3_000_000)
    assert store.calibrated_ratio(0.70) == pytest.approx(0.60)


def test_serpapi_예산은_날짜가_바뀌면_초기화된다(store):
    store.serp_budget(240, 8)
    store.spend_serp(3)
    assert store.serp_budget(240, 8) == (237, 5)
    store.state["serpapi"]["day"] = "1999-01-01"
    _, day_left = store.serp_budget(240, 8)
    assert day_left == 8


def test_오래된_알림_이력은_정리된다(store):
    store.state["alerts"] = {
        "old": (datetime.now() - timedelta(days=30)).isoformat(),
        "new": datetime.now().isoformat(),
    }
    store.prune_alerts(keep_days=14)
    assert list(store.state["alerts"]) == ["new"]


# ── 표시 ─────────────────────────────────────────────────────

def test_금액_표기(cfg):
    assert won(2_870_000) == "287만원"      # 100만 이상은 소수점을 떼서 읽기 쉽게
    assert won(950_000) == "95.0만원"       # 100만 미만은 소수 첫째 자리까지


def test_날짜_표기는_윈도우에서도_동작한다():
    # strftime("%-m/%-d") 는 Windows 에서 ValueError 를 낸다.
    assert md(date(2027, 1, 3)) == "1/3"


def test_알림_메시지에_핵심정보가_들어간다(cfg, store):
    deal = make_deal(2_870_000, entry="FCO", exit_city="BCN")
    text = format_deal(deal, cfg, evaluate(deal, store, cfg))
    assert "287만원" in text
    assert "오픈조" in text
    assert deal.deep_link in text


def test_다이제스트는_이력이_없어도_렌더링된다(cfg, store):
    assert "일일 요약" in format_digest(cfg, store)


def test_다이제스트가_route_key_를_바르게_분해한다(cfg, store):
    evaluate(make_deal(2_900_000, entry="FCO", exit_city="BCN"), store, cfg)
    text = format_digest(cfg, store)
    assert "FCO→BCN" in text and "오픈조" in text


# ── 확인 슬롯 배분 ───────────────────────────────────────────

class _FakeSerp:
    """SerpApiClient 대역. 예산만 흉내낸다."""

    def __init__(self, enabled: bool, month_left: int = 0, day_left: int = 0):
        self.enabled = enabled
        self._left = (month_left, day_left)

    def remaining(self):
        return self._left


def _cands(n_same: int, n_open: int):
    from src.models import Candidate

    out = []
    # 오픈조를 더 싸게 만들어 정렬 상 앞에 오게 한다 — 예산이 없으면
    # 이들이 슬롯을 다 먹어치우는 상황이 재현된다.
    for i in range(n_open):
        out.append(Candidate("FCO", "BCN", date(2027, 1, 3), date(2027, 1, 17), 3_000_000 + i))
    for i in range(n_same):
        out.append(Candidate("FCO", "FCO", date(2027, 1, 3), date(2027, 1, 17), 3_500_000 + i))
    out.sort(key=lambda c: c.estimated_pp)
    return out


def test_serpapi가_없으면_오픈조는_슬롯을_먹지_않는다(cfg):
    """실측에서 확인 슬롯 30개 중 24개가 확인 불가능한 오픈조로 낭비됐다."""
    from src.sweep import SweepResult, _select_for_confirmation

    result = SweepResult(mode="full")
    picked = _select_for_confirmation(cfg, _cands(10, 24), _FakeSerp(False), result, limit=10)

    assert len(picked) == 10
    assert all(not c.is_open_jaw for c in picked)
    assert result.skipped_open_jaw == 24


def test_오픈조는_남은_크레딧만큼만_확인한다(cfg):
    from src.sweep import SweepResult, _select_for_confirmation

    result = SweepResult(mode="full")
    serp = _FakeSerp(True, month_left=100, day_left=3)  # 오늘 3콜만 남음
    picked = _select_for_confirmation(cfg, _cands(10, 24), serp, result, limit=10)

    assert sum(c.is_open_jaw for c in picked) == 3
    assert len(picked) == 10


def test_한도보다_후보가_적으면_있는_만큼만(cfg):
    from src.sweep import SweepResult, _select_for_confirmation

    result = SweepResult(mode="full")
    picked = _select_for_confirmation(cfg, _cands(2, 0), _FakeSerp(False), result, limit=30)
    assert len(picked) == 2


def test_hot_목록에_같은_조합이_두_번_들어가지_않는다(cfg):
    """확인된 딜과 미확인 후보는 estimated_pp 가 달라서, dict 통째로 비교하면
    같은 조합이 중복 등록됐다 (hot 스윕에 1/4·1/14 가 두 번 나왔다)."""
    from src.models import Candidate
    from src.sweep import SweepResult, _hot_list

    out, back = date(2027, 1, 4), date(2027, 1, 14)
    result = SweepResult(mode="full")
    result.confirmed = [make_deal(3_413_320)]
    result.confirmed[0].outbound_date, result.confirmed[0].inbound_date = out, back

    # 같은 조합이 후보로도 남아 있는 상황
    selected = [Candidate("FCO", "FCO", out, back, 3_550_104)]

    hot = _hot_list(cfg, selected, result)
    idents = [(h["entry"], h["exit"], h["outbound_date"], h["inbound_date"]) for h in hot]
    assert len(idents) == len(set(idents))
    assert len(hot) == 1
