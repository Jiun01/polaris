"use client";

/**
 * AI Agent screen form.
 *
 * Lifted out of Settings so the bot's brain lives on its own surface:
 *
 *   1. PersonaSection   — the persona BLOCK EDITOR. Instead of one raw-XML
 *                         textarea, the persona is a grid of named blocks
 *                         (display name + body). Each block's display name maps
 *                         to a snake_case slug shown as a pill; that slug is the
 *                         XML tag the model sees (<product_guidance>…). The grid
 *                         is seeded with the built-in default blocks so it is
 *                         never blank. A "Using default / Customized" badge and a
 *                         "Reset to defaults" control make the inherit-vs-override
 *                         state explicit.
 *   2. AgentSection     — enable the tool-calling agent + tool toggles. Custom
 *                         guidance now lives in the persona blocks above (one
 *                         place, both reply paths), so there is no separate
 *                         instructions box.
 *   3. GuardrailsSection — read-only cards for the non-overridable safety /
 *                         format / tool / RAG layers the bot is always given.
 *
 * All saves go through the shared PATCH /api/tenant/settings helper.
 */

import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "sonner";
import {
  MAX_PERSONA_BLOCKS,
  RESERVED_BLOCK_SLUGS,
  patchSettings,
  personaBlocksSchema,
  personaToneSchema,
  SectionShell,
  slugifyBlockName,
  type PersonaBlockValue,
  type SettingsInitialValues,
} from "@/components/settings/SettingsForm";
import type { z } from "zod";

// --------------------------------------------------------------------
// Section: Bot persona — block editor
// --------------------------------------------------------------------

// Starter blocks offered under "+ Add block". Mirrors RECOMMENDED_PERSONA_BLOCKS
// in the Python SSOT (packages/shared-types/.../agent_config.py); the portal
// hand-mirrors persona constants (it doesn't import the generated TS at GA).
const RECOMMENDED_BLOCKS: readonly PersonaBlockValue[] = [
  {
    display_name: "Product Guidance",
    slug: "product_guidance",
    body: "How to talk about our products: highlight the key benefits and help customers pick the right option. Never quote a price that isn't in the knowledge base.",
  },
  {
    display_name: "Promotions",
    slug: "promotions",
    body: "Mention current promotions only when they're relevant to what the customer is asking about. Never invent a discount or offer.",
  },
  {
    display_name: "FAQ Handling",
    slug: "faq",
    body: "For common questions, answer directly and concisely using the knowledge base. Keep it short and conversational.",
  },
  {
    display_name: "Shipping & Delivery",
    slug: "shipping",
    body: "Explain shipping options and timing only from documented policy. Never promise a specific delivery date or stock level.",
  },
  {
    display_name: "Returns & Refunds",
    slug: "returns",
    body: "Walk customers through the documented returns process. For complex or undocumented refund approvals, hand off to a human.",
  },
];

// React-state row: a persona block plus a stable client key and an "isNew" flag.
// New blocks auto-derive their slug from the display name as the user types;
// server-loaded blocks keep their slug stable on rename (don't break the stored
// tag / the tenant's mental model). The key + flag are stripped before saving.
interface EditorBlock extends PersonaBlockValue {
  key: string;
  isNew: boolean;
}

// Order-sensitive equality — drives the "Using default" badge. Safe because the
// editor seeds blocks in the server's order and never reorders them (no reorder
// UI yet), so a tenant on the defaults always compares equal element-by-element.
function blocksEqual(
  a: readonly PersonaBlockValue[],
  b: readonly PersonaBlockValue[],
): boolean {
  if (a.length !== b.length) return false;
  return a.every(
    (x, i) =>
      x.display_name === b[i].display_name &&
      x.slug === b[i].slug &&
      x.body === b[i].body,
  );
}

function uniqueSlug(base: string, taken: Set<string>): string {
  if (!taken.has(base) && !RESERVED_BLOCK_SLUGS.has(base)) return base;
  let n = 2;
  while (
    taken.has(`${base}_${n}`) ||
    RESERVED_BLOCK_SLUGS.has(`${base}_${n}`)
  ) {
    n += 1;
  }
  return `${base}_${n}`;
}

