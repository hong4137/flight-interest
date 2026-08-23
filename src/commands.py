"""텔레그램으로 받은 명령을 config/search.yaml 에 반영한다.

상시 서버가 없으므로 웹훅 대신 주기적으로 getUpdates 를 확인한다. 명령은
보낸 뒤 최대 한 주기(기본 15분) 안에 처리되고 봇이 답장한다. 그래서 버튼
(inline keyboard) 대신 텍스트 명령을 쓴다 — 눌러도 몇 분간 무반응이면
고장난 것으로 느껴진다.

**getUpdates 는 이 워크플로만 호출해야 한다.** 오프셋으로 소비가 확정되므로
두 곳에서 부르면 서로의 메시지를 삼킨다.
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import requests
from ruamel.yaml import YAML

from .alert import send, won
from .config import DATA_DIR, DEFAULT_CONFIG, Config, load_config
from .store import Store

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

# 처리 위치를 state.json 이 아닌 별도 파일에 둔다. 스윕 워크플로가 state.json 을
# 커밋하므로, 같은 파일을 쓰면 두 워크플로가 겹칠 때 git 충돌이 난다.
OFFSET_PATH = DATA_DIR / "telegram.json"


def _read_offset() -> int:
    if not OFFSET_PATH.exists():
        return 0
    try:
        return int(json.loads(OFFSET_PATH.read_text(encoding="utf-8")).get("offset", 0))
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0


def _write_offset(offset: int) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(
        json.dumps({"offset": offset}, indent=2) + chr(10), encoding="utf-8"
    )

# 이탈리아·스페인·포르투갈 주요 공항. /도시 추가 시 국가 그룹을 자동으로 찾는다.
AIRPORT_COUNTRY = {
    **{c: "IT" for c in "FCO MXP VCE BLQ NAP LIN TRN FLR PSA BRI CTA PMO VRN".split()},
    **{c: "ES" for c in "BCN MAD VLC AGP SVQ BIO PMI ALC LPA TFS".split()},
    **{c: "PT" for c in "LIS OPO FAO FNC PDL".split()},
}

_HELP_BODY = """<b>사용할 수 있는 명령</b>

/상태 — 현재 최저가와 목표까지 남은 금액
/로직 — 무엇을 어떻게 찾고 있는지 설명
/목표 320 — 목표가를 1인 320만원으로 변경
/경유 1 — 경유 최대 횟수 (0=직항만)
/인원 2 — 탑승 인원
/도시 — 현재 감시 중인 도시 목록
/도시 추가 BLQ · /도시 제거 OPO — 도시 추가·제거
/날짜 출발 01-02 01-06 — 인천 출발 가능일
/날짜 도착 01-16 01-20 — 인천 도착 가능일
/도움 — 이 목록

<i>입력창의 자동완성 메뉴는 영문으로만 뜹니다 (텔레그램 제약).
/status /logic /target /stops /pax /city /dates /help 도 똑같이 동작합니다.</i>

