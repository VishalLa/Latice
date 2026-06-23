import re
from .ocr import Block

try:
    from dateutil import parser as _dateparser

    def _normalize_date(raw: str | None) -> str | None:
        if not raw:
            return None
        try:
            d = _dateparser.parse(raw.strip(), dayfirst=True)
            return d.strftime("%d-%m-%Y")
        except Exception:
            return raw   # return original if all else fails
        
except ImportError:
    def _normalize_date(raw: str | None) -> str | None:
        return raw
    

GSTIN = re.compile(r'\b(\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9])\b')
DATE = re.compile(
    r'\b(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}'
    r'|\d{4}[-/.]\d{2}[-/.]\d{2}'
    r'|\d{1,2}[-\s]+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-\s.]*\d{2,4})\b',
    re.IGNORECASE
)
PERCENT = re.compile(r'(\d+(?:\.\d+)?)\s*%')
NUM = re.compile(r'-?[\d,]+\.?\d*')

SKIP_VENDOR = {
    'tax invoice', 
    'e-invoice', 
    'einvoice', 
    'invoice', 
    'gstin',
    'irn',
    'original', 
    'duplicate', 
    'retail', 
    'bill', 
    'receipt',
    'quick service', 
    'thank you', 
    'computer generated', 
    'subject to',
    'pan :', 
    'pan:', 
    'original for recipient', 
    'insert', 
    'tax',
    'purchase', 
    'proforma'
}


def to_float(text) -> float | None:
    if text is None:
        return None 
    
    s = str(text).replace(',', '').replace('₹', '').replace('Rs.', '').replace('Rs', '').replace('INR', '').strip()
    m = NUM.search(s)

    if m:
        try:
            v = float(m.group().replace(',', ''))
            return v 
        except ValueError:
            pass 
    return None


def all_gstins(blocks: list[Block]) -> list[str]:
    seen, result = set(), []
    for b in blocks:
        for m in GSTIN.finditer(b.text):
            g = m.group(1)
            if g not in seen:
                seen.add(g); result.append(g)
    return result


def first_date(blocks: list[Block]) -> str | None:
    # Strategy 1: scan each block individually (top-to-bottom)
    for b in sorted(blocks, key=lambda b: b.y):
        m = DATE.search(b.text)
        if m:
            return _normalize_date(m.group(1))
        
    # Strategy 2: fallback - search the full joined text in case a date
    full = ' '.join(b.text for b in blocks)
    m = DATE.search(full)
    return _normalize_date(m.group(1)) if m else None


def find_percent_near_keyword(rows: list[list[Block]], keywords: list[str]) -> float | None:
    kl = [k.lower() for k in keywords]
    for row in rows:
        row_text = ' '.join(b.text for b in row)
        if any(kw in row_text.lower() for kw in kl):
            m = PERCENT.search(row_text)
            if m:
                return float(m.group(1))
    return None


def classify(cgst, sgst, igst, gstin) -> str:
    if cgst and sgst:
        return "GST Invoice (Intra-state)"
    if igst:
        return "GST Invoice (Inter-state)"
    if gstin:
        return "GST Invoice"
    return "Retail Bill"


def group_into_rows(blocks: list[Block], y_tolerance: int = 8) -> list[list[Block]]:
    if not blocks:
        return []
    rows = []

    current_row = [blocks[0]]
    for block in blocks[1:]:
        if abs(block.y - current_row[0].y) <= y_tolerance:
            current_row.append(block)
        else: 
            rows.append(sorted(current_row, key=lambda b: b.x))
            current_row = [block]
        
    rows.append(sorted(current_row, key=lambda b: b.x))
    return rows


def find_value_right_of(
    rows: list[list[Block]], keywords: list[str], min_x_gap: int = 30
) -> str | None:
    keywords = [k.lower() for k in keywords]
        
    for row in rows:
        for i, block in enumerate(row):

            if any(kw in block.text.lower() for kw in keywords):
                for j in range(i+1, len(row)):
                    right = row[j]

                    if right.x - (block.x + len(block.text) * 6) >= -20:
                        val = right.text.strip()

                        if val and not any(kw in val.lower() for kw in keywords):
                            return val
                            
    return None 

def find_amount_right_of(
    rows: list[list[Block]], keywords: list[str]
) -> float | None:
    val = find_value_right_of(rows, keywords)
    if val:
        return to_float(val)
    return None 

