// External 24/7 MemeDesk monitor. PAPER desk only. Secrets come from Worker bindings, never logged.
const STALE_MS = 15 * 60 * 1000;
const ALERT_COOLDOWN_MS = 60 * 60 * 1000;
const STATE_KEY = "monitor";

function minutesLabel(ms) {
  const m = Math.max(1, Math.round(ms / 60000));
  return m + " minut" + (m === 1 ? "o" : "i");
}

function publicHb(hb) {
  return {
    ok: !!hb.ok,
    cycle: hb.cycle ?? null,
    ts: hb.ts ?? null,
    source: hb.source ?? null,
    age_s: hb.ageMs != null ? Math.round(hb.ageMs / 1000) : null,
    stale: !!hb.stale,
    desk_alive: hb.desk_alive ?? null,
    reason: hb.reason ?? null,
    last_success_at: hb.last_success_at ?? null,
    success_age_s: hb.successAgeMs != null ? Math.round(hb.successAgeMs / 1000) : null,
    consecutive_cycle_errors: hb.consecutive_cycle_errors ?? null,
    cycles_failing: !!hb.cyclesFailing,
  };
}

async function latestHeartbeat(env) {
  const base = String(env.SUPABASE_URL || "").replace(/\/$/, "");
  if (!base || !env.SUPABASE_ANON_KEY) throw new Error("missing supabase binding");
  const headers = {
    apikey: env.SUPABASE_ANON_KEY,
    Authorization: "Bearer " + env.SUPABASE_ANON_KEY,
  };
  const [cyclesRes, statusRes] = await Promise.all([
    fetch(base + "/rest/v1/cycles?select=cycle,ts&order=ts.desc&limit=1", { headers }),
    fetch(base + "/rest/v1/desk_status?select=cycle,last_cycle_at,heartbeat_at,desk_alive,last_success_at,consecutive_cycle_errors&id=eq.1", { headers }),
  ]);
  if (!cyclesRes.ok) throw new Error("cycles http " + cyclesRes.status);
  if (!statusRes.ok) throw new Error("desk_status http " + statusRes.status);
  const cycles = await cyclesRes.json();
  const statuses = await statusRes.json();
  const c = cycles[0] || {};
  const s = statuses[0] || {};
  const candidates = [
    { source: "cycles.ts", ts: c.ts, cycle: c.cycle },
    { source: "desk_status.last_cycle_at", ts: s.last_cycle_at, cycle: s.cycle },
    { source: "desk_status.heartbeat_at", ts: s.heartbeat_at, cycle: s.cycle },
  ].filter((x) => x.ts);
  if (!candidates.length) {
    return { ok: false, reason: "no timestamps", desk_alive: s.desk_alive ?? null, stale: true };
  }
  candidates.sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime());
  const best = candidates[0];
  const ageMs = Date.now() - new Date(best.ts).getTime();
  // v7: the heartbeat keeps updating while cycles fail, so also check the last SUCCESSFUL cycle.
  const successAgeMs = s.last_success_at ? Date.now() - new Date(s.last_success_at).getTime() : null;
  return {
    last_success_at: s.last_success_at ?? null,
    successAgeMs,
    consecutive_cycle_errors: s.consecutive_cycle_errors ?? null,
    cyclesFailing: successAgeMs != null && successAgeMs > STALE_MS,
    ok: true,
    cycle: best.cycle ?? c.cycle ?? s.cycle ?? null,
    ts: best.ts,
    source: best.source,
    ageMs,
    stale: ageMs > STALE_MS,
    desk_alive: s.desk_alive ?? null,
  };
}

async function sendTelegram(env, text) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) throw new Error("missing telegram binding");
  const res = await fetch("https://api.telegram.org/bot" + env.TELEGRAM_BOT_TOKEN + "/sendMessage", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
  });
  if (!res.ok) throw new Error("telegram http " + res.status);
  const body = await res.json();
  if (!body.ok) throw new Error("telegram rejected");
  return { ok: true, message_id: body.result && body.result.message_id };
}

async function getState(env) {
  const raw = await env.STATE.get(STATE_KEY);
  if (!raw) return { down: false, last_alert_at: 0 };
  try {
    return JSON.parse(raw);
  } catch {
    return { down: false, last_alert_at: 0 };
  }
}

async function putState(env, state) {
  await env.STATE.put(STATE_KEY, JSON.stringify(state));
}

