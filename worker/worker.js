/**
 * 텔레그램 웹훅 수신기 (Cloudflare Worker).
 *
 * GitHub Actions 만으로는 명령 반영에 20~100분이 걸린다. 스케줄러가 부하에
 * 따라 크게 밀리기 때문이다. 이 Worker 가 텔레그램 메시지를 즉시 받아
 *
 *   - 조회 명령(/상태, /도움)은 여기서 바로 답한다 (1초 이내)
 *   - 값을 바꾸는 명령은 GitHub repository_dispatch 로 넘긴다 (1~2분)
 *
 * 주의: 텔레그램은 웹훅과 getUpdates 를 동시에 쓸 수 없다. 웹훅을 걸면
 * command 워크플로의 폴링은 409 를 받으므로 스케줄을 꺼야 한다.
 * 되돌리려면 deleteWebhook 후 스케줄을 되살린다. worker/README.md 참고.
 *
 * 필요한 시크릿 (wrangler secret put):
 *   TELEGRAM_BOT_TOKEN   봇 토큰
 *   TELEGRAM_SECRET      setWebhook 의 secret_token 과 같은 값
 *   GITHUB_TOKEN         fine-grained PAT, 이 레포에 Contents: read+write
 * 변수 (wrangler.toml):
 *   ALLOWED_CHATS, GITHUB_REPO, DASHBOARD_URL
 */

const READ_ONLY = new Set(["상태", "status", "도움", "help", "명령", "start"]);

const HELP = `<b>사용할 수 있는 명령</b>

/상태 — 현재 최저가와 목표까지 남은 금액
/목표 320 — 목표가를 1인 320만원으로 변경
/경유 1 — 경유 최대 횟수 (0=직항만)
/인원 2 — 탑승 인원
/도시 — 감시 중인 도시 목록
/도시 추가 FCO · /도시 제거 OPO — 도시 추가·제거
/날짜 출발 2026-12-29 2027-01-01 — 인천 출발 가능일
/날짜 도착 2027-01-10 2027-01-13 — 인천 도착 가능일
/로직 — 무엇을 어떻게 찾고 있는지 설명
/도움 — 이 목록

<i>/상태 와 /도움 은 즉시, 나머지는 20초쯤 걸립니다.</i>`;

const won = (v) =>
  v >= 1e6 ? `${Math.round(v / 1e4).toLocaleString()}만원` : `${(v / 1e4).toFixed(1)}만원`;

async function telegram(env, method, payload) {
  const res = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  return res.ok;
}

const reply = (env, chatId, text) =>
  telegram(env, "sendMessage", {
    chat_id: chatId,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  });

/** 레포에 커밋된 상태 파일을 읽어 현황을 만든다. */
async function statusText(env) {
  const url = `https://raw.githubusercontent.com/${env.GITHUB_REPO}/main/data/state.json`;
  const res = await fetch(url, { cf: { cacheTtl: 30 } });
  if (!res.ok) return "상태 파일을 읽지 못했습니다. 대시보드를 확인해주세요.";

  const s = await res.json();
  const target = s.target_per_person || 3000000;
  const lines = ["<b>현재 상태</b>", ""];

  if (s.global_best) {
    const gap = s.global_best.price - target;
    lines.push(`최저 <b>${won(s.global_best.price)}</b> / 1인`);
    lines.push(
      gap <= 0
        ? `🎯 목표 ${won(target)} 달성`
        : `목표 ${won(target)} 까지 ${won(gap)} 남음`,
    );
  } else {
    lines.push("아직 확인된 가격이 없습니다.");
    lines.push("<i>조건을 바꾼 직후라면 다음 스윕까지 비어 있습니다.</i>");
  }

  const ow = s.outbound_window || [];
  const aw = s.arrive_window || [];
  lines.push("");
  lines.push(`감시 도시: ${(s.airports || []).join(", ") || "—"}`);
  if (ow.length && aw.length) {
    lines.push(`출발 ${ow[0]} ~ ${ow[1]} · 인천 도착 ${aw[0]} ~ ${aw[1]}`);
  }
  lines.push(`${s.passengers ?? "?"}명 · ${s.cabin ?? "?"} · 경유 ${s.max_stops ?? "?"}회 이하`);

  const last = s.last_sweep;
  if (last) {
    const pct = Math.round((last.fail_rate ?? 0) * 100);
    lines.push("");
    lines.push(
      `<i>마지막 스윕 ${(last.at || "").slice(5, 16)} · 조회 ${last.queries ?? 0}건 · 실패율 ${pct}%</i>`,
    );
  }
  if (env.DASHBOARD_URL) {
    lines.push("", `<a href="${env.DASHBOARD_URL}">📊 대시보드 열기</a>`);
  }
  return lines.join("\n");
}