def find_amount_below(
    rows: list[list[Block]], keywords: list[str], x_tolerance: int = 70, max_rows_below: int = 2
) -> float | None:

    keywords = [k.lower() for k in keywords]

    for ri, row in enumerate(rows):
        for block in row: 
            if any(kw in block.text.lower() for kw in keywords):
                # Search in rows below 
                for rj in range(ri + 1, min(ri + max_rows_below + 1, len(rows))):
                    for b2 in rows[rj]:
                        if abs(b2.x - block.x) <= x_tolerance:
                            v = to_float(b2.text)
                            if v and v > 0:
                                return v
    return None 


def rightmost_number_in_row(row: list[Block]) -> float | None:
    for block in reversed(row):
        matches = NUM.findall(block.text)
        if matches:
            try:
                return float(matches[-1].replace(',', ''))
            except ValueError:
                continue
    return None


def find_row_with_keyword(
    rows: list[list[Block]], keywords: list[str]
) -> list[Block] | None:
    kl = [k.lower() for k in keywords]
    for row in rows:
        row_text = ' '.join(b.text.lower() for b in row)
        if any(kw in row_text for kw in kl):
            return row
    return None


def rows_to_lines(rows: list[list[Block]]) -> list[str]:
    return [' '.join(b.text for b in row) for row in rows]


def find_invoice_no(rows: list[list[Block]]) -> str | None:

    keywords_stat_1 = ['invoice no', 'invoice number', 'invoice#', 'bill no', 'bill number', 'inv no']
    
    # Strategy 1: value to the right of "Invoice No" label
    val = find_value_right_of(
        rows=rows, keywords=keywords_stat_1)
    if val and len(val) >= 2 and not val.lower() in ['dated', 'date', 'no.']:
        return val.strip().rstrip('.')
    

    # Strategy 2: regex on full text - handles "Bill No. : CM1/2627/18615" style
    lines = rows_to_lines(rows)
    full = '\n'.join(lines)
    m = re.search(
        r'(?:invoice\s*no\.?\s*[:\-.\s]{0,4}|bill\s*no\.?\s*[:\-.\s]{0,4})'
        r'([A-Z0-9][A-Z0-9/_\-]{1,30})',
        full, re.IGNORECASE
    )
    if m:
        return m.group(1).strip()
    return None


def find_vendor_name(blocks: list[Block]) -> str | None:
    # Sort by y, then x — top-left first
    sorted_blocks = sorted(blocks, key=lambda b: (b.y, b.x))
    for b in sorted_blocks[:15]:
        s = b.text.strip()
        sl = s.lower()
        if len(s) < 4:
            continue
        if re.match(r'^[\d\s\W]+$', s):
            continue
        if any(sk in sl for sk in SKIP_VENDOR):
            continue
        if GSTIN.search(s):
            continue
        if re.search(r'\b(?:plot|road|flat|near|street|nagar|layout|cross|floor|lane|ward)\b', sl):
            continue
        
        # Skip if it looks like an address (has digits + comma pattern)
        if re.search(r'\d+.*,', s) and len(s) > 30:
            continue
        return s
    return None


def find_buyer_name(rows: list[list[Block]]) -> str | None:
    BUYER_KW = [
        'buyer', 
        'bill to', 
        'consignee', 
        'sold to', 
        'ship to', 
        'buyer (bill to)', 
        'm/s', 
        'buyer bill to'
    ]
    for ri, row in enumerate(rows):
        row_text = ' '.join(b.text for b in row).lower()

        if any(kw in row_text for kw in BUYER_KW):
            # Look in next few rows for a company name
            for rj in range(ri + 1, min(ri + 5, len(rows))):
                candidate_row = rows[rj]
                # Only look at left-side blocks (x < 350 typically)
                left_blocks = [b for b in candidate_row if b.x < 400]
                if not left_blocks:
                    continue
                text = ' '.join(b.text for b in left_blocks).strip()
                if len(text) < 4:
                    continue
                if GSTIN.search(text):
                    continue
                if any(k in text.lower() for k in ['gstin', 'state name', 'state :', 'phone', 'email', 'address', 'flat', 'near', 'contact', 'place of supply']):
                    continue
                if re.match(r'^[\d\s]+$', text):
                    continue
                return text
    return None


