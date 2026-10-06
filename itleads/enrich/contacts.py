"""Read a company's own site for email, phone, founder / leadership and LinkedIn links."""
from __future__ import annotations

import concurrent.futures
import html as htmllib
import json
import re
import time
import unicodedata
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .. import util

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]{1,64}@[a-zA-Z0-9\-]{1,63}(?:\.[a-zA-Z0-9\-]{1,63})*\.[a-zA-Z]{2,24}")
BAD_EMAIL = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|css|js|woff2?|ico)$|sentry|wixpress|example\.|yourdomain|domain\.com|email\.com|"
    r"youremail|your@|name@|user@|test@|@2x|noreply|no-reply|donotreply|u003|@mysite\.|@company\.|@sample|"
    r"appleseed|@mac\.com$|godaddy|privacy@|abuse@|you@|@sentry|@yourcompany|@website\.com|@site\.com", re.I)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]?([2-9]\d{2})[\s.\-]?(\d{4})(?!\d)")
TITLE_WORDS = (r"(?:co-?\s?founder|founder|chief executive officer|ceo|president|owner|chief technology officer|cto|"
               r"chief operating officer|coo|chief product officer|cpo|managing director|managing partner|principal|"
               r"managing member|chief executive|executive director)")
LEAD = re.compile(r"\b" + TITLE_WORDS + r"\b", re.I)
NOT_NAME = set("""our team about contact us home services solutions company leadership meet the who we are what do
founder cofounder co-founder ceo cto coo cfo cio cmo vp svp evp president owner chief executive officer technology operating product
managing director partner principal member read more learn view all get started book call privacy policy terms
cookie careers blog news inc llc corp ltd and or of for in at to a an with by from join now sign login log menu
search follow customer customers testimonial testimonials review reviews client clients""".split())
ORGWORDS = set("""aerospace farms farm digital studio studios labs lab systems group software solutions consulting services
company holdings technologies tech university hospital bank church school health medical dental law""".split())
# words of a business, a page heading or a label: never part of a person's name (a heading such as "Jobs Pipeline" or
# "CFO Questionnaire" sits next to the word Owner or CEO on a page and looks like a name to a pattern)
BIZ_WORDS = set("""pipeline summary team teams leads lead careers career support studio studios labs lab inc llc llp lp ltd plc corp
corporation incorporated company companies co solutions solution services service systems system software technologies technology
tech consulting consultants partners associates group holdings holding enterprises ventures capital media digital agency network
networks platform platforms programs program national international global foundation institute association board council
committee department division center centre offices products product resources security analytics cloud data questionnaire explore
note notes dashboard overview welcome profile profiles staff employees jobs openings positions hiring press news blog events
webinar privacy policy conditions copyright rights reserved newsletter subscribe signup register potential today tomorrow""".split())
PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "di", "da", "dos", "du", "la", "le", "bin", "bint", "al", "el",
             "ben", "ibn", "ten", "ter", "los", "las", "y", "e"}   # "Francis van Roden", "Maria de los Angeles Perez": lower case inside a name
HONORIFICS = {"dr", "mr", "ms", "mrs", "prof", "engr"}
NAME_SUFFIX = {"jr", "sr", "ii", "iii", "iv"}

# Words that are never part of a person's name although they are written like one. Each group names what it catches. A word that
# is also a common surname or given name (Page, Hope, Hall, Day, Cook, Lane, Church, Stack, Gold, Black, Mills, Field, Stone,
# Wood, Law, Long, Short, Strong, Mason, Porter ...) is NOT here: a heading that uses one is caught by the other word in it.
PLACEHOLDER_WORDS = set("""doe lorem ipsum dolor amet consectetur name firstname lastname surname forename placeholder sample test tester
user foo tbd tba anonymous unknown admin administrator public first last full your yourname here insert example""".split())
UI_WORDS = set("""click tap swipe scroll links link responsibility studies papers base map questions question posts articles article
projects project values hours locations location message messages connected connect demo trial account accounts consultation quote
quotes estimates estimate pricing brochure video videos form back chat status order track tracking report reports results stories
practices insights owned insured cards credits next previous prev free help desk office main head regional daily annual weekly
monthly quarterly yearly appointment sunday sundays today now request subscribe try ask find send write visit submit apply accept
decline load open discover watch play download buy shop start how why when where works it its this that these those there you my
me is are say hello reach out talk go sale deals deal update updates offer offers special specials discount discounts coupon promo
copyright notice proudly serving since as on seen trusted powered designed made built award winning happy touch phone fax number
toll mobile tel telephone email emails""".split())
TRADE_WORDS = set("""design development apps intelligence learning machine managed hosting repair care control grooming photography
plumbing plumbers roofing roofers electric electrical electricians landscaping cleaning painting construction contractor contractors
builders remodeling flooring heating cooling insurance mortgage realty realtors attorneys lawyers accounting bookkeeping dentistry
orthodontics chiropractic salon fitness pizza bakery restaurant cafe grill catering florist jewelers pharmacy clinic veterinary
motors towing moving printing graphics productions entertainment academy ministries brewery roasters estate marketing web cyber
domain domains social optimization custom virtual remote voice conferencing disaster recovery backup migration integration testing
audit assessment automation computing blockchain internet transformation architecture technical hardware payroll tax preparation
inspection roof pest dental pet auto yoga food truck strategy brand quality assurance management engineering database supply chain
risk compliance works industries manufacturing supplies equipment machinery tools brothers bros sons sisters widgets widget acme
penetration reliability""".split())
ROLE_WORDS = set("""manager managers administrator analyst scientist architect technician specialist coordinator assistant ambassador
operator engineer developer consultant senior junior vice general front dev ops devops business property better bureau microsoft
google amazon oracle cisco salesforce comptia facebook linkedin twitter instagram youtube yelp trustpilot accredited certified
certification platinum channel gallery feed customer""".split())
PLACE_WORDS = {"coast", "states", "valley"}
# slogans and brand-style words of company names ("Prime Time", "Pixel Perfect", "Licensed Bonded"): none is a surname
BRAND_WORDS = set("""prime bold pure rapid clear quantum pixel byte future vision move genius growth time leap perfect ninja lining
notch horizon point licensed bonded action items""".split())
JUNK_WORDS = PLACEHOLDER_WORDS | UI_WORDS | TRADE_WORDS | ROLE_WORDS | PLACE_WORDS | BRAND_WORDS
# words that are real surnames but also business or label words: accepted as the LAST word only after a given name (Steve Jobs, Dee Staff)
SURNAMEABLE = {"jobs", "press", "cloud", "board", "center", "centre", "home", "service", "register", "staff", "welcome", "note",
               "read", "more", "book", "sales", "news", "capital"}
