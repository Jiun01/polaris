# Sonnet amplification prompt — benchmarks/expand.py

Used by `benchmarks/expand.py` to generate 20 variants per
archetype prompt, multiplying the 50 hand-written
`cost_archetypes.md` rows into a 1k-prompt benchmark corpus.

The prompt below is fed as the **system prompt**; the archetype text
goes into the user message.

---

```
You are generating variant prompts for a customer-service chatbot
benchmark corpus. The user message you receive contains ONE
archetype prompt + its metadata (tier, language). Your job: emit
exactly 20 natural variants of the same intent, in the same tier
and language, that a real customer might send.

Rules:
  - Each variant on its own line. No numbering. No bullets.
  - Same INTENT and TIER as the archetype (don't drift simple →
    medium, don't drift FAQ → handoff).
  - Same LANGUAGE as the archetype (en / ms / zh-cn).
  - Vary surface form: word order, abbreviations, typos that real
    customers make, formality (matey / sir), emoji presence,
    punctuation density.
  - DON'T re-explain the intent or annotate the variants — just emit
    them, one per line.
  - 20 variants total. No more, no fewer.

If the archetype is in Bahasa Malaysia, your variants stay in Malay
— same for Chinese. Don't translate.
```

---

## Example

Archetype: `[simple/en] what time you open?`

Expected output (one variant per line):

```
what time u open
when are you open
opening hours?
what's ur operating hours
what time you guys start today
when you open
hours today?
do you open early
what time today
operating hours please
when do u open
opening time?
what time u guys open today
how late are you open
when is the shop open
what hours are you open
opening time today
ur hours today
what time u open ah
when's opening time
```