async function runCheck(env, opts) {
  opts = opts || {};
  let hb;
  try {
    hb = await latestHeartbeat(env);
  } catch (err) {
    hb = { ok: false, stale: true, reason: String(err && err.message ? err.message : err) };
  }

  if (opts.testCycleAlert) {
    const sent = await sendTelegram(
      env,
      "🧪 TEST monitor — ⚠️ MemeDesk: nessun ciclo riuscito da 16 minuti (3 errori consecutivi): il desk è acceso ma i cicli falliscono."
    );
    return { action: "test-cycle-alert", telegram: sent, heartbeat: publicHb(hb) };
  }

  if (opts.testAlert) {
    const age = hb.ok ? minutesLabel(hb.ageMs) : "n/d";
    const sent = await sendTelegram(
      env,
      "🧪 MemeDesk: test monitor — controllo esterno attivo. Ultimo ciclo: " +
        (hb.cycle ?? "n/d") +
        " (" +
        age +
        " fa)."
    );
    return { action: "test-alert", telegram: sent, heartbeat: publicHb(hb) };
  }

  const now = Date.now();
  const state = await getState(env);

  if (!hb.ok || hb.stale) {
    const ageLabel = hb.ok ? minutesLabel(hb.ageMs) : (hb.reason ? "errore lettura cicli" : "tempo sconosciuto");
    const shouldAlert = !state.last_alert_at || now - state.last_alert_at >= ALERT_COOLDOWN_MS;
    let sent = null;
    if (shouldAlert) {
      const extra = hb.ok
        ? ""
        : hb.reason
          ? " Dettaglio: " + hb.reason + "."
          : "";
      sent = await sendTelegram(
        env,
        "⚠️ MemeDesk: nessun ciclo da " +
          ageLabel +
          " — VM o desk giù." +
          extra +
          " Per riavviare: Oracle Cloud → Compute → istanze (Milano) → memedesk-vm. Non usare Terminate."
      );
      state.last_alert_at = now;
    }
    state.down = true;
    state.last_seen_ts = hb.ts || state.last_seen_ts || null;
    state.last_cycle = hb.cycle ?? state.last_cycle ?? null;
    state.last_check_at = now;
    await putState(env, state);
    return { action: shouldAlert ? "down-alert" : "down-quiet", telegram: sent, heartbeat: publicHb(hb) };
  }

  if (hb.cyclesFailing) {
    const shouldAlert = !state.last_alert_at || now - state.last_alert_at >= ALERT_COOLDOWN_MS;
    let sent = null;
    if (shouldAlert) {
      sent = await sendTelegram(
        env,
        "⚠️ MemeDesk: nessun ciclo riuscito da " +
          minutesLabel(hb.successAgeMs) +
          (hb.consecutive_cycle_errors != null ? " (" + hb.consecutive_cycle_errors + " errori consecutivi)" : "") +
          ": il desk è acceso ma i cicli falliscono. Il watchdog della VM riavvia da solo; se continua serve un intervento."
      );
      state.last_alert_at = now;
    }
    state.down = true;
    state.last_seen_ts = hb.ts || state.last_seen_ts || null;
    state.last_cycle = hb.cycle ?? state.last_cycle ?? null;
    state.last_check_at = now;
    await putState(env, state);
    return { action: shouldAlert ? "cycles-failing-alert" : "cycles-failing-quiet", telegram: sent, heartbeat: publicHb(hb) };
  }

  const wasDown = !!state.down;
  let sent = null;
  if (wasDown) {
    sent = await sendTelegram(env, "✅ MemeDesk di nuovo online");
  }
  await putState(env, {
    down: false,
    last_alert_at: 0,
    last_seen_ts: hb.ts,
    last_cycle: hb.cycle,
    last_check_at: now,
  });
  return { action: wasDown ? "recovered" : "ok", telegram: sent, heartbeat: publicHb(hb) };
}

function authorized(request, env) {
  const header = request.headers.get("authorization") || "";
  const bearer = header.toLowerCase().startsWith("bearer ") ? header.slice(7).trim() : "";
  const secret = env.MONITOR_TEST_SECRET || "";
  return !!secret && bearer === secret;
}

export default {
  async scheduled(_controller, env) {
    if (!env.SUPABASE_ANON_KEY || !env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
      console.log("memedesk-monitor: worker secrets not configured yet");
      return;
    }
    await runCheck(env, { testAlert: false });
  },
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/run") {
      return new Response("not found", { status: 404 });
    }
    if (request.method !== "GET" && request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }
    if (!authorized(request, env)) {
      return new Response("unauthorized", { status: 401 });
    }
    const mode = url.searchParams.get("mode") || "check";
    try {
      if (mode === "status") {
        const hb = await latestHeartbeat(env);
        const state = await getState(env);
        return Response.json({
          heartbeat: publicHb(hb),
          state: {
            down: !!state.down,
            last_cycle: state.last_cycle ?? null,
            last_seen_ts: state.last_seen_ts ?? null,
            has_last_alert: !!state.last_alert_at,
          },
        });
      }
      const result = await runCheck(env, { testAlert: mode === "test-alert", testCycleAlert: mode === "test-cycle-alert" });
      return Response.json(result);
    } catch (err) {
      return Response.json({ error: String(err && err.message ? err.message : err) }, { status: 500 });
    }
  },
};