LAST_OK = {"do", "co", "to"}                                   # Tuan Do, Jose Co, Mary Ann To: surnames, accepted as the last word
STREET_WORDS = {"street", "avenue", "road", "lane", "drive", "boulevard", "highway", "parkway", "court", "place", "circle", "terrace",
                "trail", "path", "square", "plaza", "alley", "way"}   # "Oak Lane" is a street, "David Lane" is a person
LABEL_WORDS = {"plan", "option", "type", "phase", "section", "class", "level", "step", "grade", "size", "version", "part", "item",
               "tier", "zone", "unit", "room", "floor", "suite", "vitamin", "team", "group", "chapter", "figure", "table", "exhibit"}
SCENERY = set("""blue red green white black gold golden silver gray grey brown orange purple pink north south east west northern southern
eastern western iron steel copper stone rock rocks ridge hill hills grove bend view breeze peak peaks mountain mountains valley lake
creek springs harbor harbour bay island pine cedar maple oak elm birch eagle falcon hawk phoenix tiger lion dragon sun moon star stars
sky ocean sea summit lone big top true gate horizon alpha omega delta sigma gamma beta apex zenith vertex force predator rising""".split())
PHRASES = set("""new york|new jersey|new mexico|new hampshire|north carolina|south carolina|north dakota|south dakota|west virginia|rhode island|
los angeles|san francisco|san diego|san jose|san antonio|las vegas|new orleans|kansas city|salt lake city|oklahoma city|fort worth|el paso|
long beach|virginia beach|st louis|saint louis|st paul|santa ana|santa fe|santa clara|palo alto|silicon valley|bay area|wall street|gulf coast|
west coast|east coast|new england|great lakes|united states|united kingdom|north america|south america|middle east|hong kong|new delhi|
buenos aires|mexico city|tel aviv|abu dhabi|sao paulo|rio de janeiro|cape town|kuala lumpur|new zealand|south africa|district of columbia|
jane doe|john doe|jane q public|john q public|mickey mouse|minnie mouse|donald duck|santa claus|foo bar|first last|name surname|
test user|mary sue|joe bloggs|joe blow|john public|jane public|average joe""".replace("\n", "").split("|"))
# one-word states (not Washington: a common surname) and big cities, for "Austin Texas" and the like
STATES = set("""alabama alaska arizona arkansas california colorado connecticut delaware florida georgia hawaii idaho illinois indiana iowa
kansas kentucky louisiana maine maryland massachusetts michigan minnesota mississippi missouri montana nebraska nevada ohio oklahoma
oregon pennsylvania tennessee texas utah vermont virginia wisconsin wyoming""".split())
CITIES = set("""austin houston dallas phoenix portland seattle denver boston chicago atlanta miami orlando tampa detroit memphis nashville
charlotte sacramento oakland plano irving philadelphia pittsburgh cleveland columbus cincinnati milwaukee minneapolis baltimore richmond
raleigh tucson fresno omaha tulsa wichita lexington louisville buffalo rochester albany hartford stamford bridgeport""".split())
# common given names: they settle "Steve Jobs" (a person) against "Jobs Pipeline" (a heading) and "David Lane" against "Oak Lane"
GIVEN = set("""aaron abby abigail adam adrian aiden alan albert alec alex alexander alexis alfred alice alicia alison allan allen allison
alyssa amanda amber amy ana andre andrea andrew andy angela angie anita ann anna anne annie anthony antonio april arthur ashley audrey
austin barbara barry beatrice becky ben benjamin bernard beth betty beverly bill billy blake bob bobby bonnie brad bradley brandon
brenda brent brett brian brittany bruce bryan caleb callie calvin cameron carl carla carlos carmen carol caroline carolyn carrie
casey cassandra catherine cathy cecilia chad charles charlene charlie charlotte chase chelsea cheryl chip chloe chris christian
christina christine christopher cindy claire clara clarence claudia clay clifford clint cody colin connie connor corey courtney craig
crystal curtis cynthia dale dallas dan dana daniel danielle danny darren dave david dawn dean deanna debbie deborah debra denise dennis
derek diana diane diego dolores don donald donna doris dorothy doug douglas drew duane dustin dwayne earl ed eddie edgar edith edward
edwin eileen elaine eleanor elena eli elijah elizabeth ella ellen elliot emily emma enrique eric erica erik erin ernest esther ethan
eugene eva evan evelyn faith fay felicia felix fernando fiona florence frances francis francisco frank franklin fred freddie gabriel
gail gary gavin gene geoffrey george gerald geraldine gerard gilbert gina glen glenn gloria gordon grace graham grant greg gregory
gretchen guy gwen hailey hal hank hannah harold harry harvey hazel heather heidi helen henry herbert howard hugh hunter ian ida
irene iris isaac isabel isabella ivan ivy jack jackie jacob jacqueline jade jake james jamie jan jane janet janice jared jasmine
jason jay jean jeff jeffrey jenna jennifer jenny jeremy jerome jerry jesse jessica jill jim jimmy joan joann joanna joe joel john
johnny jon jonathan jordan jose joseph josephine joshua joy joyce juan judith judy julia julian julie june justin karen kate katherine
kathleen kathryn kathy katie kay keith kelly ken kenneth kevin kim kimberly kirk kristen kristin kyle lance larry laura lauren laurie
lawrence leah lee leo leon leonard leslie lewis lillian lily linda lisa liz lloyd logan lois lori lorraine louis louise lucas lucy
luis luke lydia lynn mabel madison maggie marc marcia marcus margaret maria marie marilyn marion mark marshall martha martin marvin
mary mason matt matthew maureen max megan melanie melissa michael michelle mike mildred miles miriam mitchell molly monica morgan
nancy naomi natalie nathan neil nell nicholas nick nicole nina noah noel nora norma norman olivia oscar owen pam pamela pat patricia
patrick paul paula pauline pedro peggy penny pete peter phil philip phillip phyllis rachel ralph ramon randall randy raul ray raymond
rebecca regina reginald renee rhonda ricardo rich richard rick rita rob robert roberta robin rodney roger roland ron ronald rosa rose
rosemary ross roy ruby russell ruth ryan sabrina sally sam samantha samuel sandra sandy sara sarah scott sean sergio seth shane
shannon sharon shawn sheila shelly sherry shirley sidney simon sofia sonia stacy stan stanley stella stephanie stephen steve steven
stuart sue susan suzanne sylvia tamara tammy tanya ted teresa terry thelma theodore theresa thomas tim timothy tina todd tom tommy
tony tracy travis trevor tyler valerie vanessa vera vernon veronica vicki vickie victor victoria vincent viola violet virginia vivian
wade walter wanda warren wayne wendy wesley whitney will william willie wilma yolanda yvonne zachary zoe dee greta gus lou lars cora eve ned rita walt fred
ahmed ali amir amit anil anjali arjun asha ayesha chen deepak elif fatima hana hassan hiroshi hyun imran ivana jamal jin kai kamal
kofi lars li lin mahmoud mai mei min mohamed mohammad mohammed nadia naveen nikhil omar pavel pooja priya rahul raj rajesh ravi reza
rohit sanjay sergei shirin sunil tariq tuan vikram wei xin yusuf yuki zara
anders bjorn dmitri elena emil erik freya gunnar hans heidi henrik ingrid jan jens karl klaus lukas magnus nils olga otto pieter
sven tomas ulrich
alejandro alicia ana andres beatriz camila carmen claudia cristina diego eduardo elena emilio gabriela gonzalo guillermo javier
jorge juan julio lucia luz manuel marco mateo miguel natalia pablo paola rafael ramon rosa santiago sofia valentina xavier""".split())

