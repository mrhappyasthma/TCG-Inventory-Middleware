import csv
import hashlib
import io
import json
import os
import re
from typing import Dict, Any, List, Optional
from .csvtools import find_column as _find_column, read_csv_text, strip_bom
from .db import (
    Database,
    SHARED_SCOPE,
    apply_pricing_rules,
    apply_condition_multiplier,
    normalize_condition_key,
    # Re-exported: it lived here first, and push.py and the tests import it
    # from this module. It moved to db.py so that db.py can order plan items
    # by it without importing this module, which imports db.py.
    variation_sort_key,
)


# The export column eBay's card categories require for an ungraded card's
# condition descriptor. Named here because a missing one is something the
# operator has to fix in SortSwift, and the warning has to say what to add.
CONDITION_DESCRIPTOR_COLUMN = "CD:40001"

# eBay item specifics travel in columns prefixed "C:" (the seller's own
# templates mark required ones with a leading asterisk, e.g. "*C:Game"). The
# SortSwift eBay-flavoured export already carries these, so they are forwarded
# straight through rather than being reconstructed here.
ITEM_SPECIFIC_PATTERN = re.compile(r"^\*?C:(.+)$", re.IGNORECASE)

# "Game" is required for the card categories, so it is emitted even when the
# export omits it, using a configurable default.
GAME_ITEM_SPECIFIC = "C:Game"

# Item specifics we can build from the plain SortSwift columns already parsed
# for other purposes. This maps an eBay specific to the input columns it can be
# read from -- it does not invent or translate values, so a plain (non-eBay)
# export still produces a listing with the specifics eBay expects. An explicit
# "C:"-prefixed column in the upload always wins over these.
DERIVED_ITEM_SPECIFICS = (
    ("C:Game", ["Game"]),
    ("C:Set", ["Set", "Set Name", "Expansion", "Edition"]),
    ("C:Card Name", ["Name", "Product Name", "Card Name", "Card", "Product"]),
    ("C:Card Number", ["Card Number", "Number"]),
    ("C:Language", ["Language", "Lang", "Language (Full Name)"]),
    ("C:Rarity", ["Rarity"]),
    ("C:Finish", ["Printing", "Finish", "Variant", "Foil"]),
)


def _detect_item_specific_columns(fieldnames) -> Dict[str, str]:
    """
    Map each item-specific input column to its normalised output column.

    Returns e.g. {"*C:Game": "C:Game", "C:Set": "C:Set"}. The asterisk is a
    template annotation, not part of the field name, so it is stripped.
    """
    detected: Dict[str, str] = {}
    for name in fieldnames or []:
        if not name:
            continue
        match = ITEM_SPECIFIC_PATTERN.match(name.strip())
        if match:
            detected[name] = "C:" + match.group(1).strip()
    return detected


# eBay's variation syntax: within one attribute, values are separated by
# semicolons; a pipe separates different attributes. Card names containing
# either character would corrupt the RelationshipDetails field.
VARIATION_VALUE_SEPARATOR = ";"
VARIATION_ATTRIBUTE_SEPARATOR = "|"
VARIATION_ATTRIBUTE_NAME = "Card"

# Per-variation images are declared as "<option value>=<url>", so "=" is a
# delimiter in that position and must not survive inside an option name.
VARIATION_PICTURE_SEPARATOR = "="

# An upload is always a delta of newly scanned cards, so its quantities are
# added to what is already held. There is no replace mode.
#
# It had one, and defaulted to it, because the export used to be a full dump
# of the whole shelf -- and treating such a file as a delta added the entire
# inventory on top of itself on every upload, climbing 3 to 6 to 9 and
# overselling. That premise is gone: the exports are per-batch now, and
# nothing keeps a full SortSwift count accurate any more, so replacing our
# quantities from one would restore stock that has already sold.
#
# Which makes the duplicate-upload fingerprint load-bearing rather than a
# convenience: additive quantities mean processing the same file twice
# double-counts, and `force` is the only way past it.

DEFAULT_VARIATION_OPTION_TEMPLATE = "{name} ({card_number})"

# eBay's ConditionID for an ungraded card. Graded cards use IDs extending 2750
# and need a different descriptor, which this engine does not yet emit.
UNGRADED_CONDITION_ID = "4000"

# SortSwift offers two exports and only one of them is usable here. The eBay
# template (export_eBay_*.csv) carries the File Exchange columns -- *Action,
# *Category, *Title, *ConditionID, CD:Card Condition, the *C: item specifics,
# PostalCode and the policy names. The inventory export (export_SortSwift_*.csv)
# carries none of them, and uploading it produces one skip per row: 426
# identical warnings with the real cause never stated.
#
# Detected by columns rather than by filename, because a file can be renamed
# and because the columns are what actually matter. ConditionID is the marker
# used: it is the one column whose absence stops every single row, and it is
# absent from the inventory export and present in the eBay one. Matched
# tolerantly for the same reasons find_column is -- the eBay template writes
# "*ConditionID" with the asterisk that marks a required field.
CONDITION_ID_COLUMNS = ("*ConditionID", "ConditionID", "Condition ID")


def _has_condition_id_column(fieldnames) -> bool:
    wanted = {c.replace("*", "").replace(" ", "").lower() for c in CONDITION_ID_COLUMNS}
    for name in fieldnames or []:
        if not name:
            continue
        if str(name).strip().replace("*", "").replace(" ", "").lower() in wanted:
            return True
    return False

# A batch can be several hundred rows, and one broken column produces one
# warning per row -- 426 identical lines that bury the summary telling you what
# actually happened. Warnings of the same kind are logged once with a tally
# instead.
WARN_SAMPLE_LIMIT = 3


def _warn_once(logs: List[Dict[str, str]], tallies: Dict[str, int], kind: str,
               message: str) -> None:
    """
    Log a warning, but only the first few of each kind.

    The tally is reported by :func:`_flush_warn_tallies` once the pass is done,
    so nothing is hidden -- it is summarised rather than repeated.
    """
    tallies[kind] = tallies.get(kind, 0) + 1
    if tallies[kind] <= WARN_SAMPLE_LIMIT:
        logs.append({"level": "WARN", "message": message})


