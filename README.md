# 유럽 비즈니스 특가 감시

2027년 1월 이탈리아·스페인·포르투갈 여행을 위한 **비즈니스 클래스 2장, 1인 300만원 이하(경유 1회 이내)**
항공권을 자동으로 감시하고, 조건이 뜨면 텔레그램으로 즉시 알린다.

날짜(출발 1/2–1/6, 인천 도착 1/16–1/20)와 입·출국 도시가 모두 유연해서 조합이 1,715개다.
이걸 매시간 전수 조회하는 건 불가능하므로 **편도 분해 스크리닝**으로 좁힌다.

---

## 어떻게 1,715개를 84번의 조회로 훑는가

왕복 가격은 편도 두 장 가격의 합보다 항상 싸다. 그래서 편도만 훑으면 모든 조합의 **상한**이 나온다.

```
ICN → 7개 도시 × 5개 출발일   =  35회 조회
7개 도시 → ICN × 7개 귀국일   =  49회 조회
                             ─────────────
                               84회 조회  →  7 × 7 × 5 × 7 = 1,715개 조합의 상한
```

실측한 환산비는 **왕복 ≈ 편도합산 × 0.71** 이고, 이 값은 같은 도시 왕복을 실제로 확인할 때마다
자동 보정된다(`data/state.json` 의 `calibration`).

추정가가 목표의 125% 이내인 조합만 실제 가격을 확인한다.

| 계층 | 주기 | 수단 | 조회 수 |
|---|---|---|---|
| **full** | 4시간 | 편도 84회 + 상위 30개 실확인 | ~114 |
| **hot** | 1시간 | 직전 full 이 남긴 유망 조합만 재확인 | ~12 |
| 오픈조 | 조건부 | SerpApi 멀티시티 | 후보당 1콜 |

### 기준선 (2026-08-23 실측)

| 항목 | 값 |
|---|---|
| 현재 최저 (ICN↔FCO 왕복, 2인) | **약 341만원 / 1인** |
| 목표 300만원까지 | 약 14% |
| 138회 조회 시 실패·차단 | 0건 |

목표 300만원은 현 시세의 약 86% 다. 무리한 값은 아니지만 **프로모나 특가가 떠야 닿는다** —
그래서 이 시스템이 필요하다. `data/history.csv` 가 쌓이면 목표를 유지할지 조정할지 데이터로 판단할 수 있다.

> BLQ·NAP·VLC·AGP 는 인천발/착 비즈니스 경유1회 노선이 양방향 모두 0건이라 제외했다.
> 조건을 넓히려면 `config/search.yaml` 의 주석을 참고해 되살리면 된다.

---

## 설치

```bash
python -m venv .venv
```

