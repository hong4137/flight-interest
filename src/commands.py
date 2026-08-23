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
/목표 320 — 목표가를 1인 320만원으로 변경
/경유 1 — 경유 최대 횟수 (0=직항만)
/인원 2 — 탑승 인원
/도시 — 현재 감시 중인 도시 목록
/도시 추가 BLQ · /도시 제거 OPO — 도시 추가·제거
/날짜 출발 01-02 01-06 — 인천 출발 가능일
/날짜 도착 01-16 01-20 — 인천 도착 가능일
/도움 — 이 목록

<i>입력창의 자동완성 메뉴는 영문으로만 뜹니다 (텔레그램 제약).
/status /target /stops /pax /city /dates /help 도 똑같이 동작합니다.</i>

<i>상시 서버가 없어 10분마다 확인하는 구조라, 답장까지 보통 10~30분 걸립니다.
조회만 하실 거면 대시보드가 즉시 답을 줍니다 — 명령은 값을 "바꿀" 때 쓰세요.</i>"""


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


def _parse_md(token: str, year: int) -> date:
    """'01-02', '1/2', '0102' 을 날짜로."""
    digits = re.findall(r"\d+", token)
    if len(digits) == 2:
        month, day = int(digits[0]), int(digits[1])
    elif len(digits) == 1 and len(digits[0]) == 4:
        month, day = int(digits[0][:2]), int(digits[0][2:])
    else:
        raise ValueError("날짜 형식을 알 수 없습니다: {}".format(token))
    return date(year, month, day)


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


def handle(text: str, cfg: Config, store: Store, path: Path) -> tuple[str, bool]:
    """(답장, 설정이 바뀌었는지).

    사용자 입력으로 예외가 새어나가지 않게 한다. 잘못된 명령은 설명을 담은
    답장이 되어야지, 워크플로를 실패시켜서는 안 된다.
    """
    try:
        return _dispatch(text, cfg, store, path)
    except ValueError as exc:
        return "⚠️ {}".format(exc), False


def _dispatch(text: str, cfg: Config, store: Store, path: Path) -> tuple[str, bool]:
    parts = text.split()
    raw = parts[0].lstrip("/").split("@")[0].lower()
    args = parts[1:]

    if raw in {"help", "도움", "명령", "start"}:
        return help_text(cfg), False

    if raw in {"status", "상태"}:
        return _status(cfg, store), False

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


def _handle_dates(args: list[str], cfg: Config, path: Path) -> tuple[str, bool]:
    if len(args) < 3:
        return (
            "예: <code>/날짜 출발 01-02 01-06</code>\n"
            "    <code>/날짜 도착 01-16 01-20</code>"
        ), False

    which = args[0].lower()
    year = cfg.outbound_dates[0].year
    try:
        start, end = _parse_md(args[1], year), _parse_md(args[2], year)
    except ValueError as exc:
        return str(exc), False
    if end < start:
        return "종료일이 시작일보다 빠릅니다.", False

    if which in {"out", "출발", "가는날"}:
        key, label = "outbound_dates", "인천 출발"
    elif which in {"in", "도착", "오는날", "귀국"}:
        key, label = "arrive_korea", "인천 도착"
    else:
        return "출발 또는 도착 중에 골라주세요.", False

    def mutate(doc):
        doc["trip"][key]["start"] = start
        doc["trip"][key]["end"] = end
        return ""

    _edit(path, mutate)
    return "{} 가능일을 <b>{} ~ {}</b> 로 바꿨습니다.".format(label, start, end), True


# ── 진입점 ───────────────────────────────────────────────────

def run(cfg: Config, store: Store, path: Path | None = None) -> CommandResult:
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

    authorized = set(cfg.telegram_chat_ids)
    last_seen = offset
    for update in updates:
        last_seen = update.update_id + 1

        # 봇은 공개돼 있어 누구나 말을 걸 수 있다. 등록된 대화만 처리한다.
        if str(update.chat_id) not in authorized:
            log.warning("허가되지 않은 chat_id 의 명령 무시: %s", update.chat_id)
            result.rejected += 1
            continue

        if not update.text.startswith("/"):
            continue

        result.processed += 1
        log.info("명령 처리: %s", update.text)
        try:
            reply, changed = handle(update.text, cfg, store, path)
        except Exception as exc:  # noqa: BLE001 - 명령 하나가 전체를 멈추면 안 된다
            log.exception("명령 처리 중 오류")
            reply, changed = "⚠️ 처리 중 오류가 났습니다: {}".format(exc), False

        if changed:
            result.applied += 1
            result.config_changed = True
            reply += "\n\n<i>다음 스윕부터 반영됩니다.</i>"

        # 답장은 명령이 온 대화로 보낸다. 개인방에서 물었는데 단체방에
        # 답이 가면 곤란하다.
        send(cfg, reply, chat_id=str(update.chat_id))
        result.replies.append(reply)

    _write_offset(last_seen)
    return result