def _flush_warn_tallies(logs: List[Dict[str, str]], tallies: Dict[str, int]) -> None:
    for kind, count in sorted(tallies.items()):
        if count > WARN_SAMPLE_LIMIT:
            logs.append({
                "level": "WARN",
                "message": (
                    f"{count} row(s) hit the '{kind}' problem above; only the "
                    f"first {WARN_SAMPLE_LIMIT} are listed individually."
                ),
            })
    # Cleared so a second call is a no-op: the skip path flushes early
    # to put the tally above its own warning, and the normal path
    # flushes again before the summary.
    tallies.clear()

def _log_once(logs: List[Dict[str, str]], tallies: Dict[str, int], kind: str,
              message: str, level: str = "SUCCESS") -> None:
    """
    Log a per-row SUCCESS line, sampled like the warnings are.

    A 426-row export produced 426 of these. Each one is a DOM append in the
    browser and bytes in the response, for information that is a per-kind
    tally in practice -- and the volume buried the summary that says what the
    run actually did.
    """
    tallies[kind] = tallies.get(kind, 0) + 1
    if tallies[kind] <= WARN_SAMPLE_LIMIT:
        logs.append({"level": level, "message": message})


def _flush_log_tallies(logs: List[Dict[str, str]], tallies: Dict[str, int]) -> None:
    for kind, count in sorted(tallies.items()):
        if count > WARN_SAMPLE_LIMIT:
            logs.append({
                "level": "INFO",
                "message": (
                    f"{count} row(s) routed as {kind}; the first "
                    f"{WARN_SAMPLE_LIMIT} are listed above."
                ),
            })
    # Cleared so a second call is a no-op: the skip path flushes early
    # to put the tally above its own warning, and the normal path
    # flushes again before the summary.
    tallies.clear()

# eBay accepts exactly four ungraded grades, and SortSwift does not supply them,
# so a translation is genuinely required here rather than being an invented
# vocabulary. The numeric value IDs differ by card family: game/CCG cards and
# sports cards share only "Near mint or better".
EBAY_UNGRADED_VALUE_IDS = {
    # Game / CCG / non-sport singles (e.g. category 183454)
    "game": {
        "Near mint or better": "400010",
        "Excellent": "400015",
        "Very good": "400016",
        "Poor": "400017",
    },
    # Sports card singles (category 261328)
    "sports": {
        "Near mint or better": "400010",
        "Excellent": "400011",
        "Very good": "400012",
        "Poor": "400013",
    },
}

# eBay categories that use the sports-card value IDs.
SPORTS_CARD_CATEGORIES = {"261328"}

# SortSwift / TCGplayer grades mapped onto eBay's four ungraded buckets. eBay
# has no separate bucket below "Poor", so Heavily Played and Damaged both land
# there.
CONDITION_TO_EBAY_GRADE = {
    "nm": "Near mint or better",
    "near mint": "Near mint or better",
    "near mint or better": "Near mint or better",
    "m": "Near mint or better",
    "mint": "Near mint or better",
    "lp": "Excellent",
    "lightly played": "Excellent",
    "excellent": "Excellent",
    "mp": "Very good",
    "moderately played": "Very good",
    "very good": "Very good",
    "vg": "Very good",
    "good": "Very good",
    "hp": "Poor",
    "heavily played": "Poor",
    "played": "Poor",
    "poor": "Poor",
    "dm": "Poor",
    "dmg": "Poor",
    "damaged": "Poor",
}


# The grades offered when a person sets a condition **by hand**.
#
# Not a new vocabulary, and not applied to the ingest: an uploaded export's
# condition is still passed through verbatim, because ours would be a third
# vocabulary able to disagree with both SortSwift's and eBay's. These are the
# short codes SortSwift itself exports, each shown with the eBay bucket it
# resolves to, so what a person picks is the same string an upload would have
# produced for the same card. Picking a spelling the export does not use would
# make the hand-corrected card a *different card* from the one the next upload
# creates -- condition is part of a card's identity.
#
# "D" is deliberately absent even though it is the canonical multiplier key:
# no descriptor table recognises it, so a card stored as "D" is skipped at
# export. "DM" is the spelling that works end to end.
CONDITION_CHOICES = (
    ("NM", "Near mint or better"),
    ("LP", "Lightly played / Excellent"),
    ("MP", "Moderately played / Very good"),
    ("HP", "Heavily played / Poor"),
    ("DM", "Damaged / Poor"),
)


def condition_is_mappable(condition) -> bool:
    """
    Whether a grade can travel the whole way to eBay.

    Both halves have to hold, because they are separate tables and a value in
    only one of them fails later rather than here: ``CONDITION_TO_EBAY_GRADE``
    is what becomes the required Condition Descriptor, and
    ``normalize_condition_key`` is what selects the pricing multiplier. A
    condition recognised by neither is already skipped with a warning during
    an upload; this is the same judgement, applied to a value typed in by a
    person rather than read from a file.
    """
    text = str(condition or "").strip().lower()
    if not text:
        return False
    return bool(CONDITION_TO_EBAY_GRADE.get(text)) and bool(
        normalize_condition_key(text)
    )


def _value_ids_for_category(category_id: str) -> Dict[str, str]:
    """Pick the ungraded value-ID table that matches the target category."""
    family = "sports" if str(category_id).strip() in SPORTS_CARD_CATEGORIES else "game"
    return EBAY_UNGRADED_VALUE_IDS[family]


def resolve_condition_descriptor(
    condition: str,
    category_id: str = "183454",
    style: str = "label_id",
) -> Optional[str]:
    """
    Render the CD:40001 cell for an ungraded card, or None if unmappable.

    ``style`` selects the cell format, because reports differ on which eBay
    accepts: 'label_id' produces "Excellent - (ID: 400015)" and 'id' produces
    the bare "400015". Switch it in Listing Rules if an upload is rejected.
    """
    grade = CONDITION_TO_EBAY_GRADE.get(str(condition or "").strip().lower())
    if not grade:
        return None

    value_id = _value_ids_for_category(category_id).get(grade)
    if not value_id:
        return None

    if str(style).strip().lower() == "id":
        return value_id
    return f"{grade} - (ID: {value_id})"


DEFAULT_VARIATION_TITLE_TEMPLATE = (
    "{set_name}: Pick Your Card - {condition} - Complete Your Set"
)

# The placeholders a title template may use. Named here because the set of
# them is now part of the user-facing contract -- the Listing Rules dialog
# lists them, and a template naming one that does not exist renders the
# literal text rather than failing, which is the kind of mistake that is only
# visible once a listing is live.
TITLE_PLACEHOLDERS = ("{set_name}", "{condition}", "{set_code}", "{year}")

