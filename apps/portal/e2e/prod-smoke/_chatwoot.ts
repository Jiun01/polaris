/**
 * Shared Chatwoot helper for prod-smoke specs — faithfully mirrors
 * ../docs/internal-ops/ops/e2e/bot_reply/run_bot_e2e.py's Chatwoot class (same API calls, same
 * message_type semantics) so a real inbound message can be injected into the
 * designated safe-to-test conversation (the gateway tenant's own channel,
 * configured via CW_ACCOUNT_ID / CW_CONVERSATION_ID — see
 * ../docs/internal-ops/ops/e2e/bot_reply/README.md) and the bot's reply polled for.
 *
 * Uses plain fetch (not Playwright's `request` fixture) — this call goes
 * directly to Chatwoot with an api_access_token header, not through the
 * portal's session-cookie proxy, so it has nothing to do with browser auth.
 */

const CHATWOOT_BASE = (
  process.env.CHATWOOT_BASE_URL ?? "https://chat.getpolaris.my"
).replace(/\/$/, "");
const CHATWOOT_TOKEN = process.env.CHATWOOT_AGENT_API_TOKEN ?? "";
const CW_ACCOUNT_ID = process.env.CW_ACCOUNT_ID ?? "1";
const CW_CONVERSATION_ID = process.env.CW_CONVERSATION_ID ?? "16";

function convUrl(suffix = ""): string {
  return `${CHATWOOT_BASE}/api/v1/accounts/${CW_ACCOUNT_ID}/conversations/${CW_CONVERSATION_ID}${suffix}`;
}

function headers(): Record<string, string> {
  if (!CHATWOOT_TOKEN) {
    throw new Error(
      "CHATWOOT_AGENT_API_TOKEN is not set — required to inject a test message " +
        "into the prod conv-16 Telegram conversation. See e2e/prod-smoke/README.md.",
    );
  }
  return {
    api_access_token: CHATWOOT_TOKEN,
    "Content-Type": "application/json",
  };
}

/** Bot only replies when the conversation is bot-owned (status=pending). */
export async function setPending(): Promise<void> {
  const res = await fetch(convUrl("/toggle_status"), {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ status: "pending" }),
  });
  if (!res.ok) {
    throw new Error(`toggle_status failed ${res.status}: ${await res.text()}`);
  }
}

/**
 * Post a fake incoming customer message. message_type MUST be the integer 0
 * (incoming) — Chatwoot rejects the string "incoming" on non-API inboxes but
 * accepts the integer enum on a Telegram inbox, which is exactly what lets
 * this fake a customer inbound. Returns the new message id.
 */
export async function sendIncoming(content: string): Promise<number> {
  const res = await fetch(convUrl("/messages"), {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ content, message_type: 0, private: false }),
  });
  if (!res.ok) {
    throw new Error(`send_incoming failed ${res.status}: ${await res.text()}`);
  }
  const body = await res.json();
  return Number(body.id);
}

interface ChatwootMessage {
  id: number;
  message_type: number | string;
  private?: boolean;
  content?: string;
}

async function listMessages(): Promise<ChatwootMessage[]> {
  const res = await fetch(convUrl("/messages"), { headers: headers() });
  if (!res.ok) {
    throw new Error(`list_messages failed ${res.status}: ${await res.text()}`);
  }
  const body = await res.json();
  const payload = Array.isArray(body) ? body : (body.payload ?? body);
  return Array.isArray(payload) ? payload : [];
}

function isBotReply(m: ChatwootMessage): boolean {
  // message_type: 0=incoming, 1=outgoing, 2=activity, 3=template
  const mtInt =
    typeof m.message_type === "number"
      ? m.message_type
      : ({ incoming: 0, outgoing: 1 }[String(m.message_type)] ?? -1);
  return mtInt === 1 && !m.private;
}

/**
 * Poll for the bot's reply after `afterId`. Returns the message content, or
 * null on timeout (defaults mirror the Python harness's 45s/1.5s).
 */
export async function waitForReply(
  afterId: number,
  { timeoutMs = 45_000, intervalMs = 1_500 } = {},
): Promise<string | null> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const messages = [...(await listMessages())].sort((a, b) => a.id - b.id);
    for (const m of messages) {
      if (m.id > afterId && isBotReply(m) && (m.content ?? "").trim()) {
        return m.content ?? "";
      }
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return null;
}
