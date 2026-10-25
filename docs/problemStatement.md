# Problem Statement: Mutual Fund FAQ Assistant (Facts-Only Q&A)

> **What this file is for:** the main record of what this project must do. Sections 1–9 restate `docs/problemStatement.txt` (the original brief) in a structured form, with requirement IDs added so code and tests can refer to them. Sections 10–12 and 14 are **derived interpretation**: how the brief is read, plus the open decisions. Section 13 records project decisions made after the brief, including a **deliberate departure from the brief's source rules**: the corpus is only the 5 Groww scheme pages (see §13 for every deviation). Apart from the deviations listed in §13, where this file and the brief disagree, the original brief wins.

---

## 1. Overview

Build a **facts-only FAQ assistant** for mutual fund schemes, with **Groww** as the reference product context. The assistant answers objective, verifiable questions about mutual funds. It takes its information **only from official public sources**: AMC (Asset Management Company) websites, AMFI and SEBI.

The assistant must **never** give investment advice, opinions or recommendations. Every response carries **one clear source link** and follows fixed rules on clarity, accuracy and compliance.

**Guiding principle:** *accuracy over intelligence.* A short, verifiable, cited answer (or a polite refusal) is always better than a clever answer that cannot be checked.

## 2. Objective

Design and build a lightweight **Retrieval-Augmented Generation (RAG)** assistant that:

1. Answers factual questions about mutual fund schemes.
2. Uses a **curated corpus** of official documents.
3. Gives **concise, source-backed** responses.

## 3. Target Users

| User | Need |
|---|---|
| Retail investors comparing mutual fund schemes | Quick, trustworthy answers to factual scheme questions |
| Customer support & content teams | Consistent answers to repetitive mutual fund questions |

## 4. Scope of Work

### 4.1 Corpus Definition

| ID | Requirement |
|---|---|
| C-1 | Select **one** AMC. |
| C-2 | Choose **3–5 schemes** from that AMC, across different categories (e.g., large-cap, flexi-cap, ELSS). |
| C-3 | Collect **15–25 official public URLs**, covering: scheme factsheets; KIM (Key Information Memorandum); SID (Scheme Information Document); AMC FAQ/help pages; AMFI/SEBI guidance pages; guides for downloading statements and tax documents. |

### 4.2 FAQ Assistant Requirements

The assistant must answer **facts-only** questions, for example:

- Expense ratio of a scheme
- Exit load details
- Minimum SIP amount
- ELSS lock-in period
- Riskometer classification
- Benchmark index
- How to download statements or capital-gains reports

Every answer must satisfy this **response contract**:

| ID | Rule |
|---|---|
| R-1 | At most **3 sentences**. |
| R-2 | **Exactly one** citation link. |
| R-3 | Ends with the footer: `Last updated from sources: <date>` |

### 4.3 Refusal Handling

The assistant must **refuse** non-factual or advisory questions, e.g.:

- "Should I invest in this fund?"
- "Which fund is better?"

| ID | Every refusal must… |
|---|---|
| RF-1 | Be polite and clearly worded. |
| RF-2 | Restate the facts-only limitation. |
| RF-3 | Include a relevant **educational link** (e.g., an AMFI or SEBI investor-education page). |

### 4.4 User Interface (Minimal)

| ID | The UI must show… |
|---|---|
| UI-1 | A welcome message. |
| UI-2 | Three example questions. |
| UI-3 | A visible disclaimer: **"Facts-only. No investment advice."** |

## 5. Constraints

### 5.1 Data & Sources
| ID | Constraint |
|---|---|
| D-1 | Use **only** official public sources: the AMC, AMFI, SEBI. |
| D-2 | **No** third-party blogs or aggregator websites. |

### 5.2 Privacy & Security
| ID | Constraint |
|---|---|
| P-1 | Do **not** collect, store or process: PAN or Aadhaar numbers, account numbers, OTPs, email addresses or phone numbers. |

### 5.3 Content Restrictions
| ID | Constraint |
|---|---|
| CR-1 | No investment advice or recommendations. |
| CR-2 | No performance comparisons or return calculations. |
| CR-3 | For performance-related questions, give **only** a link to the official factsheet. |

### 5.4 Transparency
| ID | Constraint |
|---|---|
| T-1 | Responses are short, factual and verifiable. |
| T-2 | Every answer includes a source link **and** a last-updated date. |

## 6. Expected Deliverables

| ID | Deliverable | Contents |
|---|---|---|
| DL-1 | **README** | Setup instructions; selected AMC and schemes; RAG architecture overview; known limitations |
| DL-2 | **Disclaimer snippet** | "Facts-only. No investment advice." |
| DL-3 | **Multiple chat thread support** | A RAG chatbot that can run several independent conversations (threads) at the same time |

