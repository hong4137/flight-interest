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
        store.add_calibration(ow_sum=5_000_000, actual=3_000_000)
    assert store.calibrated_ratio(0.70) == pytest.approx(0.60)


def test_왕복과_오픈조_환산비는_서로_섞이지_않는다(store):
    """오픈조는 왕복만큼 할인되지 않는다. 하나로 뭉치면 오픈조를 과소평가해
    후보 상위를 점령하고 비싼 SerpApi 크레딧을 낭비한다."""
    for _ in range(6):
        store.add_calibration(5_000_000, 3_550_000, "round-trip")   # 0.71
        store.add_calibration(5_000_000, 4_000_000, "open-jaw")     # 0.80

    assert store.calibrated_ratio(0.70, "round-trip") == pytest.approx(0.71)
    assert store.calibrated_ratio(0.80, "open-jaw") == pytest.approx(0.80)


def test_예전_단일_환산비_형식도_읽힌다(store):
    """state.json 은 코드 변경을 넘어 살아남는다. 옛 표본은 전부 왕복이었다."""
    store.state["calibration"] = {"samples": [[5_000_000, 3_550_000]] * 6, "ratio": 0.71}

    assert store.calibrated_ratio(0.70, "round-trip") == pytest.approx(0.71)
    assert store.calibrated_ratio(0.80, "open-jaw") == 0.80  # 오픈조 표본은 없음
    assert "samples" not in store.state["calibration"]       # 새 형식으로 이관됨


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
    assert "ICN→FCO / BCN→ICN" in text and "오픈조" in text


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


def test_오픈조는_남은_크레딧만큼만_확인한다(cfg, monkeypatch):
    from src.sweep import SweepResult, _select_for_confirmation

    monkeypatch.setattr(cfg, "serpapi_per_sweep_cap", 99)  # 스윕당 상한은 여기선 무관
    result = SweepResult(mode="full")
    serp = _FakeSerp(True, month_left=100, day_left=3)  # 오늘 3콜만 남음
    picked = _select_for_confirmation(cfg, _cands(10, 24), serp, result, limit=10)

    assert sum(c.is_open_jaw for c in picked) == 3
    assert len(picked) == 10


def test_한_스윕이_일일_오픈조_예산을_통째로_삼키지_않는다(cfg):
    """전부 첫 스윕이 써버리면 하루 중 나머지 시간대는 오픈조를 못 본다."""
    from src.sweep import SweepResult, _select_for_confirmation

    result = SweepResult(mode="full")
    serp = _FakeSerp(True, month_left=240, day_left=8)  # 예산은 넉넉하지만
    picked = _select_for_confirmation(cfg, _cands(10, 24), serp, result, limit=10)

    assert sum(c.is_open_jaw for c in picked) == cfg.serpapi_per_sweep_cap


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


def test_테스트_알림은_진짜와_구분된다(cfg, store):
    """샘플 알림이 진짜와 똑같이 생기면, 나중에 실제 특가가 떠도 무시하게 된다."""
    from src.alert import TEST_SOURCE

    deal = make_deal(2_870_000)
    deal.source = TEST_SOURCE
    text = format_deal(deal, cfg, evaluate(deal, store, cfg))

    assert "[테스트]" in text
    assert "실제 조회 결과가 아닙니다" in text
    assert "목표가 달성" not in text


def test_진짜_알림에는_테스트_표시가_없다(cfg, store):
    deal = make_deal(2_870_000)
    deal.source = "gflights"
    text = format_deal(deal, cfg, evaluate(deal, store, cfg))

    assert "[테스트]" not in text
    assert "목표가 달성" in text
    assert "출처: gflights" in text


# ── 다이제스트에 실행 가능한 정보가 들어가는가 ──────────────────

def test_노선_표기는_왕복과_오픈조를_구분한다():
    from src.alert import route_label

    assert route_label("ICN", "FCO", "FCO") == "ICN↔FCO"   # FCO→FCO 는 읽기 나쁘다
    assert route_label("ICN", "FCO", "MAD") == "ICN→FCO / MAD→ICN"


def test_예약_링크는_저장하지_않고_매번_생성한다(cfg):
    """상태에 링크를 쌓지 않으므로 이미 기록된 옛 항목에도 링크가 붙는다."""
    from src.alert import booking_link

    rt = booking_link(cfg, "FCO", "FCO", date(2027, 1, 4), date(2027, 1, 16))
    oj = booking_link(cfg, "FCO", "MAD", date(2027, 1, 4), date(2027, 1, 16))

    assert rt.startswith("https://www.google.com/travel/flights")
    assert "curr=KRW" in rt
    assert rt != oj  # 오픈조는 다른 여정이므로 링크도 달라야 한다