QUOTE_NODES = "blockquote, q, cite, [class*=testimonial], [class*=review], [class*=quote], [id*=testimonial]"
LEGAL_LINK = re.compile(r"privacy|terms|legal|imprint|impressum", re.I)
NAME_MAX = 60
TITLE_MAX = 60


class Page:
    """What we keep of a fetched page."""

    def __init__(self, url: str, content: bytes):
        self.url, self.content = url, content


def _case_word(w: str, inside: bool) -> str:
    """'o'hare' -> "O'Hare", 'russo-lynch' -> 'Russo-Lynch', 'j.r.' -> 'J.R.'; a particle such as 'van' inside a name stays small."""
    if inside and w.lower() in PARTICLES:
        return w.lower()
    if w.rstrip(".").lower() in ("ii", "iii", "iv"):               # MARK ELLISON III -> Mark Ellison III
        return w.upper()
    low = w.lower().replace("i\u0307", "i")                         # a Turkish dotted capital I lower-cases to i + a combining dot
    return re.sub(r"(^|['’\-.])([^\W\d_])", lambda m: m.group(1) + m.group(2).upper(), low)


def _latin(ch: str) -> bool:
    o = ord(ch)
    return o <= 0x24F or 0x1E00 <= o <= 0x1EFF                       # Latin, Latin Extended-A/B and the Vietnamese block


def _name_word(core: str) -> bool:
    """A capital letter first, then letters, apostrophes and hyphens: Latin letters of any accent (Łukasz, Žaneta, Šimon, Đorđe,
    İbrahim, Ūdris, Nguyễn), whatever the capital: the test is str.isupper(), not a list of ranges."""
    return bool(core) and core[0].isupper() and _latin(core[0]) and all(
        (c.isalpha() and _latin(c)) or c in "'-" for c in core[1:])


def _initial(t: str) -> bool:
    """'J' 'J.' 'J.R.' 'A.J.K.': one to three capital letters standing for given names."""
    return bool(re.fullmatch(r"(?:[^\W\d_]\.){1,3}|[^\W\d_]", t)) and t.replace(".", "").isupper()