# Shortenings tried, in order, when a rendered title exceeds eBay's limit.
#
# Two separate lists, because the two are not equally safe.
#
# A **set name is data**: it names the thing being sold, so only a shortening
# a reader can undo belongs here. "Volume" -> "Vol" qualifies. The reason to
# have the list at all is that the alternative is worse -- the last-resort
# fallback below chops the name mid-word, and "Gem Pack Volu" is not a set.
SET_NAME_ABBREVIATIONS = (
    ("Volume", "Vol"),
)

# **Template text is ours.** Shortening it states nothing false about the
# cards, which is why "Near Mint" -> "NM" has always been allowed here.
# "Singles & Holos" -> "Holos" is the same kind of edit: both describe what
# the listing offers rather than making a claim about a particular card, and
# losing the longer form costs less than a truncated set name.
#
# Note this is deliberately not applied to {condition}, which is substituted
# from the card's own grade and is a factual claim -- see the docstring below.
TEMPLATE_ABBREVIATIONS = (
    ("Near Mint", "NM"),
    ("Singles & Holos", "Holos"),
)


def _sanitize_variation_value(value: str) -> str:
    """
    Make a card name safe to embed in RelationshipDetails.

    A literal ';' or '|' inside a value would be read by eBay as a separator,
    splitting one card into several bogus options.
    """
    cleaned = str(value or "").strip()
    for sep in (
        VARIATION_VALUE_SEPARATOR,
        VARIATION_ATTRIBUTE_SEPARATOR,
        VARIATION_PICTURE_SEPARATOR,
    ):
        cleaned = cleaned.replace(sep, "/")
    return cleaned


def _ends_with_number(name: str, number: str) -> bool:
    """
    Whether a product name already finishes with its own card number.

    Deliberately narrow: it matches only at the end, and only once any
    separator between the two has been discounted. A number appearing in the
    middle of a name ("Mew 151 Promo") is part of the name and is left alone,
    because suppressing the suffix there would lose the distinction the
    number is carrying.
    """
    trimmed = re.sub(r"[\s\-_/#]+$", "", str(name or "").strip())
    if not trimmed.endswith(str(number or "")):
        return False
    head = trimmed[: len(trimmed) - len(number)]
    # Either the number is the whole name, or what precedes it is a
    # separator rather than a digit -- "Applin - 1902" yes, "Mew 1151" no.
    return head == "" or not head[-1:].isalnum()


def build_variation_option_name(
    product_name: str,
    card_number: str = "",
    template: str = DEFAULT_VARIATION_OPTION_TEMPLATE,
) -> str:
    """
    Build the dropdown label for one card, e.g. "Crushing Gloves (121/198)".

    The card number makes otherwise-identical reprints distinguishable and
    gives the list a natural order. When a card has no number the template
    collapses to just the name rather than leaving empty brackets.
    """
    name = str(product_name or "").strip()
    number = str(card_number or "").strip()
    if not number:
        return _sanitize_variation_value(name)
    # A name that already ends in its own card number does not get it twice.
    #
    # Some exports spell the product name "Applin - 1902" rather than
    # "Applin", and the template would render "Applin - 1902 (1902)" on every
    # option of every listing. The number cannot simply be stripped out of
    # the name instead: this project's natural key is (name, set, condition,
    # printing) and does *not* include the card number, so for such an export
    # the number inside the name is the only thing distinguishing two
    # different cards -- removing it merged 214 cards into 112 on a real
    # file. So the name is left exactly as it is and the suffix is skipped.
    if _ends_with_number(name, number):
        return _sanitize_variation_value(name)
    rendered = template.replace("{name}", name).replace("{card_number}", number)
    return _sanitize_variation_value(rendered)


def generate_variation_title(
    set_name: str,
    condition: str = "",
    template: str = DEFAULT_VARIATION_TITLE_TEMPLATE,
    set_code: str = "",
    year: str = "",
) -> str:
    """Render a listing title, shortening it to fit. See :func:`render_variation_title`."""
    title, _ = render_variation_title(
        set_name, condition=condition, template=template,
        set_code=set_code, year=year,
    )
    return title


def render_variation_title(
    set_name: str,
    condition: str = "",
    template: str = DEFAULT_VARIATION_TITLE_TEMPLATE,
    set_code: str = "",
    year: str = "",
):
    """
    Build an eBay variation listing title within the 80-character limit.

    The condition is substituted verbatim from the source data and is never
    abbreviated or altered here: the title makes a factual claim about the
    cards, so shortening it risks misdescribing them.

    ``set_code`` and ``year`` are the group's own values, and are only
    supplied by the caller when every card in the listing agrees on them --
    a title describes the whole listing, so a value its members disagree
    about has no business being stated as fact. Both render empty when
    unknown, and the collapse below means an unused or unresolved
    placeholder leaves no double space behind.

    Over-length titles are shortened in four steps, cheapest first, and each
    step is only taken because the one after it costs more:

    1. abbreviate the **set name** (``Volume`` -> ``Vol``), which a reader
       can undo;
    2. abbreviate the **template text** (``Near Mint`` -> ``NM``,
       ``Singles & Holos`` -> ``Holos``), which states nothing about a
       specific card;
    3. trim the set name by the overflow, which is the blunt instrument the
       first two exist to avoid -- it chops mid-word.

    Returns ``(title, trimmed)``. ``trimmed`` is True only when step 3 had
    to run, and it exists so the drafts page can flag a title that will
    reach eBay with a clipped set name. The alternative -- flagging every
    template whose *raw* substitution exceeds 80 -- would block approvals
    for titles the shortening above resolves perfectly well.
    """
    cond = str(condition or "").strip()
    code = str(set_code or "").strip()
    yr = str(year or "").strip()

    def render(tpl: str, s_name: str) -> str:
        rendered = (
            tpl.replace("{set_name}", s_name)
            .replace("{condition}", cond)
            .replace("{set_code}", code)
            .replace("{year}", yr)
        )
        # An unresolved placeholder would otherwise leave a double space, or
        # a trailing separator, in a live listing title. Collapsing is safe
        # because no legitimate title needs a run of spaces.
        return " ".join(rendered.split())

    s_name = str(set_name or "").strip()

    title = render(template, s_name)
    if len(title) <= 80:
        return title, False

    # 1. The set name's own abbreviations, tried before anything else is
    #    given up: "Gem Pack Vol 6" still names the set, where the trim at
    #    the end of this function would leave "Gem Pack Volu".
    short_name = _abbreviate(s_name, SET_NAME_ABBREVIATIONS)
    if short_name != s_name:
        title = render(template, short_name)
        if len(title) <= 80:
            return title, False

    # 2. Then the template's own wording. Cumulative with step 1: by here
    #    the set name has already given up what it can.
    short_template = _abbreviate(template, TEMPLATE_ABBREVIATIONS)
    title = render(short_template, short_name)
    if len(title) <= 80:
        return title, False

    # 3. Last resort: trim the set name by exactly the overflow amount.
    overflow = len(title) - 80
    trimmed_set = short_name[: max(0, len(short_name) - overflow)].strip()
    return render(short_template, trimmed_set)[:80], True


