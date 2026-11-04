/**
 * Conversation transcript view (Phase 8 PR-6).
 *
 * Renders messages oldest-first as alternating-aligned bubbles. Bot
 * messages carry telemetry chips (intent, model, response time, RAG
 * count) tucked underneath so the operator can debug bot behaviour
 * without expanding a panel.
 *
 * Server-renderable — no client state.
 */

import { Badge } from "@/components/ui/badge";
import { formatAbsolute } from "@/lib/format";
import { cn } from "@/lib/utils";

interface MessageDetailDTO {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  intent: string | null;
  model_used: string | null;
  response_time_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  rag_chunk_count: number;
  author_type: "bot" | "human_agent";
  author_name: string | null;
  created_at: string;
}

const ROLE_LABEL: Record<MessageDetailDTO["role"], string> = {
  user: "Customer",
  assistant: "Bot",
  system: "System",
};

/** Label for an assistant-role bubble: a human agent's mirrored Chatwoot reply
 * is shown as the agent's name (or "Agent"), distinct from the "Bot" label. */
function authorLabel(m: MessageDetailDTO): string {
  if (m.role === "assistant" && m.author_type === "human_agent") {
    return m.author_name?.trim() || "Agent";
  }
  return ROLE_LABEL[m.role];
}

export function ConversationTranscript({
  messages,
}: {
  messages: MessageDetailDTO[];
}) {
  if (messages.length === 0) {
    return (
      <div className="rounded-xl border border-dashed bg-muted/20 p-8 text-center text-sm text-muted-foreground">
        No messages on this conversation yet.
      </div>
    );
  }

  return (
    <ol className="space-y-4" aria-label="Conversation transcript">
      {messages.map((m) => (
        <li
          key={m.id}
          className={cn(
            "flex flex-col gap-1",
            m.role === "user" ? "items-start" : "items-end",
          )}
        >
          <div
            className={cn(
              "max-w-[80%] rounded-2xl px-4 py-2.5 text-sm",
              m.role === "user"
                ? "bg-muted text-foreground"
                : m.role === "assistant"
                  ? m.author_type === "human_agent"
                    ? "bg-emerald-600 text-white"
                    : "bg-primary text-primary-foreground"
                  : "border border-dashed bg-card text-muted-foreground",
            )}
          >
            <div className="mb-1 flex items-center gap-2 text-[11px] uppercase tracking-wide opacity-80">
              <span>{authorLabel(m)}</span>
              <span>·</span>
              <time dateTime={m.created_at}>
                {formatAbsolute(m.created_at)}
              </time>
            </div>
            <p className="whitespace-pre-wrap leading-relaxed">{m.content}</p>
          </div>

          {m.role === "assistant" && m.author_type !== "human_agent" ? (
            <div className="flex flex-wrap items-center gap-1.5 pr-1 text-[11px] text-muted-foreground">
              {m.intent ? (
                <Badge variant="outline" className="font-normal capitalize">
                  {m.intent}
                </Badge>
              ) : null}
              {m.model_used ? (
                <Badge variant="outline" className="font-normal">
                  {m.model_used}
                </Badge>
              ) : null}
              {m.response_time_ms !== null ? (
                <span>{m.response_time_ms} ms</span>
              ) : null}
              {m.rag_chunk_count > 0 ? (
                <span>
                  {m.rag_chunk_count} chunk{m.rag_chunk_count === 1 ? "" : "s"}
                </span>
              ) : null}
              {m.input_tokens !== null || m.output_tokens !== null ? (
                <span>
                  in {m.input_tokens ?? "—"} / out {m.output_tokens ?? "—"} tok
                </span>
              ) : null}
            </div>
          ) : null}
        </li>
      ))}
    </ol>
  );
}