MAX_TOKENS = 7                    # "Maria de los Angeles Perez" is five; the particles do not count towards the four names


def okname(n, registry: bool = False, company: str = ""):
    """The name tidied if it looks like a person's, else None: 2 to 4 names (a middle initial counts as one, a small particle
    such as 'de la' or 'van der' inside does not), letters apostrophes hyphens and a final period only, every word starting with
    a capital, no digits, colons, commas or line breaks, no business, heading, label, placeholder or service word (Jobs Pipeline,
    Case Studies, Jane Doe, Web Design, Johnson Brothers Plumbing), no street (Oak Lane, but David Lane is a person), no place
    (New York, Los Angeles), no acronym next to ordinary words (BBB National Programs), no run-together capitals (OneGCP), no
    sentence end inside (Smith. Questions), at most 60 characters. Initials may lead (J. R. Smith, J. Michael Straczynski) or sit
    between (John A. Smith); a single letter may be a surname after a full given name (Henry X). A name written all in capitals is
    put in ordinary case; so is one all in small letters when it comes from a state filing (registry=True), where people type it
    that way. `company` is the company's own name: a heading that is only that name is not a person (a state filing may name a
    person after their own one-person company, so registry names are exempt)."""
    if not isinstance(n, str):
        return None
    n = unicodedata.normalize("NFC", n).strip(" ,.-–—|•:")
    if not n or re.search(r"[\n\r\t\x00-\x1f]", n) or len(n) > NAME_MAX:
        return None
    toks = [t for t in n.replace("’", "'").split(" ") if t]
    if toks and toks[0].lower().strip(".") in HONORIFICS and len(toks) > 2:
        toks = toks[1:]
    if not 2 <= len(toks) <= MAX_TOKENS:
        return None
    last = len(toks) - 1
    text = " ".join(t for i, t in enumerate(toks) if not (i == last and t.rstrip(".").lower() in NAME_SUFFIX))   # "ROBERT GRANT Jr"
    if text.isupper() or (registry and text.islower()):
        toks = [_case_word(t, 0 < i < last) for i, t in enumerate(toks)]
    particle = [0 < i < last and t.rstrip(".").islower() and t.rstrip(".").lower() in PARTICLES for i, t in enumerate(toks)]
    if not 2 <= particle.count(False) <= 4:
        return None
    lead = 0                                                              # J. R. Smith, J.R. Smith, J. Michael Straczynski
    while lead < last and _initial(toks[lead]):
        lead += 1
    if lead and (lead > 2 or sum(len(t.replace(".", "")) for t in toks[:lead]) + len(toks) - lead < 3):
        return None                                                       # "J. Smith" alone says too little
    first_given = toks[0].rstrip(".").lower() in GIVEN
    for i, t in enumerate(toks):
        if particle[i]:
            continue
        if i < lead or (0 < i < last and _initial(t)):                    # an initial: nothing more to check
            if not _initial(t):
                return None
            continue
        core = t.rstrip(".")
        letters = core.replace("'", "").replace("-", "")
        if not _name_word(core) or len(core) > 24 or not letters:
            return None
        if t.endswith(".") and len(letters) > 4:                  # "Wexmere. No": a sentence, not an abbreviation
            return None
        if len(letters) == 1:                                     # a single letter is a surname only after one full given name
            if not (i == last and len(toks) == 2 and lead == 0 and len(toks[0].rstrip(".")) >= 2
                    and letters not in "AI" and toks[0].lower() not in LABEL_WORDS and not t.endswith(".")):
                return None
            continue
        if letters.lower() in NAME_SUFFIX and i == last:
            continue
        if len(letters) >= 3 and letters.isupper():                # BBB, AWS, LLC: an acronym is not a name
            return None
        if re.search(r"[a-z][A-Z]{2,}", core):                     # OneGCP
            return None
        low = letters.lower().replace("-", "")
        parts = core.lower().split("-")
        if i == last and (low in LAST_OK and not t.endswith(".") or (first_given and low in SURNAMEABLE)):
            continue                                               # Tuan Do, Jose Co, Steve Jobs
        if low in NOT_NAME or low in BIZ_WORDS or low in JUNK_WORDS or any(p in BIZ_WORDS or p in JUNK_WORDS for p in parts):
            return None
    words = [t.rstrip(".").lower() for t in toks]
    if " ".join(words) in PHRASES or (words[-1] in STREET_WORDS and not first_given) or all(w in SCENERY for w in words):
        return None
    if len(words) == 2 and words[1] in STATES and words[0] in CITIES | STATES:
        return None                                                # "Austin Texas": a place, not a person
    if company and not registry:
        own = set(util.core_tokens(company))
        if own and set(w for w in words if w not in util.LEGAL_SUFFIX) <= own:
            return None                                            # the company's own name is not a person
    return " ".join(toks)


TITLE_CHARS = re.compile(r"[A-Za-z0-9 &,.'’\-–—/()+;·|•]+\Z")
TITLE_FRONT = re.compile(r"^(?:meet|about|our|the|your|a|an)\s+", re.I)
TITLE_TAIL = re.compile(r"(?:\s+(?:and|of|at|for|the|to|in|with|&)|[\s,;/\-–—|•·&]+)\Z", re.I)
TITLE_SPLIT = re.compile(r"\s*[·|•;]\s*|\s+[\-–—]\s+")
DASHES = {c: "-" for c in (*range(0x2010, 0x2016), 0x2212)}      # U+2010-2015 (hyphens, dashes) and the minus sign: all a plain hyphen