## 7. Success Criteria

1. **Accurate retrieval** of factual mutual fund information.
2. **Strictly facts-only** responses.
3. **Valid source citations** in every response.
4. **Correct refusal** of advisory questions.
5. **Clean, minimal, user-friendly** interface.

## 8. Out of Scope (implied by the brief)

- Investment advice, fund recommendations, portfolio suggestions.
- Return calculations, performance comparisons, projections.
- Any user account access, transactions or personal data handling.
- More than one AMC; schemes or sources beyond the curated corpus.

## 9. Summary

The goal is a **trustworthy, transparent and compliant** mutual fund FAQ assistant that puts accuracy over intelligence. Users get only verified, source-backed financial information, with no advisory slant or speculation.

---

## 10. Interpretation Notes *(derived — not part of the original brief)*

These are working readings of points the brief leaves ambiguous, adjusted for the project's corpus decision (§13). Change them here if the team decides otherwise.

1. **Groww.** The brief names Groww as the *reference product context*. **Project decision (§13):** the RAG corpus is **exactly the 5 Groww HDFC scheme pages**, and no other URLs are used. This deliberately departs from D-1 and D-2 (Groww is an aggregator, not the AMC, AMFI or SEBI). §13 lists every deviation.
2. **Footer date.** `<date>` is the date the cited Groww page was last fetched with changed content (stored per source). It is not the current date.
3. **Sentence limit vs. footer.** The footer does not count toward the 3-sentence limit (R-1).
4. **Refusals and citations.** A refusal also carries exactly one link and the footer. With no AMFI/SEBI page in the corpus, the link is the Groww page of the scheme being discussed (or a configured default of the 5). RF-3 is therefore only partly met.
5. **Performance questions (CR-3)** are not refused outright. The reply is one short sentence pointing to the scheme's Groww page, with no numbers, returns or comparisons. Return data on the pages is removed before indexing so it can never be quoted (CR-2).
6. **Not-in-corpus questions.** Questions the 5 pages can't answer (ELSS lock-in, statement or capital-gains downloads, personal or general tax questions, other schemes or AMCs) get "I couldn't find that", with one of the 5 pages as the link. The assistant must not guess. Each scheme page's own *tax implication* rule is answerable, restated as written, never as a personal calculation (edge-case D-02).
7. **PII (P-1).** If a user types PAN, Aadhaar, account numbers, OTPs, emails or phone numbers, the system detects and redacts them **before** logging, embedding or sending anything to an LLM, and replies with a reminder not to share personal details. Conversation logs must never hold raw PII.
8. **Multi-thread support (DL-3).** Each thread has its own isolated conversation history, and threads running in parallel must not leak context between them. Follow-up questions ("what about its exit load?") resolve only against the same thread's history.

## 11. Query Classification *(derived)*

Every incoming message falls into one of these classes:

| Class | Example | Behaviour |
|---|---|---|
| Factual (in scope) | "What is the exit load of <scheme>?" | Retrieve from the scheme's Groww page → answer under R-1…R-3 |
| Performance | "What were the 3-year returns of <scheme>?" | Link to the scheme's Groww page only; no figures (CR-2, CR-3) |
| Advisory / opinion | "Should I invest?", "Which is better?" | Refuse politely (RF-1, RF-2), one scheme-page link |
| Contains PII | "My PAN is …, show my holdings" | Redact, decline, remind user not to share personal data (P-1) |
| Out of corpus / unknown | ELSS lock-in, statement download, another AMC's scheme | Say the information isn't available, link one of the 5 pages; no guessing |

## 12. Acceptance Checklist *(derived — use as an evaluation set)*

- [ ] Corpus: 1 AMC, 5 schemes from different categories, **exactly the 5 Groww URLs in §13**, each with stored source URL and fetch date.
- [ ] No URL outside those 5 is ingested or cited, including other `groww.in` pages.
- [ ] Each sample factual question about facts shown on the pages (e.g. expense ratio, exit load, min SIP, riskometer, benchmark) returns a correct answer of ≤3 sentences with exactly 1 link and the footer.
- [ ] Questions the pages can't answer (ELSS lock-in, statement/capital-gains download) return "not found" rather than a guess.
- [ ] The cited link is the page the answer actually came from.
- [ ] Advisory prompts ("Should I invest…", "Which is better…", "Is this a good fund…") are refused politely with one scheme-page link.
- [ ] Return/performance prompts give only the scheme-page link: no numbers, no comparisons.
- [ ] PII in input is never stored or forwarded in raw form.
- [ ] UI shows the welcome message, 3 example questions and the "Facts-only. No investment advice." disclaimer.
- [ ] Two or more threads can run at once without leaking context.
- [ ] README covers setup, AMC/schemes, architecture, known limitations and the deviations from the brief.