def _pick_tax_amount(row: list[Block], rate: float | None) -> float | None:
    nums = []
    for b in row:
        for m in NUM.finditer(b.text):
            try:
                v = float(m.group().replace(',', ''))
                if v > 0:
                    nums.append(v)
            except ValueError:
                pass

    if not nums:
        return None

    if rate is not None:
        candidates = [v for v in nums if abs(v - rate) > 0.01]
        if candidates:
            return max(candidates)

    return max(nums)


#  TAX AMOUNTS — coordinate-aware
#  Strategy: find the label block (CGST/SGST/IGST), then get the rightmost
def find_tax_amounts(rows: list[list[Block]]) -> dict:
    result = {
        'cgst_amount': None, 'cgst_rate': None,
        'sgst_amount': None, 'sgst_rate': None,
        'igst_amount': None, 'igst_rate': None,
        'cess_amount': None,
        'taxable':     None,
        'total_tax':   None,
        'grand_total': None,
        'subtotal':    None,
        'round_off':   None,
        'discount':    None,
        'other_chg':   None,
    }

    GRAND_KW = [
        'total billing amount',
        'billing amount',
        'grand total',
        'amount payable',
        'net payable',
        'net amount',
        'total payable',
        'invoice total',
        'net total',
    ]
    TAXABLE_KW = [
        'taxable amount',
        'taxable value',
        'taxable',
        'subtotal',
        'sub total',
        'basic amount',
        'total before tax',
        'total amount', 
        'invoice amount',
        'bill amount',
    ]

    for row in rows:
        row_text = ' '.join(b.text for b in row)
        row_lower = row_text.lower()
        amt = rightmost_number_in_row(row)

        if 'cgst' in row_lower and amt and amt > 0:
            pct = PERCENT.search(row_text)
            if pct and not result['cgst_rate']:
                result['cgst_rate'] = float(pct.group(1))
            # Prefer a number that doesn't equal the rate (avoid picking 2.5 as amount)
            cgst_amt = _pick_tax_amount(row, result['cgst_rate'])
            if cgst_amt and not result['cgst_amount']:
                result['cgst_amount'] = cgst_amt

        # SGST / UTGST
        elif any(k in row_lower for k in ['sgst', 'utgst']) and amt and amt > 0:
            pct = PERCENT.search(row_text)
            if pct and not result['sgst_rate']:
                result['sgst_rate'] = float(pct.group(1))
            sgst_amt = _pick_tax_amount(row, result['sgst_rate'])
            if sgst_amt and not result['sgst_amount']:
                result['sgst_amount'] = sgst_amt

        # IGST
        elif 'igst' in row_lower and amt and amt > 0:
            pct = PERCENT.search(row_text)
            if pct and not result['igst_rate']:
                result['igst_rate'] = float(pct.group(1))
            igst_amt = _pick_tax_amount(row, result['igst_rate'])
            if igst_amt and not result['igst_amount']:
                result['igst_amount'] = igst_amt

        # Cess
        elif 'cess' in row_lower and amt and amt > 0:
            result['cess_amount'] = amt

        # Round off (can be negative)
        elif any(k in row_lower for k in ['round off', 'round-off', 'rounding']):
            v = rightmost_number_in_row(row)
            if v is not None:
                result['round_off'] = v

        # Discount
        elif any(k in row_lower for k in ['discount', 'less:']):
            if amt and amt > 0:
                result['discount'] = amt

        # Grand total
        elif any(k in row_lower for k in GRAND_KW):
            if amt and amt > 0 and not result['grand_total']:
                result['grand_total'] = amt

        # Taxable amount
        elif any(k in row_lower for k in TAXABLE_KW):
            if amt and amt > 0 and not result['taxable']:
                result['taxable'] = amt

        # Other charges
        elif any(k in row_lower for k in ['freight', 'shipping', 'packing', 'handling', 'other charges', 'delivery charges']):
            if amt and amt > 0:
                result['other_chg'] = amt

    # Total tax
    c = result['cgst_amount'] or 0
    s = result['sgst_amount'] or 0
    ig = result['igst_amount'] or 0
    cs = result['cess_amount'] or 0
    if c or s or ig:
        result['total_tax'] = c + s + ig + cs

    # Fallback grand total: look for "Total" row near end with large number
    if not result['grand_total']:
        for row in reversed(rows[-25:]):
            row_text = ' '.join(b.text for b in row).lower().strip()
            if row_text.startswith('total') or 'total' in row_text:
                v = rightmost_number_in_row(row)
                if v and v > 10:
                    result['grand_total'] = v
                    break

    return result