function PersonaSection({ initial }: { initial: SettingsInitialValues }) {
  // Seed the editor with the tenant's saved blocks, else the built-in defaults —
  // so the grid shows the effective persona and is never empty.
  const seedBlocks = initial.persona_blocks.length
    ? initial.persona_blocks
    : initial.default_persona_blocks;

  const keyRef = React.useRef(0);
  const nextKey = () => `b${keyRef.current++}`;

  const toEditor = React.useCallback(
    (rows: readonly PersonaBlockValue[]): EditorBlock[] =>
      rows.map((b) => ({ ...b, key: nextKey(), isNew: false })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const [blocks, setBlocks] = React.useState<EditorBlock[]>(() =>
    toEditor(seedBlocks),
  );
  const [tone, setTone] = React.useState<z.infer<typeof personaToneSchema>>(
    initial.persona_tone,
  );
  const [dirty, setDirty] = React.useState(false);
  const [saving, setSaving] = React.useState(false);

  const stripped = blocks.map(({ display_name, slug, body }) => ({
    display_name,
    slug,
    body,
  }));
  const usingDefault = blocksEqual(stripped, initial.default_persona_blocks);
  const atMax = blocks.length >= MAX_PERSONA_BLOCKS;

  const updateBlock = (key: string, patch: Partial<PersonaBlockValue>) => {
    setBlocks((prev) =>
      prev.map((b) => {
        if (b.key !== key) return b;
        const next = { ...b, ...patch };
        // New blocks: keep the slug in lockstep with the display name until saved.
        if (b.isNew && patch.display_name !== undefined) {
          const taken = new Set(
            prev.filter((o) => o.key !== key).map((o) => o.slug),
          );
          next.slug = uniqueSlug(slugifyBlockName(patch.display_name), taken);
        }
        return next;
      }),
    );
    setDirty(true);
  };

  const removeBlock = (key: string) => {
    setBlocks((prev) => prev.filter((b) => b.key !== key));
    setDirty(true);
  };

  const addBlock = (template: PersonaBlockValue) => {
    setBlocks((prev) => {
      const taken = new Set(prev.map((b) => b.slug));
      return [
        ...prev,
        {
          key: nextKey(),
          isNew: true,
          display_name: template.display_name,
          slug: uniqueSlug(
            slugifyBlockName(template.slug || template.display_name),
            taken,
          ),
          body: template.body,
        },
      ];
    });
    setDirty(true);
  };

  const resetToDefault = () => {
    setBlocks(toEditor(initial.default_persona_blocks));
    setDirty(true);
  };

  const onSave = async () => {
    const parsed = personaBlocksSchema.safeParse(stripped);
    if (!parsed.success) {
      toast.error(parsed.error.issues[0]?.message ?? "Check your blocks");
      return;
    }
    setSaving(true);
    try {
      await patchSettings({ persona_blocks: parsed.data, persona_tone: tone });
      setDirty(false);
      // Saved blocks are now server-persisted: clear isNew so a later rename
      // keeps the slug stable (the just-saved slug must not re-derive).
      setBlocks((prev) => prev.map((b) => ({ ...b, isNew: false })));
    } catch {
      // patchSettings already toasted; swallow so the button resets.
    } finally {
      setSaving(false);
    }
  };

  // Recommended templates not already present (matched by slug).
  const presentSlugs = new Set(blocks.map((b) => b.slug));
  const availableRecommended = RECOMMENDED_BLOCKS.filter(
    (b) => !presentSlugs.has(b.slug),
  );

  return (
    <SectionShell
      title="Bot persona"
      description="Build your bot's brain from blocks for its identity, voice, and answering rules. Add the sections you need; each one becomes part of every reply, whether or not the tool-calling agent is on."
    >
      <div className="space-y-4">
        <div className="flex items-center justify-between gap-2">
          <p className="text-sm font-medium">Persona blocks</p>
          <span
            className={
              usingDefault
                ? "rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                : "rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary"
            }
          >
            {usingDefault ? "Using default" : "Customized"}
          </span>
        </div>

        {blocks.length === 0 ? (
          <p className="rounded-md border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
            No blocks yet. Your bot will use the built-in default persona. Add a
            block or reset to defaults to customize.
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {blocks.map((block) => (
              <Card key={block.key} className="border bg-background/60">
                <CardContent className="space-y-2 p-4">
                  <div className="flex items-center gap-2">
                    <Input
                      aria-label={`Name for block "${block.display_name || block.slug}"`}
                      value={block.display_name}
                      maxLength={60}
                      placeholder="e.g. Product Guidance"
                      className="h-8 font-medium"
                      onChange={(e) =>
                        updateBlock(block.key, { display_name: e.target.value })
                      }
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="shrink-0 text-muted-foreground hover:text-destructive"
                      onClick={() => removeBlock(block.key)}
                    >
                      Remove
                    </Button>
                  </div>
                  <Badge
                    variant="secondary"
                    className="font-mono text-[10px] font-normal"
                    title="This is the tag used internally, derived from the block name."
                  >
                    {block.slug}
                  </Badge>
                  <Textarea
                    aria-label={`Content for block "${block.display_name || block.slug}"`}
                    rows={5}
                    maxLength={2000}
                    value={block.body}
                    placeholder="Describe how the bot should behave for this section…"
                    className="text-sm"
                    onChange={(e) =>
                      updateBlock(block.key, { body: e.target.value })
                    }
                  />
                </CardContent>
              </Card>
            ))}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={atMax}
              >
                + Add block
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-56">
              <DropdownMenuLabel>Recommended</DropdownMenuLabel>
              {availableRecommended.length === 0 ? (
                <DropdownMenuItem disabled>All added</DropdownMenuItem>
              ) : (
                availableRecommended.map((b) => (
                  <DropdownMenuItem key={b.slug} onSelect={() => addBlock(b)}>
                    {b.display_name}
                  </DropdownMenuItem>
                ))
              )}
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onSelect={() =>
                  addBlock({
                    display_name: "New section",
                    slug: "new_section",
                    body: "",
                  })
                }
              >
                Custom block…
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          {atMax ? (
            <span className="text-xs text-muted-foreground">
              Up to {MAX_PERSONA_BLOCKS} blocks.
            </span>
          ) : null}
        </div>

        <div className="max-w-xs space-y-1 pt-2">
          <label htmlFor="persona-tone" className="text-sm font-medium">
            Tone
          </label>
          <Select
            value={tone}
            onValueChange={(v) => {
              // Parse rather than cast: ignore any value not in the schema so a
              // mistyped/added SelectItem can't push an invalid tone into state.
              const parsed = personaToneSchema.safeParse(v);
              if (!parsed.success) return;
              setTone(parsed.data);
              setDirty(true);
            }}
          >
            <SelectTrigger id="persona-tone">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="professional">Professional</SelectItem>
              <SelectItem value="friendly">Friendly</SelectItem>
              <SelectItem value="casual">Casual</SelectItem>
              <SelectItem value="formal">Formal</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="flex items-center gap-3 pt-1">
          <Button type="button" onClick={onSave} disabled={!dirty || saving}>
            {saving ? "Saving…" : "Save persona"}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={resetToDefault}
            disabled={usingDefault}
          >
            Reset to defaults
          </Button>
        </div>
      </div>
    </SectionShell>
  );
}

// --------------------------------------------------------------------
// Section: Tool-calling agent (ADR-063) — enable + tools
// --------------------------------------------------------------------

function AgentSection({ initial }: { initial: SettingsInitialValues }) {
  const [enabled, setEnabled] = React.useState(initial.agent_config.enabled);
  const [tools, setTools] = React.useState<string[]>(
    initial.agent_config.enabled_tools,
  );
  const [dirty, setDirty] = React.useState(false);
  const [saving, setSaving] = React.useState(false);

  const toggleTool = (id: string) => {
    setTools((prev) =>
      prev.includes(id) ? prev.filter((t) => t !== id) : [...prev, id],
    );
    setDirty(true);
  };

  const onSave = async () => {
    setSaving(true);
    try {
      await patchSettings({
        agent_config: {
          enabled,
          // Preserve any legacy per-tenant instructions — the field is no longer
          // edited here (custom guidance lives in the persona blocks), but a save
          // must never silently wipe a value an older tenant set.
          instructions: initial.agent_config.instructions ?? null,
          enabled_tools: tools,
          max_tool_iterations: initial.agent_config.max_tool_iterations,
        },
      });
      setDirty(false);
    } catch {
      // patchSettings already toasted; swallow so the button resets.
    } finally {
      setSaving(false);
    }
  };

  return (
    <SectionShell
      title="Tool-calling agent"
      description="Let your bot use tools (search your knowledge base, look up products, capture leads, or hand off to a human) and decide what to do per message. When off, your bot uses the standard reply pipeline. To customize what it says, edit the Bot persona blocks above."
    >
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => {
            setEnabled(e.target.checked);
            setDirty(true);
          }}
          className="mt-0.5 h-4 w-4 rounded border-input"
        />
        <span>Enable the AI agent for this workspace.</span>
      </label>

      <div className="mt-4 space-y-2">
        <p className="text-sm font-medium">Tools the agent can use</p>
        {initial.agent_tool_catalog.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No tools available yet.
          </p>
        ) : (
          initial.agent_tool_catalog.map((tool) => (
            <label key={tool.id} className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={tools.includes(tool.id)}
                onChange={() => toggleTool(tool.id)}
                className="mt-0.5 h-4 w-4 rounded border-input"
              />
              <span>
                <span className="font-medium">{tool.label}</span>
                <span className="block text-xs text-muted-foreground">
                  {tool.description}
                </span>
              </span>
            </label>
          ))
        )}
      </div>

      <div className="pt-3">
        <Button type="button" onClick={onSave} disabled={!dirty || saving}>
          {saving ? "Saving…" : "Save agent settings"}
        </Button>
      </div>
    </SectionShell>
  );
}

// --------------------------------------------------------------------
// Section: Always-on guardrails (read-only)
// --------------------------------------------------------------------

/** Strip XML tags from a guardrail block and split it into a lead line + bullet
 * lines for friendly rendering. Strips ALL `<...>` tokens (not just the outer
 * wrapper) so a future nested guardrail tag doesn't leak as raw markup. The raw
 * body is the literal platform prompt text (no user input); presentation only. */
function prettifyGuardrail(body: string): { lead: string; bullets: string[] } {
  const inner = body.replace(/<\/?[^>]+>/g, "").trim();
  const lines = inner
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  const bullets: string[] = [];
  const leadParts: string[] = [];
  for (const line of lines) {
    if (line.startsWith("-")) {
      bullets.push(line.replace(/^-\s*/, ""));
    } else {
      leadParts.push(line);
    }
  }
  return { lead: leadParts.join(" "), bullets };
}

function GuardrailsSection({ initial }: { initial: SettingsInitialValues }) {
  const blocks = initial.agent_guardrails ?? [];
  if (blocks.length === 0) return null;

  return (
    <SectionShell
      title="Always-on guardrails"
      description="The safety, formatting, and tool rules every reply follows. These can't be overridden. Your persona is layered on top of them."
    >
      <details className="group">
        <summary className="cursor-pointer select-none list-none text-sm font-medium">
          <span className="text-muted-foreground group-open:hidden">
            See the rules your bot always follows →
          </span>
          <span className="hidden text-muted-foreground group-open:inline">
            Hide rules ↑
          </span>
        </summary>
        <div className="grid grid-cols-1 gap-3 pt-4 md:grid-cols-2">
          {blocks.map((block) => {
            const { lead, bullets } = prettifyGuardrail(block.body);
            return (
              <Card key={block.title} className="border bg-muted/40">
                <CardContent className="space-y-2 p-4">
                  <p className="text-sm font-semibold">{block.title}</p>
                  {lead ? (
                    <p className="text-xs text-muted-foreground">{lead}</p>
                  ) : null}
                  {bullets.length > 0 ? (
                    <ul className="list-disc space-y-1 pl-4 text-xs text-foreground">
                      {bullets.map((b) => (
                        <li key={b}>{b}</li>
                      ))}
                    </ul>
                  ) : null}
                </CardContent>
              </Card>
            );
          })}
        </div>
      </details>
    </SectionShell>
  );
}

// --------------------------------------------------------------------
// Composite
// --------------------------------------------------------------------

export function AgentForm({ initial }: { initial: SettingsInitialValues }) {
  return (
    <div className="space-y-5">
      <PersonaSection initial={initial} />
      <AgentSection initial={initial} />
      <GuardrailsSection initial={initial} />
    </div>
  );
}
