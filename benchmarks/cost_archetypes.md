# Polaris — Cost benchmark archetypes (§12.3)

**Status**: locked 2026-05-16 | **Phase**: 0 | **Ref**: `04a_PHASE_0_DEEP.md §12.3` (private), Q24 / ADR-026

These 50 hand-written archetype prompts are the seed corpus for the LLM
cost benchmark that lands in Phase 4 (`benchmarks/expand.py` LLM-amplifies
50 archetypes × 20 variants → 1,000 prompts; `benchmarks/run.py` runs the
full Polaris pipeline — intent → RAG → Cohere rerank → Haiku-or-Sonnet →
response — and reports per-prompt token usage + cost).

The mix is locked per ADR-026:
- **50% simple** (25 prompts) — short FAQ, time/price/locator
- **35% medium** (17 prompts) — multi-turn, comparison, recommendation
- **15% hard** (8 prompts) — complex multi-criteria, complaint triage, ambiguity

Language mix follows Malaysia SME chatbot realities (PRD §1.5):
- ~55% English (EN)
- ~30% Bahasa Malaysia (BM)
- ~15% Chinese Simplified (zh-CN)

Each archetype represents a category Polaris must handle competently. The
expand.py step in Phase 4 generates 20 paraphrased + business-context-shifted
variants per archetype to test pipeline robustness across tone, length,
vocabulary, and emoji density typical of Malaysian SME customers.

Each row: `#` · language · archetype intent · prompt.

---

## Simple (25 prompts — 50%)

Short, single-turn, factoid. Target latency < 1.5s end-to-end. Should be
answerable from the RAG corpus alone; Haiku tier is the cost ceiling.

| #   | Lang  | Intent                | Prompt                                                                                            |
| --- | ----- | --------------------- | ------------------------------------------------------------------------------------------------- |
| 1   | EN    | hours / open today    | What time do you open today?                                                                      |
| 2   | EN    | hours / specific day  | Are you open on Sunday?                                                                           |
| 3   | EN    | location              | Where is your shop located?                                                                       |
| 4   | EN    | location / branch     | Do you have a branch in Petaling Jaya?                                                            |
| 5   | EN    | price / single item   | How much is the chocolate cake?                                                                   |
| 6   | EN    | stock check           | Do you still have the iPhone 16 Pro Max in stock?                                                 |
| 7   | EN    | delivery / general    | Do you deliver?                                                                                   |
| 8   | EN    | delivery / coverage   | Can you deliver to Shah Alam?                                                                     |
| 9   | EN    | payment methods       | What payment methods do you accept?                                                               |
| 10  | EN    | contact / phone       | What's your phone number?                                                                         |
| 11  | EN    | contact / address     | Send me your full address please.                                                                 |
| 12  | EN    | menu / quick          | What's on the menu today?                                                                         |
| 13  | EN    | promo / current       | Any promotion this week?                                                                          |
| 14  | EN    | warranty / quick      | Is there warranty for the rice cooker?                                                            |
| 15  | BM    | hours / open today    | Buka pukul berapa hari ni?                                                                        |
| 16  | BM    | hours / weekend       | Hari Ahad buka tak?                                                                               |
| 17  | BM    | location              | Kedai korang kat mana?                                                                            |
| 18  | BM    | price / single item   | Kek coklat tu berapa harga?                                                                       |
| 19  | BM    | stock check           | Ada stok set kasut Adidas size 8?                                                                 |
| 20  | BM    | delivery / general    | Hantar ke rumah ada?                                                                              |
| 21  | BM    | payment methods       | Boleh bayar guna DuitNow QR tak?                                                                  |
| 22  | BM    | contact / address     | Hantarkan alamat penuh tau.                                                                       |
| 23  | zh-CN | hours / open today    | 你们今天几点开？                                                                                  |
| 24  | zh-CN | price / single item   | 巧克力蛋糕多少钱？                                                                                |
| 25  | zh-CN | delivery / coverage   | 可以送到八打灵再也吗？                                                                            |

---

## Medium (17 prompts — 35%)

Multi-turn intent, comparison, or recommendation with light constraints.
Target latency < 3.5s. Most need RAG retrieval; Sonnet tier is the cost
ceiling for the ~30% that need broader reasoning.