# The eBay export column the year comes from. It is an item specific rather
# than a column of our own, so it lives in manifest.ebay_fields_json.
YEAR_SPECIFIC = "C:Year Manufactured"


def _card_year(card) -> str:
    try:
        fields = json.loads(card.get("ebay_fields_json") or "{}")
    except (ValueError, TypeError):
        return ""
    if not isinstance(fields, dict):
        return ""
    specifics = fields.get("item_specifics")
    if not isinstance(specifics, dict):
        return ""
    return str(specifics.get(YEAR_SPECIFIC) or "").strip()


def group_title_fields(cards) -> dict:
    """
    The ``set_code`` and ``year`` a whole listing can be said to have.

    A title describes the listing, not one card in it, so a value is only
    returned when **every** card agrees on it and returns empty otherwise.
    That is the same rule ``push._uniform_aspects`` applies to listing-level
    aspects, and it exists for the reason ``get_plan_groups`` had to stop
    using ``MIN()``: picking one member's value and printing it as the
    group's turns a disagreement into a confident, wrong statement -- there,
    a 152-card block headed with the grade of its single odd card.

    Blank is the safe answer because the renderer collapses an unresolved
    placeholder away, so a mixed group simply loses that word from its
    title rather than gaining a false one.
    """
    codes = set()
    years = set()
    for card in cards:
        codes.add(str(card.get("set_code") or "").strip())
        years.add(_card_year(card))
    return {
        "set_code": codes.pop() if len(codes) == 1 else "",
        "year": years.pop() if len(years) == 1 else "",
    }


# Where a language-specific title template is stored. The suffix is the
# card's own language code, uppercased, exactly as the export spells it.
TITLE_TEMPLATE_PREFIX = "variation_title_template"


def title_template_key(language: str) -> str:
    """The settings key holding the title template for one language."""
    return f"{TITLE_TEMPLATE_PREFIX}_{str(language or '').strip().upper()}"


def title_template_for(
    settings: Dict[str, str],
    language: str = "",
    default: str = DEFAULT_VARIATION_TITLE_TEMPLATE,
) -> str:
    """
    The title template that applies to a listing in this language.

    One template per account was enough while every card was English. It
    stopped being enough the moment a second language arrived: a Chinese
    listing wants its language, its set code and its year in the title,
    and an English one wants none of them -- rendered through a single
    template, one of the two always comes out wrong.

    So the language-specific template wins and the plain
    ``variation_title_template`` is the fallback. A **blank** language
    template falls through too, which is what makes "leave an empty one for
    Japanese" work with no special case: the key can exist, be visible in
    Listing Rules, and do nothing until it is filled in.

    The language is whatever the export put in the card's ``Language``
    column, uppercased. It is deliberately not normalised against a table of
    our own -- this project passes source values through -- which does mean
    that if the export starts spelling a language differently, the listing
    falls back to the default template rather than silently rendering the
    wrong one. That is the failure mode to expect, and it is visible on the
    drafts page before anything is pushed.
    """
    specific = str(settings.get(title_template_key(language)) or "").strip()
    if specific:
        return specific
    return str(settings.get(TITLE_TEMPLATE_PREFIX) or default)


def group_language(cards) -> str:
    """
    The language a whole listing is in, or "" when its cards disagree.

    Same rule as :func:`group_title_fields`, and it matters more here: the
    language selects the *template*, so guessing would render an entire
    listing's title in the wrong format rather than dropping one word.
    """
    languages = {str(card.get("language") or "").strip() for card in cards}
    return languages.pop() if len(languages) == 1 else ""




def _parse_price(val: Optional[str]) -> float:
    """Safely parse price string to float."""
    if not val:
        return 0.0
    try:
        clean = str(val).replace("$", "").replace(",", "").strip()
        return float(clean) if clean and clean.upper() != "N/A" else 0.0
    except (ValueError, TypeError):
        return 0.0


def _empty_batch_result(
    logs: List[Dict[str, str]],
    **overrides,
) -> Dict[str, Any]:
    """Build a no-op batch result carrying the supplied log messages."""
    result = {
        "parsed_rows": 0,
        "staged_card_count": 0,
        "staged_listing_count": 0,
        "new_catalog_count": 0,
        "skipped_count": 0,
        "duplicate": False,
        "dry_run": False,
        "logs": logs,
    }
    result.update(overrides)
    return result


