"""Collect Batdongsan.com.vn property listings directly into SQL Server.

Start with one results-page URL. The program discovers property links, follows
pagination, cleans listing fields and commits each property with its queue status.
SQL Server stores all results and resumable progress; no CSV, SQLite or HTML data
files are written. Requests run sequentially, at least ten seconds apart, and
access challenges stop the run.

From the project root:
    python main/main.py --check-db
    python main/main.py --new-run --max-pages 0 --max-items 100
    python main/main.py --resume RUN_ID --max-pages 0 --max-items 100
    python main/main.py --runs

Source labels and search keywords remain Vietnamese to match the website.
Original listing content is preserved; help, diagnostics and generated labels
are English. Robots rules are access instructions, not a license to reuse data.
Keep source URLs and check applicable terms before collecting or sharing data.
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from collections import namedtuple
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from sqlserver_store import SqlServerStore, DEFAULT_SERVER, DEFAULT_DATABASE, DEFAULT_DRIVER, utc_now

USER_AGENT = 'BDSResearchBot/1.0'
HOSTS = ('batdongsan.com.vn', 'www.batdongsan.com.vn')
DEFAULT_LIST_URL = 'https://batdongsan.com.vn/nha-dat-ban-da-nang'
MIN_DELAY = 10.0          # seconds; an application setting, not a website requirement
MAX_REDIRECTS = 3

# ---- Selectors checked against an HTTP 200 list-page response on 2026-10-12. ----
LIST_SCOPE_SELECTOR = '#product-lists-web'   # main listing-card container
CARD_SELECTOR = '.js__card'                                       # individual cards inside the main container
PAGINATION_SELECTOR = '.re__pagination-group'
LIST_EXCLUDED_SELECTOR = 'footer, aside, nav'
LIST_EMPTY_TEXT = re.compile(
    r'(?:Không tìm thấy (?:bất động sản|tin đăng|kết quả)(?: nào)?(?: phù hợp)?|'
    r'Không có (?:bất động sản|tin đăng|kết quả)(?: nào)?(?: phù hợp)?)[.!]?', re.I)
DETAIL_WAIT_SELECTOR = 'h1.re__pr-title'
PRODUCT_PATH_RE = re.compile(r'-pr(\d+)/?$')

DETAIL_COLUMNS = [
    'Title', 'Price_Text', 'Price_VND', 'Price_Per_M2', 'Area', 'Address', 'Province',
    'District', 'Ward', 'Post_ID', 'Post_Date', 'Expiration_Date', 'Verified_Status',
    'Legal_Status', 'Road_Width', 'Frontage', 'Direction', 'Is_Corner_Lot', 'Seller_Type',
    'Contact_Name', 'URL', 'Price_Per_M2_Source', 'Price_Period', 'Corner_Keywords',
    'Seller_Keywords',
]
COLUMNS = DETAIL_COLUMNS + ['Scraped_At', 'Source_List_URL']


# ======================================================================================
# 1. Error categories: access blocks, network failures, parsing and missing data
# ======================================================================================
class CrawlError(Exception):
    pass


class BlockedError(CrawlError):
    """401/403 or a CAPTCHA/bot check: stop the run without bypassing the block."""


class RateLimitedError(CrawlError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class GoneError(CrawlError):
    """HTTP 404/410: the listing is no longer available."""


class TransientError(CrawlError):
    """Timeouts, network errors and 5xx responses: retry a limited number of times."""


class HttpError(CrawlError):
    """Other 4xx responses: do not retry."""


class RobotsDisallowedError(CrawlError):
    pass


class ParseError(ValueError):
    """HTML loaded, but required fields could not be parsed."""


class StructureError(ParseError):
    """The expected listing container is missing: selectors changed or access was restricted."""


class StopRun(Exception):
    """Stop the entire run; committed progress remains saved."""


# ======================================================================================
# 2. Data cleaning and conversion helpers
# ======================================================================================
def clean(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', value or '')).strip()


def get_text(root, selector):
    element = root.select_one(selector)
    return clean(element.get_text(' ', strip=True)) if element else ''


def to_number(value):
    value = clean(value)
    if not re.fullmatch(r'[0-9]+(?:[.,][0-9]+)*', value):
        return None
    if ',' in value:
        if not re.fullmatch(r'(?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+),[0-9]+', value):
            return None
        value = value.replace('.', '').replace(',', '.')
    elif re.fullmatch(r'[0-9]{1,3}(?:\.[0-9]{3})+', value):
        value = value.replace('.', '')
    try:
        number = Decimal(value)
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def round_vnd(value):
    try:
        return int(value.quantize(Decimal('1'), rounding=ROUND_HALF_UP)) if value.is_finite() else None
    except (InvalidOperation, OverflowError):
        return None


UNIT_VALUE = {'tỷ': 10**9, 'tỉ': 10**9, 'triệu': 10**6, 'tr': 10**6, 'nghìn': 1000, 'ngàn': 1000}


def to_vnd(value):
    """Accept exact prices with explicit units; retain rental periods separately from sale prices."""
    value = clean(value).lower()
    # Strip only supported unit suffixes; never discard arbitrary qualifiers.
    suffix = r'(?:/\s*m[²2]|(?:/|mỗi)\s*(?:tháng|năm|ngày|tuần))'
    match = re.fullmatch(r'(.*?)\s*((?:' + suffix + r'\s*)*)', value)
    body = match[1].strip() if match else value
    tail = match[2] if match else ''
    if len(re.findall(r'm[²2]', tail)) > 1 or len(re.findall(r'tháng|năm|ngày|tuần', tail)) > 1:
        return None
    term = r'([0-9]+(?:[.,][0-9]+)*)\s*(tỷ|tỉ|triệu|tr|nghìn|ngàn|vnd|vnđ|đồng|đ)(?!\w)'
    matches = list(re.finditer(term, body))
    if not matches or re.sub(term, '', body).strip():
        return None  # ranges, qualifiers, negations and unknown currencies are not exact prices
    multipliers = [UNIT_VALUE.get(m[2], 1) for m in matches]
    if multipliers != sorted(set(multipliers), reverse=True):
        return None
    total = Decimal(0)
    for match, multiplier in zip(matches, multipliers):
        if multiplier > 1 and re.fullmatch(r'[0-9]{1,3}\.[0-9]{3}', match[1]):
            return None  # a dotted billion amount is ambiguous; do not guess
        amount = to_number(match[1])
        if amount is None:
            return None
        total += amount * multiplier
    return round_vnd(total)


def to_measure(value):
    import math
    match = re.fullmatch(r'([0-9]+(?:[.,][0-9]+)*)\s*m(?:²|2)?', clean(value), re.I)
    number = to_number(match[1]) if match else None
    if number is None:
        return None
    result = float(number)
    return result if math.isfinite(result) else None


# Negated owner/broker phrases must not be counted as positive seller evidence.
NEGATED_BROKER = re.compile(
    r'\b(?:không|ko|miễn|chưa)\b\s+(?:(?:phải|là|làm|qua|tiếp|nhận|cần|rõ)\s+){0,3}'
    r'(?:môi giới|trung gian|chính chủ|chủ ký gửi|chủ gửi)')


def parse_detail_page(html_content, page_url):
    soup = BeautifulSoup(html_content, 'html.parser')
    root = soup.select_one('.js__product-detail-web, .re__pr-info, #product-detail-web')
    if root is None or not root.select_one('h1.re__pr-title'):
        raise ParseError('Listing details were not found; access may be blocked or the HTML layout changed.')

    # Collect attributes by their source labels, independently of display order.
    info = {}
    total_price_text = ''
    unit_price_text = ''
    price_labels = ('mức giá', 'khoảng giá', 'giá')
    for item in root.select('.re__pr-short-info-item, .re__pr-specs-content-item'):
        label = get_text(item, '.title, .re__pr-specs-content-item-title').lower()
        value = get_text(item, '.value, .re__pr-specs-content-item-value')

        if label in price_labels:
            if re.search(r'/\s*m[²2]', value, re.I):
                unit_price_text = value
            elif value and not total_price_text:
                total_price_text = value

            # Use the supplementary unit price only as a fallback.
            extra = get_text(item, '.ext')
            if not unit_price_text and re.search(r'/\s*m[²2]', extra, re.I):
                unit_price_text = extra
        elif label and value:
            info[label] = value

    title = get_text(root, 'h1.re__pr-title')
    price_text = total_price_text or unit_price_text
    area = to_measure(info.get('diện tích', ''))
    price_is_per_m2 = bool(re.search(r'/\s*m[²2]', price_text, re.I))
    price_vnd = None if price_is_per_m2 else to_vnd(price_text)
    price_per_m2 = to_vnd(price_text if price_is_per_m2 else unit_price_text)
    price_source = 'displayed' if price_per_m2 is not None else ''
    if price_per_m2 is None and price_vnd is not None and area and area > 0:
        price_per_m2 = round_vnd(Decimal(price_vnd) / Decimal(str(area)))
        price_source = 'calculated'
    period = re.search(r'(?:/\s*|mỗi\s+)(tháng|năm|ngày|tuần)', price_text, re.I)

    address = get_text(root, '.re__ldp-address .re__address, .re__pr-address, .re__address')
    # Parse the primary address, excluding parenthesized alternatives; preserve the raw address.
    parts = [clean(part) for part in re.sub(r'\([^)]*\)', '', address).split(',')]
    parts = [part for part in parts if part]
    ward = next((x for x in parts if re.match(r'^(xã|phường|thị trấn)\b', x, re.I)), '')
    district = next((x for x in parts if re.match(r'^(quận|huyện|thị xã)\b', x, re.I)), '')
    last = parts[-1] if parts else ''
    province = last if re.fullmatch(
        r'(?:tỉnh\s+\S.*|(?:(?:thành phố|tp[.]?)\s+)?đà nẵng)', last, re.I) else ''
    # Do not infer provinces from arbitrary suffixes or map historical boundaries.

    post_id = info.get('mã tin') or root.get('prid', '')
    if not post_id:
        match = PRODUCT_PATH_RE.search(urlsplit(page_url).path)
        post_id = match[1] if match else ''

    # Use verification evidence for this listing only; missing evidence stays unknown.
    verified = None
    for script in soup.find_all('script'):
        for match in re.finditer(r"JSON\.parse\('([^']+)'\)", script.string or ''):
            try:
                data = json.loads(match[1])
                for product in data.get('products', []):
                    if str(product.get('productId')) == str(post_id) and isinstance(product.get('verified'), bool):
                        verified = product['verified']
            except (ValueError, TypeError, AttributeError):
                continue

    description = get_text(root, '.re__pr-description')
    content = NEGATED_BROKER.sub(' ', clean(title + ' ' + description).lower())
    corner_keywords = re.findall(r'\b(?:lô góc|sát góc|kề góc|(?:2|hai) mặt tiền)\b', content)
    seller_keywords = re.findall(r'\b(?:chính chủ|chủ ký gửi|chủ gửi|môi giới)\b', content)
    owner = 'chính chủ' in seller_keywords
    broker = any(x in seller_keywords for x in ('chủ gửi', 'chủ ký gửi', 'môi giới'))
    seller_type = 'Unknown'
    if owner and broker:
        seller_type = 'Unknown (conflicting keywords)'
    elif owner:
        seller_type = 'Owner (self-reported)'
    elif broker:
        seller_type = 'Broker (inferred)'

    contact = soup.select_one('.re__contact-name, .js_contact-name')
    contact_name = clean(contact.get('title') or contact.get_text(' ', strip=True)) if contact else ''
    if not contact_name:
        # Fallback for contact-name phrases; exclude digits to avoid collecting phone numbers.
        match = re.search(r'(?:liên hệ|lh)\s*[:：]\s*([^\d,;.\n]{2,50})', description, re.I)
        contact_name = clean(match[1]) if match else ''

    # Do not count an empty parsed record as a successful listing.
    if not title or not post_id:
        raise ParseError('Title or Post_ID is missing.')
    if not (price_text or area or address):
        raise ParseError('Could not read the price, area or address.')

    return {
        'Title': title,
        'Price_Text': price_text,
        'Price_VND': price_vnd,
        'Price_Per_M2': price_per_m2,
        'Area': area,
        'Address': address,
        'Province': province,
        'District': district,
        'Ward': ward,
        'Post_ID': str(post_id),
        'Post_Date': info.get('ngày đăng', ''),
        'Expiration_Date': info.get('ngày hết hạn', ''),
        'Verified_Status': verified,
        'Legal_Status': info.get('pháp lý', ''),
        'Road_Width': to_measure(info.get('đường vào', '')),
        'Frontage': to_measure(info.get('mặt tiền', '')),
        'Direction': info.get('hướng nhà') or info.get('hướng đất', ''),
        'Is_Corner_Lot': bool(corner_keywords),
        'Seller_Type': seller_type,
        'Contact_Name': contact_name,
        'URL': page_url,
        'Price_Per_M2_Source': price_source,
        'Price_Period': {'tháng': 'month', 'năm': 'year', 'ngày': 'day', 'tuần': 'week'}.get(period[1].lower(), '') if period else '',
        'Corner_Keywords': '; '.join(dict.fromkeys(corner_keywords)),
        'Seller_Keywords': '; '.join(dict.fromkeys(seller_keywords)),
    }


# ======================================================================================
# 3. List pages: discover property URLs and the actual next-page link
# ======================================================================================
def valid_url(url):
    try:
        p = urlsplit(url)
        if (p.scheme != 'https' or p.hostname not in HOSTS
                or p.username or p.password or p.port not in (None, 443)):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('Only HTTPS URLs on batdongsan.com.vn are accepted.') from None
    return url


def valid_list_url(url):
    valid_url(url)
    if PRODUCT_PATH_RE.search(urlsplit(url).path) or not transaction_prefix(url):
        raise ValueError('Enter a property sale or rental results-page URL.')
    page_number(url)  # reject ambiguous pagination
    return url


def page_number(url):
    from urllib.parse import parse_qsl
    p = urlsplit(url)
    match = re.search(r'/p(\d+)/?$', p.path)
    values = [v for k, v in parse_qsl(p.query, keep_blank_values=True) if k == 'page']
    if (len(values) > 1 or (values and not re.fullmatch(r'[1-9][0-9]*', values[0]))):
        raise ValueError('Invalid page query parameter.')
    number = int(match[1]) if match else int(values[0]) if values else 1
    if number < 1 or (match and values and int(values[0]) != number):
        raise ValueError('Invalid or conflicting page numbers.')
    return number


def page_base(url):
    p = urlsplit(url)
    return (HOSTS[0] if p.hostname in HOSTS else p.hostname), re.sub(r'/p\d+/?$', '', p.path).rstrip('/')


def transaction_prefix(list_url):
    path = urlsplit(list_url).path.strip('/').split('/')[0]
    if re.match(r'^(?:cho-thue|nha-dat-cho-thue)(?:-|$)', path):
        return 'cho-thue-'
    if re.match(r'^(?:ban|nha-dat-ban)(?:-|$)', path):
        return 'ban-'
    return ''


ListResult = namedtuple('ListResult', 'detail_urls next_url')


def parse_list_page(html_content, page_url):
    valid_list_url(page_url)
    soup = BeautifulSoup(html_content, 'html.parser')
    _check_blocked(Response(200, page_url, html_content, {}))
    # Use only the main listing container; exclude recommendation sections.
    scopes = soup.select(LIST_SCOPE_SELECTOR)
    if len(scopes) != 1:
        raise StructureError('Could not identify exactly one main listing container.')
    scope = scopes[0]
    for unwanted in scope.select(LIST_EXCLUDED_SELECTOR):
        unwanted.decompose()
    cards = scope.select(CARD_SELECTOR)
    prefix, found = transaction_prefix(page_url), {}
    for card in cards:
        anchors = ([card] if card.name == 'a' and card.has_attr('href') else [])
        anchors += card.select('a[href]')
        for anchor in anchors:
            try:
                absolute = valid_url(urljoin(page_url, anchor['href']))
            except ValueError:
                continue
            p = urlsplit(absolute)
            if not PRODUCT_PATH_RE.search(p.path) or not p.path.lstrip('/').startswith(prefix):
                continue
            found.setdefault(urlunsplit(('https', HOSTS[0], p.path.rstrip('/'), '', '')), None)
    if not found:
        # Require an explicit empty-results message inside the listing container.
        empty = any(LIST_EMPTY_TEXT.fullmatch(clean(node.get_text(' ', strip=True)))
                    for node in [scope] + scope.select('p, div, span, h1, h2, h3'))
        if cards or not empty:
            raise StructureError('No valid listing URLs; check selectors/HTML instead of assuming the end of results.')
        if find_next_page(soup, page_url):
            raise StructureError('The empty-results message conflicts with an available next-page link.')
        return ListResult([], None)
    return ListResult(list(found), find_next_page(soup, page_url))


def find_next_page(soup, page_url):
    from urllib.parse import parse_qsl
    valid_list_url(page_url)
    wanted, base = page_number(page_url) + 1, page_base(page_url)
    filters = sorted((k, v) for k, v in parse_qsl(urlsplit(page_url).query,
                     keep_blank_values=True) if k != 'page')
    candidates = soup.select('head link[rel~=next][href]')
    for pagination in soup.select(PAGINATION_SELECTOR):
        candidates.extend(pagination.select('a[href]'))
    found, unsafe = set(), False
    for tag in candidates:
        if tag.get('aria-disabled') == 'true' or 'disabled' in tag.get('class', []):
            continue
        try:
            absolute = valid_url(urljoin(page_url, tag['href']).split('#')[0])
            if page_base(absolute) != base or page_number(absolute) != wanted:
                continue
        except ValueError:
            continue
        query = sorted((k, v) for k, v in parse_qsl(urlsplit(absolute).query,
                       keep_blank_values=True) if k != 'page')
        if query != filters:
            unsafe = True
            continue
        found.add(absolute)
    if found:
        return sorted(found)[0]  # follow the actual URL; do not reconstruct missing filters
    if unsafe:
        raise StructureError('The next-page link removes or changes query filters.')
    return None


# ======================================================================================
# 4. Shared HTML request throttling and page fetchers
# ======================================================================================
class Throttle:
    """Space sequential request starts correctly, even if sleep returns early."""

    def __init__(self, min_interval=MIN_DELAY, clock=time.monotonic, sleep=time.sleep):
        import math
        if not math.isfinite(min_interval) or min_interval < MIN_DELAY:
            raise ValueError(f'The delay must be finite and >= {MIN_DELAY:g} seconds.')
        self.min_interval, self.clock, self.sleep = min_interval, clock, sleep
        self.last_start = None
        self.not_before = None

    def raise_interval(self, seconds):
        import math
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('The delay must be finite and nonnegative.')
        self.min_interval = max(self.min_interval, seconds)

    def pause(self, seconds):
        import math
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('The retry delay must be finite and nonnegative.')
        target = self.clock() + seconds
        self.not_before = max(self.not_before or target, target)

    def wait(self):
        ready = self.clock()
        if self.last_start is not None:
            ready = max(ready, self.last_start + self.min_interval)
        if self.not_before is not None:
            ready = max(ready, self.not_before)
        while True:
            now = self.clock()
            if now >= ready:
                self.last_start = now
                return
            self.sleep(ready - now)


Response = namedtuple('Response', 'status url text headers')


def decode_body(raw, headers):
    match = re.search(r'charset\s*=\s*[\"\']?([\w-]+)', headers.get('content-type', ''), re.I)
    try:
        return raw.decode(match[1] if match else 'utf-8-sig', errors='replace')
    except LookupError:
        return raw.decode('utf-8-sig', errors='replace')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HttpFetcher:
    """One request only; Crawler handles throttling, redirects and retries."""

    def __init__(self, timeout=30):
        self.timeout, self.opener = timeout, build_opener(_NoRedirect)

    def fetch(self, url, wait_selector=None):
        from http.client import HTTPException
        valid_url(url)
        request = Request(url, headers={'User-Agent': USER_AGENT,
                                        'Accept': 'text/html,application/xhtml+xml,text/plain;q=0.9',
                                        'Accept-Language': 'vi,en;q=0.5'})
        try:
            try:
                reply = self.opener.open(request, timeout=self.timeout)
            except HTTPError as error:
                reply = error  # also close error response streams
            with reply:
                headers = {k.lower(): v for k, v in reply.headers.items()}
                return Response(reply.code, url, decode_body(reply.read(), headers), headers)
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            raise TransientError(f'Network error/timeout: {error}') from error

    def close(self):
        pass


CHALLENGE_TITLE = re.compile(
    r'captcha|just a moment|access denied|verify you are human|robot check|attention required|checking your browser', re.I)
CHALLENGE_MARKERS = re.compile(r'cf-turnstile|g-recaptcha|h-captcha|challenge-platform|/cdn-cgi/challenge', re.I)


def _check_blocked(resp):
    # Do not expose query/session identifiers in diagnostic messages.
    p = urlsplit(resp.url)
    safe_url = urlunsplit((p.scheme, p.netloc, p.path, '', ''))
    if resp.headers.get('cf-mitigated', '').lower() == 'challenge':
        ray = resp.headers.get('cf-ray', 'not supplied')
        raise BlockedError(
            f'Cloudflare access challenge (HTTP {resp.status}) at {safe_url}; Ray ID: {ray}. '
            'If using a VPN/proxy, test with it disconnected. For browser loading use '
            '--fetcher playwright --browser-channel msedge. The challenge is not a robots.txt rule')
    if resp.status in (401, 403):
        raise BlockedError(f'HTTP {resp.status} at {safe_url}')
    soup = BeautifulSoup(resp.text, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    # Actual challenge UI/title is evidence even if a cached listing shell exists.
    # Script URLs and inline challenge-platform/precursor references alone are not.
    if CHALLENGE_TITLE.search(title) or soup.select_one(
            '#challenge-form, .g-recaptcha, .h-captcha, .cf-turnstile, iframe[src*="captcha"]'):
        raise BlockedError(f'Access challenge/CAPTCHA at {safe_url}')


class BrowserFetcher:
    """Chromium native navigation; CDP stops redirects before their next request.

    Context/page survive fetches. Crawler follows each captured redirect after
    scope/robots checks. Only proactive navigation/robots uses Throttle.
    """
    handles_navigation_throttle = True

    def __init__(self, robots_allows):
        self.robots_allows = robots_allows
        self.channel = 'chromium'
        self.log = print
        self._pw = self.browser = self.context = self.page = self._cdp = None
        self.throttle = None
        self._response = self._error = None
        self._expected = None
        self._active = self._document_sent = False
        self._subresource_redirects = {}
        self.skip_analytics_bootstrap = True
        self._finished_interceptions = set()
        self._finished_routes = {}
        self._route_aborts = set()
        self._route_handler = None

    @staticmethod
    def _optional_telemetry(url, resource_type):
        # Evidence: saved dataLayer bootstrap + GA user_engagement payload;
        # scope is this exact first-party collector, never all analytics/403s.
        p = urlsplit(url)
        return (resource_type in ('fetch', 'xhr', 'ping') and p.scheme == 'https'
                and p.hostname == 'batdongsan.com.vn' and p.port in (None, 443)
                and not p.username and not p.password and p.path == '/gtganalytics/ag/g/c')

    def _skip_optional_bootstrap(self, url, resource_type):
        # This exact analytics script was not needed for the verified listing parsers.
        if (self.skip_analytics_bootstrap and resource_type == 'script'
                and url == 'https://batdongsan.com.vn/gtganalytics/'):
            self._diagnostic('optional_bootstrap_skipped', url, resource_type, None)
            return True
        return False

    @staticmethod
    def _log_url(url):
        try:
            p = urlsplit(url or '')
            return urlunsplit((p.scheme, p.netloc, p.path, '', ''))
        except ValueError:
            return '<invalid-url>'

    def _diagnostic(self, classification, url, resource_type, status, redirect_target=None):
        data = {'main_url': self._log_url(self._expected),
                'resource_url': self._log_url(url), 'resource_type': resource_type,
                'status': status, 'classification': classification}
        if redirect_target is not None:
            data['redirect_target'] = self._log_url(redirect_target)
        try:
            self.log('[browser] ' + json.dumps(data, ensure_ascii=False))
        except Exception:
            pass  # logging must not conceal the original network error
        return data

    def _failure(self, error, url, resource_type, status):
        if hasattr(error, 'diagnostic'):
            return error
        scope = 'main' if resource_type in ('document', 'robots') else 'required_resource'
        classification = scope + ('_blocked' if isinstance(error, BlockedError) else '_error')
        data = self._diagnostic(classification, url, resource_type, status,
                                getattr(error, "redirect_target", None))
        error.diagnostic = data
        reason = re.sub(r'https?://[^\s]+', lambda m: self._log_url(m[0]), str(error))
        error.args = (json.dumps(data, ensure_ascii=False) + '; ' + reason,)
        return error

    def _start(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        options = {'headless': True}
        if self.channel != 'chromium':
            options['channel'] = self.channel
        self.browser = self._pw.chromium.launch(**options)
        self.context = self.browser.new_context(user_agent=USER_AGENT, service_workers='block')
        self.page = self.context.new_page()
        self._open_interception()
        self.context.on('page', lambda page: page.close() if page != self.page else None)

    def _open_interception(self):
        # A new CDP session owns each navigation. Retired listeners cannot mutate
        # the next navigation, even if Playwright delivers queued events later.
        session = self.context.new_cdp_session(self.page)
        self._cdp = session
        self._finished_interceptions.clear()
        self._finished_routes = {}
        self._route_aborts.clear()
        routes = self._finished_routes
        def routed(route):
            if self._cdp is session:
                self._route(route)
            elif id(route) not in routes:
                routes[id(route)] = route
                try:
                    route.abort()
                except Exception as error:
                    # Old owner only: report cleanup failure, never fail the new page.
                    try:
                        self.log(f'Route belongs to a retired navigation: {error}')
                    except Exception:
                        pass
        self._route_handler = routed
        self.context.route('**/*', routed)
        def paused(event):
            if self._cdp is session:
                self._paused_response(event)
        session.on('Fetch.requestPaused', paused)
        session.send('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Response'}]})

    def _end_route(self, route, abort):
        key = id(route)
        if key in self._finished_routes:
            return
        self._finished_routes[key] = route  # retain identity until navigation ends
        if abort:
            self._route_aborts.add((route.request.url, route.request.resource_type))
            route.abort()
        else:
            route.continue_()

    def _finish_interception(self, method, params):
        request_id = params['requestId']
        if request_id in self._finished_interceptions:
            return
        # Claim BEFORE send: synchronous protocol calls can dispatch more events.
        self._finished_interceptions.add(request_id)
        self._cdp.send(method, params)

    def _retire_interception(self):
        session, self._cdp = self._cdp, None
        if session is None:
            return None
        first_error = None
        # Invalidate the listener before cancellation dispatches delayed callbacks.
        for method in ('Page.stopLoading', 'Fetch.disable'):
            try:
                session.send(method)
            except Exception as error:
                first_error = first_error or error
        if self._route_handler is not None:
            handler, self._route_handler = self._route_handler, None
            try:
                self.context.unroute('**/*', handler)
            except Exception as error:
                first_error = first_error or error
        try:
            session.detach()
        except Exception as error:
            first_error = first_error or error
        if first_error is not None:
            return HttpError(f'Failed to retire Chromium interception: {first_error}')
        return None

    def _route(self, route):
        request = route.request
        try:
            if not self._active or self._error is not None:
                self._end_route(route, True); return
            valid_url(request.url)
            if self._skip_optional_bootstrap(request.url, request.resource_type):
                self._end_route(route, True); return
            if self._optional_telemetry(request.url, request.resource_type):
                self._diagnostic('optional_telemetry_skipped', request.url, request.resource_type, None)
                self._end_route(route, True); return
            main = request.resource_type == 'document'
            if main:
                if (request.frame != self.page.main_frame or request.url != self._expected
                        or self._document_sent):
                    raise HttpError('Browser rejected an iframe or navigation outside the validated main request.')
                if not self.robots_allows(request.url):
                    raise RobotsDisallowedError(f'robots.txt disallows {request.url}')
                self.throttle.wait()  # actual navigation start, after browser startup
                self._document_sent = True
            elif (request.resource_type not in ('script', 'xhr', 'fetch')
                  or not self.robots_allows(request.url)):
                self._end_route(route, True); return
            # Never run challenge helpers. Presence alone is not a blocked page.
            if re.search(r'/cdn-cgi/challenge|captcha', urlsplit(request.url).path, re.I):
                self._end_route(route, True); return
            self._end_route(route, False)
        except ValueError:
            self._end_route(route, True)
        except Exception as error:
            failure = error if isinstance(error, CrawlError) else HttpError(f'Browser route error: {error}')
            self._error = self._failure(failure, request.url, request.resource_type, None)
            self._end_route(route, True)

    def _subresource_redirect(self, event, headers):
        """Validate Location while response is paused, before Chromium follows it."""
        url = event['request']['url']
        kind = event.get('resourceType', 'unknown').lower()
        status = event.get('responseStatusCode')
        target = None
        try:
            if status not in (301, 302, 303, 307, 308):
                raise HttpError('Unsupported subresource redirect status.')
            if sum(h['name'].lower() == 'location' for h in event.get('responseHeaders', [])) > 1:
                raise HttpError('Subresource redirect contains conflicting Location headers.')
            location = headers.get('location', '').strip()
            if '\\' in location or any(ord(c) < 32 for c in location):
                raise HttpError('Location contains unsafe characters or differs from Chromium URL parsing.')
            if not location:
                raise HttpError('Subresource redirect is missing Location.')
            target = urljoin(url, location).split('#')[0]
            try:
                valid_url(target)
            except ValueError as error:
                raise HttpError('Subresource redirect target is outside the allowed domain/HTTPS URL scope.') from error
            network_id = event.get('networkId')
            if not network_id:
                raise HttpError('Missing networkId for tracking the subresource redirect chain.')
            chain = self._subresource_redirects.setdefault(network_id,
                        {'seen': {url}, 'expected': url, 'hops': 0})
            if url != chain['expected']:
                raise HttpError('Subresource redirect chain does not match the validated URL.')
            if target in chain['seen']:
                raise HttpError('Subresource redirect loop.')
            if chain['hops'] >= MAX_REDIRECTS:
                raise HttpError('Subresource redirect limit exceeded.')
            if self._optional_telemetry(target, kind):
                self._diagnostic('optional_telemetry_skipped', target, kind, None)
                self._finish_interception('Fetch.failRequest', {'requestId': event['requestId'], 'errorReason': 'Aborted'})
                self._subresource_redirects.pop(network_id, None)
                return
            if not self.robots_allows(target):
                raise RobotsDisallowedError('The subresource redirect target is not allowed by the origin robots cache.')
            if self._skip_optional_bootstrap(target, kind):
                self._finish_interception('Fetch.failRequest', {'requestId': event['requestId'], 'errorReason': 'Aborted'})
                self._subresource_redirects.pop(network_id, None)
                return
            if re.search(r'/cdn-cgi/challenge|captcha', urlsplit(target).path, re.I):
                raise HttpError('Do not load challenge helpers through subresource redirects.')
            chain['seen'].add(target)
            chain['expected'] = target
            chain['hops'] += 1
            self._diagnostic('resource_redirect_followed', url, kind, status, target)
            self._finish_interception('Fetch.continueResponse', {'requestId': event['requestId']})
        except Exception as error:
            if target is not None:
                error.redirect_target = target
            raise

    def _paused_response(self, event):
        import base64
        request_id = event['requestId']
        if request_id in self._finished_interceptions:
            return
        main = event.get('resourceType') == 'Document'
        headers = {h['name'].lower(): h['value'] for h in event.get('responseHeaders', [])}
        status = event.get('responseStatusCode', 0)
        url = event['request']['url']
        try:
            if (not status and event.get('responseErrorReason') in ('Aborted', 'Failed')
                    and (url, event.get('resourceType', '').lower()) in self._route_aborts):
                # The route is cancelled, but Fetch may still hold a response-error
                # pause. Release that pause once without re-failing the request.
                try:
                    self._finish_interception('Fetch.continueRequest', {'requestId': request_id})
                except Exception as error:
                    if not re.search(r'Protocol error \(Fetch[.]continueRequest\): Invalid [Ii]nterceptionId[.]?$', str(error)):
                        raise
                    self._diagnostic('cancelled_interception_expired', url,
                                     event.get('resourceType', '').lower(), None)
                return
            if not self._active or self._error is not None:
                self._finish_interception('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'})
                return
            # A response pause may arrive even after request routing rejected optional
            # telemetry. Apply the same exact endpoint policy before classifying it.
            kind = event.get('resourceType', 'unknown').lower()
            if not main and self._skip_optional_bootstrap(url, kind):
                self._finish_interception('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'})
                return
            if not main and self._optional_telemetry(url, kind):
                self._diagnostic('optional_telemetry_skipped', url, kind, status or None)
                self._finish_interception('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'})
                return
            if not status:
                if main:
                    raise HttpError('Chromium did not receive an HTTP response.')
                self._finish_interception('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'})
                return  # resource deliberately aborted by route policy
            response = Response(status, url, '', headers)
            if main:
                self._response = response
                if status in (401, 403) or headers.get('cf-mitigated', '').lower() == 'challenge':
                    # Capture evidence when available, but body failure must never
                    # demote a definite block into a retryable/general HTTP error.
                    try:
                        body = self._cdp.send('Fetch.getResponseBody', {'requestId': request_id})
                        text = (decode_body(base64.b64decode(body['body']), headers)
                                if body.get('base64Encoded') else body['body'])
                        self._response = response._replace(text=text)
                    except Exception:
                        pass
                    _check_blocked(self._response)
            # Status/header always wins, including pages containing real cards.
            if not main or 300 <= status < 400:
                _check_blocked(response)
            if 300 <= status < 400:
                if not main:
                    self._subresource_redirect(event, headers)
                    return
                self._response = response
                self._finish_interception('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'})
                return
            if not main:
                self._subresource_redirects.pop(event.get('networkId'), None)
            if main or 'html' in headers.get('content-type', '').lower():
                body = self._cdp.send('Fetch.getResponseBody', {'requestId': request_id})
                raw = (base64.b64decode(body['body']) if body.get('base64Encoded')
                       else body['body'].encode('utf-8'))
                text = decode_body(raw, headers) if body.get('base64Encoded') else body['body']
                response = response._replace(text=text)
                if main:
                    self._response = response
                _check_blocked(response)
            if status >= 400:
                self._diagnostic('main_http_error' if main else 'required_resource_http_error',
                                 url, event.get('resourceType', 'unknown').lower(), status)
            if main and status != 200:
                self._finish_interception('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'})
            else:
                self._finish_interception('Fetch.continueResponse', {'requestId': request_id})
        except Exception as error:
            failure = error if isinstance(error, CrawlError) else HttpError(f'Chromium response validation error: {error}')
            reported = self._failure(failure, url, event.get('resourceType', 'unknown').lower(), status or None)
            if self._error is None:
                self._error = reported
            try:
                self._finish_interception('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'})
            except Exception as cleanup_error:
                self._failure(HttpError(f'Interception cleanup error: {cleanup_error}'),
                              url, event.get('resourceType', 'unknown').lower(), status or None)

    def fetch(self, url, wait_selector=None):
        if isinstance(self._error, BlockedError):
            raise self._error  # latch: never continue the same blocked session
        self._response = self._error = None
        self._expected = url
        self._document_sent = False
        self._subresource_redirects.clear()
        try:
            valid_url(url)
            if self.throttle is None:
                raise HttpError('BrowserFetcher must be configured through Crawler.')
            if self._pw is None:
                self._start()
            elif self._cdp is None:
                self._open_interception()
            if urlsplit(url).path == '/robots.txt':
                self.throttle.wait()
                reply = self.context.request.get(url, timeout=30000, max_redirects=0, max_retries=0)
                try:
                    headers = {k.lower(): v for k, v in reply.headers.items()}
                    response = Response(reply.status, reply.url, decode_body(reply.body(), headers), headers)
                    self._response = response
                    _check_blocked(response)
                    return response
                finally:
                    reply.dispose()
            self._active = True
            try:
                self.page.goto(url, wait_until='domcontentloaded', timeout=60000)
            except Exception:
                if self._response is None and self._error is None:
                    raise
            if self._error is not None:
                raise self._error
            if self._response is None:
                raise HttpError('The browser did not receive the main document.')
            response = self._response
            if response.status != 200:
                return response
            _check_blocked(response)
            selector = wait_selector or (DETAIL_WAIT_SELECTOR if PRODUCT_PATH_RE.search(urlsplit(url).path)
                                          else LIST_SCOPE_SELECTOR)
            # Original response is authoritative when the expected content exists.
            if BeautifulSoup(response.text, 'html.parser').select_one(selector):
                return response
            try:
                self.page.wait_for_selector(selector, state='attached', timeout=15000)
            except Exception:
                pass  # parser reports unknown/missing structure, not a CAPTCHA guess
            if self._error is not None:
                raise self._error
            response = response._replace(text=self.page.content())
            _check_blocked(response)
            return response
        except BaseException as error:
            self.close()  # handles partial startup and Ctrl+C; preserves primary error
            if isinstance(error, (CrawlError, KeyboardInterrupt, SystemExit)):
                if isinstance(error, CrawlError) and not hasattr(error, 'diagnostic'):
                    kind = 'robots' if urlsplit(url).path == '/robots.txt' else 'document'
                    self._failure(error, url, kind, self._response.status if self._response else None)
                if isinstance(error, BlockedError):
                    self._error = error
                raise
            raise HttpError(f'Browser unavailable or execution failed: {error}') from error
        finally:
            self._active = False
            # Keep context/page, but retire Fetch ownership before next navigation.
            retirement_error = self._retire_interception()
            if retirement_error is not None:
                if sys.exc_info()[0] is None:
                    raise retirement_error
                try:
                    self.log(str(retirement_error))
                except Exception:
                    pass  # preserve an existing primary error

    def close(self):
        self._subresource_redirects.clear()
        self._active = False
        retirement_error = self._retire_interception()
        if retirement_error is not None:
            try:
                self.log(str(retirement_error))
            except Exception:
                pass
        for name, method in (('page', 'close'), ('context', 'close'), ('browser', 'close'), ('_pw', 'stop')):
            obj = getattr(self, name, None)
            setattr(self, name, None)
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass


def parse_retry_after(value, now=None):
    value = (value or '').strip()
    if re.fullmatch(r'[0-9]+', value):
        # Return infinity for huge values so Crawler stops at MAX_WAIT instead of sleeping.
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        current = now if now is not None else datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return max(0.0, (moment - current).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class _RobotsPolicy(RobotFileParser):
    """Match the product token and merge groups; longest rules win, with Allow winning ties.

    Avoid urllib.robotparser first-match behavior. For crawl-delay/request-rate
    extensions, use the longest interval across the selected groups.
    """

    def parse(self, lines):
        self.groups = []
        agents, records = [], []
        for raw in lines:
            key, sep, value = raw.lstrip('\ufeff').split('#', 1)[0].partition(':')
            if not sep:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == 'user-agent':
                if records:
                    self.groups.append((agents, records))
                    agents, records = [], []
                agents.append(value.lower())
            elif agents and key in ('allow', 'disallow', 'crawl-delay', 'request-rate'):
                records.append((key, value))
        if agents:
            self.groups.append((agents, records))
        self.modified()

    def _records(self, useragent):
        token = useragent.split('/', 1)[0].lower()
        groups = [r for a, r in self.groups if token in a]
        if not groups:
            groups = [r for a, r in self.groups if '*' in a]
        return [item for group in groups for item in group]

    @staticmethod
    def _normalize(value):
        from urllib.parse import quote
        value = quote(value, safe="/%:?!$&'()*+,;=@-._~")
        def decode(match):
            char = chr(int(match[1], 16))
            return char if char.isascii() and (char.isalnum() or char in '-._~') else match[0].upper()
        return re.sub(r'%([0-9a-fA-F]{2})', decode, value)

    def can_fetch(self, useragent, url):
        if self.allow_all:
            return True
        if self.disallow_all or not self.mtime():
            return False
        p = urlsplit(url)
        if p.path == '/robots.txt':
            return True
        target = self._normalize((p.path or '/') + ('?' + p.query if p.query else ''))
        matches = []
        for key, value in self._records(useragent):
            if key not in ('allow', 'disallow') or not value or not value.startswith('/'):
                continue
            pattern = self._normalize(value)
            terminal = pattern.endswith('$')
            body = pattern[:-1] if terminal else pattern
            regex = '^' + '.*'.join(re.escape(part) for part in body.split('*')) + ('$' if terminal else '')
            if re.search(regex, target):
                length = len(re.sub(r'%[0-9A-F]{2}', '_', body.replace('*', '')))
                matches.append((length, key == 'allow'))
        return max(matches)[1] if matches else True

    def crawl_delay(self, useragent):
        import math
        if self.allow_all:
            return None
        intervals = []
        for key, value in self._records(useragent):
            if key not in ('crawl-delay', 'request-rate'):
                continue
            try:
                if key == 'request-rate':
                    requests, seconds = map(float, value.split('/'))
                    if not math.isfinite(requests) or requests <= 0:
                        raise ValueError
                    interval = seconds / requests
                else:
                    interval = float(value)
                if not math.isfinite(interval) or interval < 0:
                    raise ValueError
                intervals.append(interval)
            except (ValueError, OverflowError, ZeroDivisionError):
                raise HttpError(f'robots.txt contains an invalid {key}; stop for inspection.') from None
        return max(intervals, default=None)

    def request_rate(self, useragent):
        return None  # already included in crawl_delay


class Crawler:
    """Cache robots by origin; redirects, retries and browser loading share one Throttle."""
    RATE_BACKOFF = 60
    MAX_RATE_RETRIES = 3
    TRANSIENT_BACKOFF = 30
    MAX_TRANSIENT_RETRIES = 2
    MAX_WAIT = 900

    def __init__(self, http, throttle, mode='http', browser=None, log=print):
        self.http, self.throttle, self.mode, self.log = http, throttle, mode, log
        self.browser = browser
        self.robots = {}

    @staticmethod
    def _origin(url):
        valid_url(url)
        return 'https://' + urlsplit(url).hostname  # port 443 and case variants share the same origin

    def robots_allows(self, url):
        try:
            parser = self.robots.get(self._origin(url))
        except ValueError:
            return False
        return parser is not None and parser.can_fetch(USER_AGENT, url)

    def _load_robots(self, origin):
        origin = self._origin(origin)
        if origin in self.robots:
            return
        fetcher = self.browser if self.mode == 'playwright' else self.http
        if fetcher is None:
            raise HttpError('BrowserFetcher has not been configured.')
        if fetcher is self.browser:
            self.browser.throttle = self.throttle
        resp = self._follow(origin + '/robots.txt', fetcher, check_robots=False)
        parser = _RobotsPolicy()
        if resp.status in (404, 410):
            _check_blocked(resp)
            parser.allow_all = True
        else:
            self._classify(resp)
            if (re.search(r'<\s*(?:!doctype\s+html|html|head|body|title)\b', resp.text, re.I)
                    or 'html' in resp.headers.get('content-type', '').lower()):
                raise BlockedError('robots.txt returned HTML; stop for inspection.')
            parser.parse(resp.text.splitlines())
        extra = parser.crawl_delay(USER_AGENT) or 0
        if extra > self.MAX_WAIT:
            raise HttpError('robots.txt requires a delay above MAX_WAIT; stop.')
        self.throttle.raise_interval(extra)
        # Cache redirected robots policy only for the originally requested origin.
        self.robots[origin] = parser

    def _require_allowed(self, url):
        origin = self._origin(url)
        self._load_robots(origin)
        if not self.robots[origin].can_fetch(USER_AGENT, url):
            raise RobotsDisallowedError(f'robots.txt disallows {url}')

    @staticmethod
    def _redirect_target(source, target):
        from urllib.parse import parse_qsl
        try:
            valid_url(target)
        except ValueError as error:
            raise HttpError(f'Invalid redirect: {target}') from error
        p = urlsplit(source)
        if transaction_prefix(source) and not PRODUCT_PATH_RE.search(p.path):
            # List-page redirects must preserve filters, transaction type and page number.
            q = urlsplit(target)
            if (page_base(source) != page_base(target)
                    or page_number(source) != page_number(target)
                    or sorted(parse_qsl(p.query, keep_blank_values=True)) !=
                       sorted(parse_qsl(q.query, keep_blank_values=True))):
                raise HttpError('Redirect changes the list-page scope or filters.')
        return target

    def _follow(self, url, fetcher, check_robots=True):
        seen = set()
        for step in range(MAX_REDIRECTS + 1):
            try:
                valid_url(url)
            except ValueError as error:
                raise HttpError(f'Invalid fetch URL: {url}') from error
            if url in seen:
                raise HttpError('Redirect loop; stop without retrying.')
            seen.add(url)
            if check_robots:
                self._require_allowed(url)
            if getattr(fetcher, 'handles_navigation_throttle', False) is not True:
                self.throttle.wait()
            resp = fetcher.fetch(url, None)
            _check_blocked(resp)
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get('location')
                if not location or step == MAX_REDIRECTS:
                    raise HttpError('Redirect is missing Location or exceeds the limit; stop.')
                target = urljoin(url, location).split('#')[0]
                try:
                    url = self._redirect_target(url, target)
                except ValueError as error:
                    raise HttpError('Redirect contains invalid URL parameters.') from error
                continue
            return resp
        raise HttpError('Too many redirects.')

    def _classify(self, resp):
        _check_blocked(resp)
        status = resp.status
        if status == 429 or (status == 503 and 'retry-after' in resp.headers):
            raise RateLimitedError(f'HTTP {status} at {resp.url}', parse_retry_after(resp.headers.get('retry-after')))
        if status in (404, 410):
            raise GoneError(f'HTTP {status}')
        if status >= 500:
            raise TransientError(f'HTTP {status}')
        if status >= 400:
            raise HttpError(f'HTTP {status}')
        if status != 200:
            raise HttpError(f'HTTP {status} was unexpected')

    def _fetch_once(self, url, kind, force_browser):
        fetcher = self.http
        if force_browser or self.mode == 'playwright':
            if self.browser is None:
                raise HttpError('BrowserFetcher has not been configured.')
            self.browser.throttle = self.throttle
            fetcher = self.browser
        resp = self._follow(url, fetcher)
        self._classify(resp)
        return resp

    def fetch(self, url, kind, force_browser=False):
        rate_tries = transient_tries = 0
        while True:
            try:
                return self._fetch_once(url, kind, force_browser)
            except RateLimitedError as error:
                rate_tries += 1
                wait = error.retry_after if error.retry_after is not None else self.RATE_BACKOFF * 2 ** (rate_tries - 1)
                if rate_tries > self.MAX_RATE_RETRIES or wait > self.MAX_WAIT:
                    raise
                self.log(f'  {error}: wait {wait:.0f}s and retry ({rate_tries}/{self.MAX_RATE_RETRIES}).')
                self.throttle.pause(wait)
            except TransientError as error:
                transient_tries += 1
                if transient_tries > self.MAX_TRANSIENT_RETRIES:
                    raise
                wait = self.TRANSIENT_BACKOFF * 2 ** (transient_tries - 1)
                self.log(f'  {error}; retry after {wait:.0f}s ({transient_tries}/{self.MAX_TRANSIENT_RETRIES}).')
                self.throttle.pause(wait)

    def fetch_parse(self, url, kind, parser):
        resp = self.fetch(url, kind)
        try:
            return parser(resp)
        except ParseError as error:
            error.html = resp.text
            # Require explicit JavaScript markers; broken selectors alone are not evidence.
            soup = BeautifulSoup(resp.text, 'html.parser')
            has_content = soup.select_one(LIST_SCOPE_SELECTOR if kind == 'list' else DETAIL_WAIT_SELECTOR)
            needs_js = any(re.search(r'(?:enable|turn on|bật|kích hoạt)\s+javascript',
                                    tag.get_text(' ', strip=True), re.I)
                           for tag in soup.select('noscript'))
            if (self.mode != 'auto' or self.browser is None or has_content or not needs_js):
                raise
        self.log('  The page explicitly requires JavaScript; try browser loading once.')
        resp = self.fetch(url, kind, force_browser=True)
        try:
            return parser(resp)
        except ParseError as error:
            error.html = resp.text
            raise

    def close(self):
        try:
            self.http.close()
        finally:
            if self.browser is not None:
                self.browser.close()



def run(store, crawler, max_pages=100, max_items=100, keep_contact_name=True, log=print):
    """Return (stop reason, invocation statistics). Limits include all completed work in this run; 0 means unlimited."""
    stats = {'pages': 0, 'found': 0, 'ok': 0, 'skipped': 0, 'errors': 0}

    def progress():
        log(f'  [pages read {stats["pages"]} | new URLs {stats["found"]} | saved {stats["ok"]} '
            f'| skipped {stats["skipped"]} | errors {stats["errors"]} | pending {store.count("pending")}]')

    def handle_list(page):
        url = page['url']
        log(f'[list] {url}')
        try:
            result = crawler.fetch_parse(url, 'list', lambda r: parse_list_page(r.text, r.url))
        except (BlockedError, RateLimitedError, RobotsDisallowedError) as error:
            raise StopRun(f'{error}. The list page remains pending.')
        except StructureError as error:
            raise StopRun(f'Unexpected list-page structure: {error}')
        except GoneError as error:
            raise StopRun(f'The list page is unavailable ({error}); keep it pending for inspection/resume.')
        except (TransientError, HttpError, ParseError) as error:
            raise StopRun(f'Could not read the list page ({error}); resume this run later.')
        new, repeated = store.record_list_page(url, result.detail_urls, result.next_url)
        stats['pages'] += 1
        stats['found'] += new
        if repeated:
            log('  Repeated page content: stop pagination.')
        elif not result.detail_urls:
            log('  The listing container explicitly reports no results: end of data.')
        elif not result.next_url:
            log('  No next-page link: this is the last page.')
        progress()

    def handle_detail(item):
        url = item['url']
        log(f'[property] {url}')
        try:
            row = crawler.fetch_parse(url, 'detail', lambda r: parse_detail_page(r.text, r.url))
        except GoneError:
            store.mark(url, 'gone', 'HTTP 404/410: listing unavailable')
            stats['skipped'] += 1
        except RobotsDisallowedError:
            store.mark(url, 'skipped', 'robots.txt disallows this URL')
            stats['skipped'] += 1
        except (BlockedError, RateLimitedError) as error:
            raise StopRun(f'{error}. This property remains pending.')
        except (ParseError, TransientError, HttpError) as error:
            store.mark(url, 'failed', f'{type(error).__name__}: {error}')
            stats['errors'] += 1
            log(f'  Error: {error}')
        else:
            row['Scraped_At'] = utc_now()
            row['Source_List_URL'] = item['source_list_url']
            if not keep_contact_name:
                row['Contact_Name'] = ''
            if store.save_listing(item, row):
                stats['ok'] += 1
            else:
                issue = next((x for x in store.issues() if x['url'] == url), None)
                if issue is not None and issue['status'] == 'failed':
                    stats['errors'] += 1
                    log(f'  Could not save property: {issue["error"]}')
                else:
                    stats['skipped'] += 1
                # Store already changed the status/attempt count; do not mark it twice.
        progress()

    try:
        while True:
            if max_items and store.count('done') >= max_items:
                return 'Reached the property limit.', stats
            item = store.next_pending()
            if item is not None:                       # process known properties before discovering another page
                handle_detail(item)
                continue
            if max_pages and store.pages_done() >= max_pages:
                return 'Reached the list-page limit.', stats
            page = store.next_list_page()
            if page is None:
                return 'No more list pages.', stats
            handle_list(page)
    except StopRun as stop:
        return f'STOP: {stop}', stats


def nonneg_int(text):
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError('must be >= 0')
    return value


def delay_value(text):
    import math
    value = float(text)
    if not math.isfinite(value) or value < MIN_DELAY:
        raise argparse.ArgumentTypeError(f'the delay must be finite and >= {MIN_DELAY:g} seconds')
    return value


def run_id_value(text):
    import uuid
    try:
        return str(uuid.UUID(text))
    except ValueError:
        raise argparse.ArgumentTypeError('run ID must be a UUID printed by a previous run') from None


def build_parser():
    parser = argparse.ArgumentParser(
        description='Collect property listings directly into SQL Server, with resumable server-side progress.',
        epilog='No local dataset files are created. A plain invocation starts a new run. Resume by its printed UUID.')
    parser.add_argument('--server', default=DEFAULT_SERVER, help='SQL Server instance (default: %(default)s)')
    parser.add_argument('--database', default=DEFAULT_DATABASE, help='existing SQL Server database (default: %(default)s)')
    parser.add_argument('--driver', default=DEFAULT_DRIVER, help='installed ODBC driver (default: %(default)s)')
    parser.add_argument('--list-url', help=f'results-page URL (new-run default: {DEFAULT_LIST_URL})')
    parser.add_argument('--max-pages', type=nonneg_int, default=10,
                        help='total completed list pages in this run, including resumed work (0 = unlimited; default: 2)')
    parser.add_argument('--max-items', type=nonneg_int, default=100,
                        help='total successful properties in this run, including resumed work (0 = unlimited; default: 20)')
    parser.add_argument('--delay', type=delay_value, default=MIN_DELAY, help='seconds between HTML request starts; minimum 10')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--new-run', action='store_true', help='start a separate SQL Server run (also the default)')
    mode.add_argument('--resume', type=run_id_value, metavar='RUN_ID', help='resume the saved SQL Server run with this UUID')
    mode.add_argument('--runs', action='store_true', help='show the latest 20 runs without accessing the website')
    mode.add_argument('--check-db', action='store_true', help='connect and prepare/check the schema without accessing the website')
    mode.add_argument( '--refresh-url',type=valid_url,metavar='URL',help='fetch one property directly and update or insert its SQL record')
    parser.add_argument('--fetcher', choices=('http', 'auto', 'playwright'), default='playwright',
                        help='browser loading (default), HTTP, or explicit-JavaScript browser fallback')
    parser.add_argument('--browser-channel', choices=('chromium', 'msedge', 'chrome'), default='msedge',
                        help='browser channel (default: installed Microsoft Edge)')
    parser.add_argument('--no-contact-name', action='store_true', help='omit contact names from newly collected properties')
    analytics = parser.add_mutually_exclusive_group()
    analytics.add_argument('--skip-analytics-bootstrap', '--experimental-skip-gtm-bootstrap',
                           dest='skip_analytics_bootstrap', action='store_true', default=True,
                           help='skip the exact optional first-party analytics bootstrap script (default)')
    analytics.add_argument('--load-analytics-bootstrap', dest='skip_analytics_bootstrap', action='store_false',
                           help='load that analytics script for diagnostics; its errors may stop collection')
    return parser





def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    args = build_parser().parse_args(argv)
    if args.list_url:
        try:
            valid_list_url(args.list_url)
        except ValueError as error:
            print(error)
            return 2
    store = http = crawler = None
    code = 0
    reason = None
    try:
        administrative = args.runs or args.check_db or bool(args.refresh_url)
        list_url = args.list_url or (None if args.resume or administrative else DEFAULT_LIST_URL)
        store = SqlServerStore(server=args.server, database=args.database, driver=args.driver,
                               run_id=args.resume, list_url=list_url, create_run=not administrative)
        if args.check_db:
            print(f'SQL Server ready: {args.server} / {args.database}. Schema checked; no crawl started.')
        elif args.runs:
            for saved in store.runs():
                print(f'{saved["run_id"]} | {saved["created_at"]} | saved={saved["done"]} | '
                      f'{saved["list_url"]} | {saved["last_stop_reason"] or "not recorded"}')
        elif args.refresh_url:
            http = HttpFetcher()
            crawler = Crawler(http, Throttle(args.delay), mode=args.fetcher)

            if args.fetcher != 'http':
                crawler.browser = BrowserFetcher(crawler.robots_allows)
                crawler.browser.channel = args.browser_channel
                crawler.browser.skip_analytics_bootstrap = args.skip_analytics_bootstrap
                crawler.browser.log = crawler.log

            print(f'Refreshing: {args.refresh_url}')

            row = refresh_property(
                args.refresh_url, crawler, store, not args.no_contact_name
            )

            print(
                f"Saved Post_ID={row['Post_ID']} | "
                f"Price_Text={row['Price_Text']} | "
                f"Price_VND={row['Price_VND']} | "
                f"Price_Per_M2={row['Price_Per_M2']}"
            )
        else:
            list_url = store.get_meta('list_url')
            valid_list_url(list_url)
            print(f'Run ID: {store.run_id}\nSQL Server: {args.server} / {args.database}\nList URL: {list_url}')
            resume_args = ['--resume', store.run_id, '--server', args.server, '--database', args.database,
                           '--driver', args.driver, '--max-pages', str(args.max_pages), '--max-items', str(args.max_items),
                           '--delay', str(args.delay), '--fetcher', args.fetcher, '--browser-channel', args.browser_channel]
            if args.no_contact_name:
                resume_args.append('--no-contact-name')
            resume_args.append('--skip-analytics-bootstrap' if args.skip_analytics_bootstrap else '--load-analytics-bootstrap')
            resume_values = [sys.executable, str(Path(__file__).resolve()), *resume_args]
            resume_command = '& ' + ' '.join("'" + value.replace("'", "''") + "'" for value in resume_values)
            print(f'Resume in PowerShell from any folder: {resume_command}')
            if 'da-nang' not in list_url:
                print('The URL does not contain da-nang; collection stays within the selected results scope.')
            print(f'Delay >= {args.delay:g}s per HTML request. Limits count all completed work in this run.')
            print('Results and progress are committed directly to SQL Server. No local dataset files are written.')
            if args.resume:
                print(f'Requeued {store.reset_failed()} eligible failed properties.')
            throttle = Throttle(args.delay)
            http = HttpFetcher()
            crawler = Crawler(http, throttle, mode=args.fetcher)
            if args.fetcher != 'http':
                crawler.browser = BrowserFetcher(crawler.robots_allows)
                crawler.browser.channel = args.browser_channel
                crawler.browser.skip_analytics_bootstrap = args.skip_analytics_bootstrap
                crawler.browser.log = crawler.log
            reason, stats = run(store, crawler, args.max_pages, args.max_items, not args.no_contact_name)
            print(f'\n{reason}\nThis invocation: {stats}; total saved in this run: {store.count("done")}')
            code = 3 if reason.startswith('STOP:') else 0
    except KeyboardInterrupt:
        reason = 'Interrupted with Ctrl+C.'
        retry = 'Retry the same --refresh-url command.' if args.refresh_url else 'Resume using the run ID printed above.'
        print(f'Interrupted. Committed properties remain in SQL Server. {retry}')
        code = 130
    except ValueError as error:
        reason = f'STOP: {error}'
        print(reason)
        code = 2
    except Exception as error:
        reason = f'STOP: {type(error).__name__}: {error}'
        retry = 'Retry the same --refresh-url command.' if args.refresh_url else 'Retry using the same run ID.'
        print(f'{reason}\nPreviously committed work remains in SQL Server. {retry}')
        code = 3
    finally:
        resource = crawler if crawler is not None else http
        if resource is not None:
            try:
                resource.close()
            except (Exception, KeyboardInterrupt) as error:
                print(f'Crawler cleanup failed: {type(error).__name__}: {error}')
                if code == 0:
                    code = 3
        if store is not None:
            try:
                if store.run_id is not None and reason is not None:
                    store.finish(reason)
            except (Exception, KeyboardInterrupt) as error:
                print(f'Could not record the stop reason: {error}')
                if code == 0:
                    code = 3
            finally:
                try:
                    store.close()
                except (Exception, KeyboardInterrupt) as error:
                    print(f'SQL Server cleanup failed: {type(error).__name__}: {error}')
                    if code == 0:
                        code = 3
    return code

def refresh_property(url, crawler, store, keep_contact_name=True):
    valid_url(url)
    match = PRODUCT_PATH_RE.search(urlsplit(url).path)

    if match is None:
        raise ValueError('Enter a property URL ending in -pr followed by its Post_ID.')

    expected_post_id = match[1]

    row = crawler.fetch_parse(
        url, 'detail',
        lambda response: parse_detail_page(response.text, response.url)
    )

    if str(row.get('Post_ID')) != expected_post_id:
        raise ParseError('The fetched property does not match the requested Post_ID.')

    row['Scraped_At'] = utc_now()
    row['Source_List_URL'] = None

    if not keep_contact_name:
        row['Contact_Name'] = ''

    store.save_refreshed_property(row, expected_post_id)
    return row

def refresh_unit_price_properties(candidates, crawler, store):
    for property in candidates:
        try:
            print(f"Refreshing Post_ID={property['Post_ID']}")

            refresh_property(
                property["URL"], crawler, store
            )

        except (BlockedError, RateLimitedError):
            raise

        except Exception as error:
            print(f"Could not refresh {property['Post_ID']}: {error}")
if __name__ == '__main__':
    sys.exit(main())