/** 값을 바꾸는 명령은 GitHub Actions 로 넘긴다. */
async function dispatch(env, chatId, text) {
  const res = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
    {
      method: "POST",
      headers: {
        // 붙여넣을 때 앞뒤 공백·개행이 섞이면 GitHub 이 401 을 준다.
        authorization: `Bearer ${(env.GITHUB_TOKEN || "").trim()}`,
        accept: "application/vnd.github+json",
        "content-type": "application/json",
        "user-agent": "flight-watch-worker",
      },
      body: JSON.stringify({
        event_type: "telegram-command",
        client_payload: { chat_id: String(chatId), text },
      }),
    },
  );
  if (!res.ok) {
    console.error("dispatch 실패", res.status, await res.text());
  }
  return res.ok;
}

/**
 * 설정이 제대로 들어갔는지 확인한다. 값은 절대 돌려주지 않고 상태만 알린다.
 * 비밀 헤더가 있어야 접근할 수 있다.
 *
 *   curl -H "X-Telegram-Bot-Api-Secret-Token: <비밀값>" "<주소>/diag"
 */
async function diagnose(env) {
  const raw = env.GITHUB_TOKEN || "";
  const token = raw.trim();
  const out = {
    github_token: {
      설정됨: Boolean(raw),
      길이: raw.length,
      앞머리: token.slice(0, 4),
      앞뒤공백: raw !== token,
    },
    vars: {
      ALLOWED_CHATS: env.ALLOWED_CHATS || null,
      GITHUB_REPO: env.GITHUB_REPO || null,
      TELEGRAM_BOT_TOKEN: Boolean(env.TELEGRAM_BOT_TOKEN),
      TELEGRAM_SECRET: Boolean(env.TELEGRAM_SECRET),
    },
  };

  if (token) {
    const head = { authorization: `Bearer ${token}`, "user-agent": "flight-watch-worker" };
    const repo = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}`, { headers: head });
    out.github_token.repo_조회 = repo.status;
    out.github_token.필요권한 = repo.headers.get("x-accepted-github-permissions") || "-";
    if (!repo.ok) out.github_token.오류 = (await repo.text()).slice(0, 160);
  }
  return Response.json(out, { headers: { "cache-control": "no-store" } });
}

export default {
  async fetch(request, env) {
    const isDiag = new URL(request.url).pathname === "/diag";

    if (request.method !== "POST" && !isDiag) {
      return new Response("flight-watch webhook", { status: 200 });
    }

    // 이 주소는 공개다. 텔레그램이 보낸 것인지 헤더로 확인한다.
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TELEGRAM_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    if (isDiag) return diagnose(env);

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad request", { status: 400 });
    }

    const message = update.message || update.edited_message;
    const text = (message?.text || "").trim();
    const chatId = message?.chat?.id;

    // 200 을 돌려주지 않으면 텔레그램이 계속 재전송한다. 처리할 게 없어도 OK.
    if (!text || chatId === undefined) return new Response("ok");

    const allowed = (env.ALLOWED_CHATS || "").split(",").map((s) => s.trim());
    if (!allowed.includes(String(chatId))) {
      console.warn("허가되지 않은 chat:", chatId);
      return new Response("ok");
    }
    if (!text.startsWith("/")) return new Response("ok");

    const name = text.slice(1).split(/[\s@]/)[0].toLowerCase();

    if (READ_ONLY.has(name)) {
      const body = name === "상태" || name === "status" ? await statusText(env) : HELP;
      await reply(env, chatId, body);
      return new Response("ok");
    }

    const ok = await dispatch(env, chatId, text);
    await reply(
      env,
      chatId,
      ok
        ? "⏳ 처리 중입니다. 20초쯤 걸려요."
        : "⚠️ GitHub 에 전달하지 못했습니다. 토큰이나 권한을 확인해주세요.",
    );
    return new Response("ok");
  },
};