ABBREV = {"CEO", "COO", "CTO", "CFO", "CIO", "CMO", "CPO", "CRO", "CISO", "CDO", "CCO", "CSO", "CAO", "CHRO", "VP", "SVP", "EVP", "MD",
          "HR", "IT", "AI", "QA", "UX", "UI", "MBA", "CPA", "LLC", "INC", "SEO", "AWS", "SAP", "ERP", "CRM", "PMO", "NOC", "PMP",
          "CISSP", "DBA"}
SMALL = {"AND", "OF", "AT", "FOR", "THE", "TO", "IN", "WITH", "OR", "AN", "ON", "BY"}


def _unshout(title: str) -> str:
    """A title typed all in capitals, put in ordinary case: CHIEF -> Chief, CO-FOUNDER -> Co-Founder, VP OF SALES -> VP of Sales;
    CEO, CISO, CPA, AWS and the like stay as they are. Used only when the WHOLE title is in capitals: in a title written in
    ordinary case an acronym (Group LLC, MBA Candidate, SEO Director, Owner, CPA) is left alone."""
    def fix(m):
        w = m.group()
        letters = w.replace("-", "").replace("'", "").replace("’", "")
        if m.start() and w in SMALL:
            return w.lower()
        if len(letters) < 3 or w in ABBREV:
            return w
        return "-".join(p if p in ABBREV else p.capitalize() for p in w.split("-"))
    return re.sub(r"[A-Za-z]+(?:[-'’][A-Za-z]+)*", fix, title)


def _trim_title(t: str) -> str:
    """Strip separators from both ends, but keep a dot that belongs to the first word (.NET Developer)."""
    m = re.match(r"[\s,;:.\-–—|•·]*", t)
    lead, rest = m.group(), t[m.end():]
    if lead.endswith(".") and rest[:1].isalpha():
        rest = "." + rest
    return rest.rstrip(" ,;:.-–—|•·")


def clean_title(t, limit: int = TITLE_MAX) -> str:
    """A job title as plain text of at most 60 characters, or '' when what was found is not a title: letters, digits and a
    few marks only (no colon, quote, dollar sign, markup or line break), a heading word such as 'Meet the' dropped, an
    ending such as 'Owner of' or 'Co-Founder &' trimmed, a title written in capitals put in ordinary case (one in ordinary
    case is never changed: Group LLC, iOS Developer). Every kind of dash is written as a plain hyphen. A title that is
    too long is cut to its first part ('Founder · MBA, Harvard ...' -> 'Founder') when that part is a title by itself."""
    if not isinstance(t, str):
        return ""
    t = re.sub(r"\s+", " ", t.translate(DASHES)).strip()
    t = _trim_title(t)
    for _ in range(3):
        t = TITLE_FRONT.sub("", t, count=1)
    t = _trim_title(t)
    if len(t) > limit:
        t = next((p.strip() for p in TITLE_SPLIT.split(t) if p.strip() and len(p.strip()) <= limit and LEAD.search(p)), "")
    for _ in range(3):
        t = TITLE_TAIL.sub("", t).strip()
    if t.startswith("(") and t.endswith(")") and t.count("(") == 1:
        t = t[1:-1].strip()
    if not t or len(t) > limit or not TITLE_CHARS.match(t):
        return ""
    if t.isupper():
        t = _unshout(t)
    return t if re.match(r"[a-z][A-Z]", t) else t[:1].upper() + t[1:]          # iOS Developer keeps its small first letter


same_site = util.same_site                  # the email's domain is the site's or a subdomain of it (never a look-alike)


def _cf_decode(h: str) -> str:
    try:
        k = int(h[:2], 16)
        return "".join(chr(int(h[i:i + 2], 16) ^ k) for i in range(2, len(h), 2))
    except Exception:
        return ""


def load(url: str, timeout: int = 8):
    """Fetch one page with a hard deadline and size cap. Page or None (never raises)."""
    for verify in (True, False):
        try:
            r, body = util.get_bounded(url, timeout=timeout, deadline=20.0, max_bytes=2_000_000, verify=verify)
            if r.status_code == 429:                        # a busy host asking for patience: one more try
                time.sleep(2.5)
                r, body = util.get_bounded(url, timeout=timeout, deadline=20.0, max_bytes=2_000_000, verify=verify)
        except requests.exceptions.SSLError:
            continue
        except Exception:
            return None
        ct = r.headers.get("content-type", "").lower()
        if r.status_code >= 400 or ("html" not in ct and "text" not in ct) or not body:
            return None
        return Page(r.url, body)
    return None


def emails_in(soup, text: str) -> list:
    """mailto links, Cloudflare-obfuscated addresses and addresses in the visible text (never raw script code)."""
    found = []
    for a in soup.select("a[href^=mailto]"):
        for e in re.split(r"[;,]", unquote(a["href"][7:]).split("?")[0]):
            if "@" in e:
                found.append(e.strip())
    for t in soup.select("[data-cfemail]"):
        e = _cf_decode(t.get("data-cfemail", ""))
        if "@" in e:
            found.append(e)
    un = htmllib.unescape((text or "")[:TEXT_CAP])
    # bounded spaces only: a pattern that starts with \s* takes quadratic time on a page that is one long run of blanks
    un = re.sub(r"\s{0,3}[\[\(]\s{0,3}at\s{0,3}[\]\)]\s{0,3}", "@", un, flags=re.I)
    un = re.sub(r"\s{0,3}[\[\(]\s{0,3}dot\s{0,3}[\]\)]\s{0,3}", ".", un, flags=re.I)
    found += EMAIL_RE.findall(un)
    out = []
    for e in found:
        e = unquote(e).strip(".,;:()<>\"' ").lower()
        if e.count("@") != 1 or BAD_EMAIL.search(e) or e in out or not util.valid_email(e):
            continue
        out.append(e)
    return out


