"""Obsidian vault = plain folder of .md files. Journal appends + BM25 retrieval +
the self-improving 'brain' write path (auto [[wikilink]] cross-linking, lessons, staging)."""
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path


def _slug(text):
    return re.sub(r"[^\w\- ]+", "", text or "").strip().replace(" ", "_")[:60] or "note"


def related_links(vault, text, exclude_rel=None, k=3):
    """Up to k [[wikilinks]] to existing notes most related to `text` (BM25). This is
    the literal 'brain' behavior — every write threads itself into the graph (§4d)."""
    seen, links = set(), []
    for _score, rel, _chunk in retrieve(vault, text, k=k + 4):
        if rel == exclude_rel:
            continue
        title = Path(rel).stem
        if title in seen or title == "_index":
            continue
        seen.add(title)
        links.append(f"[[{title}]]")
        if len(links) >= k:
            break
    return links


def write_note(vault, relpath, body, link=True):
    """Write a note and, unless it already links out, append a Related line of
    [[wikilinks]] to related existing notes. Returns the absolute path."""
    path = Path(vault) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (body or "").rstrip()
    if link and "[[" not in text:  # don't double-link if the model already wrote links
        rel = Path(relpath).as_posix()
        related = related_links(vault, text, exclude_rel=rel)
        if related:
            text += "\n\n**Related:** " + " ".join(related)
    path.write_text(text + "\n", encoding="utf-8")
    return str(path)


def add_lesson(vault, category, title, body):
    """Record a mistake+fix under Lessons/<category>/ and make sure the lessons Base
    exists. Called by the MCP/cron validators (§5b) and by reflect()."""
    ensure_lessons_base(vault)
    relpath = f"Lessons/{category}/{_slug(title)}.md"
    return write_note(vault, relpath, f"# {title}\n\n{body}\n\nTags: #lesson/{category.lower()}")


def ensure_lessons_base(vault):
    """Create Lessons/Lessons.base (Obsidian Bases table over the Lessons/ folder) once."""
    base = Path(vault) / "Lessons" / "Lessons.base"
    if base.exists():
        return
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(
        'filters:\n  and:\n    - file.folder.startsWith("Lessons")\n'
        "views:\n  - type: table\n    name: Lessons\n    order:\n"
        "      - file.name\n      - file.mtime\n",
        encoding="utf-8",
    )


def stage_note(vault, kind, target_rel, body):
    """Approval OFF: park a proposed note in _pending/ instead of the knowledge base (§4c)."""
    rel = f"_pending/{datetime.now():%Y%m%d_%H%M%S}_{kind}_{_slug(target_rel)}.md"
    return write_note(vault, rel, f"---\nkind: {kind}\ntarget: {target_rel}\n---\n{body}", link=False)


def list_pending(vault):
    """[(name, target, body)] of staged notes awaiting approval."""
    out = []
    for md in sorted((Path(vault) / "_pending").glob("*.md")):
        text = md.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"target:\s*(.+)", text)
        out.append({"name": md.name, "target": (m.group(1).strip() if m else ""), "body": text})
    return out


def approve_pending(vault, name):
    """Move one staged note from _pending/ into its target folder (auto-linking on the way in)."""
    src = Path(vault) / "_pending" / name
    text = src.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"target:\s*(.+)", text)
    target = m.group(1).strip() if m else f"raw/{name}"
    body = re.sub(r"^---.*?---\n", "", text, count=1, flags=re.S)  # strip the staging frontmatter
    path = write_note(vault, target, body)
    src.unlink()
    return path


def append_journal(vault, user_text, assistant_text, tool_notes=None):
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    path = Path(vault) / "Journal" / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"\n## {now:%H:%M}", f"**You:** {user_text}"]
    lines += [f"**Tool:** {n}" for n in tool_notes or []]
    lines += [f"**Jarvis:** {assistant_text}", ""]
    block = "\n".join(lines)
    if not path.exists():
        block = f"# {day}\n{block}"
    with open(path, "a", encoding="utf-8") as f:
        f.write(block)


def _tokens(text):
    return re.findall(r"[a-z0-9]+", text.lower())


_CORPUS_CACHE = {}  # str(path) -> (mtime, size, [(rel, chunk, tokens), ...])


def _file_docs(md, vault):
    """Tokenized paragraph chunks for one .md, cached by (mtime, size).

    # ponytail: mtime+size invalidation only misses an edit that changes neither,
    # which real text edits don't. Entries for deleted files linger (a few KB); fine.
    """
    st = md.stat()
    key = str(md)
    hit = _CORPUS_CACHE.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rel = md.relative_to(vault).as_posix()
    docs = []
    for chunk in re.split(r"\n\s*\n", text):
        chunk = chunk.strip()
        if len(chunk) >= 40:
            docs.append((rel, chunk, _tokens(chunk)))
    _CORPUS_CACHE[key] = (st.st_mtime, st.st_size, docs)
    return docs


def _corpus(vault):
    """All (rel, chunk, tokens) across the vault. Per-file cached, so an unchanged note
    is read and tokenized once, not every query — usually only the Journal changed since
    the last turn."""
    vault = Path(vault)
    docs = []
    for md in vault.rglob("*.md"):
        parts = md.relative_to(vault).parts
        # ponytail: _index.md is the second-brain pipeline's generated map — every
        # source's summary again in one 50KB+ blob. Redundant with the sources it
        # indexes, so skip it (retrieval reads the real Second Brain/ notes directly).
        if ".obsidian" in parts or ".trash" in parts or md.name == "_index.md":
            continue
        docs.extend(_file_docs(md, vault))
    return docs


def retrieve(vault, query, k=6):
    """Hand-rolled BM25 (k1=1.5, b=0.75) over paragraph chunks. Returns [(score, path, chunk)]."""
    k1, b = 1.5, 0.75
    docs = _corpus(vault)
    if not docs:
        return []
    n = len(docs)
    avg_len = sum(len(t) for _, _, t in docs) / n
    df = Counter()
    for _, _, toks in docs:
        df.update(set(toks))
    scored = []
    for rel, chunk, toks in docs:
        tf = Counter(toks)
        score = 0.0
        for term in set(_tokens(query)):
            if term not in tf:
                continue
            idf = math.log((n - df[term] + 0.5) / (df[term] + 0.5) + 1)
            score += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * len(toks) / avg_len))
        if score > 0:
            scored.append((score, rel, chunk))
    scored.sort(key=lambda s: s[0], reverse=True)
    return scored[:k]
