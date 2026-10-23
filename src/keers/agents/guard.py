"""The number guard: a model writes figures only as ``{fact.key}``; code fills in the value and a citation.

Any number the model types itself (digits, or words such as "million" or "percent") is a problem, and so is a
key that is not on the fact sheet. A text with problems is sent back once with the list; if it still has them,
the agent's output is not used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .facts import FactSheet

PLACEHOLDER = re.compile(r"\{([A-Za-z][A-Za-z0-9_.]*)\}")
# Names that contain digits but are not figures: SEC form types and the usual index names.
ALLOWED = re.compile(r"\b(?:10-K|10-Q|8-K|20-F|40-F|6-K|S&P 500|Russell 2000|Q[1-4]|kci-v[12]|distress-v1|"
                     r"risk-limits-v1|costs-v1|sizing-v1|rules-v1|agents-v1)\b", re.IGNORECASE)  # fmt: skip
DIGITS = re.compile(r"\d")
NUMBER_WORDS = re.compile(
    r"\b(?:percent|per cent|hundred|thousand|million|billion|trillion|bn|mn|bps|basis points?|"
    r"twice|triple|threefold|twofold)\b",
    re.IGNORECASE,
)


@dataclass
class Rendered:
    text: str
    cited: list[str] = field(default_factory=list)  # fact keys used, in order of first use
    problems: list[str] = field(default_factory=list)


def check(text: str, sheet: FactSheet) -> list[str]:
    return render(text, sheet).problems


def render(text: str, sheet: FactSheet, cite: bool = True, cited: list[str] | None = None) -> Rendered:
    """Fill in ``{key}`` placeholders from the fact sheet, numbering each source once, and list any problems.

    Pass the same ``cited`` list for every section of one memo, so a source keeps one number throughout.
    """
    out = Rendered("", cited if cited is not None else [])
    problems: list[str] = []
    for key in PLACEHOLDER.findall(text):
        if key not in sheet.facts:
            problems.append(f"unknown fact key {{{key}}}")
        elif not sheet.facts[key].known:
            problems.append(f"fact {{{key}}} has no value; do not rely on it")
    bare = ALLOWED.sub(" ", PLACEHOLDER.sub(" ", text))
    for m in DIGITS.finditer(bare):
        lo, hi = max(0, m.start() - 15), min(len(bare), m.end() + 15)
        problems.append(f"a number was typed instead of a fact key, near: “{bare[lo:hi].strip()}”")
        break  # one example is enough to correct it
    for m in NUMBER_WORDS.finditer(bare):
        problems.append(f"a quantity was written in words (“{m.group(0)}”); use a fact key instead")
        break

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        f = sheet.facts.get(key)
        if f is None:
            return m.group(0)
        if key not in out.cited:
            out.cited.append(key)
        n = out.cited.index(key) + 1
        return f"{f.formatted()} [{n}]" if cite else f.formatted()

    out.text = PLACEHOLDER.sub(sub, text)
    out.problems = list(dict.fromkeys(problems))
    return out


def keys_in(text: str) -> list[str]:
    return list(dict.fromkeys(PLACEHOLDER.findall(text)))