#  ITEM EXTRACTION — coordinate-aware
# HSN/SAC code lookup — common codes for Indian GST invoices
# Source: GST Act 2017 Schedules + common usage patterns
HSN_KEYWORD_MAP = {
    # Food & beverages
    "rice": "1006", "wheat": "1001", "flour": "1101", "sugar": "1701",
    "salt": "2501", "oil": "1507", "ghee": "0405", "butter": "0405",
    "milk": "0401", "paneer": "0406", "tea": "0902", "coffee": "0901",
    "biscuit": "1905", "bread": "1905", "chocolate": "1806",
    "noodles": "1902", "pasta": "1902", "chips": "2008", "snack": "2008",
    "mineral water": "2201", "juice": "2009", "soft drink": "2202",
    "aerated": "2202", "alcohol": "2208", "beer": "2203",
    # Textiles
    "cotton": "5208", "shirt": "6205", "trouser": "6203", "pant": "6203",
    "saree": "5007", "fabric": "5208", "cloth": "5209", "garment": "6211",
    "t-shirt": "6109", "jean": "6203", "dress": "6204", "kurta": "6211",
    "underwear": "6107", "sock": "6115", "shoe": "6403", "footwear": "6403",
    "sandal": "6402", "chappal": "6402", "bag": "4202", "handbag": "4202",
    # Electronics
    "mobile": "8517", "phone": "8517", "laptop": "8471", "computer": "8471",
    "tablet": "8471", "printer": "8443", "monitor": "8528", "tv": "8528",
    "television": "8528", "refrigerator": "8418", "washing machine": "8450",
    "ac": "8415", "air conditioner": "8415", "fan": "8414",
    "microwave": "8516", "mixer": "8509", "iron": "8516",
    "earphone": "8518", "speaker": "8518", "charger": "8504",
    "battery": "8507", "cable": "8544", "switch": "8536",
    # Stationery & office
    "pen": "9608", "pencil": "9609", "notebook": "4820", "paper": "4802",
    "envelope": "4817", "stapler": "8305", "file": "4820", "folder": "4820",
    "ink": "3215", "toner": "3215", "stamp": "9704",
    # Medicines & health
    "medicine": "3004", "tablet": "3004", "capsule": "3004",
    "syrup": "3004", "injection": "3004", "sanitizer": "3808",
    "mask": "6307", "glove": "3926", "soap": "3401", "shampoo": "3305",
    "toothpaste": "3306", "toothbrush": "9603", "cream": "3304",
    "lotion": "3304", "perfume": "3303", "deodorant": "3307",
    # Construction & hardware
    "cement": "2523", "steel": "7213", "iron rod": "7213",
    "brick": "6901", "tile": "6907", "paint": "3210", "wire": "7217",
    "pipe": "3917", "nut": "7318", "bolt": "7318", "screw": "7318",
    "wood": "4407", "plywood": "4412", "glass": "7005",
    # Fuel & energy
    "petrol": "2710", "diesel": "2710", "lpg": "2711", "gas": "2711",
    "coal": "2701", "solar panel": "8541",
    # Services (SAC codes)
    "rent": "997212", "repair": "998719", "maintenance": "998719",
    "transport": "996511", "courier": "998521", "advertising": "998361",
    "consulting": "998311", "professional": "998311", "audit": "998222",
    "legal": "998211", "software": "998313", "it service": "998313",
    "security": "998522", "cleaning": "998531", "catering": "996334",
    "hotel": "996311", "restaurant": "996331", "salon": "996411",
    "printing": "998912", "photography": "998385", "event": "998554",
    "insurance": "997130", "banking": "997111",
}


_HSN_RE = re.compile(r'\b(\d{4,8})\b')

def _extract_hsn_from_row(row: list) -> str | None:
    """Try to extract HSN/SAC code from a row: look for 4-8 digit standalone numbers."""
    for b in row:
        m = _HSN_RE.search(b.text)
        if m:
            val = m.group(1)
            # Skip years, amounts (usually > 8 digits or have decimals), qty
            if 4 <= len(val) <= 8 and not re.match(r'^20[0-9]{2}$', val):
                return val
    return None