# wording around a number that belongs to somebody else: a PR agency, an arbitrator, a complaints body, a regulator
OTHER_PEOPLES = re.compile(r"media contact|press contact|public relations|press inquir|media inquir|for press|for media|investor relations|"
                           r"arbitrat|adr\.org|dispute|complain|better business|\bbbb\b|dmca|copyright agent|regulator|attorney general|ombuds")
LEGAL_PATH = re.compile(r"privacy|terms|legal|policy|policies|imprint|impressum|cookie|gdpr|dpa\b", re.I)


TEL_EXT = re.compile(r"[;,#]|ext|x|(?<=\d)[pw](?=\d)", re.I)


def phones_in(soup, text: str) -> list:
    """Phone numbers a company could answer on: tel: links first, then numbers in the text. Never a fax, a number named
    in the same sentence as a press office, arbitrator or regulator, or one that cannot be a company's line
    (util.placeholder_phone: any 555 number, an exchange starting with 0 or 1, a personal-number area code ...)."""
    ranked, seen = [], set()

    def add(d):
        d = util.usable_phone(d)
        if d and d not in seen:
            seen.add(d)
            ranked.append(d)

    for a in soup.select("a[href^=tel]"):
        # "telecom.html" also starts with tel: no colon, no number. An extension ends the number: ;ext=2210 ,2210 x2210 #2210 p2210
        add(TEL_EXT.split(unquote(a["href"].partition(":")[2]), 1)[0])
    for m in PHONE_RE.finditer(text):
        before = text[max(0, m.start() - 80):m.start()].lower()
        if "fax" in before[-14:] or OTHER_PEOPLES.search(re.split(r"[.\n]\s", before)[-1]):     # same sentence only
            continue
        add(m.group(1) + m.group(2) + m.group(3))
    return ranked


def _abs(base: str, href: str) -> str:
    """urljoin that gives up quietly on a link no browser could follow (for example https://[YOUR-URL]/contact)."""
    try:
        u = urljoin(base, href)
        urlparse(u)
        return u
    except ValueError:
        return ""


def _jsonld(soup) -> list:
    objs = []
    for t in soup.find_all("script", type=re.compile(r"ld\+json")):
        try:
            d = json.loads(t.string or "")
        except Exception:
            continue
        stack = [d]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack += x
            elif isinstance(x, dict):
                objs.append(x)
                stack += [v for v in x.values() if isinstance(v, (dict, list))]
    return objs


def _ld_email(v) -> str:
    e = str(v or "").replace("mailto:", "").strip().lower()          # not repaired: an address with junk in it is not used
    return e if util.valid_email(e) else ""


def _person(name, title, email: str):
    """One person as the lookup keeps them, or None when the name is not a person's (okname) or the title is no title."""
    n = okname(util.clean_text(name, 120))
    return {"name": n, "title": clean_title(util.clean_text(title, 120)), "email": email} if n else None


def people_ld(objs) -> list:
    out = []
    for o in objs:
        ty = o.get("@type")
        ty = " ".join(str(t) for t in ty) if isinstance(ty, list) else str(ty)
        found = []
        if "Person" in ty and o.get("name"):
            found.append(_person(o["name"], o.get("jobTitle") or "", _ld_email(o.get("email"))))
        for k in ("founder", "founders"):
            v = o.get(k)
            for p in (v if isinstance(v, list) else [v] if v else []):
                if isinstance(p, dict) and p.get("name"):
                    found.append(_person(p["name"], "Founder", _ld_email(p.get("email"))))
                elif isinstance(p, str):
                    found.append(_person(p, "Founder", ""))
        out += [f for f in found if f]
    return out


NAME_TOK_BY = r"[A-Z][a-zA-Z'’\-]{1,20}"            # no final period: "founded by Ada Lovelace. Questions?" ends at the dot


def people_text(text: str) -> list:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    out = []
    for i, l in enumerate(lines):
        if len(l) > 110 or not LEAD.search(l):
            continue
        m = re.match(r"^(.{3,45}?)\s*[,\-–—|•:/(]\s*(.{3,70})$", l)
        if m:
            a, b = m.group(1), m.group(2).rstrip(")")
            if okname(a) and LEAD.search(b) and not LEAD.search(a):
                out.append((okname(a), b.strip()))
                continue
            if okname(b) and LEAD.search(a) and not LEAD.search(b):
                out.append((okname(b), a.strip()))
                continue
        if okname(l):
            continue
        for j in (i - 1, i + 1):                          # a name on the line above or below a line that is only a title
            if 0 <= j < len(lines):
                n = okname(lines[j])
                if n and not LEAD.search(lines[j]) and clean_title(l):
                    out.append((n, l))
                    break
    for m in re.finditer(r"(?:founded|started|created|co-founded|established|owned|run|operated)\s+by\s+((?:Dr\.? )?"
                         + NAME_TOK_BY + r"(?: " + NAME_TOK_BY + r"){1,2})", text):
        n = okname(m.group(1))
        if n:
            out.append((n, "Founder"))
    res = []
    for n, t in out:
        title = clean_title(t)
        if title:
            res.append({"name": n, "title": title, "email": ""})
    return res