| #   | Lang  | Intent                          | Prompt                                                                                                       |
| --- | ----- | ------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| 26  | EN    | FAQ multi-turn followup         | Hi, I asked about my order last week and you said it would arrive in 3-5 days. It's been 6 days now. Where is it? |
| 27  | EN    | comparison                      | What's the difference between your Basic and Pro membership? Which one is better for someone who comes twice a month? |
| 28  | EN    | recommendation w/ constraints   | I need a present for my mum, RM150–RM200, she likes gardening. Any ideas?                                    |
| 29  | EN    | menu + dietary                  | Do you have any vegan options under RM30? I'm allergic to nuts.                                              |
| 30  | EN    | refund / policy probe           | I bought the speaker yesterday but the bluetooth doesn't connect properly. Can I return it for a refund?     |
| 31  | EN    | booking / availability          | I want to book a haircut on Saturday afternoon, around 2pm. Who's available?                                 |
| 32  | EN    | order status + new request      | Can you check the status of order #ORD-8821? Also, can I add a 6-pack of cans to it?                         |
| 33  | EN    | upsell / cross-sell             | I'm buying the chair. Do you sell matching cushions or covers for it?                                        |
| 34  | EN    | warranty / what's covered       | The blender stopped working after 4 months. The warranty card says 1 year. What's covered exactly?           |
| 35  | BM    | FAQ multi-turn                  | Saya order baju semalam, tapi ada salah size. Boleh tukar tak? Atau kena refund baru order balik?            |
| 36  | BM    | comparison + constraints        | Antara skim ahli silver dengan gold, mana lagi worth it kalau saya datang sebulan dua kali?                  |
| 37  | BM    | recommendation                  | Saya nak cari hadiah untuk birthday isteri, around RM250, dia suka skincare. Boleh suggest?                  |
| 38  | BM    | booking                         | Nak book treatment facial untuk Sabtu petang, sekitar pukul 3. Ada slot?                                     |
| 39  | BM    | order tracking + add-on         | Boleh tolong check order ORD-7714? Sekali nak tambah satu lagi tin susu cair.                                |
| 40  | zh-CN | comparison + constraints        | 你们的银卡和金卡会员有什么不同？我一个月大概来一两次，应该选哪一个？                                          |
| 41  | zh-CN | recommendation                  | 我想送礼给妈妈，预算200令吉，她喜欢园艺。可以推荐什么吗？                                                    |
| 42  | zh-CN | refund / policy                 | 昨天买的蓝牙音箱连不上手机，可以退货吗？需要带发票吗？                                                       |

---

## Hard (8 prompts — 15%)

Complex multi-criteria recommendations, edge-case complaint triage, or
ambiguous intent requiring a clarification turn. Target latency < 6s.
Sonnet tier mandatory; some need multiple RAG hops or policy lookup.

| #   | Lang  | Intent                                 | Prompt                                                                                                                                                                                       |
| --- | ----- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 43  | EN    | multi-criteria reco + tradeoff         | I'm looking for a laptop under RM4000 that's good for video editing but also light enough to carry to uni daily, with at least 16GB RAM and a screen that's not too glossy. What do you have? |
| 44  | EN    | complex complaint + policy lookup      | I bought 3 air purifiers last month for my office. Two are fine but one has been making a loud rattling noise since day 5. I want a replacement for that one, but the warranty says I have to bring all 3 back. Is that really necessary? |
| 45  | EN    | ambiguous intent / needs clarification | Hey, my husband bought something here last week and I want to return it but I don't have the receipt and I'm not sure exactly what it was — some kind of kitchen appliance, black, around RM300. Can you help? |
| 46  | EN    | escalation w/ emotional tone           | This is the third time I'm messaging about this. My order has been "in transit" for two weeks. I've already paid. I want to know exactly what's happening or I'm calling my bank to reverse the payment. |
| 47  | BM    | multi-criteria recommendation          | Nak cari smartphone untuk mak saya, budget RM800–RM1200, dia bukan tech-savvy, kena ada huruf besar, kamera senang nak guna untuk ambil gambar cucu, dan battery tahan lama. Ada cadangan? |
| 48  | BM    | complex complaint + policy             | Saya beli set perabot living room bulan lepas. Sofa OK, tapi meja kopi tu retak kat tepi. Boleh tukar bahagian tu saja ke kena pulang semua set?                                              |
| 49  | zh-CN | multi-criteria recommendation          | 想买一台空气净化器，办公室大概25平方米，三个员工，预算1500令吉以内，最好静音、过滤PM2.5、有显示屏，能远程控制就更好了。有什么推荐？                                                            |
| 50  | zh-CN | complex complaint                      | 我上个月买了三台同款空气净化器给办公室，其中一台从第五天开始就有奇怪的咔哒声。保修条款说要全部送回来检查，但我办公室不能没有空气净化器。能不能只送一台过来？                                  |

---

## Mix verification

| Bucket | Count | Target | OK  |
| ------ | ----- | ------ | --- |
| Simple | 25    | 25     | ✅  |
| Medium | 17    | 17     | ✅  |
| Hard   | 8     | 8      | ✅  |
| **Total** | **50** | **50** | ✅ |

| Language | Count | Approx % | Notes                                                |
| -------- | ----- | -------- | ---------------------------------------------------- |
| EN       | 28    | 56%      | Matches Malaysia SME chatbot reality                 |
| BM       | 14    | 28%      | Includes informal register ("korang", "tau", "OK")   |
| zh-CN    | 8     | 16%      | Simplified — Malaysian Chinese tends Simplified over Traditional |

## Phase 4 wiring (preview, NOT in scope for Phase 0)

`benchmarks/expand.py` will (Phase 4):

1. Read this file.
2. For each archetype, call Claude Sonnet to produce 20 variants that:
   - Preserve intent + difficulty bucket + language.
   - Vary tone (formal / casual / curt / chatty), length (1 line → 4 lines),
     emoji density (0 → 3), and Malaysian-specific business context (food,
     beauty, electronics, services, retail).
3. Write `benchmarks/expanded_corpus.jsonl` — 1,000 lines × `{archetype_id,
   variant_id, lang, bucket, prompt}`.

`benchmarks/run.py` (Phase 4) then drives the full Polaris pipeline against
that corpus and emits per-tier cost projections that feed ADR-026's
`bot_replies` caps.

## Owner + maintenance

- Owner: operator (this file) + engineering (Phase 4 runner).
- Append-only: new archetypes get higher IDs. Never edit existing rows
  after Phase 4 begins, or variant outputs drift from baseline.
- Sign-off bar before Phase 4 runs: ≥80% of these prompts return a
  response Polaris would actually send to a real customer (sanity bar,
  not quality SLO).