def process_batch_csv(
    csv_text: str,
    db: Database,
    source_name: str = "batch.csv",
    force: bool = False,
    dry_run: bool = False,
    user_id: int = SHARED_SCOPE,
) -> Dict[str, Any]:
    """
    Process fresh SortSwift inventory batch CSV.

    Ingest only: it catalogues what the export contains and stages the
    resulting drafts. It writes no files -- the two CSVs this once produced,
    for revising and for adding listings, went with the File Exchange path.
    eBay is reached through its API, from an approved plan.

    Automatically creates:
    - Multi-item Variation Listings (grouped by Set Name, for cards below the
      configured single-listing threshold, when 'group_by_set' is enabled)
    - Standalone Single Listings (for cards at or above the threshold, and for
      every card when 'group_by_set' is disabled)

    An upload is a **delta of newly scanned cards**, so its quantities are
    added to what is already held. Processing the same file twice therefore
    double-counts stock, which is why uploads are fingerprinted and a repeat
    is refused unless ``force`` is set. A card held in two bins appears on
    two rows and both are added, because the bin is not part of a card's
    identity.

    A card **absent** from an upload means nothing: this file is one batch,
    not a picture of the shelves. Sales are learned from eBay's orders.

    ``user_id`` selects whose pricing rules and listing settings to apply.
    Both are per-user, so two sellers can process the same export and each get
    their own prices, title template and business policy names. The catalogue
    itself is shared, but no computed price is stored in it -- prices live only
    in the generated CSV -- so per-user rules cannot conflict there. Defaults to
    the shared baseline, which is what the command line uses.

    ``dry_run`` regenerates the output files **without writing anything**: no
    catalogue entries, no quantity accumulation, no store-mirror updates and no
    batch fingerprint. It exists for the common case of re-downloading a batch
    that has already been applied because a setting changed and the CSV needs
    rebuilding -- so it renders with current settings rather than replaying a
    stored file. Cards it cannot already find in the catalogue are skipped,
    since minting a manifest ID would itself be a write.
    """
    csv_text = strip_bom(csv_text)
    logs: List[Dict[str, str]] = []

    # Raw items to be added (categorized into singles vs variation sets)
    staged_singles: List[Dict[str, Any]] = []
    # (set_name, condition) -> [cards]. Condition is part of the key because a
    # variation listing carries a single ConditionID for all of its options.
    staged_variations: Dict[tuple, List[Dict[str, Any]]] = {}

    new_catalog_count = 0
    skipped_count = 0
    # Repeated per-row warnings are sampled and tallied rather than
    # logged once each; a few hundred identical lines bury the summary.
    warn_tallies: Dict[str, int] = {}
    log_tallies: Dict[str, int] = {}

    # The rules cannot change while a batch runs, so read them once instead of
    # once per card.
    pricing_rules = db.get_pricing_rules(user_id=user_id)
    condition_multipliers = {
        m["condition_key"]: m["multiplier"]
        for m in db.get_condition_multipliers(user_id=user_id)
    }
    # Grades seen that have no multiplier, reported once each rather than once
    # per row: a whole set of played cards would otherwise bury the log.
    unpriced_grades = set()

    # Total seen in *this file* per card, so several rows for one card (the
    # same card in two bins) sum together before replacing the stored value.
    file_totals: Dict[str, int] = {}

    # Cards this file touched that are already live on eBay. They need no
    # work here: the catalogue has been corrected and the planner diffs it
    # against what eBay is known to hold.
    live_count = 0

    # Load configuration settings
    single_threshold = float(
        db.get_listing_setting("single_threshold", "5.00", user_id=user_id)
    )
    title_template = db.get_listing_setting(
        "variation_title_template",
        DEFAULT_VARIATION_TITLE_TEMPLATE,
        user_id=user_id,
    )
    category_id = db.get_listing_setting("category_id", "183454", user_id=user_id)
    descriptor_style = db.get_listing_setting(
        "condition_descriptor_style", "label_id", user_id=user_id
    )
    default_game = str(
        db.get_listing_setting("default_game", "", user_id=user_id)
    ).strip()
    option_template = db.get_listing_setting(
        "variation_option_template", DEFAULT_VARIATION_OPTION_TEMPLATE, user_id=user_id
    )
    # Populated once the uploaded file's headers are known.
    item_specific_columns: Dict[str, str] = {}
    group_by_set = str(
        db.get_listing_setting("group_by_set", "true", user_id=user_id)
    ).strip().lower() not in (
        "false",
        "0",
        "no",
    )

    lines = [line for line in csv_text.splitlines() if line.strip()]
    if not lines:
        return _empty_batch_result(
            [{"level": "WARN", "message": "Uploaded SortSwift batch file is empty."}],
        )

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return _empty_batch_result(
            [{
                "level": "ERROR",
                "message": "Unable to parse CSV headers in SortSwift batch file.",
            }],
        )

    # Fail fast, and with the actual diagnosis, when the wrong SortSwift export
    # has been uploaded. Without this the run reaches the per-row checks and
    # produces one skip per row -- hundreds of identical warnings about a
    # missing ConditionID, with the real cause (wrong export template) never
    # stated anywhere.
    if not _has_condition_id_column(reader.fieldnames):
        return _empty_batch_result(
            [{
                "level": "ERROR",
                "message": (
                    "This export has no ConditionID column at all, which means "
                    "it is SortSwift's inventory export rather than its eBay "
                    "export. Every row would be skipped for the same reason, "
                    "and the file is also missing Category, Title, C:Game, "
                    "PostalCode and the business policy names -- so accepting "
                    "it would produce a listing file that is wrong in several "
                    "ways at once. In SortSwift, export using the eBay "
                    "template: the file is named export_eBay_<date>.csv rather "
                    "than export_SortSwift_<date>.csv. Nothing was catalogued "
                    "and no eBay file was built, so nothing has changed."
                ),
            }],
        )

    # Item specifics are discovered from the uploaded file's own headers, so
    # whatever your export provides is forwarded without needing a mapping.
    item_specific_columns = _detect_item_specific_columns(reader.fieldnames)

    # Derived specifics only apply where the upload has no explicit C: column
    # for them and does have a plain column to read from.
    present = {str(f).strip().lower() for f in (reader.fieldnames or []) if f}
    derived_specifics = tuple(
        (out_col, candidates)
        for out_col, candidates in DERIVED_ITEM_SPECIFICS
        if out_col not in set(item_specific_columns.values())
        and any(c.strip().lower() in present for c in candidates)
    )

    item_specific_outputs = sorted(
        set(item_specific_columns.values())
        | {out for out, _ in derived_specifics}
        | {GAME_ITEM_SPECIFIC}
    )
    if item_specific_outputs:
        forwarded = ", ".join(item_specific_outputs)
        logs.append({
            "level": "INFO",
            "message": f"eBay item specifics included: {forwarded}",
        })

    # Refuse a replay of an already-processed file unless explicitly forced.
    batch_hash = hashlib.sha256(csv_text.encode("utf-8", errors="replace")).hexdigest()
    previous = db.find_processed_batch(batch_hash)
    if previous and not force and not dry_run:
        return _empty_batch_result(
            [{
                "level": "WARN",
                "message": (
                    f"This exact batch file was already processed on "
                    f"{previous['processed_at']} (as '{previous['source_name']}', "
                    f"{previous['row_count']} rows). Re-processing would add its "
                    f"quantities to your live eBay stock a second time. "
                    f"Re-upload with 'force' enabled if that is really intended."
                ),
            }],
            duplicate=True,
        )

    grouping_note = (
        f"Single Threshold: ${single_threshold:.2f}"
        if group_by_set
        else "Set grouping disabled - every card listed as a single"
    )
    logs.append({
        "level": "INFO",
        "message": f"Processing SortSwift scan batch with {len(lines) - 1} cards ({grouping_note})..."
    })
    if dry_run:
        logs.append({
            "level": "INFO",
            "message": (
                "Preview run: reporting what this file would change "
                "with current settings. Nothing will be written."
            ),
        })
    if previous and force and not dry_run:
        logs.append({
            "level": "WARN",
            "message": (
                f"Forced re-processing of a batch already handled on "
                f"{previous['processed_at']}; quantities will be added again."
            ),
        })

    for row_idx, row in enumerate(reader, start=1):
        product_name = _find_column(row, [
            "*C:Card Name", "Name", "Product Name", "Product Name (No Card Number)",
            "Card Name", "Card", "Product",
        ])
        set_name = _find_column(row, ["*C:Set", "Set", "Set Name", "Expansion", "Edition"])
        raw_condition = _find_column(row, [
            "Condition", "Condition Code", "Condition (Full Name)", "C:Card Condition",
            "Card Condition", "Grade",
        ])
        printing = _find_column(row, [
            "Printing", "*C:Finish", "Finish", "Variant", "Foil", "Foil (normal/foil)",
        ]) or "Normal"
        qty_str = _find_column(row, ["*Quantity", "Quantity", "Qty", "Count", "Amount"])

        # Extra SortSwift / eBay-export fields
        sku_id = _find_column(row, ["SKU ID", "SKU Id", "SKU", "SkuId", "skuId"])
        tcgplayer_id = _find_column(row, [
            "TCGPlayer ID", "TCGplayer Id", "Product ID", "ProductId", "productId", "ID Product",
        ])
        card_number = _find_column(row, ["*C:Card Number", "Card Number", "Number"])
        set_code = _find_column(row, ["Set Code", "SetCode"])
        language = _find_column(row, ["Language", "*C:Language", "Lang", "Language (Full Name)"]) or "EN"
        cdn_image = _find_column(row, ["PicURL", "CDN Image", "CDN Link", "cdn_link", "Image URL"])
        cdn_back_image = _find_column(row, ["CDN Back Link", "Card Back CDN Image", "Back Image"])
        stock_image = _find_column(row, ["Stock Image", "Stock Photo"])
        remarks = _find_column(row, [
            "Remark + Bin Index", "Remarks", "Remark", "Bin Index", "Bin", "binIndex", "Comment",
        ])
        # Raw ConditionID from CSV (e.g. 4000) — prefer over string mapping
        raw_condition_id = _find_column(row, ["*ConditionID", "ConditionID", "Condition ID"])

        # Clean remark / bin string for eBay SKU encoding
        clean_remark = ""
        if remarks and str(remarks).strip().lower() not in ("no remark", "none", ""):
            clean_remark = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(remarks).strip())
            clean_remark = clean_remark[:20]

        # Dynamic Pricing Calculation
        ebay_price_val = _parse_price(_find_column(row, ["Platform Price (Ebay)", "eBay Price", "Ebay Price", "ebay_price"]))
        standard_price_val = _parse_price(_find_column(row, ["Your Price", "Price", "Selling Price", "Platform Price (Internal)"]))
        market_price_val = _parse_price(_find_column(row, ["Market Price", "Platform Price (Tcgplayer)", "TCGPlayer Price", "Market"]))

        raw_base_price = market_price_val if market_price_val > 0 else standard_price_val

        if not product_name or not set_name:
            skipped_count += 1
            logs.append({
                "level": "WARN",
                "message": f"Row {row_idx}: Missing Product Name or Set Name. Skipped.",
            })
            continue

        # Condition is passed through from the source export verbatim. We do not
        # translate it: the value originates in SortSwift and is destined for
        # eBay or back into SortSwift, so interposing our own vocabulary only
        # creates a third one that can disagree with both.
        condition_name = str(raw_condition or "").strip()

        # Priced here rather than above, because the grade is part of the
        # calculation and is only known now. An explicit eBay Price column
        # still wins outright: it is a per-card override and must not be
        # discounted a second time.
        if ebay_price_val > 0:
            effective_price = ebay_price_val
        else:
            # The market price available to us is product-level -- neither
            # TCGplayer's public data nor the SortSwift export breaks it down
            # by condition -- so the grade discount is applied here, before
            # the tiers. Discounting first is deliberate: a played card should
            # fall into a cheaper tier, not the tier its mint price implies.
            adjusted_base, condition_factor = apply_condition_multiplier(
                raw_base_price, condition_name, condition_multipliers
            )
            if condition_factor is None and condition_name:
                unpriced_grades.add(condition_name)
            effective_price, _ = apply_pricing_rules(
                pricing_rules, adjusted_base
            )

        if not condition_name:
            skipped_count += 1
            logs.append({
                "level": "WARN",
                "message": f"Row {row_idx}: No Condition value. Skipped rather than guessing the card's condition.",
            })
            continue

        # An explicit descriptor in the export wins; we only derive one when the
        # source does not supply it.
        explicit_descriptor = _find_column(row, [
            "CD:40001", "CD:Card Condition - (ID: 40001)", "CD_Card_Condition",
            "Condition Descriptor", "Card Condition Descriptor",
        ])

        # eBay requires a numeric ConditionID for category 183454. It must come
        # from the input; we will not infer one.
        #
        # Deliberately not defaulted to UNGRADED_CONDITION_ID even though it is
        # 4000 for every ungraded card. The only file that omits this column is
        # SortSwift's *inventory* export, which is also missing Category,
        # Title, C:Game, PostalCode and the policy names -- so accepting it
        # would trade one loud failure for an Add file that is quietly wrong in
        # five other ways. The template check above catches that case and says
        # so; this stays strict.
        condition_id = str(raw_condition_id or "").strip()
        if not condition_id.isdigit():
            skipped_count += 1
            _warn_once(
                logs,
                warn_tallies,
                "no ConditionID",
                (
                    f"Row {row_idx}: No numeric ConditionID for "
                    f"'{product_name}' (Condition: {condition_name}). Skipped "
                    f"rather than guessing."
                ),
            )
            continue

        # Resolve the eBay Condition Descriptor, required for card categories.
        if explicit_descriptor and str(explicit_descriptor).strip():
            condition_descriptor = str(explicit_descriptor).strip()
        elif condition_id != UNGRADED_CONDITION_ID:
            skipped_count += 1
            logs.append({
                "level": "WARN",
                "message": (
                    f"Row {row_idx}: ConditionID {condition_id} is not the ungraded "
                    f"value ({UNGRADED_CONDITION_ID}). Graded cards need a different "
                    f"Condition Descriptor, which is not supported yet. Skipped."
                ),
            })
            continue
        else:
            condition_descriptor = resolve_condition_descriptor(
                condition_name, category_id=category_id, style=descriptor_style
            )
            if not condition_descriptor:
                skipped_count += 1
                accepted = ", ".join(sorted(set(CONDITION_TO_EBAY_GRADE.values())))
                logs.append({
                    "level": "WARN",
                    "message": (
                        f"Row {row_idx}: Condition '{condition_name}' does not map to "
                        f"an eBay ungraded grade ({accepted}). Skipped rather than "
                        f"guessing. Add a '{CONDITION_DESCRIPTOR_COLUMN}' column to "
                        f"your export to set it explicitly."
                    ),
                })
                continue

        try:
            quantity = int(qty_str.strip()) if qty_str else 1
            if quantity <= 0:
                quantity = 1
        except (ValueError, AttributeError):
            quantity = 1

        # Check or create manifest ID
        if dry_run:
            # Read-only: find the existing catalogue entry, never mint one.
            existing = db.find_manifest(
                product_name, set_name, condition_name, printing
            )
            if not existing:
                skipped_count += 1
                logs.append({
                    "level": "WARN",
                    "message": (
                        f"Row {row_idx}: '{product_name}' is not in the catalogue yet, "
                        f"so it has no manifest ID to put in the file. Process the "
                        f"batch for real to catalogue it."
                    ),
                })
                continue
            manifest_id, is_new, card_data = existing["manifest_id"], False, existing
        else:
            manifest_id, is_new, card_data = db.get_or_create_manifest(
                product_name=product_name,
                set_name=set_name,
                condition=condition_name,
                printing=printing,
                sku_id=sku_id,
                tcgplayer_id=tcgplayer_id,
                card_number=card_number,
                set_code=set_code,
                language=language,
                price=effective_price,
                market_price=market_price_val,
                cdn_image=cdn_image,
                stock_image=stock_image,
                remarks=remarks,
            )

        # The export normally owns the bin, but a value set by hand on
        # the dashboard wins -- see remarks_edited_at in db.py. Say so
        # when the two disagree, because both are plausible and dropping
        # either silently is how someone ends up looking in the wrong
        # box. Sampled like the rest: a re-upload of the same file would
        # otherwise repeat it for every hand-edited card every time.
        export_remark = str(remarks or "").strip()
        stored_remark = str(card_data.get("remarks") or "").strip()
        if (
            card_data.get("remarks_edited_at")
            and export_remark
            and export_remark.lower() != "no remark"
            and export_remark != stored_remark
        ):
            _log_once(
                logs, log_tallies, "BIN KEPT",
                (
                    f"Row {row_idx}: [{manifest_id}] kept its bin "
                    f"'{stored_remark}', which was set here by hand. "
                    f"The export says '{export_remark}'. Clear the bin "
                    f"on the dashboard to let the export own it again."
                ),
                level="WARN",
            )

        # Forward this row's item specifics verbatim, defaulting Game because
        # eBay requires it for the card categories.
        row_specifics: Dict[str, str] = {}
        for source_col, out_col in item_specific_columns.items():
            value = str(row.get(source_col) or "").strip()
            if value:
                row_specifics[out_col] = value
        for out_col, candidates in derived_specifics:
            if row_specifics.get(out_col):
                continue
            value = str(_find_column(row, candidates) or "").strip()
            if value:
                row_specifics[out_col] = value
        # Game is the one specific where the configured value OVERRIDES the
        # export rather than merely filling a gap. eBay only accepts values from
        # its own list for the category (e.g. "Pokemon TCG" with an accented e),
        # and SortSwift exports a looser label such as "Pokemon", which eBay
        # rejects as invalid. The export's value is used only when no setting is
        # configured.
        if default_game:
            row_specifics[GAME_ITEM_SPECIFIC] = default_game
        elif not row_specifics.get(GAME_ITEM_SPECIFIC):
            game = str(_find_column(row, ["Game", "*C:Game", "C:Game"]) or "").strip()
            row_specifics[GAME_ITEM_SPECIFIC] = game

        # Our own catalogued stock count for this card.
        file_totals[manifest_id] = file_totals.get(manifest_id, 0) + quantity
        if not dry_run:
            db.increment_manifest_quantity(manifest_id, quantity)

        ebay_custom_label = f"{manifest_id}-{clean_remark}" if clean_remark else manifest_id

        if is_new:
            new_catalog_count += 1
            bin_note = f" | Bin: {remarks}" if remarks and str(remarks).strip().lower() != "no remark" else ""
            # Sampled: a first-time import registers every row, and 426
            # near-identical lines are noise rather than information. The
            # count is reported as "new catalog entries created" in the
            # summary either way.
            _log_once(
                logs, log_tallies, "new catalogue entry",
                f"Row {row_idx}: Registered NEW card -> [{manifest_id}] "
                f"{product_name} ({set_name} | {condition_name} | {printing} "
                f"| SKU: {sku_id or 'N/A'}{bin_note})",
                level="INFO",
            )

        # Check if live on eBay
        variation = db.get_variation(manifest_id)

        if variation and variation.get("ebay_parent_id"):
            # Already live on eBay, so this row is an update rather than a new
            # listing. What that update *is* no longer gets worked out here:
            # the catalogue has just been corrected above, and the planner
            # diffs it against what eBay is known to hold. This branch only
            # says so, which is the part a person reading the log wants.
            ebay_item_id = str(variation["ebay_parent_id"]).strip()

            # eBay knows this variation by whichever label it was listed under,
            # which Module B records. Inventing a fresh label from this row's
            # bin would address a variation that does not exist.
            known_label = (variation.get("custom_label") or "").strip()
            revise_label = known_label or ebay_custom_label

            # Accumulation rests on increment_manifest_quantity above, which
            # adds to the catalogue's own figure. That is the right base: it
            # is correct whether or not eBay has been told anything yet. The
            # bases tried before it -- eBay's last reported quantity, or a
            # `pending_qty` recording what a generated file had asked for --
            # both meant two batches uploaded before a sync started from the
            # same stale number and the first was lost.
            live_count += 1
            _log_once(logs, log_tallies, "LIVE",
                      f"Row {row_idx}: [LIVE] #{ebay_item_id} [{revise_label}] "
                      f"{product_name} (catalogue now "
                      f"{db.get_manifest_by_id(manifest_id)['quantity']}, "
                      f"eBay reports {variation.get('last_known_qty')})")
        else:
            # ADD Scenario: Stage for Single vs Variation listing
            card_entry = {
                "manifest_id": manifest_id,
                "custom_label": ebay_custom_label,
                "product_name": product_name,
                "set_name": set_name,
                "condition_name": condition_name,
                "condition_id": condition_id,
                "condition_descriptor": condition_descriptor,
                "item_specifics": row_specifics,
                "printing": printing,
                "quantity": quantity,
                "price": effective_price,
                "cdn_image": cdn_image or "",
                "cdn_back_image": cdn_back_image or "",
                "stock_image": stock_image or "",
                "card_number": card_number or "",
                "option_name": build_variation_option_name(
                    product_name, card_number or "", option_template
                ),
            }

            # Keep the export-derived fields, so an approved draft plan can
            # rebuild this card's Add row later. Without them a plan could
            # record a decision but never produce a listing file, because
            # these values exist only in the uploaded CSV and eBay requires
            # them. Skipped on a dry run, which must write nothing.
            if not dry_run:
                db.set_manifest_ebay_fields(manifest_id, {
                    "item_specifics": row_specifics,
                    "condition_id": condition_id,
                    "condition_descriptor": condition_descriptor,
                    # Whether that descriptor came from the upload or was
                    # rendered here. A later export must re-render our own,
                    # so that changing condition_descriptor_style in Listing
                    # Rules actually reaches the file -- otherwise the style
                    # in force the day a card was catalogued is frozen into
                    # it. The upload's own value is never rewritten.
                    "condition_descriptor_from_export": bool(
                        explicit_descriptor and str(explicit_descriptor).strip()
                    ),
                    "cdn_back_image": cdn_back_image or "",
                    "stock_image": stock_image or "",
                })

            if not group_by_set:
                staged_singles.append(card_entry)
                _log_once(logs, log_tallies, "ADD SINGLE",
                          f"Row {row_idx}: [ADD SINGLE] [{ebay_custom_label}] "
                          f"{product_name} (set grouping disabled)")
            elif effective_price >= single_threshold:
                staged_singles.append(card_entry)
                _log_once(logs, log_tallies, "ADD SINGLE",
                          f"Row {row_idx}: [ADD SINGLE] [{ebay_custom_label}] "
                          f"{product_name} (Price: ${effective_price:.2f} >= "
                          f"${single_threshold:.2f} threshold)")
            else:
                group_key = (set_name, condition_name)
                staged_variations.setdefault(group_key, []).append(card_entry)
                _log_once(logs, log_tallies, "ADD VARIATION",
                          f"Row {row_idx}: [ADD VARIATION] [{ebay_custom_label}] "
                          f"{product_name} grouped into '{set_name}' / "
                          f"{condition_name} (Price: ${effective_price:.2f})")

    # A card absent from an upload means nothing at all: the upload is a
    # delta of newly scanned cards, so it says only what was in that batch.
    #
    # This used to be where sold-out cards were detected -- a card live on
    # eBay and missing from a *full dump* had gone, and its quantity was set
    # to zero. That inference only worked while the upload was a complete
    # picture of the shelves. Sales are now learned from eBay's own orders,
    # which is a better source than an omission: it names the order, the
    # quantity and the moment, and it cannot mistake a partial export for a
    # sale.
    total_added_cards = len(staged_singles) + sum(len(c) for c in staged_variations.values())

    # Tallies before the summary, so the counts read as detail leading into it
    # rather than trailing after the conclusion.
    _flush_log_tallies(logs, log_tallies)
    _flush_warn_tallies(logs, warn_tallies)

    logs.append({
        "level": "INFO",
        "message": (
            f"Ingest finished: {len(file_totals)} card(s) read, "
            f"{new_catalog_count} new to the catalogue. They route to "
            f"{len(staged_variations)} Set/Condition variation listing(s) "
            f"({sum(len(c) for c in staged_variations.values())} child cards) "
            f"and {len(staged_singles)} single listing(s). Rebuild the draft "
            f"to see what eBay needs."
        ),
    })

    if unpriced_grades:
        logs.append({
            "level": "WARN",
            "message": (
                "No condition multiplier is configured for: "
                + ", ".join(sorted(unpriced_grades))
                + ". Those cards were priced from the market price with no "
                "grade discount, which over-prices a played card. Add them "
                "under Pricing Rules."
            ),
        })

    parsed_rows = len(file_totals)
    if skipped_count and not parsed_rows:
        logs.append({
            "level": "ERROR",
            "message": (
                f"Not one row of this file could be processed ({skipped_count} "
                f"skipped), so nothing was catalogued and the draft will be "
                f"empty. See the warnings above for the reason."
            ),
        })

    # Fingerprint only after the batch has actually been applied, so a failure
    # part-way through does not mark the file as done.
    if not dry_run:
        db.record_processed_batch(
            sha256=batch_hash,
            source_name=source_name,
            row_count=len(lines) - 1,
        )

    return {
        "parsed_rows": parsed_rows,
        # How the cards route, which is what the draft will group them into.
        # Informational: the planner derives the grouping itself from stored
        # state, so these are a preview of it and never its input.
        "staged_card_count": total_added_cards,
        "staged_listing_count": len(staged_variations) + len(staged_singles),
        "new_catalog_count": new_catalog_count,
        "skipped_count": skipped_count,
        "duplicate": False,
        "dry_run": dry_run,
        "logs": logs,
    }


def process_batch_file(
    input_path: str,
    db: Database,
    force: bool = False,
    dry_run: bool = False,
    user_id: int = SHARED_SCOPE,
) -> Dict[str, Any]:
    """
    Ingest a SortSwift export from disk.

    Writes no files: it updates the catalogue and reports what it found.
    The eBay-bound work is the drafts page and the API push.
    """
    content = read_csv_text(input_path)
    # One connection for the whole run; see Database.session.
    with db.session():
        result = process_batch_csv(
            content,
            db,
            source_name=os.path.basename(input_path),
            force=force,
            dry_run=dry_run,
            user_id=user_id,
        )
    return result