def _lookup_hsn_by_description(desc: str) -> str | None:
    """Fallback: match description keywords to known HSN codes."""
    desc_lower = desc.lower()
    # Longest match wins (more specific)
    best = None
    best_len = 0
    for kw, code in HSN_KEYWORD_MAP.items():
        if kw in desc_lower and len(kw) > best_len:
            best = code
            best_len = len(kw)
    return best

def find_items(rows: list[list[Block]]) -> list[dict]:
    SKIP = {'total', 'cgst', 'sgst', 'igst', 'tax', 'subtotal', 'grand', 'discount', 'cess', 'round', 'taxable', 'summary', 'amount', 'description', 'product', 'particulars', 'goods', 'service', 'sr', 'sl', 'no', 'hsn', 'sac', 'qty', 'rate', 'mrp', 'per', 'disc', 'si', 'central', 'state', 'integrated'}

    # Find the table header row (contains "description" or "particulars" + "amount")
    header_row_idx = None
    for i, row in enumerate(rows):
        row_lower = ' '.join(b.text for b in row).lower()
        if any(k in row_lower for k in ['description of goods', 'description', 'particulars', 'name of product', 'name of service']):
            if any(k in row_lower for k in ['amount', 'qty', 'quantity', 'rate']):
                header_row_idx = i
                break

    if header_row_idx is None:
        return _fallback_items(rows)

    # The "Amount" column x-position from header
    amount_x = None
    for b in rows[header_row_idx]:
        if 'amount' in b.text.lower():
            amount_x = b.x

    items = []
    # Parse rows below header until we hit a total/tax row
    for row in rows[header_row_idx + 1:]:
        row_text = ' '.join(b.text for b in row)
        row_lower = row_text.lower()

        # Stop at summary/total rows or POS footer rows
        if any(k in row_lower for k in ['total', 'cgst', 'sgst', 'igst', 'taxable', 'round', 'discount', 'amount chargeable', 'tax amount', 'add gst', 'gst @', 'gst@', 'cashier', 'e & oe', 'e&oe']):
            break

        # Skip header-like rows
        if any(k in row_lower for k in ['description', 'particulars', 'hsn/sac', 'hsn', 'quantity', 'sr. no']):
            continue

        # Skip very short rows
        if len(row) < 2:
            continue

        # Get description: leftmost non-numeric block (skip serial number)
        desc_blocks = []
        for b in row:
            text = b.text.strip()
            # Skip pure serial numbers at start
            if re.match(r'^\d{1,3}\.?$', text):
                continue
            # Skip if it's clearly a number (amount/qty/rate column)
            if re.match(r'^[\d,]+\.?\d*$', text.replace(',', '')):
                continue
            desc_blocks.append(text)

        if not desc_blocks:
            continue

        desc = ' '.join(desc_blocks).strip()
        if len(desc) < 2:
            continue
        if re.match(r'^\d+[\.,]\d+\s*[A-Z]{1,5}\.?$', desc):
            continue
        if any(w in desc.lower() for w in SKIP):
            continue

        amt = None
        if amount_x:
            best_dist = 999
            for b in row:
                v = to_float(b.text)
                if v and v > 0:
                    dist = abs(b.x - amount_x)
                    if dist < best_dist:
                        best_dist = dist
                        amt = v
        else:
            amt = rightmost_number_in_row(row)

        if not amt or amt <= 0:
            continue

        # HSN/SAC extraction: try row first, then description keyword lookup
        hsn = _extract_hsn_from_row(row) or _lookup_hsn_by_description(desc)

        items.append({
            "description": desc,
            "hsn_sac":     hsn,
            "quantity":    None,
            "unit":        None,
            "rate":        None,
            "amount":      amt,
        })

    return items if items else _fallback_items(rows)


def _fallback_items(rows: list[list[Block]]) -> list[dict]:
    """Last resort item extraction."""
    items = []
    SKIP = {
        'total', 
        'cgst', 
        'sgst', 
        'igst', 
        'tax', 
        'subtotal', 
        'grand', 
        'discount', 
        'cess', 
        'round', 
        'taxable', 
        'summary'
    }

    for row in rows:
        if len(row) < 2:
            continue
        row_text = ' '.join(b.text for b in row)
        if any(w in row_text.lower() for w in SKIP):
            continue
        amt = rightmost_number_in_row(row)
        if not amt or amt <= 0:
            continue
        # First text block as description
        for b in row:
            if not re.match(r'^[\d,.\s]+$', b.text):
                desc = b.text.strip()
                if len(desc) > 2:
                    items.append(
                        {
                            "description": desc, 
                            "hsn_sac": _extract_hsn_from_row(row) or _lookup_hsn_by_description(desc),
                            "quantity": None, 
                            "unit": None,
                            "rate": None, 
                            "amount": amt
                        }
                    )
                break

    return items or [
        {
            "description": "See original bill", 
            "hsn_sac": None,
            "quantity": None, 
            "unit": None, 
            "rate": None, 
            "amount": 0
        }
    ]


