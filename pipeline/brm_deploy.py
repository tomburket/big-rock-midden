#!/usr/bin/env python3
# Big Rock Midden pipeline — build + deploy (v1.3-brm.1, August 7 2026)
#
# Forked from pmwr_deploy.py v1.3 (tomburket/prepared-mind-weekly). Helpers,
# encoders, dedup gate, commit path and BOTH v1.2/v1.3 correctness fixes are
# carried over byte-identical. See CHANGES below for everything that differs.
#
# Weekly:  python3 ~/Downloads/brm_deploy.py            (newest edition-NN.json in ~/Downloads)
# Preview: python3 ~/Downloads/brm_deploy.py --dry-run  (builds + verifies, writes to ~/Downloads/brm_build)
# Genesis: python3 ~/Downloads/brm_deploy.py --genesis   (one-time: seeds an empty repo)
#
# Token: a GitHub fine-grained PAT in ~/.kko_token (Contents read/write on this repo only).
#        Separate token from ~/.pmwr_token by design — separate blast radius.
#
# CHANGES FROM THE PMWR ORIGINAL
#   1. Constants retargeted (repo, site, token path, build dir).
#   2. LANDSCAPE = False. The reading layer does not exist in Phase 1. The
#      emitters and their verifications are gated, not deleted, so Phase 2
#      restores rather than rewrites. Landscape metadata is still REQUIRED on
#      every card and still merged into the corpus — that is the whole point.
#   3. STRICT_READS = True. The raw.githubusercontent fallback is deleted as a
#      code path, not just discouraged. PMWR kept it for the no-token dry run;
#      but a stale CDN read is precisely what silently clobbered Ed 19, and the
#      only machine that runs this HAS the token. A token problem should stop
#      the run, not quietly degrade it into the failure mode v1.3 exists to
#      prevent. This is stricter than the repo's visibility requires.
#   4. --bootstrap (derive scaffolding from a live site) replaced by --genesis
#      (seed an empty repo from local files). There is no live site to derive
#      from at Edition 1.
#   5. Template pinned at pipeline/template_v1.html, template_version v1.0.

import base64, glob, html as HTML, json, os, re, sys, time, urllib.request, urllib.error
from pathlib import Path

REPO = "tomburket/big-rock-midden"
RAW = f"https://raw.githubusercontent.com/{REPO}/main"
API = f"https://api.github.com/repos/{REPO}/contents"
SITE = "https://big-rock-midden.vercel.app"
DL = Path.home() / "Downloads"
BUILD = DL / "brm_build"
TOKEN_PATH = Path.home() / ".kko_token"
TEMPLATE = "pipeline/template_v1.html"

# Phase flags. See CHANGES 2 and 3 in the header.
LANDSCAPE = False     # flip to True at Phase 2 (~Ed 10-13) to restore the reading layer
STRICT_READS = True   # never fall back to the raw CDN -- see fetch_repo. Independent
                      # of repo visibility: keep this True even on a public repo.

# ============================= shared helpers =============================

def die(msg):
    print("\nABORT — nothing committed. " + msg)
    sys.exit(1)

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pmwr-deploy"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")

def extract_balanced(text, anchor, label):
    if text.count(anchor) != 1:
        die(f"{label}: anchor not unique ({text.count(anchor)} hits): {anchor!r}")
    i = text.index(anchor) + len(anchor)
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    if i >= len(text) or text[i] not in "[{":
        die(f"{label}: no opening bracket after anchor.")
    oc = text[i]; cc = "]" if oc == "[" else "}"
    depth, j, in_str, n = 0, i, False, len(text)
    while j < n:
        c = text[j]
        if in_str:
            if c == "\\": j += 2; continue
            if c == '"': in_str = False
            j += 1; continue
        if c == '"': in_str = True; j += 1; continue
        if c == "/" and j + 1 < n and text[j+1] == "*":
            k = text.find("*/", j + 2)
            if k == -1: die(f"{label}: unterminated comment.")
            j = k + 2; continue
        if c == "/" and j + 1 < n and text[j+1] == "/":
            k = text.find("\n", j + 2); j = n if k == -1 else k; continue
        if c == oc: depth += 1
        elif c == cc:
            depth -= 1
            if depth == 0: return text[i:j+1]
        j += 1
    die(f"{label}: brackets never balanced — live structure changed.")