## 13. Selected AMC, Schemes & Corpus *(project decision)*

- **AMC:** HDFC Mutual Fund
- **Plan / option:** Direct – Growth

| # | Scheme | Category (as shown on the Groww page, checked 25 Oct 2026) | Corpus URL |
|---|---|---|---|
| 1 | HDFC Large Cap Fund – Direct Growth | Equity – Large Cap | https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth |
| 2 | HDFC Mid Cap Fund – Direct Growth | Equity – Mid Cap | https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth |
| 3 | HDFC Small Cap Fund – Direct Growth | Equity – Small Cap | https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth |
| 4 | HDFC Multi Cap Fund – Direct Growth | Equity – Multi Cap | https://groww.in/mutual-funds/hdfc-multi-cap-fund-direct-growth |
| 5 | HDFC Silver ETF FoF – Direct Growth | Commodities – Silver (a fund of funds investing in HDFC Silver ETF) | https://groww.in/mutual-funds/hdfc-silver-etf-fof-direct-growth |

**Corpus rule:** these 5 URLs are the **entire** RAG corpus. No HDFC MF, AMFI, SEBI or other URLs are added. Every fact the assistant states, and every link it shows, comes from one of these 5 pages.

**Deviations from the brief (accepted deliberately; must be stated in the README):**

| Brief rule | Status | Handling |
|---|---|---|
| C-3: 15–25 official URLs (factsheet, KIM, SID, FAQ, AMFI/SEBI, statement guides) | Not met: 5 URLs, all from one aggregator | Build gate enforces exactly these 5 |
| D-1: official sources only (AMC, AMFI, SEBI) | Not met | Exact-URL allowlist of the 5 Groww pages |
| D-2: no aggregator websites | Not met | Recorded here and in the README |
| RF-3: refusals link to AMFI/SEBI education | Partly met | Refusals link to the relevant scheme page |
| CR-3: performance → official factsheet link | Partly met | Link to the scheme page; returns stripped at ingestion |
| Statement / capital-gains download questions | Not answerable | "Not found" reply; listed as a limitation |
| ELSS lock-in questions (no ELSS scheme, no general source) | Not answerable | "Not found" reply; listed as a limitation |

All other requirements (R-1…R-3, P-1, CR-1, CR-2, T-1, T-2, UI-1…3, DL-1…3) still apply in full.

## 14. Open Decisions *(to be resolved and recorded here)*

| # | Decision | Status |
|---|---|---|
| 1 | Which AMC? | **Decided:** HDFC Mutual Fund (§13) |
| 2 | Which 3–5 schemes? | **Decided:** 5 schemes (§13) |
| 3 | Source URLs | **Decided:** exactly the 5 Groww scheme pages; no other URLs (§13) |
| 4 | Tech stack: LLM provider, embedding model, vector store, backend framework, UI framework | **Decided:** Groq (`openai/gpt-oss-120b`) as the LLM; rest as in architecture §3 / §17 (implementation plan P0.1) |
| 5 | Ingestion approach and chunking | Proposed: HTML-only parsing, returns stripped, one chunk per fact group (architecture §5) |
| 6 | Refresh cadence and footer date | **Implemented (26 Oct 2026), pending owner confirmation of plan P7.1:** daily re-ingest at 10:00 IST via GitHub Actions (was monthly), state kept in private-repo releases, server pulls and hot-reloads; footer = cited page's `fetched_at` (architecture §5.6–5.7; plan P0.2, Phase 7) |
| 7 | Thread storage | Proposed: SQLite, redacted text, 30-day TTL (architecture §8; plan P0.5) |
| 8 | Advisory/PII detection method | Proposed: rules + LLM status + validator (architecture §6) |
| 9 | ELSS gap | **Decided:** keep the 5 schemes; ELSS questions get a "not found" reply |
| 10 | Groww terms of use / `robots.txt` for automated fetching | **Decided (25 Oct 2026): automated fetch**, accepting the terms risk. `robots.txt` allows the 5 scheme paths, and it is re-checked on every run. A polite fetch (5 requests a month, 3 s apart); the manual-save fallback is kept for blocks or objections (plan P0.6, P2.2) |
| 11 | Edge-case decisions affecting the corpus (edge-case.md §15) | **Applied recommended defaults in Phase 1** (owner may override): D-01 NAV and fund size answerable with the page's date; D-02 the page's tax rule answerable, no personal calculations; D-03 ratings and pros/cons stripped as opinion; D-12 `not_in_scope` deny-list of other HDFC schemes in `corpus/schemes.yaml` |