def leaders_text(soup) -> list:
    """People with leadership titles found in the page text, ignoring customer quotes and reviews."""
    for node in soup.select(QUOTE_NODES):
        node.decompose()
    return people_text(soup.get_text("\n", strip=True))


LI_URL = re.compile(r"^https?://(?:[a-z]{2,3}\.|www\.)?linkedin\.com/(in|company)/([A-Za-z0-9\-_%.]{2,100})/?$", re.I)


def li_clean(href: str) -> str:
    """A page can claim anything is a LinkedIn link. Keep only a plain profile or company address."""
    h = (href or "").split("?")[0].split("#")[0].strip()
    m = LI_URL.match(h)
    return f"https://www.linkedin.com/{m.group(1).lower()}/{m.group(2)}" if m else ""


def linkedin(soup):
    """-> (company url, [(person url, own text, [ancestor texts nearest first])])"""
    company, people = "", []
    texts: dict = {}                                    # text of a block, worked out once however many links sit in it
    for a in soup.find_all("a", href=True)[:MAX_LINKS]:
        h = a["href"]
        if "linkedin.com/company/" in h:
            if not company:
                company = li_clean(h)
        elif "linkedin.com/in/" in h and li_clean(h):
            if len(people) >= 40:                       # more profile links than any team page has
                continue
            own = " ".join([a.get_text(" ", strip=True)[:300], a.get("aria-label", ""), a.get("title", "")])
            anc, p = [], a.parent
            for _ in range(4):
                if p is None:
                    break
                if id(p) not in texts:
                    texts[id(p)] = p.get_text(" ", strip=True)[:600]
                anc.append(texts[id(p)])
                p = p.parent
            people.append((li_clean(h), own, anc))
    return company, people


def match_linkedin(person: str, others: list, links: list) -> str:
    """A profile counts only if its slug carries the person's name, its own label names them, or the nearest
    block around the link names them and nobody else on the team."""
    parts = person.lower().split()
    first, last = parts[0], parts[-1]
    for url, own, anc in links:
        slug = url.lower()
        if first in slug and last in slug:
            return url
        if person.lower() in own.lower():
            return url
    for url, own, anc in links:
        for block in anc:
            low = block.lower()
            if person.lower() in low:
                if not any(o.lower() in low for o in others):
                    return url
                break
    return ""