def js_to_json(src):
    out, i, n, in_str = [], 0, len(src), False
    while i < n:
        c = src[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n: out.append(src[i+1]); i += 2; continue
            if c == '"': in_str = False
            i += 1; continue
        if c == '"': in_str = True; out.append(c); i += 1; continue
        if c == "/" and i + 1 < n and src[i+1] == "*":
            j = src.find("*/", i + 2)
            if j == -1: die("Unterminated comment.")
            i = j + 2; continue
        if c == "/" and i + 1 < n and src[i+1] == "/":
            j = src.find("\n", i + 2); i = n if j == -1 else j; continue
        if c == ",":
            j = i + 1
            while j < n and src[j] in " \t\r\n": j += 1
            if j < n and src[j] in "]}": i += 1; continue
            out.append(c); i += 1; continue
        m = re.match(r"(?:[A-Za-z_$][\w$]*|\d+)(?=\s*:)", src[i:])
        if m and (not out or out[-1].strip() in ("", "{", ",")):
            out.append('"' + m.group(0) + '"'); i += len(m.group(0)); continue
        out.append(c); i += 1
    return "".join(out)

def parse_js(text, anchor, label):
    return json.loads(js_to_json(extract_balanced(text, anchor, label)))

# ============================= entity encoding =============================

ENT = [("&", "&amp;"), ("—", "&mdash;"), ("–", "&ndash;"), ("·", "&middot;"),
       ("’", "&rsquo;"), ("‘", "&lsquo;"), ("“", "&ldquo;"), ("”", "&rdquo;"),
       ("é", "&eacute;"), ("↗", "&#8599;"), ("↑", "&uarr;"), ("→", "&rarr;"),
       ("•", "&bull;"), (" ", "&nbsp;")]

def enc(s):
    for ch, e in ENT: s = s.replace(ch, e)
    return "".join(c if ord(c) < 128 else f"&#{ord(c)};" for c in s)

def js_str(s):
    return json.dumps(s, ensure_ascii=True)

# ============================= edition page =============================

def build_card(c, num):
    return f'''<div class="item-card" id="c{num:02d}">
<div class="item-header">
<span class="item-tag {c["tag_class"]}">{enc(c["tag_label"])}</span>
<div class="item-headline">{enc(c["headline"])}</div>
</div>
<div class="item-source">{enc(c["source"])}</div>
<div class="item-summary">{enc(c["summary"])}</div>
<div class="implication-block">
<div class="implication-label">Why it matters</div>
<div class="implication-text">{enc(c["why"])}</div>
</div>
<a class="item-link" href="{c["url"]}" target="_blank" rel="noopener">{enc(c["link_label"])} &#8599;</a>
</div>'''

def build_page(ed, template):
    n = sum(len(s["cards"]) for s in ed["sections"])
    meta = (f"\nEDITION METADATA\nedition: {ed['edition']}\ndate: {ed['date_long']}\n"
            f"cards: {n}\nsections: {', '.join(s['title'] for s in ed['sections'])}\n"
            f"template_version: v1.0\n")
    pills = "\n".join(
        f'<div class="summary-pill"><div class="summary-pill-num">{len(s["cards"])}</div>'
        f'<div class="summary-pill-label">{enc(s["pill"]).replace(chr(10), "<br>")}</div></div>'
        for s in ed["sections"])
    nav = "\n".join(f'<a href="#{s["id"]}">{enc(s["title"])}</a>' for s in ed["sections"])
    body, k = [], 0
    for i, s in enumerate(ed["sections"], 1):
        cards = []
        for c in s["cards"]:
            k += 1
            cards.append(build_card(c, k))
        body.append(f'''<!-- {s["title"].upper()} -->
<section id="{s["id"]}">
<div class="section-header"><span class="section-number">{i:02d}</span><span class="section-title">{enc(s["title"])}</span></div>
<hr class="section-rule">

{chr(10).join(cards)}
</section>''')
    out = template
    for tok, val in [("{{META}}", meta), ("{{TITLE_DATE}}", enc(ed["date_long"])),
                     ("{{MAST_DATE}}", ed["date_long"]), ("{{N_ITEMS}}", str(n)),
                     ("{{TOPICS}}", enc(ed["topics"])), ("{{PILLS}}", pills), ("{{NAV}}", nav),
                     ("{{SECTIONS}}", "\n\n".join(body)), ("{{FOOT_DATE}}", ed["date_long"]),
                     ("{{ED_NO}}", str(ed["edition"]))]:
        out = out.replace(tok, val)
    return out

# ============================= corpus merge =============================

ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|apos|nbsp|mdash|ndash|middot|hellip|[lr]squo|[lr]dquo|bull|uarr|rarr|eacute|#\d+|#x[0-9a-fA-F]+);")

def check_authored_text(ed):
    """Refuse pre-encoded HTML entities in authored JSON.

    brm.1. enc() encodes exactly once. An author who writes "Enforcement &amp;
    Rulemaking" in the JSON gets "&amp;amp;" in the page -- which renders as
    visible "&amp;" and still passes the pure-ASCII check, because it IS pure
    ASCII. This taxonomy has three ampersanded section names, so the trap is
    not hypothetical. Write literal characters in JSON; the encoder handles it.
    """
    bad = []
    def scan(where, s):
        if isinstance(s, str):
            for m in ENTITY_RE.finditer(s):
                bad.append(f"{where}: {m.group(0)!r} in {s[:70]!r}")
    scan("topics", ed.get("topics"))
    scan("date_long", ed.get("date_long"))
    for s in ed["sections"]:
        scan(f"section {s['id']} title", s.get("title"))
        scan(f"section {s['id']} pill", s.get("pill"))
        for k, c in enumerate(s["cards"], 1):
            for f in ("tag_label", "headline", "source", "summary", "why", "link_label"):
                scan(f"section {s['id']} card {k} {f}", c.get(f))
            L = c.get("landscape") or {}
            for f in ("gloss", "src_short"):
                scan(f"section {s['id']} card {k} landscape.{f}", L.get(f))
    if bad:
        print("\nPRE-ENCODED ENTITIES in the edition JSON -- these would double-encode:")
        for b in bad:
            print("  " + b)
        die("Write literal characters (&, --, curly quotes) in the JSON. enc() encodes once.")
    print("Entity guard ....... PASS (no pre-encoded entities in authored text)")

def edition_cards_flat(ed):
    flat = []
    for s in ed["sections"]:
        for c in s["cards"]:
            flat.append((s, c))
    return flat

def merge_corpus(corpus, ed):
    new = []
    for k, (s, c) in enumerate(edition_cards_flat(ed), 1):
        L = c.get("landscape")
        if not L:
            die(f"Card {k} ({c['headline'][:50]!r}) has no 'landscape' block — every weekly card needs tag/t/src_short/gloss.")
        entry = {"id": f"e{ed['edition']}c{k}", "ed": ed["edition"], "sec": s["title"],
                 "tag": L["tag"], "h": c["headline"], "src": L["src_short"], "t": L["t"]}
        if L.get("t2"): entry["t2"] = L["t2"]
        entry["s"] = L["gloss"]
        entry["url"] = c["url"]
        entry["img"] = c.get("img")
        new.append(entry)
    merged = dict(corpus)
    merged["cards"] = corpus["cards"] + new
    merged["current_edition"] = ed["edition"]
    merged["edition_dates"] = dict(corpus["edition_dates"])
    merged["edition_dates"][str(ed["edition"])] = ed["date_long"].rsplit(",", 1)[0]
    ids = [c["id"] for c in merged["cards"]]
    if len(ids) != len(set(ids)):
        die("Duplicate card ids after merge.")
    return merged, new

# ============================= landscape / footlines emitters =============================

def js_card(c):
    fields = ["id", "ed", "sec", "tag", "h", "src", "t", "t2", "s", "url"]
    parts = []
    for f in fields:
        if f not in c or c[f] is None: continue
        v = c[f]
        parts.append(f"{f}:{v if isinstance(v, int) else js_str(v)}")
    return " {" + ",".join(parts) + "}"

def emit_landscape(live_landscape, corpus):
    by_ed = {}
    for c in corpus["cards"]:
        by_ed.setdefault(c["ed"], []).append(c)
    blocks = []
    for e in sorted(by_ed):
        blocks.append(f" /* ---- Edition {e} ---- */")
        blocks.append(",\n".join(js_card(c) for c in by_ed[e]) + ",")
    cards_lit = "[\n" + "\n".join(blocks) + "\n]"
    dates_lit = "{" + ",".join(f'{int(k)}:{js_str(v)}' for k, v in
                               sorted(corpus["edition_dates"].items(), key=lambda x: int(x[0]))) + "}"
    active_lit = "[" + ",".join(js_str(t) for t in corpus["active_threads"]) + "]"
    out = live_landscape
    out = out.replace(extract_balanced(out, "const CARDS=", "CARDS"), cards_lit, 1)
    out = re.sub(r"const CURRENT_EDITION=\d+", f"const CURRENT_EDITION={corpus['current_edition']}", out, count=1)
    out = out.replace(extract_balanced(out, "const EDITION_DATES=", "EDITION_DATES"), dates_lit, 1)
    out = out.replace(extract_balanced(out, "const ACTIVE=new Set(", "ACTIVE"), active_lit, 1)
    return out

def emit_footlines(live_footlines, corpus):
    live_d = parse_js(live_footlines, "var D=", "footlines D")
    d = {"threads": live_d["threads"], "active": corpus["active_threads"],
         "cards": [{k: c[k] for k in ("id", "ed", "h", "t", "t2", "url") if c.get(k) is not None}
                   for c in corpus["cards"]]}
    return live_footlines.replace(extract_balanced(live_footlines, "var D=", "D"),
                                  json.dumps(d, ensure_ascii=True, separators=(",", ":")), 1)

def emit_editions_index(live_index, ed):
    arr = parse_js(live_index, "const editions = ", "editions[]")
    if arr[0]["number"] != ed["edition"] - 1:
        die(f"editions index head is #{arr[0]['number']}, expected #{ed['edition']-1} — sequence mismatch.")
    arr[0]["file"] = f"edition-{ed['edition']-1:02d}.html"
    arr.insert(0, {"number": ed["edition"], "date": ed["date_long"],
                   "items": sum(len(s["cards"]) for s in ed["sections"]),
                   "topics": ed["topics"], "file": None})
    def entry(e):
        f = "null" if e["file"] is None else js_str(e["file"])
        return ("{\n" + f"number: {e['number']},\ndate: {js_str(e['date'])},\n"
                f"items: {e['items']},\ntopics: {js_str(e['topics'])},\nfile: {f}" + "\n}")
    lit = "[\n" + ",\n".join(entry(e) for e in arr) + "\n]"
    return live_index.replace(extract_balanced(live_index, "const editions = ", "editions[]"), lit, 1)

# ============================= dedup gate =============================

STOP = set("a an the and or of to in on for by with at as is are was were has have had this that its it "
           "from after before over under new now says said will would could".split())

def norm_url(u):
    u = re.sub(r"^https?://(www\.)?", "", (u or "").lower())
    return u.split("?")[0].rstrip("/")

def tokens(*ss):
    return {w for s in ss for w in re.findall(r"[a-z0-9%\.]+", (s or "").lower()) if w not in STOP and len(w) > 1}

def numbers(*ss):
    return {n for s in ss for n in re.findall(r"\d[\d,\.]*%?", s or "")}

def propers(*ss):
    return {w.lower() for s in ss for w in re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", s or "")}

def dedup_gate(corpus, ed):
    hits = []
    for k, (s, c) in enumerate(edition_cards_flat(ed), 1):
        L = c.get("landscape", {})
        nh, ng, nu = c["headline"], L.get("gloss", ""), c["url"]
        nt, nn, np_ = tokens(nh, ng), numbers(nh, ng, c.get("summary", "")), propers(nh, ng)
        for p in corpus["cards"]:
            reasons = []
            if norm_url(nu) and norm_url(nu) == norm_url(p.get("url")):
                reasons.append("same source URL")
            pt = tokens(p["h"], p.get("s", ""))
            jac = len(nt & pt) / max(1, len(nt | pt))
            if jac >= 0.45:
                reasons.append(f"headline/gloss similarity {jac:.2f}")
            shared_n = nn & numbers(p["h"], p.get("s", ""))
            shared_p = np_ & propers(p["h"], p.get("s", ""))
            if shared_n and shared_p and jac >= 0.15:
                reasons.append(f"shared figures {sorted(shared_n)} + entities {sorted(shared_p)}")
            if reasons:
                hits.append((k, nh, p, reasons))
    if not hits:
        print("Dedup gate ......... PASS (no matches against %d published cards)" % len(corpus["cards"]))
        return
    print("\nDEDUP GATE — possible duplicates against the published corpus:")
    for k, nh, p, reasons in hits:
        print(f"  NEW card {k}: {nh!r}")
        print(f"  ~ PUBLISHED {p['id']} (Ed {p['ed']}, {p.get('src','')}): {p['h']!r}")
        print(f"    reason: {'; '.join(reasons)}\n")
    if not sys.stdin.isatty():
        die("Dedup hits present and no interactive terminal to confirm them.")
    ans = input("Review the matches above. Type 'continue' to publish anyway, anything else aborts: ")
    if ans.strip().lower() != "continue":
        die("Duplicate gate not confirmed.")

# ============================= verification =============================

def verify_built(root, landscape, footlines, ed_index, corpus, ed):
    n = sum(len(s["cards"]) for s in ed["sections"])
    checks = []
    ids = re.findall(r'<div class="item-card" id="(c\d+)"', root)
    checks.append(("root: card count", len(ids) == n))
    checks.append(("root: ids sequential", ids == [f"c{i:02d}" for i in range(1, n + 1)]))
    checks.append(("root: pure ASCII (entities throughout)", all(ord(ch) < 128 for ch in root)))
    checks.append(("root: footer archive link", "Past editions" in root))
    checks.append((f"root: metadata edition {ed['edition']}", f"edition: {ed['edition']}\n" in root))

    # Corpus-side checks run in every phase: the landscape BLOCK is mandatory on
    # every card from Edition 1 even though the landscape LAYER is not built yet.
    #
    # brm.2 (per Ed 20 learnings, launch-doc amendment to 6d): the active-thread
    # test applies to THIS EDITION'S cards only. Threads get deactivated as the
    # taxonomy settles -- PMWR did exactly that in Ed 20 -- and historical cards
    # keep the thread they were filed under. Testing the whole corpus against
    # active_threads would kill every build after the first deactivation, and
    # would kill it at a gate that looks like data corruption. Historical cards
    # must have A thread; they need not have a CURRENT one.
    checks.append(("corpus: every card carries a thread", all(c.get("t") for c in corpus["cards"])))
    new_t = {(c.get("landscape") or {}).get("t") for _, c in edition_cards_flat(ed)}
    checks.append(("edition: new-card threads all active",
                   all(x in corpus["active_threads"] for x in new_t if x)))
    retired = sorted({c["t"] for c in corpus["cards"] if c.get("t")} - set(corpus["active_threads"]))
    if retired:
        print(f"  note: {len(retired)} retired thread(s) present on historical cards: {retired}")
    checks.append(("corpus: card count == editions to date",
                   corpus["current_edition"] == ed["edition"]))
    checks.append(("corpus: edition_dates complete", len(corpus["edition_dates"]) == ed["edition"]))

    if LANDSCAPE:
        checks.append(("root: cache-busted footlines hook", f'src="/landscape/footlines.js?v={ed["edition"]}"' in root))
        lc = parse_js(landscape, "const CARDS=", "verify CARDS")
        checks.append(("landscape: CARDS parse == corpus", len(lc) == len(corpus["cards"])))
        checks.append((f"landscape: CURRENT_EDITION={ed['edition']}", f"const CURRENT_EDITION={ed['edition']}" in landscape))
        checks.append(("landscape: EDITION_DATES entries", len(parse_js(landscape, "const EDITION_DATES=", "d")) == ed["edition"]))
        fd = parse_js(footlines, "var D=", "verify D")
        checks.append(("footlines: D.cards == corpus", len(fd["cards"]) == len(corpus["cards"])))
        checks.append(("footlines: threads config preserved", bool(fd.get("threads"))))

    ei = parse_js(ed_index, "const editions = ", "verify editions[]")
    checks.append(("editions index: head entry", ei[0]["number"] == ed["edition"] and ei[0]["file"] is None))
    if ed["edition"] > 1:
        checks.append(("editions index: prior flipped", ei[1]["file"] == f"edition-{ed['edition']-1:02d}.html"))
    checks.append(("editions index: entry count", len(ei) == ed["edition"]))
    ok = True
    for name, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
        ok = ok and passed
    if not ok:
        die("Pre-commit verification failed — see FAIL lines above.")

def verify_live(ed, corpus):
    """Wait until the CDN edge actually serves the new files, then verify.

    v1.2 (Aug 7, 2026): the previous gate polled the root page for `edition: N`
    and broke as soon as it appeared. On a REPUBLISH of an edition that is
    already live, that string is already present -- so the gate never waited,
    and the corpus-size checks compared the new corpus against stale landscape
    and footlines files still cached at the edge. The gate now polls the actual
    invariant (every file agreeing with the corpus), and every fetch carries a
    timestamp nonce so no URL can be answered from a warm cache key.
    """
    want = len(corpus["cards"])
    E = ed["edition"]
    n_cards = sum(len(s["cards"]) for s in ed["sections"])

    def _count(text, anchor, label, key=None):
        # non-fatal parse: parse_js -> die() -> SystemExit while polling a
        # half-propagated file must not abort the run.
        try:
            v = parse_js(text, anchor, label)
        except (SystemExit, Exception):
            return None
        if key is not None:
            v = v.get(key) if isinstance(v, dict) else None
        return None if v is None else len(v)

    def _agrees(root, land, foot):
        if not LANDSCAPE:
            # Phase 1 invariant: the root page is the whole site. Poll the card
            # count too, not just the edition string -- on a REPUBLISH the
            # edition string is already present and the gate would never wait.
            return (f"edition: {E}\n" in root
                    and len(re.findall(r'class="item-card"', root)) == n_cards)
        return (f"edition: {E}\n" in root
                and f"const CURRENT_EDITION={E}" in land
                and _count(land, "const CARDS=", "live CARDS") == want
                and _count(foot, "var D=", "live D", "cards") == want)

    print("\nWaiting for Vercel rebuild ", end="", flush=True)
    deadline = time.time() + 300
    root = land = foot = ""
    ready = False
    while True:
        try:
            q = int(time.time() * 1000)
            root = fetch(f"{SITE}/?nocache={q}")
            if LANDSCAPE:
                land = fetch(f"{SITE}/landscape/index.html?nocache={q}")
                foot = fetch(f"{SITE}/landscape/footlines.js?nocache={q}")
            if _agrees(root, land, foot):
                ready = True
                break
        except Exception:
            pass
        if time.time() >= deadline:
            break
        print(".", end="", flush=True)
        time.sleep(10)

    print(" live." if ready else " timed out.")
    if not ready:
        print("  (5 minutes without full agreement -- reporting the last read below)")

    checks = [
        ("live root: card count", len(re.findall(r'class="item-card"', root)) == n_cards),
        (f"live root: edition {E} metadata", f"edition: {E}\n" in root),
    ]
    if LANDSCAPE:
        checks += [
            (f"live landscape: CURRENT_EDITION={E}", f"const CURRENT_EDITION={E}" in land),
            ("live landscape: corpus size", _count(land, "const CARDS=", "live CARDS") == want),
            ("live footlines: corpus size", _count(foot, "var D=", "live D", "cards") == want),
        ]
    ok = all(p for _, p in checks)
    for name, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print("\nDEPLOY " + ("VERIFIED -- edition is live and consistent."
                         if ok else
                         "COMPLETED WITH FAILURES -- the commits ARE in the repo; tell Claude."))

# ============================= GitHub API =============================

def token():
    if not TOKEN_PATH.exists():
        die(f"No token at {TOKEN_PATH} — run the one-time setup (see instructions).")
    return TOKEN_PATH.read_text().strip()

def gh(method, url, tok, payload=None):
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "pmwr-deploy"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

def commit_file(tok, path, content, message):
    status, body = gh("GET", f"{API}/{path}?ref=main", tok)
    sha = body.get("sha") if status == 200 else None
    payload = {"message": message, "branch": "main",
               "content": base64.b64encode(content.encode()).decode()}
    if sha: payload["sha"] = sha
    status, body = gh("PUT", f"{API}/{path}", tok, payload)
    if status not in (200, 201):
        die(f"GitHub commit failed for {path} (HTTP {status}): {body.get('message')}"
            + (" — token may be expired or lack Contents write access." if status in (401, 403) else ""))
    print(f"  committed {path} ({'update' if sha else 'create'})")

# ============================= modes =============================

def load_local(name, required=True):
    for base in (DL, Path.cwd(), Path(__file__).resolve().parent):
        p = base / name
        if p.exists():
            return p.read_text()
    if required:
        die(f"Could not find {name} in ~/Downloads, the current folder, or next to this script.")
    return None

def fetch_repo(path):
    """Read a file from the repo's CANONICAL state, not from the CDN.

    v1.3 (Aug 7, 2026). raw.githubusercontent is a cache and can serve a copy
    that is minutes stale. Building on a stale corpus is undetectable
    downstream -- every pre-commit check passes, because the build is
    internally consistent with whatever was read. That is how t11 was
    activated during Ed 19 and silently overwritten by the next deploy.

    The authenticated Contents API is not CDN-cached, and the token is already
    loaded for the commit step. Without a token (a --dry-run on a machine that
    has none) this falls back to raw with a cache-busting nonce: weaker, but
    strictly better than the bare CDN read it replaces.

    brm.1: STRICT_READS removes the raw fallback entirely. PMWR kept it for the
    no-token dry-run case, reasoning it was "weaker but strictly better than the
    bare CDN read it replaces." True -- but a silent degrade to the CDN is the
    Ed 19 clobber mechanism, and the machine that runs this always has the
    token. A missing or unreadable token is a stop, not a downgrade. This holds
    regardless of whether the repo is public.
    """
    tok = None
    if TOKEN_PATH.exists():
        tok = TOKEN_PATH.read_text().strip() or None
    if tok:
        status, body = gh("GET", f"{API}/{path}?ref=main", tok)
        if status == 200 and isinstance(body, dict) and body.get("content"):
            if body.get("encoding") == "base64":
                return base64.b64decode(body["content"]).decode("utf-8")
            return body["content"]
        if status == 404:
            raise urllib.error.HTTPError(f"{API}/{path}", 404, "Not Found", None, None)
        die(f"API read of {path} returned HTTP {status}. The token at {TOKEN_PATH} "
            f"cannot read this repo — check it has not expired and still grants "
            f"Contents access to {REPO}. Nothing committed.")
    if STRICT_READS:
        die(f"No token at {TOKEN_PATH}. Canonical reads require it — the raw CDN "
            f"fallback is disabled because a stale read is undetectable "
            f"downstream. Create the token before running.")
    return fetch(f"{RAW}/{path}?nocache={int(time.time() * 1000)}")

def get_repo_or_local(path, local_name):
    try:
        return fetch_repo(path), "repo"
    except urllib.error.HTTPError:
        t = load_local(local_name)
        return t, "local"

# ============================= bootstrap: derive scaffolding from live =============================

def _dec(s): return HTML.unescape(s)

def _page_links(page_html):
    pairs = []
    for mm in re.finditer(r'<div class="item-headline">(.*?)</div>.*?<a class="item-link" href="(.*?)"',
                          page_html, re.S):
        pairs.append((_dec(mm.group(1)).strip(), mm.group(2)))
    return pairs

def build_corpus_from_live(live_land, live_foot):
    cards = parse_js(live_land, "const CARDS=", "Landscape CARDS")
    d = parse_js(live_foot, "var D=", "footlines D")
    cur = int(re.search(r"const CURRENT_EDITION=(\d+)", live_land).group(1))
    dates = parse_js(live_land, "const EDITION_DATES=", "EDITION_DATES")
    active = json.loads(re.search(r"const ACTIVE=new Set\((\[[^\]]*\])\)", live_land).group(1))
    furl = {c["id"]: c["url"] for c in d["cards"] if c.get("url")}
    out = []
    for c in cards:
        c = dict(c)
        c.setdefault("url", furl.get(c["id"]))
        c.setdefault("img", None)
        out.append(c)
    # backfill missing urls from the edition pages themselves
    def norm(s): return re.sub(r"\W+", "", s).lower()
    missing_eds = sorted({c["ed"] for c in out if not c["url"]})
    for ed_no in missing_eds:
        path = "index.html" if ed_no == cur else f"editions/edition-{ed_no:02d}.html"
        try:
            page = fetch(f"{RAW}/{path}")
        except Exception:
            continue
        links = _page_links(page)
        page_ids = re.findall(r'<div class="item-card" id="c(\d+)"', page)
        for c in out:
            if c["ed"] != ed_no or c["url"]:
                continue
            for h, u in links:
                if norm(h) == norm(c["h"]) or norm(c["h"]) in norm(h) or norm(h) in norm(c["h"]):
                    c["url"] = u; break
            if not c["url"] and page_ids and len(links) == sum(1 for x in cards if x["ed"] == ed_no):
                mm = re.match(rf"e{ed_no}c(\d+)$", c["id"])
                if mm:
                    c["url"] = links[int(mm.group(1)) - 1][1]
    still = [c["id"] for c in out if not c["url"]]
    if still:
        die(f"Corpus build: could not resolve URLs for {still} — tell Claude before bootstrapping.")
    return {"schema_version": 1, "current_edition": cur,
            "edition_dates": {str(k): v for k, v in sorted(dates.items(), key=lambda x: int(x[0]))},
            "active_threads": active, "cards": out}

def extract_edition(page):
    g = lambda p: re.search(p, page, re.S)
    ed = {}
    meta = g(r"<!--\s*EDITION METADATA\s*(.*?)-->").group(1)
    ed["edition"] = int(re.search(r"edition:\s*(\d+)", meta).group(1))
    ed["date_long"] = re.search(r"date:\s*(.+)", meta).group(1).strip()
    ed["topics"] = _dec(g(r'<div class="masthead-meta">\d+ Items\s*&nbsp;&bull;&nbsp;\s*(.*?)</div>').group(1)).strip()
    sections = []
    for sm in re.finditer(r'<section id="([^"]+)">(.*?)</section>', page, re.S):
        sid, body = sm.group(1), sm.group(2)
        title = _dec(re.search(r'<span class="section-title">(.*?)</span>', body).group(1))
        cards = []
        for cm in re.finditer(r'<div class="item-card"[^>]*>(.*?)</div>\s*(?=\n*\s*(?:<div class="item-card"|$))',
                              body, re.S):
            cb = cm.group(1)
            tag = re.search(r'<span class="item-tag (tag-\w+)">(.*?)</span>', cb)
            cards.append({
                "tag_class": tag.group(1), "tag_label": _dec(tag.group(2)),
                "headline": _dec(re.search(r'<div class="item-headline">(.*?)</div>', cb, re.S).group(1)).strip(),
                "source": _dec(re.search(r'<div class="item-source">(.*?)</div>', cb, re.S).group(1)).strip(),
                "summary": _dec(re.search(r'<div class="item-summary">(.*?)</div>', cb, re.S).group(1)).strip(),
                "why": _dec(re.search(r'<div class="implication-text">(.*?)</div>', cb, re.S).group(1)).strip(),
                "url": re.search(r'<a class="item-link" href="(.*?)"', cb).group(1),
                "link_label": _dec(re.search(r'rel="noopener">(.*?)\s*&#8599;</a>', cb).group(1)).strip(),
            })
        sections.append({"id": sid, "title": title, "cards": cards})
    pills = [_dec(pm.group(1).replace("<br>", "\n"))
             for pm in re.finditer(r'<div class="summary-pill-label">(.*?)</div>', page, re.S)]
    for s, p in zip(sections, pills):
        s["pill"] = p
    ed["sections"] = sections
    return ed

def templatize(live):
    t = live
    t = re.sub(r"<!--\s*\nEDITION METADATA.*?-->", "<!--{{META}}-->", t, flags=re.S)
    t = re.sub(r"<title>The Prepared Mind, Weekly Reader &mdash; .*?</title>",
               "<title>The Prepared Mind, Weekly Reader &mdash; {{TITLE_DATE}}</title>", t)
    t = re.sub(r'<div class="masthead-date">.*?</div>', '<div class="masthead-date">{{MAST_DATE}}</div>', t)
    t = re.sub(r'<div class="masthead-meta">.*?</div>',
               '<div class="masthead-meta">{{N_ITEMS}} Items &nbsp;&bull;&nbsp; {{TOPICS}}</div>', t)
    t = re.sub(r'(<div class="summary-strip"[^>]*>\n).*?(\n</div>\n\n<nav)', r"\1{{PILLS}}\2", t, flags=re.S)
    t = re.sub(r'(<nav class="section-nav"[^>]*>\n).*?(\n</nav>)', r"\1{{NAV}}\2", t, flags=re.S)
    t = re.sub(r'(<main class="content">\n\n).*?(\n\n<a class="back-to-top")', r"\1{{SECTIONS}}\2", t, flags=re.S)
    t = re.sub(r"\n[^\n<]*&middot; Edition No\. \d+", "\n{{FOOT_DATE}} &middot; Edition No. {{ED_NO}}", t)
    t = re.sub(r'<script src="/landscape/footlines\.js[^"]*" defer></script>',
               '<script src="/landscape/footlines.js?v={{ED_NO}}" defer></script>', t)
    for tok in ("{{META}}", "{{PILLS}}", "{{NAV}}", "{{SECTIONS}}", "{{ED_NO}}"):
        if tok not in t:
            die(f"Templatize: token {tok} missing — live root structure changed.")
    return t

def genesis(dry):
    """Seed an empty repo. Replaces PMWR's --bootstrap.

    Bootstrap derived its scaffolding from an already-live site. At Edition 1
    there is no live site, so genesis goes the other way: it takes the local
    template, the seeded corpus and edition-01.json, builds the first edition
    and the archive shell, and commits the whole initial tree in one pass.
    """
    print("GENESIS — seeding an empty repo from local files.\n")
    tpl = load_local("template_v1.html")
    corpus = json.loads(load_local("corpus.json"))
    ed = json.loads(load_local("edition-01.json"))

    if ed["edition"] != 1:
        die(f"Genesis builds Edition 1; edition-01.json says {ed['edition']}.")
    if corpus.get("current_edition") != 0:
        die(f"Genesis needs a virgin corpus (current_edition 0); got {corpus.get('current_edition')}.")
    if corpus.get("cards"):
        die(f"Genesis needs an empty corpus; got {len(corpus['cards'])} cards.")

    check_authored_text(ed)
    merged, new_cards = merge_corpus(corpus, ed)
    root = build_page(ed, tpl)
    n = sum(len(s["cards"]) for s in ed["sections"])
    edix = build_editions_index_genesis(ed)

    print(f"  template loaded ................ {len(tpl)} bytes")
    print(f"  Edition 1 built ................ {n} cards, {len(ed['sections'])} sections, {len(root)} bytes")
    print(f"  corpus seeded .................. {len(merged['cards'])} cards, threads {merged['active_threads']}")
    print(f"  archive index built ............ 1 entry")

    print("\nPre-commit verification:")
    verify_built(root, None, None, edix, merged, ed)

    files = [
        ("index.html", root, "Genesis: Edition 1"),
        ("editions/index.html", edix, "Genesis: archive index"),
        ("data/corpus.json", json.dumps(merged, indent=1, ensure_ascii=False),
         f"Genesis: corpus ({len(merged['cards'])} cards)"),
        ("data/edition-01.json", json.dumps(ed, indent=1, ensure_ascii=False),
         "Genesis: Edition 1 full card copy"),
        (TEMPLATE, tpl, "Genesis: pinned edition template v1.0"),
        ("pipeline/brm_deploy.py", Path(__file__).resolve().read_text(),
         "Genesis: deploy pipeline v1.3-brm.1"),
    ]
    for path, content, _ in files:
        print(f"  ready: {path} ({len(content)} bytes)")
    if dry:
        BUILD.mkdir(parents=True, exist_ok=True)
        for path, content, _ in files:
            (BUILD / path.replace("/", "__")).write_text(content)
        print(f"\nDRY RUN — files written to {BUILD}, nothing committed.")
        return
    if input("\nType 'genesis' to commit the initial tree: ").strip().lower() != "genesis":
        die("Not confirmed.")
    tok = token()
    print()
    for path, content, msg in files:
        commit_file(tok, path, content, msg)
    print("\nGENESIS COMPLETE — the repo carries its own pipeline. Verifying live:")
    verify_live(ed, merged)

def build_editions_index_genesis(ed):
    shell = load_local("editions_index_v1.html")
    entry = ("[\n{\n" + f"number: {ed['edition']},\ndate: {js_str(ed['date_long'])},\n"
             f"items: {sum(len(s['cards']) for s in ed['sections'])},\n"
             f"topics: {js_str(ed['topics'])},\nfile: null" + "\n}\n]")
    return shell.replace(extract_balanced(shell, "const editions = ", "editions[]"), entry, 1)

def weekly(dry, edition_path=None):
    # 1. locate edition file
    if edition_path:
        ep = Path(edition_path)
    else:
        cands = sorted(glob.glob(str(DL / "edition-*.json")))
        cands = [c for c in cands if re.search(r"edition-\d+\.json$", c)]
        if not cands:
            die("No edition-NN.json found in ~/Downloads.")
        ep = Path(max(cands, key=lambda p: int(re.search(r"edition-(\d+)\.json$", p).group(1))))
    ed = json.loads(ep.read_text())
    n = sum(len(s["cards"]) for s in ed["sections"])
    print(f"Edition file ....... {ep}  (Edition {ed['edition']}, {n} cards, {len(ed['sections'])} sections)")

    # 2. live state + canonical inputs
    corpus_text, src = get_repo_or_local("data/corpus.json", "corpus.json")
    corpus = json.loads(corpus_text)
    template, tsrc = get_repo_or_local(TEMPLATE, "template_v1.html")
    print(f"Corpus ............. {len(corpus['cards'])} cards ({src}) | template ({tsrc})")
    print(f"Active threads ..... {corpus.get('active_threads')}")
    if ed["edition"] != corpus["current_edition"] + 1:
        die(f"Edition {ed['edition']} but corpus current_edition is {corpus['current_edition']} — sequence mismatch.")
    live_root = fetch_repo("index.html")
    live_edix = fetch_repo("editions/index.html")
    live_land = fetch_repo("landscape/index.html") if LANDSCAPE else None
    live_foot = fetch_repo("landscape/footlines.js") if LANDSCAPE else None
    if LANDSCAPE:
        print(f"Live fetched ....... root {len(live_root)}B, landscape {len(live_land)}B, "
              f"footlines {len(live_foot)}B, editions index {len(live_edix)}B")
    else:
        print(f"Live fetched ....... root {len(live_root)}B, editions index {len(live_edix)}B "
              f"(landscape layer not built — Phase 1)")

    # 3. authored-text guard + dedup gate BEFORE anything builds
    check_authored_text(ed)
    dedup_gate(corpus, ed)

    # 4. build everything
    merged, new_cards = merge_corpus(corpus, ed)
    new_root = build_page(ed, template)
    new_land = emit_landscape(live_land, merged) if LANDSCAPE else None
    new_foot = emit_footlines(live_foot, merged) if LANDSCAPE else None
    new_edix = emit_editions_index(live_edix, ed)
    archive_name = f"editions/edition-{ed['edition']-1:02d}.html"
    merged_text = json.dumps(merged, indent=1, ensure_ascii=False)

    # 5. verify built artifacts
    print("\nPre-commit verification:")
    verify_built(new_root, new_land, new_foot, new_edix, merged, ed)

    outputs = [
        (archive_name, live_root, f"Edition {ed['edition']}: archive Edition {ed['edition']-1}"),
        ("editions/index.html", new_edix, f"Edition {ed['edition']}: editions index"),
        ("index.html", new_root, f"Edition {ed['edition']}: publish"),
    ]
    if LANDSCAPE:
        outputs += [
            ("landscape/footlines.js", new_foot, f"Edition {ed['edition']}: footlines corpus"),
            ("landscape/index.html", new_land, f"Edition {ed['edition']}: Landscape corpus"),
        ]
    outputs += [
        ("data/corpus.json", merged_text, f"Edition {ed['edition']}: corpus merge ({len(merged['cards'])} cards)"),
        (f"data/edition-{ed['edition']:02d}.json", ep.read_text(), f"Edition {ed['edition']}: full card copy"),
    ]
    if dry:
        BUILD.mkdir(parents=True, exist_ok=True)
        for path, content, _ in outputs:
            p = BUILD / path.replace("/", "__")
            p.write_text(content)
        print(f"\nDRY RUN — all files written to {BUILD}, nothing committed.")
        print("""
BEFORE YOU SHIP -- gates this script cannot check (Ed 20 doctrine):
  [ ] Every "Read at" URL opened, and every number, byline and date in the
      card matched against the primary. A figure seen only in a search
      snippet is unverified however specific it looks.
  [ ] Any card on a developing matter re-read on ship morning. Counts move.
  [ ] Every figure carries its issuer's own scoping. Figures from different
      scopes never share a sentence.
  [ ] Headlines attributing action to an institution checked for capacity:
      who acted, under what authority. Ranking member != committee.
      Settlement agreement != consent decree. NPRM != final rule.
  [ ] Saturation: for anything already through the legal-trade alert layer,
      the card carries what the alerts did not -- usually the primary itself.

  Type 'ship' at the SCRIPT's prompt on the real run. A stray 'ship' typed at
  the shell is a no-op that looks like a completed step.""")
        return
    if input(f"\nType 'ship' to publish Edition {ed['edition']} ({len(outputs)} commits): ").strip().lower() != "ship":
        die("Not confirmed.")
    tok = token()
    print()
    for path, content, msg in outputs:
        commit_file(tok, path, content, msg)

    # 6. live verification
    verify_live(ed, merged)

if __name__ == "__main__":
    args = sys.argv[1:]
    dry = "--dry-run" in args
    if "--genesis" in args:
        genesis(dry)
    else:
        paths = [a for a in args if not a.startswith("--")]
        weekly(dry, paths[0] if paths else None)
