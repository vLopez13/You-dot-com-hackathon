# Source Reliability & Trust Classification Matrix

This document outlines the **Source Trust Architecture** for LiveCheck. When claims are fact-checked via the You.com APIs (Research, Search, or MCP), retrieved citation URLs are evaluated and categorized into standard **Trust Levels (Level 0 – Level 4)**. 

These weights determine how heavily a cited source influences the final `VERDICT` (TRUE, FALSE, MISLEADING, or UNVERIFIED) during real-time claim verification.

---

## 🏛️ Trust Hierarchy Overview

| Trust Level | Classification Category | Description | Source Examples |
| :--- | :--- | :--- | :--- |
| **Level 0** | **Absolute Source of Truth** | Institutional, peer-reviewed, academic, and government repositories with strict editorial standards and empirical oversight. | `.edu`, `.gov`, PubMed, Nature, IEEE, NASA, WHO, CDC |
| **Level 1** | **Reputable Major Journalism & Wire Services** | Primary news agencies, major global outlets, and non-partisan statistical databases with high editorial integrity and public retraction policies. | Reuters, AP News, BBC, Bloomberg, WSJ, Pew Research |
| **Level 2** | **Secondary News, Niche Media & Industry Outlets** | Standard commercial journalism, specialized tech/sports/finance publications, and corporate press releases. Higher risk of partisan framing or commercial bias. | TechCrunch, Forbes, Verge, ESPN, corporate blogs |
| **Level 3** | **Unverified User-Generated & Social Content** | Forums, self-published blogs, public commentary, and social posts lacking peer review or formal editorial verification. | Reddit, Medium, Substack, tweets/X posts, YouTube comments |
| **Level 4** | **Controversial, Misleading & High-Bias Outlets** | Sites with documented histories of conspiracy, unverified tabloid claims, state propaganda, or aggressive partisan clickbait. | Tabloids, conspiracy forums, known clickbait farms |

---

## 📐 Detailed Breakdown & Verification Rules

### Level 0: Institutional & Primary Truth (Highest Authority)
* **Domains / TLDs:** `.edu`, `.gov`, `.mil`, `.int`
* **Academic Databases:** JSTOR, PubMed, ScienceDirect, ArXiv, IEEE Xplore
* **Global Health & Science Agencies:** WHO, CDC, NASA, NOAA, NIH
* **Rule:** If a Level 0 source directly confirms or refutes a quantitative claim (e.g., statistics, dates, medical data), its verdict overrides lower-level sources.

### Level 1: Primary Journalism & Wire Services
* **Wire Services:** Associated Press (AP), Reuters, AFP
* **National / International Outlets:** BBC, Wall Street Journal, Financial Times, The Economist, NPR, PBS
* **Data Repositories:** World Bank, OECD, Bureau of Labor Statistics (BLS), Pew Research Center
* **Rule:** High confidence for current events, political developments, and breaking news. Minimum requirement of 2 independent Level 1 citations if no Level 0 source is available.

### Level 2: Secondary Media & Industry Publications
* **Tech & Business Media:** TechCrunch, Wired, CNBC, Forbes, Bloomberg Linea
* **Sports & Culture:** ESPN, The Athletic, Rolling Stone
* **Verified Corporate Announcements:** Official company newsrooms (e.g., Apple Newsroom, Google Blog)
* **Rule:** Acceptable for verifying market trends, product releases, or sports scores, but should not override Level 0/1 sources in disputes regarding scientific or historical facts.

### Level 3: User-Generated Content & Opinion
* **Social Platforms:** X / Twitter, Reddit, LinkedIn posts, TikTok, Quora
* **Self-Published Platforms:** Medium, personal blogs, Substack (non-journalistic)
* **Rule:** Classified as **UNVERIFIED** or **ANECDOTAL**. Cannot be used as a primary justification for a `TRUE` or `FALSE` verdict on factual claims. Useful only for identifying viral rumors or claim origin.

### Level 4: High-Bias & Low-Reliability Sources
* **Outlets:** Tabloids (e.g., Daily Mail, National Enquirer), known partisan outrage sites, conspiracy domains.
* **Rule:** Automatically flagged. If a claim relies *solely* on Level 4 sources, the LiveCheck verdict MUST default to **`MISLEADING`** or **`UNVERIFIED`**.

---

## ⚡ Integration with You.com Backend Prompts

When sending queries via the You.com Research API or MCP (`POST https://api.you.com/v1/research`), include the following prompt constraint to enforce this structure:

```text
Assess the validity of the following claim using live web citations. 
Rank your evidence using this trust hierarchy:
1. Level 0 (.edu, .gov, peer-reviewed science) - Absolute Truth
2. Level 1 (Reuters, AP, Major News) - High Trust
3. Level 2 (Industry/Tech Media) - Moderate Trust
4. Level 3/4 (Social media, blogs, low-trust sites) - Unverified / Do Not Weight Highly

Lead your response strictly with:
VERDICT: [TRUE | FALSE | MISLEADING | UNVERIFIED] (Trust Level: [0-4])