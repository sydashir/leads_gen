"""Does this website describe an IT / software business? Cheap keyword scoring on what the site says."""
from __future__ import annotations

import re

STRONG = [
    r"\bsoftware\b", r"\bsaas\b", r"\bapis?\b", r"\bcloud\b", r"\bcyber ?security\b", r"\bmanaged (it|services)\b",
    r"\bit (services|support|consulting|solutions|infrastructure)\b", r"\bweb (development|design|apps?|hosting)\b",
    r"\bapp (development|developers?)\b", r"\bmobile apps?\b", r"\bmachine learning\b", r"\bartificial intelligence\b",
    r"\b(ai|a\.i\.)[- ](powered|driven|platform|agents?|assistants?|tools?|native|first)\b",
    r"\bdata (platform|engineering|analytics|pipelines?|science|infrastructure|warehouse)\b", r"\bdevops\b",
    r"\bdevelopers?\b", r"\bsoftware engineer", r"\bcustom software\b", r"\bdigital (products?|platforms?|agency)\b",
    r"\bintegrations?\b", r"\bsystems integration\b", r"\bnetwork(ing)? (security|infrastructure)\b",
    r"\bhelp ?desk\b", r"\bplatforms?\b", r"\bweb ?sites?\b", r"\bwordpress\b", r"\bshopify\b", r"\bcoding\b",
    r"\bfull[- ]stack\b", r"\bopen[- ]source\b", r"\bllms?\b", r"\bautomation\b", r"\btelecom", r"\bvoip\b",
    r"\bhosting\b", r"\bdata ?center\b", r"\bgame (studio|development|developer)\b", r"\bgames?\b",
]
WEAK = [r"\btechnolog", r"\bdigital\b", r"\bdata\b", r"\bonline\b", r"\bapps?\b", r"\bweb\b", r"\bai\b",
        r"\bdesign\b", r"\bengineering\b", r"\bstartup\b", r"\bsystems?\b", r"\bconsult"]
NEG = [r"\brestaurants?\b", r"\bsalon\b", r"\bbarber", r"\bplumb", r"\bhvac\b", r"\broofing\b", r"\blandscap",
       r"\bdental\b", r"\bclinics?\b", r"\breal estate\b", r"\bboutique\b", r"\bbakery\b", r"\bcaf[eé]\b",
       r"\byoga\b", r"\bauto (repair|body)\b", r"\btires?\b", r"\btowing\b", r"\bcleaning\b", r"\bcatering\b",
       r"\bmassage\b", r"\bwedding", r"\bphotograph", r"\bapparel\b", r"\bjewelry\b", r"\bfitness\b"]

_S = [re.compile(p, re.I) for p in STRONG]
_W = [re.compile(p, re.I) for p in WEAK]
_N = [re.compile(p, re.I) for p in NEG]


def _hits(pats, text: str) -> int:
    return sum(1 for p in pats if p.search(text))


def classify(head: str, body: str) -> dict:
    """head = title + description + headings; body = visible text.
    level: strong | weak | unknown (page shows no text) | none (text present, no sign of IT)."""
    head, body = head or "", (body or "")[:12000]
    s_head, s_body = _hits(_S, head), _hits(_S, body)
    w_head, w_body = _hits(_W, head), _hits(_W, body)
    n_head, n_body = _hits(_N, head), _hits(_N, body)
    score = 3 * s_head + min(s_body, 8) + 1 * w_head + 0.5 * min(w_body, 5) - 3 * n_head - 0.5 * min(n_body, 4)
    thin = len(body.strip()) < 400 and len(head.strip()) < 120     # JS-only pages show almost no text
    level = "strong" if score >= 6 else "weak" if score >= 2 else ("unknown" if thin and n_head == 0 else "none")
    return {"score": round(score, 1), "level": level}
