"""Tests for relay.chunk_text.

Checks HARD invariants (must always hold) and reports QUALITY metrics
(break placement, size balance) so we can judge and tune the heuristic.

Run:  python test_chunking.py
"""
import re
import statistics
import relay

LIMIT = relay.LIMIT
URL_RE = relay.URL_RE


def eff(s):
    return relay._eff(s, 0, len(s), relay._url_reductions(s))


def words(s):
    return re.findall(r"\S+", s)


# ---------- invariants ----------
def check_invariants(name, text, reserve=0):
    chunks = relay.chunk_text(text, reserve=reserve)
    errs = []

    # 1. never exceed the limit
    for i, c in enumerate(chunks):
        if eff(c) > LIMIT:
            errs.append(f"post {i+1} eff={eff(c)} > {LIMIT}")

    # 2. no empty posts (unless the whole input was empty, or the trailing
    #    reserve slot which the caller fills with the author suffix)
    for i, c in enumerate(chunks):
        if c == "" and text.strip() and not (reserve and i == len(chunks) - 1):
            errs.append(f"post {i+1} is unexpectedly empty")

    # 3. content preserved: no characters lost or duplicated. Whitespace-insensitive,
    #    so hard-splitting an unbreakable token (which inserts a post boundary mid-token)
    #    is allowed while genuine loss/duplication is still caught.
    strip_ws = lambda s: re.sub(r"\s+", "", s)
    if strip_ws("".join(chunks)) != strip_ws(text):
        errs.append("characters not preserved")

    # 4. every URL in the source appears whole inside exactly one post
    for m in URL_RE.finditer(text):
        u = m.group(0)
        if sum(u in c for c in chunks) != 1:
            errs.append(f"URL not intact/unique: {u[:40]}")

    # 5. no non-final post ends right after a NON-sentence dot (bad break)
    for c in chunks[:-1]:
        c = c.rstrip()
        if c.endswith("."):
            i = len(c) - 1
            if not relay._is_sentence_end(c, i, relay._url_reductions(c)):
                errs.append(f"broke after non-sentence dot: ...{c[-25:]!r}")
    return chunks, errs


# ---------- quality ----------
def break_kinds(text, chunks):
    """Classify each inter-post break by what punctuation precedes it."""
    kinds = []
    for c in chunks[:-1]:
        c = c.rstrip()
        if not c:
            kinds.append("empty")
        elif c[-1] in ".!?" and relay._is_sentence_end(c, len(c) - 1, relay._url_reductions(c)):
            kinds.append("sentence")
        elif c[-1] in ",;:":
            kinds.append("clause")
        else:
            kinds.append("word")
    return kinds


CASES = {
    "short link": "https://arxiv.org/abs/2606.24579 quick share, worth a read",
    "two sentences short": "Nice paper. Worth a read: https://arxiv.org/abs/2601.1",
    "multi-sentence long": (
        "We introduce a new method. It scales cleanly with data and compute. "
        "The key idea is a paired tokenizer, which halves fragmentation. "
        "Results hold across five languages, including low-resource ones. "
        "We release code and checkpoints. Feedback very welcome! "
        "Paper: https://arxiv.org/abs/2606.24579"),
    "abbrev/decimal/arxiv soup": (
        "Great result in Fig. 3 (see e.g. et al. 2026) with id 2606.24579 "
        "and v2.1 numbers like 3.14 everywhere. " * 3 +
        "End. https://arxiv.org/abs/2601.00001"),
    "no punctuation": ("alpha bravo charlie delta echo foxtrot golf hotel "
                       "india juliet kilo lima " * 6).strip(),
    "long clauses only": ("first clause here, second clause here, third here, "
                          "fourth here, fifth here, sixth here, " * 4).strip(),
    "very long url": "check this https://scholar.google.com/scholar_url?url=" + "x" * 240,
    "giant token": "prefix " + "z" * 700 + " suffix",
    "emoji": ("great thread 🧵 with fun 👨‍👩‍👧‍👦 family emoji and math ∑∫ " * 12).strip(),
    "newlines/paragraphs": (
        "Intro paragraph one is here and it continues on for a good while so that "
        "the whole message comfortably exceeds a single post and must be split.\n\n"
        "Second paragraph starts here and also runs on for a bit, long enough that "
        "a paragraph boundary is the natural place to cut the thread.\n\n"
        "Third paragraph wraps it up. https://arxiv.org/abs/2606.24579"),
    "empty": "",
    "whitespace only": "   \n  ",
    "url only": "https://arxiv.org/abs/2606.24579",
    "one long unspaced token": "z" * 500,
}


def main():
    total_fail = 0
    print(f"LIMIT={LIMIT}\n")
    for name, text in CASES.items():
        chunks, errs = check_invariants(name, text)
        sizes = [eff(c) for c in chunks]
        kinds = break_kinds(text, chunks)
        bal = f"{min(sizes)}-{max(sizes)}" if len(sizes) > 1 else str(sizes[0])
        status = "FAIL" if errs else "ok"
        print(f"[{status}] {name}: {len(chunks)} post(s), eff {bal}, breaks={kinds}")
        for e in errs:
            print(f"        !! {e}")
            total_fail += 1

    # reserve behavior
    suffix = "\n— LChoshen"
    chunks, errs = check_invariants("reserve", CASES["multi-sentence long"], reserve=len(suffix))
    room = eff(chunks[-1]) + len(suffix) <= LIMIT
    print(f"\n[{'ok' if room and not errs else 'FAIL'}] reserve: last post leaves room for suffix = {room}")
    total_fail += len(errs) + (0 if room else 1)

    print(f"\n{'ALL PASS' if total_fail==0 else str(total_fail)+' FAILURES'}")
    return total_fail


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
