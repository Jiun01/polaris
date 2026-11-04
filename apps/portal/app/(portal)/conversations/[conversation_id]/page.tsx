/**
 * Conversation detail pane — Screen #2 detail (Phase 8 PR-6).
 *
 * Server Component. Fetches `/api/tenant/conversations/{id}` and renders
 * the full message history with role badges, intent tags, model used,
 * response time, and the RAG chunk count.
 *
 * No reply surface — the "Reply in Chatwoot" CTA opens
 * `chat.getpolaris.my` in a new tab. ADR-030 keeps Chatwoot's auth
 * separate from the portal's NextAuth session at GA.
 */

import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft, ExternalLink } from "lucide-react";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { Button } from "@/components/ui/button";
import { ConversationTranscript } from "@/components/conversations/ConversationTranscript";
import { formatAbsolute } from "@/lib/format";

type Channel =
  | "whatsapp"
  | "instagram"
  | "instagram_login"
  | "messenger"
  | "demo"
  | "telegram";

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

interface ConversationDetailDTO {
  id: string;
  tenant_id: string;
  channel: Channel;
  channel_user_id: string;
  customer_display_name: string;
  chatwoot_conversation_id: number | null;
  last_message_at: string | null;
  created_at: string;
  messages: MessageDetailDTO[];
}

const CHANNEL_LABEL: Record<Channel, string> = {
  whatsapp: "WhatsApp",
  instagram: "Instagram",
  instagram_login: "Instagram",
  messenger: "Messenger",
  telegram: "Telegram",
  demo: "Demo",
};

async function loadConversation(
  id: string,
): Promise<ConversationDetailDTO | null> {
  try {
    const res = await apiFetchServer<ConversationDetailDTO>(
      TenantRoutes.conversation(id),
    );
    return res ?? null;
  } catch (err) {
    if (err instanceof PolarisApiError && err.status === 404) {
      return null;
    }
    // eslint-disable-next-line no-console
    console.error(`[portal /conversations/${id}] fetch failed`, err);
    throw err;
  }
}

export default async function ConversationDetailPage({
  params,
}: {
  params: { conversation_id: string };
}) {
  const conversation = await loadConversation(params.conversation_id);
  if (!conversation) {
    notFound();
  }

  const chatwootHref = conversation.chatwoot_conversation_id
    ? `https://chat.getpolaris.my/app/accounts/1/conversations/${conversation.chatwoot_conversation_id}`
    : "https://chat.getpolaris.my";

  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/conversations"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
          All conversations
        </Link>
      </div>

      <header className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {conversation.customer_display_name}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {CHANNEL_LABEL[conversation.channel] ?? conversation.channel} ·{" "}
            <span className="font-mono">{conversation.channel_user_id}</span>
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            Started {formatAbsolute(conversation.created_at)}
            {conversation.last_message_at
              ? ` · last active ${formatAbsolute(conversation.last_message_at)}`
              : ""}
          </p>
        </div>
        <div className="flex flex-col items-start gap-2 sm:items-end">
          <Button asChild>
            <a
              href={chatwootHref}
              target="_blank"
              rel="noreferrer"
              aria-label="Reply to this conversation in your messaging inbox (opens in new tab)"
            >
              Reply in inbox
              <ExternalLink className="ml-1.5 h-3.5 w-3.5" aria-hidden />
            </a>
          </Button>
          <p className="text-[11px] text-muted-foreground sm:text-right">
            Your messaging inbox uses a separate sign-in
          </p>
        </div>
      </header>

      <ConversationTranscript messages={conversation.messages} />
    </div>
  );
}