def _head_text(soup) -> str:
    bits = []
    if soup.title:
        bits.append(soup.title.get_text(" ", strip=True))
    for sel in ({"name": "description"}, {"property": "og:title"}, {"property": "og:description"},
                {"property": "og:site_name"}):
        m = soup.find("meta", attrs=sel)
        if m and m.get("content"):
            bits.append(m["content"])
    bits += [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"])[:8]]
    return " . ".join(bits)


def _visible(soup) -> str:
    for t in soup(["script", "style", "noscript", "template", "svg"]):
        t.decompose()
    return soup.get_text("\n", strip=True)[:TEXT_CAP]


TEXT_CAP = 200_000            # characters of page text any pattern ever sees
MAX_LINKS = 400               # links per page that are looked at
PAGE_HINT = re.compile(r"contact|about|team|people|leader|founder|company|who-we|our-story|meet|staff|story", re.I)
# pages that carry other people's contact details (a PR agency's number on a press release, a recruiter's address, a customer's
# name on a success story) are not read
SKIP_PATH = re.compile(r"\.(pdf|jpe?g|png|gif)$|blog|/tag/|/category/|privacy|terms|login|cart|shop"
                       r"|news|press|article|media|career|/jobs?\b|/events?\b|webinar|podcast|/posts?\b"
                       r"|case-stud|success-stor|client-stor|customer-stor|testimonial|perspective|insight|whitepaper|ebook", re.I)


def scrape(url: str, domain: str, company_name: str = "", home=None) -> dict:
    """Read the home page (reused if already fetched) plus a few contact / about / team / legal pages."""
    home = home or load(url)
    out = {"ok": False, "emails": [], "phones": [], "legal_only_emails": [], "legal_only_phones": [], "people": [],
           "linkedin_company": "", "head": "", "body": "", "pages": 0}
    if home is None:
        return out
    base_host = urlparse(home.url).netloc.replace("www.", "")
    root = f"{urlparse(home.url).scheme}://{urlparse(home.url).netloc}"
    home_soup = BeautifulSoup(home.content, "lxml")
    soups = [(home, home_soup)]
    seen = {home.url.rstrip("/")}

    cand = []
    for a in home_soup.find_all("a", href=True)[:MAX_LINKS]:
        u = _abs(home.url, a["href"]).split("#")[0]
        if not u:
            continue
        p = urlparse(u)
        if p.netloc.replace("www.", "") != base_host or SKIP_PATH.search(p.path):
            continue
        if PAGE_HINT.search(p.path + " " + a.get_text(" ", strip=True)) and u.rstrip("/") not in seen:
            cand.append(u)
            seen.add(u.rstrip("/"))
    for pth in ("/contact", "/contact-us", "/about", "/about-us", "/team"):
        if (root + pth) not in seen:
            cand.append(root + pth)
            seen.add(root + pth)

    def grab(u):
        pg = load(u, timeout=8)
        if pg is None or pg.url.rstrip("/") == home.url.rstrip("/"):
            return None
        host = urlparse(pg.url).netloc.replace("www.", "")
        if not (same_site(host, base_host) or same_site(base_host, host)):      # a redirect to another company's site
            return None
        return pg

    fetched = {home.url.rstrip("/")}
    pool = concurrent.futures.ThreadPoolExecutor(4)          # this site's own helpers: a slow site cannot starve the others
    try:
        for pg in pool.map(grab, cand[:6]):
            if pg is not None and pg.url.rstrip("/") not in fetched:
                fetched.add(pg.url.rstrip("/"))
                soups.append((pg, BeautifulSoup(pg.content, "lxml")))

        legal = [_abs(home.url, a["href"]).split("#")[0] for a in home_soup.find_all("a", href=True)[:MAX_LINKS]
                 if LEGAL_LINK.search(a["href"] + " " + a.get_text(" ", strip=True))
                 and urlparse(_abs(home.url, a["href"])).netloc.replace("www.", "") == base_host]
        legal = [u for u in dict.fromkeys(legal) if u.rstrip("/") not in fetched][:2] or \
            [root + "/privacy", root + "/privacy-policy"]
        for pg in pool.map(grab, legal):      # footer pages often carry the only email or phone
            if pg is not None and pg.url.rstrip("/") not in fetched:
                fetched.add(pg.url.rstrip("/"))
                soups.append((pg, BeautifulSoup(pg.content, "lxml")))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    out["head"] = _head_text(home_soup)
    emails, phones, people, li_links, texts = [], [], [], [], []
    on_a_real_page, mail_on_a_real_page = set(), set()      # values seen somewhere other than a privacy or terms page
    company_li = ""
    for pg, soup in soups:
        legal_page = bool(LEGAL_PATH.search(urlparse(pg.url).path))     # privacy and terms name regulators, not the company's people
        c, lp = linkedin(soup)
        company_li = company_li or c
        li_links += [] if legal_page else lp
        ld = _jsonld(soup)
        people += [] if legal_page else people_ld(ld)
        for o in ld:
            for key in ("telephone", "phone"):
                d = util.usable_phone(TEL_EXT.split(o[key], 1)[0]) if isinstance(o.get(key), str) else ""
                if d and d not in phones:
                    phones.insert(0, d)
                if d and not legal_page:
                    on_a_real_page.add(d)
            if isinstance(o.get("email"), str):
                for e in emails_in(BeautifulSoup("", "lxml"), o["email"]):
                    if e not in emails:
                        emails.append(e)
                    if not legal_page:
                        mail_on_a_real_page.add(e)
        txt = _visible(soup)                         # drops scripts and styles
        texts.append(txt)
        for e in emails_in(soup, txt):
            if e not in emails:
                emails.append(e)
            if not legal_page:
                mail_on_a_real_page.add(e)
        for p in phones_in(soup, txt):
            if p not in phones:
                phones.append(p)
            if not legal_page:
                on_a_real_page.add(p)
        people += [] if legal_page else leaders_text(soup)           # customer quotes are not the company's people
    out["body"] = "\n".join(texts)[:20000]

    own = [e for e in emails if same_site(e.split("@")[1], domain)]
    other = [e for e in emails if e not in own]
    GEN = ("info", "hello", "contact", "hi", "team", "sales", "office", "inquiries", "admin", "support", "mail")
    own.sort(key=lambda e: 0 if e.split("@")[0] in GEN else 1)
    out["emails"] = own + other
    out["legal_only_emails"] = [e for e in out["emails"] if e not in mail_on_a_real_page]

    cname = set(util.core_tokens(company_name))
    leaders = {}
    for p in people:
        nn = okname(p["name"], company=company_name)
        if not nn:
            continue
        toks = [t.lower().strip(".") for t in nn.split()]
        if set(toks) <= cname or any(t in ORGWORDS for t in toks):
            continue
        ttl = clean_title(p["title"])
        if not ttl or not LEAD.search(ttl):
            continue
        if re.search(r",\s*[A-Za-z]|\b(at|of|for)\s+[A-Z]", ttl) and not re.search(r"(&|and|/)", ttl):
            continue                       # "CEO of Acme Freight", "Owner, McLain Farms": someone else's company
        leaders.setdefault(nn.lower(), {"name": nn, "title": ttl, "email": p.get("email", ""), "linkedin": ""})
    names = [p["name"] for p in leaders.values()]
    for p in leaders.values():
        first, last = p["name"].split()[0].lower(), p["name"].split()[-1].lower()
        if not p["email"]:
            for e in emails:
                lp = e.split("@")[0]
                if same_site(e.split("@")[1], domain) and lp in (
                        first, f"{first}.{last}", f"{first}{last}", f"{first[0]}{last}", f"{first}_{last}",
                        f"{first[0]}.{last}"):
                    p["email"] = e
                    break
        p["linkedin"] = match_linkedin(p["name"], [n for n in names if n != p["name"]], li_links)

    def rank(p):
        t = p["title"].lower()
        return 0 if re.search(r"ceo|chief executive", t) else 1 if "founder" in t else 2

    out["people"] = sorted(leaders.values(), key=rank)[:4]
    phones.sort(key=lambda d: d not in on_a_real_page)         # a number shown only in a privacy policy comes last (stable)
    out["phones"] = phones[:6]
    out["legal_only_phones"] = [d for d in out["phones"] if d not in on_a_real_page]
    out["linkedin_company"] = company_li
    out["pages"] = len(soups)
    out["ok"] = True
    return out