def test_다이제스트에_항공사와_링크가_들어간다(cfg, store):
    """가격만 알려주면 직접 확인할 방법이 없다."""
    deal = make_deal(3_413_320, airlines=["루프트한자", "ITA"])
    deal.outbound_date, deal.inbound_date = date(2027, 1, 4), date(2027, 1, 16)
    evaluate(deal, store, cfg)

    text = format_digest(cfg, store)
    assert "루프트한자" in text
    assert "경유 1회" in text
    assert 'href="https://www.google.com/travel/flights' in text
    assert "ICN↔FCO" in text


# ── 빈 응답 재시도 ───────────────────────────────────────────

def _fetcher_with_responses(cfg, monkeypatch, responses):
    """fetch_flights_html 을 대역으로 갈아끼운 Fetcher. responses 는 HTML 목록."""
    import src.gflights as gf

    calls = {"n": 0}

    def fake_fetch(query, proxy=None):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[i]

    monkeypatch.setattr(gf, "fetch_flights_html", fake_fetch)
    monkeypatch.setattr(gf.time, "sleep", lambda *_: None)  # 테스트에서 대기 제거
    return gf.Fetcher(cfg), calls


def test_빈_응답은_재시도해서_살려낸다(cfg, monkeypatch):
    """Google 이 오류가 아닌 정상 응답에 0건을 담아 보낼 때가 있다. 실측에서
    84개 구간 중 9개가 이렇게 사라졌고, 재조회하니 전부 결과가 있었다."""
    empty, full = _payload([]), _payload([_entry(2_000_000)])
    fetcher, calls = _fetcher_with_responses(cfg, monkeypatch, [empty, full])

    got = fetcher.one_way("ICN", "FCO", date(2027, 1, 6))

    assert len(got) == 1
    assert calls["n"] == 2                 # 한 번 더 조회했다
    assert fetcher.empty_recovered == 1
    assert fetcher.failures == 0           # 빈 응답은 실패가 아니다


def test_진짜로_결과가_없으면_상한만큼만_재시도한다(cfg, monkeypatch):
    """노선 자체가 없는 구간까지 무한정 재시도하면 스윕이 느려진다."""
    fetcher, calls = _fetcher_with_responses(cfg, monkeypatch, [_payload([])])

    got = fetcher.one_way("ICN", "BLQ", date(2027, 1, 6))

    assert got == []
    assert calls["n"] == cfg.empty_retries + 1
    assert fetcher.empty_final == 1
    assert fetcher.empty_recovered == 0


def test_결과가_있으면_재시도하지_않는다(cfg, monkeypatch):
    fetcher, calls = _fetcher_with_responses(cfg, monkeypatch, [_payload([_entry(1_000)])])

    fetcher.one_way("ICN", "FCO", date(2027, 1, 4))

    assert calls["n"] == 1
    assert fetcher.empty_recovered == 0


# ── 시간대 ───────────────────────────────────────────────────

def test_시계는_러너의_UTC_가_아니라_한국시간을_쓴다(monkeypatch):
    """러너는 UTC 로 돈다. 한국시간 8/23 새벽 2시에 받은 요약에 8/22 가 찍혔다."""
    import src.clock as clock
    from datetime import datetime as real_dt, timezone as real_tz

    class FakeDatetime(real_dt):
        @classmethod
        def now(cls, tz=None):
            # 실제 순간: 2026-08-22 17:09 UTC = 2026-08-23 02:09 KST
            utc = real_dt(2026, 8, 22, 17, 9, tzinfo=real_tz.utc)
            return utc.astimezone(tz) if tz else utc.replace(tzinfo=None)

    monkeypatch.setattr(clock, "datetime", FakeDatetime)

    assert clock.now() == real_dt(2026, 8, 23, 2, 9)
    assert clock.today() == date(2026, 8, 23)


def test_serpapi_일일한도는_한국_자정에_초기화된다(store, monkeypatch):
    """UTC 자정 기준이면 한국시간 오전 9시에 한도가 풀린다."""
    import src.store as store_mod

    monkeypatch.setattr(store_mod, "today", lambda: date(2026, 8, 23))
    store.serp_budget(240, 8)
    store.spend_serp(8)
    assert store.serp_budget(240, 8)[1] == 0

    monkeypatch.setattr(store_mod, "today", lambda: date(2026, 8, 24))
    assert store.serp_budget(240, 8)[1] == 8   # 한국 자정을 넘겨 초기화


# ── 텔레그램 명령 ────────────────────────────────────────────

@pytest.fixture
def cfgfile(tmp_path):
    """건드려도 되는 설정 파일 사본."""
    import shutil
    from src.config import DEFAULT_CONFIG

    dst = tmp_path / "search.yaml"
    shutil.copy2(DEFAULT_CONFIG, dst)
    return dst


def _run(text, cfgfile, store):
    from src.commands import handle
    from src.config import load_config

    return handle(text, load_config(cfgfile), store, cfgfile)