def detect_type(blocks: list[Block]) -> str:
    full = ' '.join(b.text for b in blocks).lower()

    if re.search(r'\birn\b', full) or re.search(r'\back\s*(no|date)\b', full):
        return 'einvoice'

    pos_score = sum([
        any(k in full for k in ['grand total', 'total billing amount', 'billing amount']),
        'cashier' in full,
        'station id' in full,
        'bill no' in full and len(blocks) < 80,
        'quick service' in full,
        'e & oe' in full or 'e&oe' in full,
        'uom' in full, 
    ])
    if pos_score >= 2:
        return 'pos'

    ecom_kw = ['forward invoice', 'blink commerce', 'cloudtail', 'appario',
               'flipkart', 'amazon', 'order id', 'shipped by', 'fulfilled by']
    if any(k in full for k in ecom_kw):
        return 'ecommerce'

    return 'structured'


#  PLACE OF SUPPLY
def find_place_of_supply(rows: list[list[Block]]) -> str | None:
    val = find_value_right_of(rows, ['place of supply'])
    if val:
        return val.strip()[:60]
    # Fallback: regex on lines
    for row in rows:
        row_text = ' '.join(b.text for b in row)
        m = re.search(r'place\s+of\s+supply\s*[:\-]?\s*(.+)', row_text, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:60]
    return None

#  AMOUNT IN WORDS
def find_amount_words(rows: list[list[Block]]) -> str | None:
    for row in rows:
        row_text = ' '.join(b.text for b in row)
        rl = row_text.lower()
        if 'rupee' in rl and len(row_text) > 15:
            return row_text.strip()
        if 'only' in rl and any(w in rl for w in ['thousand', 'hundred', 'lakh', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'zero', 'inr']):
            return row_text.strip()
    return None


def detect_return_type(rows: list[list[Block]], blocks: list[Block]) -> str | None:
    """Detect credit note / debit note from PDF/invoice text."""
    full = ' '.join(b.text for b in blocks).lower()
    row_texts = [' '.join(b.text for b in row).lower() for row in rows]

    if any(k in full for k in ['credit note', 'credit-note', 'credit note no', 'cn no', 'cn no.']):
        return 'credit_note'
    if any(k in full for k in ['debit note', 'debit-note', 'debit note no', 'dn no', 'dn no.']):
        return 'debit_note'

    # look for specific line patterns in rows
    for row_text in row_texts[:10]:
        if 'credit note' in row_text or 'cn no' in row_text:
            return 'credit_note'
        if 'debit note' in row_text or 'dn no' in row_text:
            return 'debit_note'

    return None

#  PAYMENT MODE
def find_payment_mode(rows: list[list[Block]]) -> str | None:
    for row in rows:
        row_text = ' '.join(b.text for b in row).lower()
        if any(k in row_text for k in ['paid by', 'payment mode', 'pay using', 'mode of payment', 'payment method']):
            for mode in ['upi', 'cash', 'card', 'cheque', 'neft', 'rtgs', 'online', 'credit', 'debit', 'imps']:
                if mode in row_text:
                    return mode.upper()
    return None