<i>/상태 와 /도움 은 즉시, 나머지는 20초쯤 걸립니다.</i>"""


def dashboard_link(cfg: Config, label: str = "📊 대시보드 열기") -> str:
    """설정에 주소가 있으면 링크 한 줄을 만든다."""
    if not cfg.dashboard_url:
        return ""
    return '<a href="{}">{}</a>'.format(cfg.dashboard_url, label)


def help_text(cfg: Config) -> str:
    link = dashboard_link(cfg)
    return _HELP_BODY + (chr(10) * 2 + link if link else "")


@dataclass
class Update:
    update_id: int
    chat_id: int
    text: str


@dataclass
class CommandResult:
    processed: int = 0
    applied: int = 0
    rejected: int = 0
    replies: list[str] = field(default_factory=list)
    config_changed: bool = False

    def summary(self) -> str:
        return "명령 {}건 처리 · 적용 {} · 거부 {}".format(
            self.processed, self.applied, self.rejected
        )


# ── 텔레그램 입출력 ──────────────────────────────────────────

def fetch_updates(cfg: Config, offset: int) -> list[Update]:
    resp = requests.get(
        API.format(token=cfg.telegram_token, method="getUpdates"),
        params={"offset": offset, "timeout": 0, "allowed_updates": '["message"]'},
        timeout=cfg.timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError("getUpdates 실패: {}".format(data.get("description")))

    out: list[Update] = []
    for item in data.get("result", []):
        message = item.get("message") or {}
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        if text and chat.get("id") is not None:
            out.append(Update(int(item["update_id"]), int(chat["id"]), text))
    return out


def publish_commands(cfg: Config) -> bool:
    """텔레그램 입력창의 명령 자동완성 목록을 갱신한다.

    이름은 반드시 영문 소문자·숫자·밑줄이어야 한다. 한글로 보내면 텔레그램이
    BOT_COMMAND_INVALID 로 거부한다. 그래서 메뉴는 영문으로 등록하고, 파서는
    한글 별칭도 계속 받는다 (/status 도 /상태 도 동작한다).
    """
    commands = [
        {"command": "status", "description": "현재 최저가와 목표까지 남은 금액 (/상태)"},
        {"command": "logic", "description": "무엇을 어떻게 찾고 있는지 설명 (/로직)"},
        {"command": "target", "description": "목표가 변경 — 예: /target 320 (/목표)"},
        {"command": "stops", "description": "경유 최대 횟수 — 예: /stops 1 (/경유)"},
        {"command": "pax", "description": "탑승 인원 — 예: /pax 2 (/인원)"},
        {"command": "city", "description": "감시 도시 조회·추가·제거 (/도시)"},
        {"command": "dates", "description": "출발·도착 가능일 변경 (/날짜)"},
        {"command": "help", "description": "명령 목록 (/도움)"},
    ]
    resp = requests.post(
        API.format(token=cfg.telegram_token, method="setMyCommands"),
        json={"commands": commands},
        timeout=cfg.timeout,
    )
    if resp.status_code != 200 or not resp.json().get("ok", False):
        log.warning("명령 메뉴 등록 실패: %s", resp.text[:200])
        return False
    return True


# ── YAML 편집 ────────────────────────────────────────────────

def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # 긴 줄이 접히면 주석 정렬이 망가진다
    return y


def _load_doc(path: Path):
    return _yaml().load(io.open(path, encoding="utf-8"))


def _save_doc(path: Path, doc) -> None:
    with io.open(path, "w", encoding="utf-8") as fh:
        _yaml().dump(doc, fh)


def _edit(path: Path, mutate) -> str:
    """mutate(doc) 를 적용하고, 결과가 유효한 설정인지 확인한 뒤 저장한다.

    잘못된 명령 하나가 파이프라인 전체를 멈추면 안 되므로, 검증에 실패하면
    원본으로 되돌린다.
    """
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    try:
        doc = _load_doc(path)
        reply = mutate(doc)
        _save_doc(path, doc)
        load_config(path)  # 유효성 검증
        return reply
    except Exception as exc:  # noqa: BLE001
        shutil.copy2(backup, path)
        raise ValueError("설정을 바꾸지 못했습니다: {}".format(exc)) from exc
    finally:
        backup.unlink(missing_ok=True)


def _airport_groups(doc) -> dict:
    return doc["trip"]["entry_airports"]


def _all_airports(doc) -> list[str]:
    return [c for codes in _airport_groups(doc).values() for c in codes]


_DATE_TOKEN = re.compile(
    r"(?<!\d)"
    r"(?:(?P<y>\d{4})[-/.]?)?"          # 연도는 있어도 되고 없어도 된다
    r"(?P<m>\d{1,2})[-/.]?(?P<d>\d{1,2})"
    r"(?!\d)"
)


def _find_dates(text: str, fallback_year: int) -> list[tuple[date, bool]]:
    """문장에서 날짜를 찾아 (날짜, 연도를_직접_적었는지) 목록으로 돌려준다.

    사람이 실제로 치는 형태를 넓게 받는다 — '2026-12-29부터 2027-01-01',
    '12/29 ~ 1/1', '1229 0101' 모두 통한다. 연도를 적었으면 그 연도를 쓰고,
    안 적었으면 fallback_year 를 쓴다.

    연도를 적었는지 함께 돌려주는 이유: 이 여행은 연말을 넘긴다. 연도 없이
    '12/29 ~ 1/1' 이라고 쓰면 둘 다 같은 해로 잡혀 순서가 뒤집히는데, 그때만
    보정해야 한다. 연도를 직접 적은 경우의 역순은 진짜 오타이므로 거부한다.
    """
    cleaned = re.sub(r"(부터|까지|에서|년|월|일|~|-{2,})", " ", text)
    found: list[tuple[date, bool]] = []
    for m in _DATE_TOKEN.finditer(cleaned):
        explicit = m.group("y") is not None
        year = int(m.group("y")) if explicit else fallback_year
        month, day = int(m.group("m")), int(m.group("d"))
        try:
            found.append((date(year, month, day), explicit))
        except ValueError:
            continue  # 2월 30일 같은 것
    return found


# ── 명령 해석 ────────────────────────────────────────────────

def _status(cfg: Config, store: Store) -> str:
    best = store.state.get("global_best")
    lines = ["<b>현재 상태</b>", ""]
    if best:
        gap = best["price"] - cfg.threshold_pp
        lines.append("최저 <b>{}</b> / 1인".format(won(best["price"])))
        if gap <= 0:
            lines.append("🎯 목표 {} 달성".format(won(cfg.threshold_pp)))
        else:
            lines.append("목표 {} 까지 {} 남음".format(won(cfg.threshold_pp), won(gap)))
    else:
        lines.append("아직 확인된 가격이 없습니다.")

    lines.append("")
    lines.append("감시 도시: {}".format(", ".join(cfg.entry_airports)))
    lines.append(
        "출발 {} ~ {} · 인천 도착 {} ~ {}".format(
            cfg.outbound_dates[0], cfg.outbound_dates[-1],
            cfg.arrive_korea[0], cfg.arrive_korea[1],
        )
    )
    lines.append("{}명 · {} · 경유 {}회 이하".format(cfg.passengers, cfg.cabin, cfg.max_stops))

    last = store.state.get("last_sweep")
    if last:
        lines.append("")
        lines.append(
            "<i>마지막 스윕 {} · 조회 {}건 · 실패율 {:.0%}</i>".format(
                last.get("at", "?")[5:16], last.get("queries", 0), last.get("fail_rate", 0.0)
            )
        )
    return "\n".join(lines)


AIRPORT_NAME = {
    "FCO": "로마", "MXP": "밀라노", "VCE": "베네치아", "BLQ": "볼로냐", "NAP": "나폴리",
    "LIN": "밀라노(리나테)", "TRN": "토리노", "FLR": "피렌체", "PSA": "피사",
    "BRI": "바리", "CTA": "카타니아", "PMO": "팔레르모", "VRN": "베로나",
    "BCN": "바르셀로나", "MAD": "마드리드", "VLC": "발렌시아", "AGP": "말라가",
    "SVQ": "세비야", "BIO": "빌바오", "PMI": "마요르카", "ALC": "알리칸테",
    "LPA": "라스팔마스", "TFS": "테네리페",
    "LIS": "리스본", "OPO": "포르투", "FAO": "파루", "FNC": "마데이라", "PDL": "폰타델가다",
    "ICN": "인천",
}

CABIN_NAME = {
    "economy": "이코노미", "premium-economy": "프리미엄 이코노미",
    "business": "비즈니스", "first": "퍼스트",
}


def _city(code: str) -> str:
    name = AIRPORT_NAME.get(code)
    return "{}({})".format(name, code) if name else code


def _logic(cfg: Config, store: Store) -> str:
    """지금 무엇을 어떻게 찾고 있는지 사람 말로 설명한다.

    /상태 는 결과만 보여준다. 조건을 바꾼 뒤 "이게 맞게 돌고 있나" 를 확인할
    방법이 없어서 만들었다.
    """
    n_city = len(cfg.entry_airports)
    n_out, n_in = len(cfg.outbound_dates), len(cfg.return_search_dates)
    combos = n_city * len(cfg.exit_airports) * n_out * n_in
    ow_queries = n_city * n_out + len(cfg.exit_airports) * n_in
    ratio = store.calibrated_ratio(cfg.ow_to_rt_ratio, "round-trip")

    lines = [
        "🔍 <b>지금 하고 있는 일</b>",
        "",
        "<b>무엇을 찾는가</b>",
        "인천 ↔ {}".format(", ".join(_city(c) for c in cfg.entry_airports)),
        "{} ~ {} 인천 출발 ({}일)".format(
            cfg.outbound_dates[0], cfg.outbound_dates[-1], n_out
        ),
        "{} ~ {} 인천 도착".format(cfg.arrive_korea[0], cfg.arrive_korea[1]),
        "{} {}석 · 경유 {}회 이하 · 목표 1인 {}".format(
            CABIN_NAME.get(cfg.cabin, cfg.cabin), cfg.passengers,
            cfg.max_stops, won(cfg.threshold_pp),
        ),
        "",
    ]

    if cfg.allow_open_jaw:
        lines.append("입국·출국 도시가 달라도 됩니다 (오픈조).")
    lines += [
        "가능한 조합 <b>{:,}개</b>".format(combos),
        "  <i>{}일 출발 × {}일 귀국 × {}도시 입국 × {}도시 출국</i>".format(
            n_out, n_in, n_city, len(cfg.exit_airports)
        ),
        "",
        "<b>어떻게 찾는가</b>",
        "{:,}개를 매번 다 조회할 수는 없어 편도로 쪼갭니다.".format(combos),
        "  인천→각 도시 {}일 = {}회".format(n_out, n_city * n_out),
        "  각 도시→인천 {}일 = {}회".format(n_in, len(cfg.exit_airports) * n_in),
        "  <b>{}회</b>로 {:,}개 조합의 가격 상한을 계산합니다".format(ow_queries, combos),
        "",
        "왕복가 ≈ 편도합산 × {:.2f} <i>(실측·자동보정)</i>".format(ratio),
        "추정가가 {} 이내인 조합만 실제로 확인합니다.".format(won(cfg.threshold_trigger_pp())),
        "",
        "<b>언제 돌리는가</b>",
        "4시간마다  전체 — 편도 {}회 + 유망 조합 최대 {}개 실확인".format(
            ow_queries, cfg.full_confirm_count
        ),
        "매시간     집중 — 직전에 유망했던 {}개만 재확인".format(
            cfg.hot_roundtrip_count + cfg.hot_openjaw_count
        ),
        "매일 09:00 요약",
    ]

    last = store.state.get("last_sweep")
    if last:
        mode = {"full": "전체", "hot": "집중"}.get(last.get("mode"), last.get("mode", "?"))
        lines += [
            "",
            "<b>최근 스윕</b>",
            "{} · {} · 조회 {}건 · 실패 {:.0%}".format(
                last.get("at", "?")[5:16].replace("T", " "), mode,
                last.get("queries", 0), last.get("fail_rate", 0.0),
            ),
        ]
        if last.get("candidates") is not None:
            lines.append(
                "임계값 이내 후보 {}개 → 실제 확인 {}건".format(
                    last.get("candidates", 0), last.get("confirmed", 0)
                )
            )
        if last.get("cheapest"):
            gap = last["cheapest"] - cfg.threshold_pp
            lines.append(
                "최저 {} ({})".format(
                    won(last["cheapest"]),
                    "목표 달성" if gap <= 0 else "목표까지 {}".format(won(gap)),
                )
            )

    if not cfg.serpapi_enabled:
        lines += ["", "<i>SerpApi 미설정 — 오픈조 실가 확인은 건너뜁니다.</i>"]

    link = dashboard_link(cfg)
    if link:
        lines += ["", link]
    return chr(10).join(lines)


def handle(text: str, cfg: Config, store: Store, path: Path) -> tuple[str, bool]:
    """(답장, 설정이 바뀌었는지).

    사용자 입력으로 예외가 새어나가지 않게 한다. 잘못된 명령은 설명을 담은
    답장이 되어야지, 워크플로를 실패시켜서는 안 된다.
    """
    try:
        return _dispatch(text, cfg, store, path)
    except ValueError as exc:
        return "⚠️ {}".format(exc), False


# 붙여 쓴 명령을 갈라준다. 사람은 /날짜출발 처럼 띄어쓰기 없이 치는 일이 흔하다.
_GLUED = {
    "날짜": ("출발", "도착", "가는날", "오는날", "귀국", "출국"),
    "도시": ("추가", "제거", "삭제"),
    "dates": ("out", "in"),
    "city": ("add", "remove"),
}


def _split_glued(raw: str, args: list[str]) -> tuple[str, list[str]]:
    for head, tails in _GLUED.items():
        if raw.startswith(head) and raw != head:
            tail = raw[len(head):]
            if tail in tails:
                return head, [tail, *args]
    return raw, args


def _dispatch(text: str, cfg: Config, store: Store, path: Path) -> tuple[str, bool]:
    parts = text.split()
    raw = parts[0].lstrip("/").split("@")[0].lower()
    args = parts[1:]
    raw, args = _split_glued(raw, args)

    if raw in {"help", "도움", "명령", "start"}:
        return help_text(cfg), False

    if raw in {"status", "상태"}:
        return _status(cfg, store), False

    if raw in {"logic", "로직", "설명", "계획"}:
        return _logic(cfg, store), False

    if raw in {"target", "목표"}:
        if not args:
            return "금액을 함께 적어주세요. 예: <code>/목표 320</code>", False
        try:
            value = int(re.sub(r"[^\d]", "", args[0]))
        except ValueError:
            return "숫자를 읽지 못했습니다. 예: <code>/목표 320</code>", False
        # 320 -> 320만원, 3200000 -> 그대로
        krw = value * 10_000 if value < 100_000 else value
        if not (500_000 <= krw <= 20_000_000):
            return "목표가는 50만원~2000만원 사이여야 합니다.", False

        def mutate(doc):
            doc["threshold"]["per_person_krw"] = krw
            return ""

        _edit(path, mutate)
        return "목표가를 <b>{}</b> / 1인 으로 바꿨습니다.".format(won(krw)), True

    if raw in {"stops", "경유"}:
        if not args or not args[0].isdigit():
            return "예: <code>/경유 1</code> (0 이면 직항만)", False
        stops = int(args[0])
        if stops > 2:
            return "경유는 0~2회까지만 지정할 수 있습니다.", False

        def mutate(doc):
            doc["search"]["max_stops"] = stops
            return ""

        _edit(path, mutate)
        label = "직항만" if stops == 0 else "경유 {}회 이하".format(stops)
        return "검색 조건을 <b>{}</b> 로 바꿨습니다.".format(label), True

    if raw in {"pax", "인원"}:
        if not args or not args[0].isdigit():
            return "예: <code>/인원 2</code>", False
        pax = int(args[0])
        if not (1 <= pax <= 9):
            return "인원은 1~9명까지 지정할 수 있습니다.", False

        def mutate(doc):
            doc["search"]["passengers"] = pax
            return ""

        _edit(path, mutate)
        return (
            "인원을 <b>{}명</b> 으로 바꿨습니다.\n"
            "<i>좌석 수에 따라 운임이 달라지므로 가격이 크게 바뀔 수 있습니다.</i>".format(pax)
        ), True

    if raw in {"city", "도시"}:
        return _handle_city(args, path)

    if raw in {"dates", "날짜"}:
        return _handle_dates(args, cfg, path)

    return "모르는 명령입니다." + chr(10) * 2 + help_text(cfg), False


def _handle_city(args: list[str], path: Path) -> tuple[str, bool]:
    if not args:
        doc = _load_doc(path)
        groups = _airport_groups(doc)
        lines = ["<b>감시 중인 도시</b>", ""]
        for country, codes in groups.items():
            lines.append("{}: {}".format(country, ", ".join(codes) if codes else "(없음)"))
        lines.append("")
        lines.append("추가: <code>/도시 추가 BLQ</code> · 제거: <code>/도시 제거 OPO</code>")
        return "\n".join(lines), False

    action = args[0].lower()
    codes = [c.upper() for c in args[1:] if len(c) == 3 and c.isalpha()]
    if not codes:
        return "공항 코드를 IATA 3자리로 적어주세요. 예: <code>/도시 추가 BLQ</code>", False

    if action in {"add", "추가"}:
        unknown = [c for c in codes if c not in AIRPORT_COUNTRY]
        if unknown:
            return (
                "{} 는 이탈리아·스페인·포르투갈 공항 목록에 없습니다.\n"
                "다른 나라를 넣으시려면 config/search.yaml 을 직접 고쳐주세요.".format(
                    ", ".join(unknown)
                )
            ), False

        added: list[str] = []

        def mutate(doc):
            groups = _airport_groups(doc)
            existing = set(_all_airports(doc))
            for code in codes:
                if code in existing:
                    continue
                country = AIRPORT_COUNTRY[code]
                groups.setdefault(country, [])
                groups[country].append(code)
                added.append(code)
            return ""

        _edit(path, mutate)
        if not added:
            return "이미 감시 중입니다: {}".format(", ".join(codes)), False
        return (
            "감시 도시에 <b>{}</b> 을(를) 추가했습니다.\n"
            "<i>스윕 조회량이 도시당 12건 늘어납니다.</i>".format(", ".join(added))
        ), True

    if action in {"remove", "제거", "삭제"}:
        removed: list[str] = []

        def mutate(doc):
            groups = _airport_groups(doc)
            for country, existing in groups.items():
                for code in codes:
                    if code in existing:
                        existing.remove(code)
                        removed.append(code)
            if not _all_airports(doc):
                raise ValueError("도시를 전부 지울 수는 없습니다")
            return ""

        _edit(path, mutate)
        if not removed:
            return "감시 목록에 없습니다: {}".format(", ".join(codes)), False
        return "감시 도시에서 <b>{}</b> 을(를) 뺐습니다.".format(", ".join(removed)), True

    return "추가 또는 제거 중에 골라주세요. 예: <code>/도시 추가 BLQ</code>", False


def _nearest_year(value: date, anchor: date) -> date:
    """연도를 안 적은 날짜를 anchor 에서 가장 가까운 해로 옮긴다.

    1월 출발 여행에서 "12-29" 는 11개월 뒤가 아니라 직전 12월을 뜻한다.
    """
    best = value
    for shift in (-1, 0, 1):
        try:
            candidate = value.replace(year=value.year + shift)
        except ValueError:
            continue  # 2/29
        if abs((candidate - anchor).days) < abs((best - anchor).days):
            best = candidate
    return best


_DATE_HELP = (
    "예: <code>/날짜 출발 2026-12-29 2027-01-01</code>\n"
    "    <code>/날짜 도착 2027-01-10 2027-01-13</code>\n\n"
    "연도를 빼고 <code>12-29 01-01</code> 처럼 써도 되지만, 이 여행은 연말을 "
    "넘기므로 <b>연도를 함께 적는 편이 안전합니다.</b>"
)


def _handle_dates(args: list[str], cfg: Config, path: Path) -> tuple[str, bool]:
    if len(args) < 2:
        return _DATE_HELP, False

    which = args[0].lower()
    if which in {"out", "출발", "가는날", "가는", "출국"}:
        key, label = "outbound_dates", "인천 출발"
        fallback_year = cfg.outbound_dates[0].year
        anchor = cfg.outbound_dates[0]
    elif which in {"in", "도착", "오는날", "오는", "귀국"}:
        key, label = "arrive_korea", "인천 도착"
        fallback_year = cfg.arrive_korea[0].year
        anchor = cfg.arrive_korea[0]
    else:
        return "출발 또는 도착 중에 골라주세요.\n\n" + _DATE_HELP, False

    found = _find_dates(" ".join(args[1:]), fallback_year)
    if len(found) < 2:
        return (
            "날짜 두 개를 읽지 못했습니다 (찾은 것: {}).\n\n{}".format(
                ", ".join(str(d) for d, _ in found) or "없음", _DATE_HELP
            )
        ), False

    (start, start_explicit), (end, end_explicit) = found[0], found[1]

    # 연도를 안 적었으면 지금 설정된 창에서 가장 가까운 해로 본다. 1월 여행에
    # "12-29" 라고 쓰면 11개월 뒤가 아니라 직전 12월을 뜻한다.
    start = start if start_explicit else _nearest_year(start, anchor)
    end = end if end_explicit else _nearest_year(end, anchor)

    # 연말을 넘기는 구간은 순서가 뒤집힌다 ('12/29 ~ 1/1').
    if end < start and not end_explicit:
        end = end.replace(year=end.year + 1)

    if end < start:
        return "종료일({})이 시작일({})보다 빠릅니다.".format(end, start), False

    def mutate(doc):
        doc["trip"][key]["start"] = start
        doc["trip"][key]["end"] = end
        return ""

    _edit(path, mutate)
    return "{} 가능일을 <b>{} ~ {}</b> 로 바꿨습니다.".format(label, start, end), True


# ── 진입점 ───────────────────────────────────────────────────

def _apply(
    cfg: Config, store: Store, path: Path, chat_id: str, text: str, result: CommandResult
) -> None:
    """명령 하나를 처리하고 온 대화로 답장한다."""
    if str(chat_id) not in set(cfg.telegram_chat_ids):
        # 봇은 공개돼 있어 누구나 말을 걸 수 있다. 등록된 대화만 처리한다.
        log.warning("허가되지 않은 chat_id 의 명령 무시: %s", chat_id)
        result.rejected += 1
        return

    if not text.startswith("/"):
        return

    result.processed += 1
    log.info("명령 처리: %s", text)
    try:
        reply, changed = handle(text, cfg, store, path)
    except Exception as exc:  # noqa: BLE001 - 명령 하나가 전체를 멈추면 안 된다
        log.exception("명령 처리 중 오류")
        reply, changed = "⚠️ 처리 중 오류가 났습니다: {}".format(exc), False

    if changed:
        result.applied += 1
        result.config_changed = True
        reply += chr(10) * 2 + "<i>다음 스윕부터 반영됩니다.</i>"

    # 답장은 명령이 온 대화로 보낸다. 개인방에서 물었는데 단체방에 답이 가면 곤란하다.
    send(cfg, reply, chat_id=str(chat_id))
    result.replies.append(reply)


def run_one(
    cfg: Config, store: Store, chat_id: str, text: str, path: Path | None = None
) -> CommandResult:
    """웹훅이 넘겨준 명령 하나를 처리한다.

    텔레그램은 웹훅과 getUpdates 를 동시에 쓸 수 없다. 웹훅을 걸면 폴링은
    409 를 받으므로, 메시지를 Cloudflare Worker 가 받아 여기로 넘겨준다.
    """
    path = Path(path) if path else DEFAULT_CONFIG
    result = CommandResult()
    if not cfg.telegram_enabled:
        log.info("텔레그램 미설정 — 명령을 처리하지 않습니다")
        return result
    _apply(cfg, store, path, chat_id, text, result)
    return result


def run(cfg: Config, store: Store, path: Path | None = None) -> CommandResult:
    """getUpdates 로 밀린 명령을 가져와 처리한다 (웹훅을 안 쓸 때의 경로)."""
    path = Path(path) if path else DEFAULT_CONFIG
    result = CommandResult()

    if not cfg.telegram_enabled:
        log.info("텔레그램 미설정 — 명령 확인을 건너뜁니다")
        return result

    offset = _read_offset()
    try:
        updates = fetch_updates(cfg, offset)
    except Exception as exc:  # noqa: BLE001
        log.warning("명령 조회 실패: %s", exc)
        return result

    if not updates:
        log.info("새 명령 없음")
        return result

    last_seen = offset
    for update in updates:
        last_seen = update.update_id + 1
        _apply(cfg, store, path, str(update.chat_id), update.text, result)

    _write_offset(last_seen)
    return result