def test_목표가_명령은_만원_단위로_받는다(cfgfile, store):
    from src.config import load_config

    reply, changed = _run("/목표 320", cfgfile, store)

    assert changed
    assert load_config(cfgfile).threshold_pp == 3_200_000
    assert "320만원" in reply


def test_목표가는_원_단위로도_받는다(cfgfile, store):
    from src.config import load_config

    _run("/목표 3300000", cfgfile, store)
    assert load_config(cfgfile).threshold_pp == 3_300_000


def test_설정을_고쳐도_주석이_살아남는다(cfgfile, store):
    """search.yaml 의 주석은 왜 그 값인지를 설명한다. 잃으면 안 된다."""
    before = cfgfile.read_text(encoding="utf-8")
    assert "BLQ" in before  # 제외 이유를 적어둔 주석

    _run("/목표 320", cfgfile, store)

    after = cfgfile.read_text(encoding="utf-8")
    assert "BLQ" in after
    assert "실측" in after


def test_말도_안되는_값은_거부하고_설정을_건드리지_않는다(cfgfile, store):
    from src.config import load_config

    original = load_config(cfgfile).threshold_pp
    reply, changed = _run("/목표 999999999", cfgfile, store)

    assert not changed
    assert load_config(cfgfile).threshold_pp == original


def test_도시_추가와_제거(cfgfile, store):
    from src.config import load_config

    _run("/도시 추가 FLR", cfgfile, store)
    assert "FLR" in load_config(cfgfile).entry_airports

    _run("/도시 제거 FLR", cfgfile, store)
    assert "FLR" not in load_config(cfgfile).entry_airports


def test_도시를_전부_지우려_하면_되돌린다(cfgfile, store):
    """설정이 깨지면 파이프라인 전체가 멈춘다."""
    from src.config import load_config

    codes = load_config(cfgfile).entry_airports
    reply, changed = _run("/도시 제거 " + " ".join(codes), cfgfile, store)

    assert not changed
    assert "⚠️" in reply                                  # 이유를 알려준다
    assert load_config(cfgfile).entry_airports == codes   # 원상복구


def test_날짜_변경(cfgfile, store):
    from src.config import load_config

    _run("/날짜 출발 01-03 01-05", cfgfile, store)
    cfg = load_config(cfgfile)
    assert cfg.outbound_dates[0] == date(2027, 1, 3)
    assert cfg.outbound_dates[-1] == date(2027, 1, 5)


def test_출발창이_도착창을_넘어서면_되돌린다(cfgfile, store):
    from src.config import load_config

    before = load_config(cfgfile).outbound_dates
    reply, changed = _run("/날짜 출발 01-02 01-25", cfgfile, store)  # 도착창과 겹침

    assert not changed
    assert "⚠️" in reply
    assert load_config(cfgfile).outbound_dates == before


def test_모르는_명령은_도움말을_준다(cfgfile, store):
    reply, changed = _run("/아무거나", cfgfile, store)
    assert not changed
    assert "/목표" in reply


