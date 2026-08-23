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

## 설치

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

5. 배포가 알려준 주소(`https://flight-watch.<계정>.workers.dev`)를 텔레그램에 등록한다.

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