"""
Classify a bill as 'input' (purchase from wholesaler/supplier)
or 'output' (sales to customer).
Heuristics:
  OUTPUT indicators — POS receipts, retail bills, cash memos,
                      customer-facing keywords, no buyer GSTIN,
                      known restaurant/food-delivery vendors.
  INPUT indicators  — einvoice types, has buyer GSTIN,
                      purchase-related keywords.
    Default           — 'input' (shopkeeper scanning supplier invoices).
"""
def classify_direction(data: dict, inv_type: str) -> str:

    full_text = ' '.join(
        item.get('description', '') for item in (data.get('items') or [])
    ).lower()
    vendor   = (data.get('vendor_name') or '').lower()
    buyer    = (data.get('buyer_name') or '').lower()
    bill_typ = (data.get('bill_type') or '').lower()
    notes    = (data.get('notes') or '').lower()

    all_text = f"{vendor} {buyer} {bill_typ} {notes} {full_text}"

    # Strong OUTPUT signals 
    OUTPUT_KW = [
        'cash memo', 
        'retail invoice', 
        'customer copy',
        'pos receipt', 
        'cash receipt', 
        'sales invoice',
        'sales bill', 
        'delivery note', 
        'customer invoice',
    ]

    # TODO: need to update the output vendors to database so that output bills can be eassily classified
    OUTPUT_VENDORS = [
        'jubilant foodworks', 
        'jubilant food', 
        'dominos', 
        "domino's",
        'pizza hut', 
        'kfc', 
        'mcdonald', 
        'burger king',
        'swiggy', 
        'zomato', 
        'restaurant', 
        'sweet house', 
        'bakery',
        'cafe', 
        'hotel', 
        'dhaba', 
        'food works',
    ]

    if inv_type == 'pos':
        return 'output'

    if any(kw in all_text for kw in OUTPUT_KW):
        return 'output'

    # Vendor name alone is a strong signal — Domino's etc. are always output
    if any(kw in vendor for kw in OUTPUT_VENDORS):
        return 'output'

    # INPUT signals
    INPUT_KW = [
        'purchase order', 
        'supplier invoice', 
        'proforma',
        'purchase invoice', 
        'wholesale', 
        'distributor', 
        'dealer invoice',
    ]

    if inv_type in ('einvoice', 'ecommerce'):
        return 'input'

    if any(kw in all_text for kw in INPUT_KW):
        return 'input'

    # Structured invoice with buyer GSTIN → likely B2B purchase
    if data.get('buyer_gstin') and inv_type == 'structured':
        return 'input'

    # Retail bill with no buyer info → likely output (sales)
    if bill_typ == 'retail bill' and not data.get('buyer_name'):
        return 'output'

    # Default: treat as input (shopkeeper's purchase bill)
    return 'input'



def parse_invoice(blocks: list[Block]) -> dict:
    rows = group_into_rows(blocks)

    gs          = all_gstins(blocks)
    vendor_gstin = gs[0] if gs else None
    buyer_gstin  = gs[1] if len(gs) > 1 else None

    vendor      = find_vendor_name(blocks)
    date        = first_date(blocks)
    inv_no      = find_invoice_no(rows)
    buyer       = find_buyer_name(rows)
    pos_supply  = find_place_of_supply(rows)
    awords      = find_amount_words(rows)
    pmode       = find_payment_mode(rows)
    return_type = detect_return_type(rows, blocks)
    items       = find_items(rows)
    tax         = find_tax_amounts(rows)

    cgst_rate   = tax['cgst_rate']
    cgst_amount = tax['cgst_amount']
    sgst_rate   = tax['sgst_rate']
    sgst_amount = tax['sgst_amount']
    igst_rate   = tax['igst_rate']
    igst_amount = tax['igst_amount']
    cess        = tax['cess_amount']
    total_tax   = tax['total_tax']
    subtotal    = tax['taxable'] or tax['subtotal']
    grand_total = tax['grand_total']
    round_off   = tax['round_off']
    discount    = tax['discount']
    other_chg   = tax['other_chg']

    bill_type = classify(cgst_amount, sgst_amount, igst_amount, vendor_gstin)

    return {
        "vendor_name":         vendor,
        "vendor_gstin":        vendor_gstin,
        "invoice_number":      inv_no,
        "invoice_date":        date,
        "bill_type":           bill_type,
        "buyer_name":          buyer,
        "buyer_gstin":         buyer_gstin,
        "place_of_supply":     pos_supply,
        "items":               items,
        "subtotal":            subtotal,
        "discount":            discount,
        "taxable_amount":      subtotal,
        "cgst_rate":           cgst_rate,
        "cgst_amount":         cgst_amount,
        "sgst_rate":           sgst_rate,
        "sgst_amount":         sgst_amount,
        "igst_rate":           igst_rate,
        "igst_amount":         igst_amount,
        "cess_amount":         cess,
        "other_charges":       other_chg,
        "other_charges_label": None,
        "round_off":           round_off,
        "total_tax":           total_tax,
        "grand_total":         grand_total or 0,
        "amount_in_words":     awords,
        "payment_mode":        pmode,
        "notes":               None,
        "return_type":         return_type,
    }