```bash
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

> **왜 venv 인가**: fast-flights 3.1.0 은 protobuf ≥ 6 을 요구한다. 전역 환경의 protobuf 를
> 올리면 다른 도구가 깨질 수 있어 프로젝트 전용 환경을 쓴다.

## 시크릿

로컬은 `.env`, GitHub 는 **Settings → Secrets and variables → Actions** 에 넣는다.
코드와 설정에는 절대 넣지 않는다 (레포가 공개이므로).

| 이름 | 필수 | 얻는 법 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | 텔레그램에서 [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | ✅ | 봇에게 아무 메시지나 보낸 뒤 `https://api.telegram.org/bot<토큰>/getUpdates` 의 `chat.id` |
| `SERPAPI_KEY` | 선택 | [serpapi.com](https://serpapi.com/) 무료 가입 (월 250콜). 없으면 오픈조 확인만 건너뛴다 |

## 사용법

```bash
./.venv/Scripts/python.exe -m src.main test-provider --route ICN-FCO --date 2027-01-03
```
스크레이퍼가 살아 있는지 단건으로 확인한다. **문제가 생기면 여기부터 본다.**

```bash
./.venv/Scripts/python.exe -m src.main sweep --mode full --dry-run
```
전체 스윕. `--dry-run` 은 알림도 저장도 하지 않는다. 약 10분 걸린다.

```bash
./.venv/Scripts/python.exe -m src.main sweep --mode hot
```

```bash
./.venv/Scripts/python.exe -m src.main digest --dry-run
```

```bash
./.venv/Scripts/python.exe -m src.main alert-test
```
텔레그램 샘플 알림. 설정이 맞는지 확인한다.

```bash
./.venv/Scripts/python.exe -m src.main serp-test
```
SerpApi 키와 오픈조 조회를 확인한다. **크레딧 1콜을 소모한다.**

## 조건 바꾸기

전부 [`config/search.yaml`](config/search.yaml) 한 곳에 있다. 코드는 건드릴 필요 없다.
날짜, 공항 목록, 목표가, 경유 횟수, 인원, 스윕 주기 모두 여기서 조정한다.

목표가를 바꾸려면:

```yaml
threshold:
  per_person_krw: 3000000   # 1인당. 이 값 이하면 즉시 알림
```

## 자동 실행

`.github/workflows/sweep.yml` 이 매시 7분에 돈다 (UTC 기준 4의 배수 시각엔 full, 나머지는 hot).
`digest.yml` 은 매일 09:00 KST 에 요약을 보낸다.

처음에는 **Actions 탭 → sweep → Run workflow** 로 `full` 을 한 번 수동 실행해서
러너에서도 조회가 되는지, 커밋과 알림이 도는지 확인한다.

---

## 구조

```
src/
  gflights.py         Google Flights 조회·파싱 (핵심)
  sweep.py            3계층 오케스트레이션
  store.py            state.json / history.csv
  alert.py            알림 규칙 + 텔레그램
  config.py           search.yaml 로드·검증
  models.py           Itinerary / Candidate / Deal
  providers/serpapi.py  오픈조 + 사각지대 스캔
data/
  state.json          역대 최저가, 알림 이력, 환산비 표본, SerpApi 예산
  history.csv         관측된 모든 가격 (목표가 재조정의 근거)
```

### fast-flights 를 쿼리 빌더로만 쓰는 이유

`fast_flights.get_flights()` 는 현재 **모든 검색에서 실패한다.** Google 이 "가격 미제공"으로
내려보내는 항목(`k[1][0] == []`)에서 `IndexError` 가 나는데, 라이브러리가 이를 건너뛰지 않고
결과 **전체**를 버리기 때문이다. 실측에서 33건 중 4건이 그런 항목이었고 정상 29건까지 함께 날아갔다.

그래서 protobuf 쿼리 생성(안정적이고 재구현하기 어려운 부분)만 라이브러리에 맡기고,
파싱은 [`src/gflights.py`](src/gflights.py) 에서 직접 한다. 불량 항목은 건너뛴다.

### 알아둘 것

- **왕복 조회 결과의 구간 정보는 가는 편만 담겨 있다.** Google 이 그렇게 내려준다.
  그래서 인천 도착 시각은 편도 귀국편 조회에서 가져온다.
- **가격은 인원수에 따라 달라진다.** 최저 운임 버킷에 2석이 없으면 더 비싼 값이 나온다.
  실측: ICN↔FCO 1인 358만원 vs 2인 418만원. 그래서 항상 실제 인원수로 조회한다.
- **커버리지 한계.** Google Flights 가 소스이므로 한국 OTA 단독 특가(인터파크투어 등)와
  마일리지 발권은 잡히지 않는다.
- **차단 위험.** TLS 지문 위장 + 지터 + 백오프로 완화하지만 완전하지는 않다.
  실패율이 35% 를 넘거나 차단이 감지되면 텔레그램으로 경고가 간다.
  그때는 `SERPAPI_KEY` 를 유료 플랜($25/1,000콜)으로 올리는 게 즉효약이다.
