# 텔레그램 웹훅 (Cloudflare Worker)

GitHub Actions 스케줄러만 쓰면 명령 반영에 20~100분이 걸린다 (실측값이다 —
`*/10` 으로 걸어도 실제 실행 간격이 21·44·99분이었다). 이 Worker 가 텔레그램
메시지를 즉시 받아, 조회는 바로 답하고 변경은 GitHub 으로 넘긴다.

```
텔레그램 ──웹훅──> Worker ──┬─ /상태 /도움  → 즉시 답장 (1초)
                            └─ 나머지        → repository_dispatch → Actions (1~2분)
```

## 왜 폴링을 껐는가

텔레그램은 **웹훅과 getUpdates 를 동시에 쓸 수 없다.** 웹훅을 걸면 폴링은
`409 Conflict` 를 받는다. 그래서 `command.yml` 의 `schedule` 을 제거했다.

## 설치 — 대시보드 (계정이 이미 있으면 이쪽이 쉽다)

파일 하나에 의존성이 없으므로 붙여넣기만 하면 된다. wrangler 도 npm 도 필요 없다.

1. **Workers & Pages → Create → Worker** → 이름 `flight-watch` → Deploy
2. **Edit code** 를 눌러 [`worker.js`](worker.js) 내용을 통째로 붙여넣고 Deploy
3. **Settings → Variables and Secrets** 에서 값을 넣는다

   암호화(Secret)로 넣을 것 — 대시보드에서 다시 볼 수 없다:

   | 이름 | 값 |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | @BotFather 가 준 봇 토큰 |
   | `TELEGRAM_SECRET` | 아무 문자열 (아래 setWebhook 에서 같은 값을 쓴다) |
   | `GITHUB_TOKEN` | 아래 2번에서 만드는 PAT |

   평문(Text)으로 넣을 것 — 공개돼도 되는 값이다:

   | 이름 | 값 |
   |---|---|
   | `ALLOWED_CHATS` | `-5590845708` |
   | `GITHUB_REPO` | `hong4137/flight-interest` |
   | `DASHBOARD_URL` | `https://hong4137.github.io/flight-interest/` |

4. 아래 "웹훅 등록" 으로 넘어간다. `wrangler.toml` 은 CLI 로 배포할 때만 쓰인다.

## 설치 — CLI

1. **Cloudflare 계정**을 만들고 (무료) `npm i -g wrangler` 후 `wrangler login`.

2. **GitHub 토큰 발급** — Settings → Developer settings → Personal access tokens →
   Fine-grained tokens. 이 레포만 선택하고 **Repository permissions → Contents:
   Read and write**. (`repository_dispatch` 에 필요한 권한이다. 403 이 나면 응답의
   `X-Accepted-GitHub-Permissions` 헤더가 필요한 권한을 알려준다.)

3. **웹훅 비밀값**을 아무 문자열로 정한다 (예: `openssl rand -hex 16`).

4. 시크릿을 넣고 배포한다.

```bash
cd worker
wrangler secret put TELEGRAM_BOT_TOKEN
wrangler secret put TELEGRAM_SECRET
wrangler secret put GITHUB_TOKEN
wrangler deploy
```

## 웹훅 등록

배포 주소(`https://flight-watch.<계정>.workers.dev`)를 텔레그램에 알려준다.

```bash
curl -X POST "https://api.telegram.org/bot<봇토큰>/setWebhook" \
  -H "content-type: application/json" \
  -d '{"url":"https://flight-watch.<계정>.workers.dev","secret_token":"<위에서 정한 비밀값>","allowed_updates":["message"]}'
```

6. 확인:

```bash
curl "https://api.telegram.org/bot<봇토큰>/getWebhookInfo"
```

`pending_update_count` 가 계속 쌓이거나 `last_error_message` 가 있으면 Worker 가
실패하고 있는 것이다. `wrangler tail` 로 로그를 본다.

## 문제가 생기면

먼저 진단 엔드포인트를 본다. 값은 돌려주지 않고 상태만 알려준다.

```bash
curl -H "X-Telegram-Bot-Api-Secret-Token: <비밀값>" "https://flight-watch.<계정>.workers.dev/diag"
```

`github_token.repo_조회` 가 200 이 아니면 토큰 문제다. 401 이면 값이 잘못됐거나
앞뒤 공백이 붙은 것이고(`앞뒤공백` 항목을 본다), 403 이면 권한이 모자란 것이다
(`필요권한` 항목이 무엇이 필요한지 알려준다).

> ⚠️ **대시보드로 시크릿을 넣으면 즉시 반영되지 않는다.** 이미 돌고 있는 Worker
> 인스턴스가 옛 환경을 들고 있어서 401 이 난다. `wrangler deploy` 로 한 번
> 재배포하면 새 값을 집는다. 실제로 이것 때문에 한참 헤맸다.

## 되돌리기

```bash
curl -X POST "https://api.telegram.org/bot<봇토큰>/deleteWebhook"
```

그리고 `.github/workflows/command.yml` 의 `schedule` 을 되살리면 폴링 방식으로
돌아간다. 코드는 두 방식을 모두 지원한다 (`commands` 는 `--text` 가 있으면 단건,
없으면 폴링).

## 보안

- 주소는 공개되지만 `X-Telegram-Bot-Api-Secret-Token` 헤더를 검사한다.
- `ALLOWED_CHATS` 에 없는 대화는 무시한다. 봇이 공개돼 있어 누구나 말을 걸 수 있다.
- GitHub 토큰은 Worker 시크릿(암호화)에만 있고 브라우저나 레포에는 없다.
  이게 GitHub Pages 에 편집 UI 를 만들지 않은 이유다 — 정적 페이지는 토큰을
  브라우저에 둘 수밖에 없다.
