"""명령줄 진입점.

  python -m src.main sweep --mode full [--dry-run]
  python -m src.main sweep --mode hot
  python -m src.main digest [--dry-run]
  python -m src.main test-provider --route ICN-FCO --date 2027-01-03
  python -m src.main serp-test
  python -m src.main alert-test
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from datetime import date
from pathlib import Path

from .alert import format_digest, md, send, won
from .config import ROOT, load_config
from .store import Store


def _setup_io(verbose: bool) -> None:
    # Windows 콘솔은 기본이 cp949 라 한글/이모지가 깨진다.
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # fast-flights 가 요청마다 찍는 200 로그는 시끄럽다.
    logging.getLogger("fast_flights").setLevel(logging.WARNING)
    logging.getLogger("primp").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _load_dotenv() -> None:
    """로컬 개발용. Actions 에서는 Secrets 가 환경변수로 들어오므로 없어도 된다."""
    import os

    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# ── 하위 명령 ────────────────────────────────────────────────

def cmd_sweep(args) -> int:
    from .sweep import run_sweep

    cfg = load_config(args.config)
    store = Store.load()
    result = run_sweep(cfg, store, mode=args.mode, dry_run=args.dry_run)

    if not args.dry_run:
        store.save()

    print("\n" + "=" * 72)
    print(result.summary())
    print("=" * 72)

    if result.confirmed:
        rows = sorted(result.confirmed, key=lambda d: d.price_per_person)
        print("\n{:>12}  {:<12} {:<11} {:<5} {:<22} {}".format(
            "1인가", "구간", "출발/귀국", "경유", "항공사", "종류"))
        print("-" * 88)
        for deal in rows[:20]:
            print("{:>12,}  {:<12} {:<11} {:<5} {:<22} {}".format(
                deal.price_per_person,
                "{}→{}".format(deal.entry, deal.exit),
                "{} / {}".format(md(deal.outbound_date), md(deal.inbound_date)),
                deal.stops,
                (", ".join(deal.airlines))[:22],
                deal.kind,
            ))
        best = rows[0]
        gap = best.price_per_person - cfg.threshold_pp
        if gap <= 0:
            print("\n🎯 목표 {} 달성  ({})".format(won(cfg.threshold_pp), best.route_key))
        else:
            print("\n목표 {} 까지 {} 남음 ({:.0f}%)".format(
                won(cfg.threshold_pp), won(gap), gap / cfg.threshold_pp * 100))
    else:
        print("\n확인된 조합이 없습니다. 임계값이 너무 낮거나 조회가 실패했을 수 있습니다.")

    if args.dry_run:
        print("\n[dry-run] 알림을 보내지 않았고 상태/이력도 저장하지 않았습니다.")
    return 0


def cmd_digest(args) -> int:
    cfg = load_config(args.config)
    store = Store.load()
    text = format_digest(cfg, store)

    plain = text.replace("<b>", "").replace("</b>", "")
    plain = plain.replace("<i>", "").replace("</i>", "")
    print(plain)

    if args.dry_run:
        print("\n[dry-run] 발송하지 않았습니다.")
        return 0
    ok = send(cfg, text, silent=True)
    store.save()
    return 0 if ok else 1


def cmd_test_provider(args) -> int:
    from .gflights import Fetcher

    cfg = load_config(args.config)
    frm, _, to = args.route.upper().partition("-")
    when = date.fromisoformat(args.date)

    fetcher = Fetcher(cfg)
    print("조회: {} → {} · {} · {} · {}명 · 경유 {}회 이하".format(
        frm, to, when, cfg.cabin, cfg.passengers, cfg.max_stops))

    results = fetcher.one_way(frm, to, when)
    if not results:
        print("결과가 없습니다 (실패율 {:.0%}).".format(fetcher.fail_rate))
        return 1

    print("\n{}건:\n".format(len(results)))
    for it in results[:10]:
        print("  {:>12,}원/인  {:<24} 경유{} via {:<10} {} → {}".format(
            it.price_per_person,
            it.airline_label()[:24],
            it.stops,
            it.via() or "-",
            it.depart_at.strftime("%m/%d %H:%M"),
            it.arrive_at.strftime("%m/%d %H:%M"),
        ))
    print("\n링크: {}".format(results[0].deep_link))
    return 0


def cmd_serp_test(args) -> int:
    from .providers.serpapi import SerpApiClient

    cfg = load_config(args.config)
    store = Store.load()
    client = SerpApiClient(cfg, store)

    if not client.enabled:
        print("SERPAPI_KEY 가 설정되지 않았습니다. .env 또는 환경변수에 넣어주세요.")
        return 1

    month_left, day_left = client.remaining()
    print("잔여 크레딧: 월 {} / 일 {}".format(month_left, day_left))

    entry = cfg.entry_airports[0]
    exit_city = cfg.exit_airports[-1] if len(cfg.exit_airports) > 1 else entry
    out_d, in_d = cfg.outbound_dates[0], cfg.return_search_dates[len(cfg.return_search_dates) // 2]
    print("오픈조 조회: {}→{} / {}→{}  ({} / {})  — 크레딧 1 소모".format(
        cfg.origin, entry, exit_city, cfg.origin, out_d, in_d))

    try:
        results = client.open_jaw(entry, exit_city, out_d, in_d)
    except Exception as exc:  # noqa: BLE001
        print("실패: {}".format(exc))
        store.save()
        return 1

    store.save()
    if not results:
        print("결과 없음 (크레딧은 소모됨).")
        return 1
    for it in results[:5]:
        print("  {:>12,}원/인  {:<22} 경유{}".format(
            it.price_per_person, it.airline_label()[:22], it.stops))
    return 0


def cmd_commands(args) -> int:
    from .commands import publish_commands, run as run_commands

    cfg = load_config(args.config)
    store = Store.load()

    if args.publish_menu:
        ok = publish_commands(cfg)
        print("명령 메뉴 등록 {}".format("성공" if ok else "실패"))

    result = run_commands(cfg, store, args.config)
    store.save()

    print(result.summary())
    for reply in result.replies:
        plain = reply.replace("<b>", "").replace("</b>", "")
        plain = plain.replace("<i>", "").replace("</i>", "")
        plain = plain.replace("<code>", "").replace("</code>", "")
        print("-" * 60)
        print(plain)
    # 설정이 바뀌었으면 워크플로가 커밋해야 하므로 표시를 남긴다.
    if result.config_changed:
        print("::notice::config changed")
    return 0


def cmd_alert_test(args) -> int:
    from datetime import datetime

    from .alert import TEST_SOURCE, AlertDecision, format_deal, notify_deal
    from .models import Deal

    cfg = load_config(args.config)
    store = Store.load()

    sample = Deal(
        entry="FCO",
        exit="BCN",
        outbound_date=cfg.outbound_dates[0],
        inbound_date=cfg.return_search_dates[-2],
        price_per_person=2_870_000,
        airlines=["중국동방항공"],
        stops=1,
        deep_link="https://www.google.com/travel/flights",
        source=TEST_SOURCE,
        kind="open-jaw",
        korea_arrival=datetime.combine(cfg.arrive_korea[1], datetime.min.time()).replace(hour=18, minute=40),
        total_minutes=980,
    )
    decision = AlertDecision(True, "threshold", 3_260_000)

    print(format_deal(sample, cfg, decision).replace("<b>", "").replace("</b>", ""))
    print()

    if not cfg.telegram_enabled:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없어 발송하지 않았습니다.")
        return 1
    ok = notify_deal(cfg, store, sample, decision)
    print("발송 {}".format("성공" if ok else "실패"))
    return 0 if ok else 1


# ── 파서 ─────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flight-watch", description="유럽 비즈니스 특가 감시")
    parser.add_argument("--config", default=None, help="설정 파일 경로")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sweep", help="검색 스윕 실행")
    p.add_argument("--mode", choices=["full", "hot"], default="full")
    p.add_argument("--dry-run", action="store_true", help="알림/저장 없이 조회만")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("digest", help="일일 요약 발송")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("test-provider", help="단건 조회로 스크레이퍼 상태 확인")
    p.add_argument("--route", default="ICN-FCO")
    p.add_argument("--date", default=None)
    p.set_defaults(func=cmd_test_provider)

    p = sub.add_parser("serp-test", help="SerpApi 키와 오픈조 조회 확인 (크레딧 1 소모)")
    p.set_defaults(func=cmd_serp_test)

    p = sub.add_parser("commands", help="텔레그램으로 받은 명령 처리")
    p.add_argument("--publish-menu", action="store_true", help="봇 명령 자동완성 목록도 갱신")
    p.set_defaults(func=cmd_commands)

    p = sub.add_parser("alert-test", help="텔레그램 샘플 알림")
    p.set_defaults(func=cmd_alert_test)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_io(args.verbose)
    _load_dotenv()

    if getattr(args, "date", None) is None and args.command == "test-provider":
        args.date = load_config(args.config).outbound_dates[0].isoformat()

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n중단되었습니다.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
