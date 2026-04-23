#!/usr/bin/env python3
"""
Slop Galaxy Weekly Agent
Fetches new AI slop/spill stories from RSS, Google News, Reddit, arXiv,
scores them with Claude, and patches new nodes into the galaxy HTML.
"""

import os
import re
import json
import time
import hashlib
import datetime
import urllib.request
import urllib.parse
import urllib.error
from xml.etree import ElementTree as ET

# ── CONFIG ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_API_KEY    = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID     = os.environ.get("GOOGLE_CSE_ID", "")

IMPACT_THRESHOLD  = 6          # minimum impact score to add a node
MAX_NEW_NODES     = 12         # cap per run to avoid noise floods
GALAXY_FILE       = "index.html"  # path in repo root

RSS_FEEDS = [
    ("Wired AI",            "https://www.wired.com/feed/tag/artificial-intelligence/rss"),
    ("The Verge AI",        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("404 Media",           "https://www.404media.co/rss"),
    ("MIT Tech Review",     "https://www.technologyreview.com/feed/"),
    ("Reuters Tech",        "https://feeds.reuters.com/reuters/technologyNews"),
    ("BBC Tech",            "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    ("arXiv cs.AI",         "https://rss.arxiv.org/rss/cs.AI"),
    ("arXiv cs.CY",         "https://rss.arxiv.org/rss/cs.CY"),
    ("Reddit r/artificial", "https://www.reddit.com/r/artificial/.rss"),
    ("Reddit r/MediaSynthesis", "https://www.reddit.com/r/MediaSynthesis/.rss"),
]

GOOGLE_QUERIES = [
    '"AI slop" OR "AI-generated content" ban OR lawsuit OR legislation',
    '"AI-generated" fake OR fabricated news OR journalism 2026',
    'artificial intelligence deepfake institutional response 2026',
    '"model collapse" OR "synthetic content" research findings 2026',
    'AI writing fraud academic publishing 2026',
    '"AI content" platform policy ban moderation 2026',
    'AI propaganda deepfake election misinformation 2026',
]

IMPACT_RUBRIC = """
IMPACT SCORING RUBRIC — apply this strictly to every node:

10 — CIVILIZATIONAL: permanently changes a generation's relationship with AI-generated information.
     The event is a before/after moment for how humans trust digital content.

 9 — LANDMARK: named, memorialized, or canonized by major cultural institutions.
     Entered dictionaries, became a named scandal, or defined a year in the public imagination.

 8 — SYSTEMIC FAILURE EXPOSED: reveals an institutional or platform breakdown with lasting consequences.
     Changed policies, caused firings, triggered investigations, or exposed a structural vulnerability.

 7 — NOTABLE: widely documented, clear real-world consequence, meaningful coverage in reputable outlets.
     A well-sourced story that advances understanding of slop's spread or harm.

 6 — MEANINGFUL PATTERN: documented evidence of a trend, modest but real coverage.
     Worth tracking because it reveals direction, even if not yet a crisis.

 5 — DATA POINT: a single documented instance or a platform making a minor policy tweak.

1–4 — MINOR / ANECDOTAL / SPECULATIVE: a single post, unverified claim, or very niche story.

CRITICAL: Score based on epistemic and cultural damage, NOT media buzz.
A story that got 10 minutes of attention but revealed something structurally broken
scores higher than a flashy story with no lasting consequence.
"""

SLOP_SPILL_RUBRIC = """
SLOP/SPILL SCALE (1–10):
1–2: Pure SLOP — the AI-generated artifact itself is the story (fake image, fake article, fake band)
3–4: Mostly slop — generated content with notable institutional breach
5–6: Both — the artifact and its consequences are equally central
7–8: Mostly spill — documented downstream damage, platform responses, cultural reckoning
9–10: Pure SPILL — legislation, institutional measurement, language change, regulatory response
"""

CATEGORY_GUIDE = """
CATEGORIES:
- platform: decisions/failures by tech platforms (Google, Facebook, Amazon, etc.)
- media: journalism failures, AI-written articles, fake bylines, publishing
- cultural: moments that entered public consciousness, art, music, entertainment
- legislation: laws, regulations, legal actions, court rulings, policy
- research: studies, data, academic findings, institutional reports
- societal: political use, propaganda, social impact, behavior shifts
"""

QUALIFIER_PROMPT = """Does this article document a SPECIFIC, VERIFIABLE MOMENT where AI-generated content:
- Crossed an institutional line (courtroom, newsroom, platform policy, scientific journal, public record)
- Caused a documented real-world consequence (lawsuit, ban, policy change, public scandal)
- Revealed a structural failure or systemic pattern

DISQUALIFY if the article is:
- Opinion, commentary, or think-piece about AI in general
- A product announcement or company funding news
- Speculation about future AI risks
- A how-to guide or tutorial
- About AI technology/capabilities rather than AI-generated CONTENT causing harm

Answer YES or NO first, then explain in one sentence."""


# ── HELPERS ───────────────────────────────────────────────────────────────────

def fetch_url(url, timeout=15):
    """Fetch a URL, return text or None."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; SlopGalaxyBot/1.0)"
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"    FETCH ERROR {url[:60]}: {e}")
        return None


def call_claude(prompt, max_tokens=1200):
    """Call Claude claude-sonnet-4-20250514 and return text response."""
    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["content"][0]["text"]
    except Exception as e:
        print(f"    CLAUDE ERROR: {e}")
        return None


def url_fingerprint(url):
    """Stable short hash of a URL for dedup."""
    return hashlib.md5(url.encode()).hexdigest()[:12]


def extract_existing(html):
    """Pull existing node titles and links from galaxy HTML for dedup."""
    titles = set(t.lower() for t in re.findall(r'title:"([^"]+)"', html))
    links  = set(re.findall(r'link:"([^"]+)"', html))
    fps    = set(url_fingerprint(l) for l in links)
    return titles, links, fps


def get_max_id(html):
    """Find the highest node ID in the galaxy."""
    ids = [int(m.group(1)) for m in re.finditer(r'\{ id:(\d+),', html)]
    return max(ids) if ids else 67


# ── SOURCES ───────────────────────────────────────────────────────────────────

def fetch_rss(name, url):
    """Fetch RSS/Atom feed, return list of {title, link, summary}."""
    items = []
    text = fetch_url(url)
    if not text:
        return items
    try:
        root = ET.fromstring(text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        # RSS 2.0
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link") or "").strip()
            desc  = (item.findtext("description") or "").strip()[:400]
            if title and link:
                items.append({"title": title, "link": link, "summary": desc, "source": name})
        # Atom
        for entry in root.findall("atom:entry", ns):
            title = (entry.findtext("atom:title", namespaces=ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            link  = link_el.get("href", "") if link_el is not None else ""
            desc  = (entry.findtext("atom:summary", namespaces=ns) or "").strip()[:400]
            if title and link:
                items.append({"title": title, "link": link, "summary": desc, "source": name})
    except Exception as e:
        print(f"    RSS PARSE ERROR {name}: {e}")
    print(f"  RSS {name}: {len(items)} items")
    return items


def fetch_google_news(query):
    """Fetch Google Custom Search results for a query."""
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        return []
    params = urllib.parse.urlencode({
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "num": 10,
        "dateRestrict": "w2",   # last 2 weeks
        "sort": "date",
    })
    url = f"https://www.googleapis.com/customsearch/v1?{params}"
    text = fetch_url(url)
    if not text:
        return []
    try:
        data = json.loads(text)
        items = []
        for item in data.get("items", []):
            items.append({
                "title":   item.get("title", "").strip(),
                "link":    item.get("link", "").strip(),
                "summary": item.get("snippet", "").strip()[:400],
                "source":  "Google News",
            })
        print(f"  Google '{query[:50]}': {len(items)} results")
        return items
    except Exception as e:
        print(f"    GOOGLE PARSE ERROR: {e}")
        return []


# ── FILTERING ─────────────────────────────────────────────────────────────────

SLOP_KEYWORDS = [
    "ai slop", "ai-generated", "artificial intelligence generated",
    "deepfake", "synthetic content", "generative ai", "ai content",
    "ai fabricat", "ai fake", "ai fraud", "model collapse",
    "ai hallucin", "ai misinform", "ai propaganda", "ai plagiar",
    "ai copyright", "ai lawsuit", "ai ban", "ai legislation",
    "ai regulation", "ai policy", "ai moderation",
]

def keyword_filter(item):
    """Quick keyword pre-filter before sending to Claude."""
    text = (item["title"] + " " + item["summary"]).lower()
    return any(kw in text for kw in SLOP_KEYWORDS)


# ── SCORING ───────────────────────────────────────────────────────────────────

def score_candidate(item):
    """
    Ask Claude to:
    1. Qualify (is this a node-worthy event?)
    2. Score impact (1-10)
    3. Score slop_spill (1-10)
    4. Draft the full node object
    Returns a node dict or None.
    """
    prompt = f"""You are the curator of the Slop Galaxy — a living record of moments when AI-generated content crossed an institutional line.

ARTICLE:
Title: {item['title']}
Source: {item['source']}
URL: {item['link']}
Summary: {item['summary']}

STEP 1 — QUALIFICATION
{QUALIFIER_PROMPT}

STEP 2 — If qualified (YES), score and draft a node. If not qualified (NO), return only: {{"qualify": false}}

SCORING RUBRICS:
{IMPACT_RUBRIC}
{SLOP_SPILL_RUBRIC}
{CATEGORY_GUIDE}

STEP 3 — Return ONLY valid JSON (no markdown, no backticks, no explanation):
{{
  "qualify": true,
  "title": "concise headline title (max 80 chars)",
  "date": "Month YYYY",
  "year": 2026,
  "source": "Publication Name",
  "category": "platform|media|cultural|legislation|research|societal",
  "impact": 7,
  "slop_spill": 5,
  "description": "2-3 sentence factual description of what happened. No editorializing.",
  "significance": "1-2 sentences on why this matters beyond the headline. What does it reveal structurally?",
  "link": "{item['link']}"
}}

Be strict. Most articles will NOT qualify. Only add nodes for documented institutional moments.
Return only the JSON object, nothing else."""

    response = call_claude(prompt, max_tokens=800)
    if not response:
        return None

    # Strip any markdown fences
    response = re.sub(r'^```[a-z]*\n?', '', response.strip())
    response = re.sub(r'\n?```$', '', response.strip())

    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        # Try to extract JSON object
        m = re.search(r'\{.*\}', response, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group())
        except:
            return None

    if not data.get("qualify"):
        return None

    impact = int(data.get("impact", 0))
    if impact < IMPACT_THRESHOLD:
        return None

    return data


# ── NODE SERIALISATION ────────────────────────────────────────────────────────

def escape_js(s):
    """Escape a string for use inside a JS string literal."""
    return (str(s)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("'", "\\'")
        .replace("\n", " ")
        .replace("\r", ""))


def node_to_js(node, node_id):
    """Serialize a node dict to the JS object format used in SEED."""
    today = datetime.date.today().isoformat()
    title       = escape_js(node.get("title", ""))
    date_str    = escape_js(node.get("date", ""))
    year        = int(node.get("year", datetime.date.today().year))
    source      = escape_js(node.get("source", ""))
    category    = node.get("category", "cultural")
    impact      = int(node.get("impact", 6))
    slop_spill  = int(node.get("slop_spill", 5))
    description = escape_js(node.get("description", ""))
    significance= escape_js(node.get("significance", ""))
    link        = escape_js(node.get("link", ""))

    return (
        f'  {{ id:{node_id}, year:{year}, title:"{title}", '
        f'date:"{date_str}", source:"{source}", category:"{category}", '
        f'impact:{impact}, slop_spill:{slop_spill},\n'
        f'    description:"{description}",\n'
        f'    significance:"{significance}",\n'
        f'    link:"{link}",\n'
        f'    date_added:"{today}" }}'
    )


# ── PATCH GALAXY ──────────────────────────────────────────────────────────────

def patch_galaxy(html, new_nodes):
    """Insert new node objects into the SEED array before the closing ];"""
    if not new_nodes:
        return html

    seed_close = html.rfind('\n];')
    if seed_close < 0:
        raise ValueError("Could not find SEED array closing ]; in galaxy HTML")

    insertion = ",\n" + ",\n".join(new_nodes)
    return html[:seed_close] + insertion + html[seed_close:]


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"Slop Galaxy Agent — {datetime.date.today()}")
    print(f"{'='*60}\n")

    # Load galaxy
    if not os.path.exists(GALAXY_FILE):
        raise FileNotFoundError(f"Galaxy file not found: {GALAXY_FILE}")
    with open(GALAXY_FILE, encoding="utf-8") as f:
        html = f.read()

    existing_titles, existing_links, existing_fps = extract_existing(html)
    max_id = get_max_id(html)
    print(f"Galaxy loaded: {len(existing_titles)} existing nodes, max ID {max_id}\n")

    # ── FETCH ALL SOURCES ──────────────────────────────────────────────────────
    print("── Fetching sources ──")
    candidates = []

    # RSS feeds
    for name, url in RSS_FEEDS:
        items = fetch_rss(name, url)
        candidates.extend(items)
        time.sleep(0.5)

    # Google News
    if GOOGLE_API_KEY:
        print()
        for query in GOOGLE_QUERIES:
            items = fetch_google_news(query)
            candidates.extend(items)
            time.sleep(1)  # respect rate limits

    print(f"\nTotal raw candidates: {len(candidates)}")

    # ── DEDUP ──────────────────────────────────────────────────────────────────
    seen_fps = set()
    deduped = []
    for item in candidates:
        fp = url_fingerprint(item["link"])
        if fp in existing_fps:
            continue
        if fp in seen_fps:
            continue
        if item["link"] in existing_links:
            continue
        seen_fps.add(fp)
        deduped.append(item)

    print(f"After dedup: {len(deduped)} candidates\n")

    # ── KEYWORD PRE-FILTER ─────────────────────────────────────────────────────
    filtered = [item for item in deduped if keyword_filter(item)]
    print(f"After keyword filter: {len(filtered)} candidates\n")

    # ── SCORE WITH CLAUDE ──────────────────────────────────────────────────────
    print("── Scoring with Claude ──")
    approved = []
    for i, item in enumerate(filtered):
        if len(approved) >= MAX_NEW_NODES:
            print(f"  Reached max new nodes ({MAX_NEW_NODES}), stopping.")
            break

        print(f"  [{i+1}/{len(filtered)}] {item['title'][:65]}")
        node = score_candidate(item)
        time.sleep(1.5)  # rate limit

        if node:
            node["link"] = item["link"]  # ensure original link preserved
            approved.append(node)
            print(f"    ✓ APPROVED — impact:{node['impact']} ss:{node['slop_spill']} cat:{node['category']}")
        else:
            print(f"    ✗ rejected")

    print(f"\n── Results: {len(approved)} new nodes approved ──\n")

    if not approved:
        print("No new nodes this week. Galaxy unchanged.")
        # Write a run log
        with open("agent_run_log.txt", "a") as f:
            f.write(f"{datetime.date.today()}: 0 new nodes added (from {len(filtered)} candidates)\n")
        return

    # ── SERIALIZE & PATCH ──────────────────────────────────────────────────────
    new_node_js = []
    for i, node in enumerate(approved):
        node_id = max_id + i + 1
        js = node_to_js(node, node_id)
        new_node_js.append(js)
        print(f"  Node {node_id}: {node.get('title', '')[:60]}")

    patched_html = patch_galaxy(html, new_node_js)

    # Sanity check — count nodes in patched file
    new_count = len(re.findall(r'\{ id:\d+,', patched_html))
    expected  = len(existing_titles) + len(approved)
    if abs(new_count - expected) > 2:
        raise ValueError(f"Node count sanity check failed: got {new_count}, expected ~{expected}")

    with open(GALAXY_FILE, "w", encoding="utf-8") as f:
        f.write(patched_html)

    print(f"\n✓ Galaxy updated: {len(approved)} new nodes added ({len(existing_titles)} → {new_count})")

    # Write run log
    log_lines = [f"{datetime.date.today()}: {len(approved)} new nodes added"]
    for node in approved:
        log_lines.append(f"  - [{node.get('impact',0)}/ss:{node.get('slop_spill',0)}] {node.get('title','')}")
    log_lines.append("")
    with open("agent_run_log.txt", "a") as f:
        f.write("\n".join(log_lines) + "\n")

    print("\nRun log written to agent_run_log.txt")
    print("Done.")


if __name__ == "__main__":
    main()