def test_허가되지_않은_chat_id_의_명령은_무시된다(cfg, store, monkeypatch, tmp_path):
    """봇은 공개돼 있어 누구나 말을 걸 수 있다."""
    import src.commands as cmds

    monkeypatch.setattr(cmds, "OFFSET_PATH", tmp_path / "telegram.json")
    monkeypatch.setattr(cmds, "DATA_DIR", tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setattr(
        cmds, "fetch_updates",
        lambda c, o: [cmds.Update(1, 999, "/목표 100"), cmds.Update(2, 111, "/상태")],
    )
    sent = []
    monkeypatch.setattr(cmds, "send", lambda c, t, **k: sent.append(t) or True)

    result = cmds.run(load_config(), store)

    assert result.rejected == 1      # 낯선 chat_id
    assert result.processed == 1     # 등록된 chat_id 만
    assert result.applied == 0       # /상태 는 설정을 안 바꾼다
    assert cmds._read_offset() == 3              # 둘 다 소비 확정


def test_예약_링크는_가격이_안_떨어져도_채워진다(cfg, store):
    """best_by_route 는 가격이 갱신될 때만 덮어썼다. 값이 안 떨어지는 노선은
    영영 링크가 비어서 대시보드에 '열기' 버튼이 안 나왔다."""
    first = make_deal(3_400_000)
    first.deep_link = ""
    evaluate(first, store, cfg)
    assert store.state["best_by_route"][first.route_key].get("link") == ""

    # 같은 가격 재관측 — 최저가는 그대로지만 링크는 최신이어야 한다
    again = make_deal(3_400_000)
    again.deep_link = "https://example.test/booking"
    evaluate(again, store, cfg)

    entry = store.state["best_by_route"][again.route_key]
    assert entry["link"] == "https://example.test/booking"
    assert entry["price"] == 3_400_000   # 최저가 기록은 유지


def test_대시보드_링크가_알림과_요약에_들어간다(cfg, store):
    """가격만 보고 끝나지 않게, 다른 조합을 볼 수 있는 경로를 같이 준다."""
    assert cfg.dashboard_url.startswith("https://")

    deal = make_deal(2_900_000)
    assert cfg.dashboard_url in format_deal(deal, cfg, evaluate(deal, store, cfg))
    assert cfg.dashboard_url in format_digest(cfg, store)


def test_대시보드_주소가_비면_링크를_넣지_않는다(cfg, store, monkeypatch):
    monkeypatch.setattr(cfg, "dashboard_url", "")

    deal = make_deal(2_900_000)
    assert "대시보드" not in format_deal(deal, cfg, evaluate(deal, store, cfg))
    assert "대시보드" not in format_digest(cfg, store)


def test_영문_명령과_한글_명령이_같이_동작한다(cfgfile, store):
    """텔레그램 자동완성 메뉴는 영문 이름만 받는다 (한글은 BOT_COMMAND_INVALID).
    메뉴는 영문으로 등록하되 한글 입력도 계속 받아야 한다."""
    from src.config import load_config

    for cmd, expected in [("/target 310", 3_100_000), ("/목표 320", 3_200_000)]:
        _, changed = _run(cmd, cfgfile, store)
        assert changed, cmd
        assert load_config(cfgfile).threshold_pp == expected

    for cmd in ("/status", "/상태", "/city", "/도시"):
        reply, changed = _run(cmd, cfgfile, store)
        assert not changed
        assert "모르는 명령" not in reply, cmd


def test_자동완성_메뉴는_영문_이름만_쓴다():
    """한글 이름을 보내면 텔레그램이 400 BOT_COMMAND_INVALID 로 거부한다."""
    import inspect
    import re

    from src.commands import publish_commands

    names = re.findall(r'"command":\s*"([^"]+)"', inspect.getsource(publish_commands))
    assert names
    assert all(re.fullmatch(r"[a-z0-9_]{1,32}", n) for n in names), names


# ── 여러 대화 지원 ───────────────────────────────────────────

def test_알림은_등록된_모든_대화로_간다(cfg, monkeypatch):
    """개인방과 단체방을 함께 쓸 수 있어야 한다."""
    import src.alert as al

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111, -222")
    sent = []

    class Resp:
        status_code = 200
    monkeypatch.setattr(al.requests, "post",
                        lambda *a, **k: sent.append(k["json"]["chat_id"]) or Resp())

    assert al.send(cfg, "테스트")
    assert sent == ["111", "-222"]


def test_한_대화가_막혀도_나머지에는_간다(cfg, monkeypatch):
    """봇을 차단했거나 그룹에서 나갔다고 다른 사람 알림까지 끊기면 안 된다."""
    import src.alert as al

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111,-222")

    class Resp:
        def __init__(self, code): self.status_code, self.text = code, "blocked"
    calls = []

    def fake_post(*a, **k):
        cid = k["json"]["chat_id"]
        calls.append(cid)
        return Resp(403 if cid == "111" else 200)
    monkeypatch.setattr(al.requests, "post", fake_post)

    assert al.send(cfg, "테스트")      # 하나라도 갔으면 성공
    assert calls == ["111", "-222"]


def test_명령_답장은_명령이_온_대화로만_간다(cfg, store, monkeypatch, tmp_path):
    """개인방에서 물었는데 단체방에 답이 가면 곤란하다."""
    import src.commands as cmds

    monkeypatch.setattr(cmds, "OFFSET_PATH", tmp_path / "telegram.json")
    monkeypatch.setattr(cmds, "DATA_DIR", tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111,-222")
    monkeypatch.setattr(cmds, "fetch_updates",
                        lambda c, o: [cmds.Update(1, -222, "/상태")])
    seen = []
    monkeypatch.setattr(cmds, "send",
                        lambda c, t, **k: seen.append(k.get("chat_id")) or True)

    result = cmds.run(load_config(), store)

    assert result.processed == 1 and result.rejected == 0
    assert seen == ["-222"]          # 단체방에서 왔으니 단체방으로만


def test_목록에_없는_대화는_여전히_거부한다(cfg, store, monkeypatch, tmp_path):
    import src.commands as cmds

    monkeypatch.setattr(cmds, "OFFSET_PATH", tmp_path / "telegram.json")
    monkeypatch.setattr(cmds, "DATA_DIR", tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111,-222")
    monkeypatch.setattr(cmds, "fetch_updates",
                        lambda c, o: [cmds.Update(1, 999, "/목표 100")])
    monkeypatch.setattr(cmds, "send", lambda c, t, **k: True)

    result = cmds.run(load_config(), store)
    assert result.rejected == 1 and result.processed == 0
