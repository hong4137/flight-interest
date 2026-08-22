"""한국 시간 기준 시계.

GitHub Actions 러너는 UTC 로 돈다. `datetime.now()` 를 그대로 쓰면 개발 PC(KST)와
러너(UTC)에서 다른 값이 나오고, 알림을 읽는 사람은 한국에 있으므로 9시간 어긋난
시각을 보게 된다. 실제로 한국시간 8/23 새벽 2시에 받은 요약에 "8/22" 가 찍혔다.

표시만의 문제가 아니다. SerpApi 일일 한도가 UTC 자정(= 한국시간 오전 9시)에
초기화되어 하루 경계가 어긋난다.

한국은 서머타임이 없으므로 고정 오프셋으로 정확히 표현된다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")


def now() -> datetime:
    """한국 시간. 코드 전반이 naive datetime 을 비교하므로 tzinfo 는 떼서 준다."""
    return datetime.now(KST).replace(tzinfo=None)


def today() -> date:
    """한국 날짜. 일일 예산 경계가 한국 자정에 맞도록."""
    return now().date()
