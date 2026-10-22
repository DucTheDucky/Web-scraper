"""Thu thập tin bán BĐS Đà Nẵng từ batdongsan.com.vn (dự án ADY).

Chỉ nhập MỘT URL trang danh sách; chương trình tự lấy URL tin, tự theo phân trang,
đọc chi tiết, lưu tiến độ vào SQLite và xuất CSV. Chạy tuần tự, mỗi request HTML
cách nhau tối thiểu 10 giây, dừng ngay khi gặp 401/403/CAPTCHA.

Cài đặt (Windows, PowerShell):
    python -m pip install -r requirements_danang.txt
    (chỉ khi dùng --fetcher playwright/auto)  python -m playwright install chromium

Ví dụ chạy:
    python bds_danang.py                                  # mặc định: 2 trang, tối đa 20 tin
    python bds_danang.py --max-pages 5 --max-items 100
    python bds_danang.py --resume                         # tiếp tục lượt trước sau khi bị ngắt
    python bds_danang.py --new-run                        # lượt mới, file riêng có dấu thời gian
    python bds_danang.py --export-only                    # chỉ xuất lại CSV từ dữ liệu đã lưu

Lưu ý pháp lý/đạo đức: robots.txt chỉ là quy tắc truy cập của crawler, KHÔNG phải giấy phép
sao chép dữ liệu. Điều khoản của website hạn chế sao chép/phân phối/khai thác thương mại nội
dung và yêu cầu ghi nguồn. Chỉ dùng dữ liệu cho học tập/nội bộ, giữ cột URL làm nguồn.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
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

USER_AGENT = 'BDSResearchBot/1.0'
HOSTS = ('batdongsan.com.vn', 'www.batdongsan.com.vn')
DEFAULT_LIST_URL = 'https://batdongsan.com.vn/nha-dat-ban-da-nang'
MIN_DELAY = 10.0          # giây; tự đặt của chương trình, không phải quy định của website
MAX_REDIRECTS = 3
BASE_DIR = Path(__file__).resolve().parent

# ---- Đã xác minh 2026-10-12 trên một HTML danh sách HTTP 200; xem TASK1_NOTES.md. ----
LIST_SCOPE_SELECTOR = '#product-lists-web'   # vùng chứa các thẻ tin
CARD_SELECTOR = '.js__card'                                       # từng thẻ tin trong vùng đó
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
# 1. Lỗi có phân loại: chặn / mạng / parse / dữ liệu thiếu
# ======================================================================================
class CrawlError(Exception):
    pass


class BlockedError(CrawlError):
    """401/403 hoặc CAPTCHA/trang kiểm tra bot: dừng lượt chạy, không vượt chặn."""


class RateLimitedError(CrawlError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class GoneError(CrawlError):
    """HTTP 404/410: tin không còn tồn tại."""


class TransientError(CrawlError):
    """Timeout, lỗi mạng, 5xx: có thể thử lại hữu hạn."""


class HttpError(CrawlError):
    """Các mã 4xx khác: không thử lại."""


class RobotsDisallowedError(CrawlError):
    pass


class ParseError(ValueError):
    """HTML tải được nhưng không đọc được dữ liệu cần thiết."""


class StructureError(ParseError):
    """Trang danh sách không có vùng tin như mong đợi (selector đổi hoặc bị chặn mềm)."""


class StopRun(Exception):
    """Dừng cả lượt chạy (tiến độ đã được lưu)."""


# ======================================================================================
# 2. Hàm làm sạch và chuyển đổi dữ liệu (giữ từ code cũ, có sửa lỗi)
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
    """Chỉ nhận giá chính xác, đơn vị rõ; kỳ thuê giữ riêng, không quy đổi thành giá bán."""
    value = clean(value).lower()
    # Bỏ đúng các hậu tố đơn vị đã hỗ trợ, không bỏ chữ/từ ước lượng tùy ý.
    suffix = r'(?:/\s*m[²2]|(?:/|mỗi)\s*(?:tháng|năm|ngày|tuần))'
    match = re.fullmatch(r'(.*?)\s*((?:' + suffix + r'\s*)*)', value)
    body = match[1].strip() if match else value
    tail = match[2] if match else ''
    if len(re.findall(r'm[²2]', tail)) > 1 or len(re.findall(r'tháng|năm|ngày|tuần', tail)) > 1:
        return None
    term = r'([0-9]+(?:[.,][0-9]+)*)\s*(tỷ|tỉ|triệu|tr|nghìn|ngàn|vnd|vnđ|đồng|đ)(?!\w)'
    matches = list(re.finditer(term, body))
    if not matches or re.sub(term, '', body).strip():
        return None  # khoảng, từ, gần, ~, phủ định, tiền tệ lạ... không phải giá chính xác
    multipliers = [UNIT_VALUE.get(m[2], 1) for m in matches]
    if multipliers != sorted(set(multipliers), reverse=True):
        return None
    total = Decimal(0)
    for match, multiplier in zip(matches, multipliers):
        if multiplier > 1 and re.fullmatch(r'[0-9]{1,3}\.[0-9]{3}', match[1]):
            return None  # 3.950 tỷ có thể là 3,95 hoặc 3950 tỷ; không đoán
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


# "Không qua môi giới", "miễn môi giới"... là phủ định, không được tính là môi giới.
NEGATED_BROKER = re.compile(
    r'\b(?:không|ko|miễn|chưa)\b\s+(?:(?:phải|là|làm|qua|tiếp|nhận|cần|rõ)\s+){0,3}'
    r'(?:môi giới|trung gian|chính chủ|chủ ký gửi|chủ gửi)')


def parse_detail_page(html_content, page_url):
    soup = BeautifulSoup(html_content, 'html.parser')
    root = soup.select_one('.js__product-detail-web, .re__pr-info, #product-detail-web')
    if root is None or not root.select_one('h1.re__pr-title'):
        raise ParseError('Không thấy chi tiết tin; trang bị chặn hoặc cấu trúc HTML đã đổi.')

    # Gom các thuộc tính theo nhãn để không phụ thuộc thứ tự hiển thị.
    info = {}
    unit_price_text = ''
    price_labels = ('mức giá', 'khoảng giá', 'giá')
    for item in root.select('.re__pr-short-info-item, .re__pr-specs-content-item'):
        label = get_text(item, '.title, .re__pr-specs-content-item-title').lower()
        value = get_text(item, '.value, .re__pr-specs-content-item-value')
        if label and value:
            info[label] = value
        if label in price_labels:
            extra = get_text(item, '.ext')
            if re.search(r'/\s*m[²2]', extra, re.I):
                unit_price_text = extra

    title = get_text(root, 'h1.re__pr-title')
    price_text = next((info[key] for key in price_labels if key in info), '')
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
    # Tách địa chỉ đầu tiên, không trộn với địa giới thay thế trong ngoặc (Address giữ nguyên văn).
    parts = [clean(part) for part in re.sub(r'\([^)]*\)', '', address).split(',')]
    parts = [part for part in parts if part]
    ward = next((x for x in parts if re.match(r'^(xã|phường|thị trấn)\b', x, re.I)), '')
    district = next((x for x in parts if re.match(r'^(quận|huyện|thị xã)\b', x, re.I)), '')
    last = parts[-1] if parts else ''
    province = last if re.fullmatch(
        r'(?:tỉnh\s+\S.*|(?:(?:thành phố|tp[.]?)\s+)?đà nẵng)', last, re.I) else ''
    # Không suy tỉnh từ thành phần cuối tùy ý hoặc ánh xạ địa giới cũ/mới.

    post_id = info.get('mã tin') or root.get('prid', '')
    if not post_id:
        match = PRODUCT_PATH_RE.search(urlsplit(page_url).path)
        post_id = match[1] if match else ''

    # Cờ xác thực của đúng tin; không thấy cờ thì để trống, không gán False.
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
    seller_type = 'Không rõ'
    if owner and broker:
        seller_type = 'Không rõ (từ khóa mâu thuẫn)'
    elif owner:
        seller_type = 'Chính chủ (tự khai)'
    elif broker:
        seller_type = 'Môi giới (suy luận)'

    contact = soup.select_one('.re__contact-name, .js_contact-name')
    contact_name = clean(contact.get('title') or contact.get_text(' ', strip=True)) if contact else ''
    if not contact_name:
        # Dự phòng đơn giản cho mô tả có dạng "Liên hệ: Nguyễn Văn A" (loại bỏ chữ số nên không lấy SĐT).
        match = re.search(r'(?:liên hệ|lh)\s*[:：]\s*([^\d,;.\n]{2,50})', description, re.I)
        contact_name = clean(match[1]) if match else ''

    # Không xuất bản ghi rỗng như một tin thành công.
    if not title or not post_id:
        raise ParseError('Thiếu tiêu đề hoặc Post_ID.')
    if not (price_text or area or address):
        raise ParseError('Không đọc được giá, diện tích hay địa chỉ.')

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
        'Price_Period': period[1] if period else '',
        'Corner_Keywords': '; '.join(dict.fromkeys(corner_keywords)),
        'Seller_Keywords': '; '.join(dict.fromkeys(seller_keywords)),
    }


# ======================================================================================
# 3. Trang danh sách: lấy URL tin và liên kết trang kế tiếp
# ======================================================================================
def valid_url(url):
    try:
        p = urlsplit(url)
        if (p.scheme != 'https' or p.hostname not in HOSTS
                or p.username or p.password or p.port not in (None, 443)):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('Chỉ nhận URL HTTPS của batdongsan.com.vn.') from None
    return url


def valid_list_url(url):
    valid_url(url)
    if PRODUCT_PATH_RE.search(urlsplit(url).path) or not transaction_prefix(url):
        raise ValueError('Hãy nhập URL danh sách bán hoặc cho thuê bất động sản.')
    page_number(url)  # từ chối phân trang không rõ nghĩa
    return url


def page_number(url):
    from urllib.parse import parse_qsl
    p = urlsplit(url)
    match = re.search(r'/p(\d+)/?$', p.path)
    values = [v for k, v in parse_qsl(p.query, keep_blank_values=True) if k == 'page']
    if (len(values) > 1 or (values and not re.fullmatch(r'[1-9][0-9]*', values[0]))):
        raise ValueError('Tham số page không hợp lệ.')
    number = int(match[1]) if match else int(values[0]) if values else 1
    if number < 1 or (match and values and int(values[0]) != number):
        raise ValueError('Số trang không hợp lệ hoặc mâu thuẫn.')
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
    # Chỉ vùng chính đã xác minh; không gom vùng gợi ý vào danh sách.
    scopes = soup.select(LIST_SCOPE_SELECTOR)
    if len(scopes) != 1:
        raise StructureError('Không xác định được duy nhất vùng danh sách tin.')
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
        # Chỉ công nhận thông báo rỗng rõ ràng trong vùng danh sách, không suy từ số trang.
        empty = any(LIST_EMPTY_TEXT.fullmatch(clean(node.get_text(' ', strip=True)))
                    for node in [scope] + scope.select('p, div, span, h1, h2, h3'))
        if cards or not empty:
            raise StructureError('Không có URL tin hợp lệ; cần kiểm tra selector/HTML, không coi là hết dữ liệu.')
        if find_next_page(soup, page_url):
            raise StructureError('Thông báo rỗng mâu thuẫn với liên kết trang tiếp theo.')
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
        return sorted(found)[0]  # URL thực tế; không tự ghép lại bộ lọc bị mất
    if unsafe:
        raise StructureError('Liên kết trang kế tiếp làm mất/thay đổi query bộ lọc.')
    return None


# ======================================================================================
# 4. Bộ điều tiết tốc độ (dùng chung cho mọi request HTML) và các bộ tải trang
# ======================================================================================
class Throttle:
    """Điều tiết tuần tự theo thời điểm bắt đầu, kể cả sleep thức dậy sớm."""

    def __init__(self, min_interval=MIN_DELAY, clock=time.monotonic, sleep=time.sleep):
        import math
        if not math.isfinite(min_interval) or min_interval < MIN_DELAY:
            raise ValueError(f'Khoảng chờ phải hữu hạn và >= {MIN_DELAY:g} giây.')
        self.min_interval, self.clock, self.sleep = min_interval, clock, sleep
        self.last_start = None
        self.not_before = None

    def raise_interval(self, seconds):
        import math
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('Khoảng chờ phải hữu hạn, không âm.')
        self.min_interval = max(self.min_interval, seconds)

    def pause(self, seconds):
        import math
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('Khoảng chờ retry phải hữu hạn, không âm.')
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
    """Một request duy nhất; redirect/retry do Crawler điều tiết."""

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
                reply = error  # cũng phải đóng stream của response lỗi
            with reply:
                headers = {k.lower(): v for k, v in reply.headers.items()}
                return Response(reply.code, url, decode_body(reply.read(), headers), headers)
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            raise TransientError(f'Lỗi mạng/timeout: {error}') from error

    def close(self):
        pass


CHALLENGE_TITLE = re.compile(
    r'captcha|just a moment|access denied|verify you are human|robot check|attention required|checking your browser', re.I)
CHALLENGE_MARKERS = re.compile(r'cf-turnstile|g-recaptcha|h-captcha|challenge-platform|/cdn-cgi/challenge', re.I)


def _check_blocked(resp):
    # Do not expose query/session identifiers in diagnostic messages.
    p = urlsplit(resp.url)
    safe_url = urlunsplit((p.scheme, p.netloc, p.path, '', ''))
    if resp.status in (401, 403):
        raise BlockedError(f'HTTP {resp.status} tại {safe_url}')
    if resp.headers.get('cf-mitigated', '').lower() == 'challenge':
        raise BlockedError(f'cf-mitigated: challenge tại {safe_url}')
    soup = BeautifulSoup(resp.text, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    # Actual challenge UI/title is evidence even if a cached listing shell exists.
    # Script URLs and inline challenge-platform/precursor references alone are not.
    if CHALLENGE_TITLE.search(title) or soup.select_one(
            '#challenge-form, .g-recaptcha, .h-captcha, .cf-turnstile, iframe[src*="captcha"]'):
        raise BlockedError(f'Trang kiểm tra truy cập/CAPTCHA tại {safe_url}')


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
        self.experimental_skip_gtm_bootstrap = False
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

    def _skip_experimental_bootstrap(self, url, resource_type):
        # Opt-in dependency experiment, not a claim about every GTM tag.
        if (self.experimental_skip_gtm_bootstrap and resource_type == 'script'
                and url == 'https://batdongsan.com.vn/gtganalytics/'):
            self._diagnostic('experimental_bootstrap_skipped', url, resource_type, None)
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
                        self.log(f'Route thuộc navigation đã đóng: {error}')
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
            return HttpError(f'Lỗi kết thúc phiên interception Chromium: {first_error}')
        return None

    def _route(self, route):
        request = route.request
        try:
            if not self._active or self._error is not None:
                self._end_route(route, True); return
            valid_url(request.url)
            if self._skip_experimental_bootstrap(request.url, request.resource_type):
                self._end_route(route, True); return
            if self._optional_telemetry(request.url, request.resource_type):
                self._diagnostic('optional_telemetry_skipped', request.url, request.resource_type, None)
                self._end_route(route, True); return
            main = request.resource_type == 'document'
            if main:
                if (request.frame != self.page.main_frame or request.url != self._expected
                        or self._document_sent):
                    raise HttpError('Browser chặn iframe/điều hướng ngoài navigation đã kiểm tra.')
                if not self.robots_allows(request.url):
                    raise RobotsDisallowedError(f'robots.txt không cho phép {request.url}')
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
            failure = error if isinstance(error, CrawlError) else HttpError(f'Lỗi route browser: {error}')
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
                raise HttpError('Redirect tài nguyên có status không hỗ trợ.')
            if sum(h['name'].lower() == 'location' for h in event.get('responseHeaders', [])) > 1:
                raise HttpError('Redirect tài nguyên có nhiều Location mâu thuẫn.')
            location = headers.get('location', '').strip()
            if '\\' in location or any(ord(c) < 32 for c in location):
                raise HttpError('Location chứa ký tự không an toàn/khác cách phân tích URL của Chromium.')
            if not location:
                raise HttpError('Redirect tài nguyên thiếu Location.')
            target = urljoin(url, location).split('#')[0]
            try:
                valid_url(target)
            except ValueError as error:
                raise HttpError('Đích redirect tài nguyên ngoài domain/HTTPS/URL cho phép.') from error
            network_id = event.get('networkId')
            if not network_id:
                raise HttpError('Thiếu networkId để kiểm soát chuỗi redirect tài nguyên.')
            chain = self._subresource_redirects.setdefault(network_id,
                        {'seen': {url}, 'expected': url, 'hops': 0})
            if url != chain['expected']:
                raise HttpError('Chuỗi redirect tài nguyên không khớp URL đã duyệt.')
            if target in chain['seen']:
                raise HttpError('Vòng lặp redirect tài nguyên.')
            if chain['hops'] >= MAX_REDIRECTS:
                raise HttpError('Redirect tài nguyên vượt giới hạn.')
            if self._optional_telemetry(target, kind):
                self._diagnostic('optional_telemetry_skipped', target, kind, None)
                self._finish_interception('Fetch.failRequest', {'requestId': event['requestId'], 'errorReason': 'Aborted'})
                self._subresource_redirects.pop(network_id, None)
                return
            if not self.robots_allows(target):
                raise RobotsDisallowedError('robots chưa cho phép đích redirect tài nguyên (cache theo origin).')
            if self._skip_experimental_bootstrap(target, kind):
                self._finish_interception('Fetch.failRequest', {'requestId': event['requestId'], 'errorReason': 'Aborted'})
                self._subresource_redirects.pop(network_id, None)
                return
            if re.search(r'/cdn-cgi/challenge|captcha', urlsplit(target).path, re.I):
                raise HttpError('Không tải challenge helper qua redirect tài nguyên.')
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
            if not status:
                if main:
                    raise HttpError('Chromium không nhận được HTTP response.')
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
            failure = error if isinstance(error, CrawlError) else HttpError(f'Lỗi kiểm tra response Chromium: {error}')
            reported = self._failure(failure, url, event.get('resourceType', 'unknown').lower(), status or None)
            if self._error is None:
                self._error = reported
            try:
                self._finish_interception('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'})
            except Exception as cleanup_error:
                self._failure(HttpError(f'Lỗi kết thúc interception: {cleanup_error}'),
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
                raise HttpError('BrowserFetcher phải được cấu hình qua Crawler.')
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
                raise HttpError('Browser không nhận được main document.')
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
            raise HttpError(f'Browser không sẵn sàng/thực thi thất bại: {error}') from error
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
        # Giá trị lớn trả infinity để Crawler dừng theo MAX_WAIT, không đưa vào sleep.
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
    """Chọn đúng product-token, gộp nhóm trùng; rule dài nhất, Allow thắng khi hòa.

    Không dùng first-match của urllib.robotparser. Crawl-delay/Request-rate là
    extension: lấy khoảng chờ lớn nhất trong các nhóm đã chọn.
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
                raise HttpError(f'robots.txt có {key} không hợp lệ; dừng để kiểm tra.') from None
        return max(intervals, default=None)

    def request_rate(self, useragent):
        return None  # đã hợp nhất vào crawl_delay


class Crawler:
    """Cache robots theo origin; redirect, retry và browser dùng chung Throttle."""
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
        return 'https://' + urlsplit(url).hostname  # :443 và dạng viết hoa cùng origin

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
            raise HttpError('Chưa cấu hình BrowserFetcher.')
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
                raise BlockedError('robots.txt trả HTML; dừng để kiểm tra.')
            parser.parse(resp.text.splitlines())
        extra = parser.crawl_delay(USER_AGENT) or 0
        if extra > self.MAX_WAIT:
            raise HttpError('robots.txt yêu cầu khoảng chờ vượt MAX_WAIT; dừng.')
        self.throttle.raise_interval(extra)
        # Chính sách của response redirect chỉ lưu cho origin yêu cầu ban đầu.
        self.robots[origin] = parser

    def _require_allowed(self, url):
        origin = self._origin(url)
        self._load_robots(origin)
        if not self.robots[origin].can_fetch(USER_AGENT, url):
            raise RobotsDisallowedError(f'robots.txt không cho phép {url}')

    @staticmethod
    def _redirect_target(source, target):
        from urllib.parse import parse_qsl
        try:
            valid_url(target)
        except ValueError as error:
            raise HttpError(f'Redirect không hợp lệ: {target}') from error
        p = urlsplit(source)
        if transaction_prefix(source) and not PRODUCT_PATH_RE.search(p.path):
            # Redirect của danh sách không được đổi bộ lọc, loại tin hoặc số trang.
            q = urlsplit(target)
            if (page_base(source) != page_base(target)
                    or page_number(source) != page_number(target)
                    or sorted(parse_qsl(p.query, keep_blank_values=True)) !=
                       sorted(parse_qsl(q.query, keep_blank_values=True))):
                raise HttpError('Redirect làm thay đổi phạm vi/bộ lọc trang danh sách.')
        return target

    def _follow(self, url, fetcher, check_robots=True):
        seen = set()
        for step in range(MAX_REDIRECTS + 1):
            try:
                valid_url(url)
            except ValueError as error:
                raise HttpError(f'URL tải không hợp lệ: {url}') from error
            if url in seen:
                raise HttpError('Vòng lặp redirect; dừng, không retry.')
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
                    raise HttpError('Redirect thiếu Location hoặc vượt giới hạn; dừng.')
                target = urljoin(url, location).split('#')[0]
                try:
                    url = self._redirect_target(url, target)
                except ValueError as error:
                    raise HttpError('Redirect có tham số URL không hợp lệ.') from error
                continue
            return resp
        raise HttpError('Quá nhiều redirect.')

    def _classify(self, resp):
        _check_blocked(resp)
        status = resp.status
        if status == 429 or (status == 503 and 'retry-after' in resp.headers):
            raise RateLimitedError(f'HTTP {status} tại {resp.url}', parse_retry_after(resp.headers.get('retry-after')))
        if status in (404, 410):
            raise GoneError(f'HTTP {status}')
        if status >= 500:
            raise TransientError(f'HTTP {status}')
        if status >= 400:
            raise HttpError(f'HTTP {status}')
        if status != 200:
            raise HttpError(f'HTTP {status} không mong đợi')

    def _fetch_once(self, url, kind, force_browser):
        fetcher = self.http
        if force_browser or self.mode == 'playwright':
            if self.browser is None:
                raise HttpError('Chưa cấu hình BrowserFetcher.')
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
                self.log(f'  {error}: chờ {wait:.0f}s rồi thử lại ({rate_tries}/{self.MAX_RATE_RETRIES}).')
                self.throttle.pause(wait)
            except TransientError as error:
                transient_tries += 1
                if transient_tries > self.MAX_TRANSIENT_RETRIES:
                    raise
                wait = self.TRANSIENT_BACKOFF * 2 ** (transient_tries - 1)
                self.log(f'  {error}; thử lại sau {wait:.0f}s ({transient_tries}/{self.MAX_TRANSIENT_RETRIES}).')
                self.throttle.pause(wait)

    def fetch_parse(self, url, kind, parser):
        resp = self.fetch(url, kind)
        try:
            return parser(resp)
        except ParseError as error:
            error.html = resp.text
            # Chỉ marker cần JS rõ ràng; selector hỏng không phải bằng chứng cần JS.
            soup = BeautifulSoup(resp.text, 'html.parser')
            has_content = soup.select_one(LIST_SCOPE_SELECTOR if kind == 'list' else DETAIL_WAIT_SELECTOR)
            needs_js = any(re.search(r'(?:enable|turn on|bật|kích hoạt)\s+javascript',
                                    tag.get_text(' ', strip=True), re.I)
                           for tag in soup.select('noscript'))
            if (self.mode != 'auto' or self.browser is None or has_content or not needs_js):
                raise
        self.log('  Trang yêu cầu JavaScript rõ ràng; thử browser hạn chế một lần.')
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


# ======================================================================================
# 5. Lưu trữ SQLite: hàng đợi, trang danh sách, bản ghi (nguồn sự thật để tiếp tục & chống trùng)
# ======================================================================================
def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Store:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS list_pages(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE, status TEXT, signature TEXT);
    CREATE TABLE IF NOT EXISTS queue(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE, post_id TEXT UNIQUE,
        source_list_url TEXT, status TEXT, attempts INTEGER DEFAULT 0, error TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS listings(
        post_id TEXT PRIMARY KEY, url TEXT, scraped_at TEXT, data TEXT);
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(self.SCHEMA)
        # Định danh bền vững của lượt chạy, không đổi schema/CLI.
        import uuid
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('csv_run_id',?)",
                            (uuid.uuid4().hex,))

    def close(self):
        self.db.close()

    def get_meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, value))

    # ---- trang danh sách ----
    def add_list_page(self, url):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO list_pages(url, status) VALUES(?, 'pending')", (url,))

    def next_list_page(self):
        return self.db.execute("SELECT * FROM list_pages WHERE status='pending' ORDER BY seq LIMIT 1").fetchone()

    def pages_done(self):
        return self.db.execute("SELECT COUNT(*) FROM list_pages WHERE status='done'").fetchone()[0]

    def record_list_page(self, page_url, detail_urls, next_url):
        """MỘT giao dịch: thêm URL tin vào hàng đợi + thêm trang kế + đánh dấu trang xong.
        Nếu bị ngắt giữa chừng thì toàn bộ không xảy ra và trang sẽ được đọc lại; không mất URL."""
        detail_urls = list(detail_urls)
        signature = hashlib.sha1('\n'.join(detail_urls).encode()).hexdigest()
        new = 0
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            page = self.db.execute('SELECT status FROM list_pages WHERE url=?', (page_url,)).fetchone()
            if page is not None and page['status'] == 'done':
                return 0, True  # replay sau commit không thay đổi trạng thái đã lưu
            repeated = bool(detail_urls) and self.db.execute(
                "SELECT 1 FROM list_pages WHERE status='done' AND signature=?", (signature,)).fetchone() is not None
            self.db.execute("INSERT OR IGNORE INTO list_pages(url,status) VALUES(?,'pending')", (page_url,))
            if not repeated:
                for url in detail_urls:
                    valid_url(url)
                    match = PRODUCT_PATH_RE.search(urlsplit(url).path)
                    if match is None:
                        raise ValueError(f'URL queue không có Post_ID: {url}')
                    cur = self.db.execute(
                        "INSERT OR IGNORE INTO queue(url, post_id, source_list_url, status, updated_at) "
                        "VALUES(?,?,?, 'pending', ?)", (url, match[1], page_url, utc_now()))
                    new += cur.rowcount
                if next_url:
                    self.db.execute("INSERT OR IGNORE INTO list_pages(url, status) VALUES(?, 'pending')", (next_url,))
            self.db.execute("UPDATE list_pages SET status='done', signature=? WHERE url=?", (signature, page_url))
        return new, repeated

    # ---- hàng đợi tin ----
    def next_pending(self):
        return self.db.execute("SELECT * FROM queue WHERE status='pending' ORDER BY seq LIMIT 1").fetchone()

    def mark(self, url, status, error=''):
        if status not in ('failed', 'gone', 'skipped', 'duplicate'):
            raise ValueError('Trạng thái không hợp lệ; done chỉ qua save_listing.')
        with self.db:
            self.db.execute("UPDATE queue SET status=?, error=?, attempts=attempts+?, updated_at=? "
                            "WHERE url=? AND status='pending'",
                            (status, error, 1 if status == 'failed' else 0, utc_now(), url))

    def save_listing(self, item, row):
        """Commit listing + queue cùng lúc. False gồm duplicate hoặc lỗi danh tính (xem issues)."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            queued = self.db.execute('SELECT * FROM queue WHERE url=?', (item['url'],)).fetchone()
            if queued is None:
                raise ValueError('URL không có trong queue; không lưu listing ngoài hàng đợi.')
            if queued['status'] != 'pending':
                return False  # callback/replay không đổi done thành duplicate
            expected = queued['post_id']
            actual = str(row.get('Post_ID', '')).strip()
            problem = ''
            try:
                valid_url(row.get('URL', ''))
                match = PRODUCT_PATH_RE.search(urlsplit(row['URL']).path)
            except (ValueError, TypeError, KeyError):
                match = None
            if str(item['post_id']) != expected or actual != expected or not match or match[1] != expected:
                problem = f'Post_ID không khớp queue={expected}, parser={actual}, URL={row.get("URL", "")}'
            elif not row.get('Scraped_At'):
                problem = 'Thiếu Scraped_At; không lưu listing chưa hoàn chỉnh.'
            elif self.db.execute('SELECT 1 FROM listings WHERE url=? AND post_id<>?',
                                 (row['URL'], expected)).fetchone():
                problem = 'URL đã gắn với Post_ID khác trong listings.'
            if problem:
                self.db.execute("UPDATE queue SET status='failed', error=?, attempts=attempts+1, updated_at=? "
                                "WHERE url=?", (problem, utc_now(), queued['url']))
                return False  # run hiện tại không bắt exception ở nhánh save_listing
            saved = dict(row, Post_ID=expected)
            cur = self.db.execute('INSERT OR IGNORE INTO listings(post_id,url,scraped_at,data) VALUES(?,?,?,?)',
                                  (expected, saved['URL'], saved['Scraped_At'], json.dumps(saved, ensure_ascii=False)))
            status = 'done' if cur.rowcount else 'duplicate'
            self.db.execute('UPDATE queue SET status=?, error=?, updated_at=? WHERE url=?',
                            (status, '' if cur.rowcount else 'Post_ID đã có', utc_now(), queued['url']))
        return bool(cur.rowcount)

    def reset_failed(self, max_attempts=3):
        if not isinstance(max_attempts, int) or max_attempts < 0:
            raise ValueError('max_attempts phải là số nguyên không âm.')
        with self.db:
            cur = self.db.execute("UPDATE queue SET status='pending', updated_at=? "
                                  "WHERE status='failed' AND attempts<?", (utc_now(), max_attempts))
        return cur.rowcount

    def count(self, status):
        return self.db.execute('SELECT COUNT(*) FROM queue WHERE status=?', (status,)).fetchone()[0]

    def total_found(self):
        return self.db.execute('SELECT COUNT(*) FROM queue').fetchone()[0]

    def listings(self):
        for row in self.db.execute('SELECT data FROM listings ORDER BY rowid'):
            yield json.loads(row[0])

    def issues(self):
        return self.db.execute("SELECT url, status, attempts, error, source_list_url FROM queue "
                               "WHERE status NOT IN ('done','pending') ORDER BY seq").fetchall()


# ======================================================================================
# 6. Xuất CSV
# ======================================================================================
def csv_value(value):
    # None vẫn là ô trống; numeric giữ nguyên, chỉ bảo vệ chuỗi.
    if isinstance(value, str) and (value.lstrip().startswith(('=', '+', '-', '@'))
                                   or value[:1] in ('\t', '\r', '\n')):
        return "'" + value
    return value


class _CsvRows(list):
    """Mang định danh Store tới writer mà không đổi chữ ký công khai."""
    def __init__(self, rows, run_id):
        super().__init__(rows)
        self.run_id = run_id


def write_csv_atomic(path, header, rows):
    import tempfile
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    owner_path = path.with_name(path.name + '.ady-owner.json')
    run_id = getattr(rows, 'run_id', None)
    temp = owner_temp = None

    def digest(target):
        if not target.exists():
            return None
        hasher = hashlib.sha256()
        with target.open('rb') as file:
            for chunk in iter(lambda: file.read(65536), b''):
                hasher.update(chunk)
        return hasher.hexdigest()

    # Writer dùng độc lập chỉ tạo mới. Header giống nhau không cấp quyền ghi đè.
    before = digest(path)
    owner = None
    if owner_path.exists():
        try:
            owner = json.loads(owner_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise FileExistsError(f'Không đọc được quyền sở hữu CSV: {owner_path}') from error
        if (not isinstance(owner, dict) or not run_id or owner.get('run_id') != run_id
                or owner.get('version') != 1):
            raise FileExistsError(f'{path} không thuộc cùng lượt chạy; chọn --output khác.')
    if before is not None:
        if owner is None or before not in (owner.get('sha256'), owner.get('pending_sha256')):
            raise FileExistsError(f'{path} chưa xác nhận cùng lượt chạy hoặc đã bị sửa; chọn --output khác.')
    try:
        with tempfile.NamedTemporaryFile(mode='w', newline='', encoding='utf-8-sig',
                                         prefix=path.name + '.', suffix='.tmp',
                                         dir=path.parent, delete=False) as file:
            temp = Path(file.name)
            writer = csv.writer(file)
            writer.writerow(header)
            for row in rows:
                writer.writerow([csv_value(value) for value in row])
            file.flush()
            os.fsync(file.fileno())
        after = digest(temp)
        if run_id:
            # Journal trước publish: cả hash cũ/mới hợp lệ nếu ngắt trước/sau replace.
            metadata = {'version': 1, 'run_id': run_id, 'sha256': before, 'pending_sha256': after}
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix=owner_path.name + '.',
                                             suffix='.tmp', dir=path.parent, delete=False) as file:
                owner_temp = Path(file.name)
                json.dump(metadata, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(owner_temp, owner_path)
            owner_temp = None
        if digest(path) != before:
            raise FileExistsError(f'{path} thay đổi trong khi xuất; không ghi đè.')
        os.replace(temp, path)
        temp = None
    finally:
        for temporary in (temp, owner_temp):
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # không che lỗi chính nếu file tạm bị khóa


def export_csv(store, output, log=print):
    success = True
    run_id = store.get_meta('csv_run_id')
    rows = _CsvRows([[csv_value(item.get(col)) for col in COLUMNS] for item in store.listings()], run_id)
    if rows:
        try:
            write_csv_atomic(output, COLUMNS, rows)
            log(f'CSV: {len(rows)} tin -> {Path(output).resolve()}')
        except OSError as error:
            success = False
            log(f'Không ghi được CSV ({error}). Dữ liệu vẫn an toàn trong {store.path}; dùng --export-only để xuất lại.')
    else:
        log('Chưa có tin thành công nào để xuất CSV.')
    issues = store.issues()
    issues_path = Path(output).with_name(Path(output).stem + '_issues.csv')
    if issues or issues_path.exists():
        try:
            write_csv_atomic(issues_path, ['URL', 'Status', 'Attempts', 'Error', 'Source_List_URL'],
                             _CsvRows([[csv_value(v) for v in row] for row in issues], run_id))
            log(f'Danh sách tin bỏ qua/lỗi: {issues_path.resolve()}')
        except OSError as error:
            success = False
            log(f'Không ghi được file issues ({error}). SQLite vẫn giữ nguyên; dùng --export-only để thử lại.')
    return success


# ======================================================================================
# 7. Vòng lặp chính
# ======================================================================================
def run(store, crawler, max_pages=0, max_items=0, keep_contact_name=True, debug_html_path=None, log=print):
    """Trả về (lý do dừng, thống kê lượt này). Giới hạn là tổng trang done/tin done trong DB; 0 = không giới hạn."""
    stats = {'pages': 0, 'found': 0, 'ok': 0, 'skipped': 0, 'errors': 0}

    def progress():
        log(f'  [trang đọc {stats["pages"]} | URL mới tìm thấy {stats["found"]} | thành công {stats["ok"]} '
            f'| bỏ qua {stats["skipped"]} | lỗi {stats["errors"]} | còn chờ {store.count("pending")}]')

    def handle_list(page):
        url = page['url']
        log(f'[danh sách] {url}')
        try:
            result = crawler.fetch_parse(url, 'list', lambda r: parse_list_page(r.text, r.url))
        except (BlockedError, RateLimitedError, RobotsDisallowedError) as error:
            raise StopRun(f'{error}. Trang danh sách vẫn nằm trong hàng đợi.')
        except StructureError as error:
            if debug_html_path and getattr(error, 'html', None):
                try:
                    Path(debug_html_path).write_text(error.html, encoding='utf-8')
                    log(f'  Đã lưu HTML trang lỗi để kiểm tra: {debug_html_path}')
                except OSError as save_error:
                    log(f'  Không lưu được HTML chẩn đoán: {save_error}')
            raise StopRun(f'Cấu trúc trang danh sách không như mong đợi: {error}')
        except GoneError as error:
            raise StopRun(f'Trang danh sách không còn tồn tại ({error}); giữ trang pending để kiểm tra/resume.')
        except (TransientError, HttpError, ParseError) as error:
            raise StopRun(f'Không đọc được trang danh sách ({error}); chạy lại với --resume sau.')
        new, repeated = store.record_list_page(url, result.detail_urls, result.next_url)
        stats['pages'] += 1
        stats['found'] += new
        if repeated:
            log('  Trang lặp lại nội dung trang trước: dừng phân trang.')
        elif not result.detail_urls:
            log('  Trang không có tin (vùng danh sách có nhưng rỗng): coi là hết dữ liệu.')
        elif not result.next_url:
            log('  Không có liên kết trang kế tiếp: đây là trang cuối.')
        progress()

    def handle_detail(item):
        url = item['url']
        log(f'[tin] {url}')
        try:
            row = crawler.fetch_parse(url, 'detail', lambda r: parse_detail_page(r.text, r.url))
        except GoneError:
            store.mark(url, 'gone', 'HTTP 404/410: tin không còn tồn tại')
            stats['skipped'] += 1
        except RobotsDisallowedError:
            store.mark(url, 'skipped', 'robots.txt không cho phép')
            stats['skipped'] += 1
        except (BlockedError, RateLimitedError) as error:
            raise StopRun(f'{error}. URL này vẫn nằm trong hàng đợi.')
        except (ParseError, TransientError, HttpError) as error:
            store.mark(url, 'failed', f'{type(error).__name__}: {error}')
            stats['errors'] += 1
            log(f'  Lỗi: {error}')
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
                    log(f'  Không lưu được tin: {issue["error"]}')
                else:
                    stats['skipped'] += 1
                # Store đã chuyển trạng thái/cộng attempts; không mark thêm lần nữa.
        progress()

    try:
        while True:
            if max_items and store.count('done') >= max_items:
                return 'Đạt giới hạn số tin.', stats
            item = store.next_pending()
            if item is not None:                       # xử lý hết tin đã biết trước khi sang trang mới
                handle_detail(item)
                continue
            if max_pages and store.pages_done() >= max_pages:
                return 'Đạt giới hạn số trang.', stats
            page = store.next_list_page()
            if page is None:
                return 'Đã hết trang danh sách.', stats
            handle_list(page)
    except StopRun as stop:
        return f'DỪNG: {stop}', stats


def nonneg_int(text):
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError('phải >= 0')
    return value


def delay_value(text):
    import math
    value = float(text)
    if not math.isfinite(value) or value < MIN_DELAY:
        raise argparse.ArgumentTypeError(f'khoảng chờ phải hữu hạn và >= {MIN_DELAY:g} giây')
    return value


def build_parser():
    p = argparse.ArgumentParser(description='Thu thập tin bán BĐS từ một trang danh sách batdongsan.com.vn.',
        epilog='CSV đi kèm .ady-owner.json: chuyển cả hai file. CSV cũ chưa có sidecar phải xuất sang --output mới.')
    p.add_argument('--list-url', help=f'URL trang danh sách (mặc định: {DEFAULT_LIST_URL})')
    p.add_argument('--max-pages', type=nonneg_int, default=2, help='tổng trang danh sách done trong DB, kể cả resume (0 = không giới hạn; mặc định 2)')
    p.add_argument('--max-items', type=nonneg_int, default=20, help='tổng tin done trong DB, kể cả resume (0 = không giới hạn; mặc định 20)')
    p.add_argument('--delay', type=delay_value, default=MIN_DELAY, help='giây giữa hai request HTML, tối thiểu 10')
    p.add_argument('--state', default=str(BASE_DIR / 'bds_danang_state.sqlite'), help='file trạng thái SQLite')
    p.add_argument('--output', default=str(BASE_DIR / 'bds_danang.csv'), help='file CSV kết quả')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--resume', action='store_true', help='tiếp tục lượt trước (dùng lại file trạng thái)')
    mode.add_argument('--new-run', action='store_true', help='tạo lượt mới, file trạng thái/CSV riêng có dấu thời gian')
    mode.add_argument('--export-only', action='store_true', help='chỉ xuất lại CSV từ trạng thái đã lưu, không truy cập mạng')
    p.add_argument('--fetcher', choices=('http', 'auto', 'playwright'), default='http',
                   help='http (mặc định) | auto (HTTP, chỉ thử browser khi có dấu hiệu cần JavaScript rõ ràng) | playwright')
    p.add_argument('--browser-channel', choices=('chromium', 'msedge', 'chrome'), default='chromium',
                   help='Chromium Playwright hoặc Edge/Chrome đã cài; chỉ dùng với browser')
    p.add_argument('--no-contact-name', action='store_true', help='không lưu tên người liên hệ')
    p.add_argument('--experimental-skip-gtm-bootstrap', action='store_true',
                   help='Thử nghiệm browser: không gửi đúng script https://batdongsan.com.vn/gtganalytics/; mặc định tắt.')
    return p


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    args = build_parser().parse_args(argv)
    state, output = Path(args.state), Path(args.output)
    if args.new_run:
        import uuid
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_' + uuid.uuid4().hex[:8]
        state = state.with_name(f'{state.stem}_{stamp}{state.suffix}')
        output = output.with_name(f'{output.stem}_{stamp}{output.suffix}')
        if state.exists() or output.exists():
            print('Tên lượt mới đã tồn tại; dừng để tránh ghi đè.')
            return 2
    elif state.exists() and not (args.resume or args.export_only):
        print(f'Đã có file trạng thái: {state}\nDùng --resume hoặc --new-run.')
        return 2
    if (args.resume or args.export_only) and not state.exists():
        print(f'Không có file trạng thái để resume/xuất: {state}')
        return 2

    store = http = crawler = None
    code, ready = 0, False
    try:
        store = Store(state)
        if args.export_only:
            code = 0 if export_csv(store, output) else 4
        else:
            stored = store.get_meta('list_url')
            list_url = args.list_url or stored or DEFAULT_LIST_URL
            try:
                valid_list_url(list_url)
            except ValueError as error:
                print(error)
                return 2
            if stored and stored != list_url:
                print(f'Lượt này dùng URL khác: {stored}\nDùng --new-run nếu muốn URL mới.')
                return 2
            if not stored:
                store.set_meta('list_url', list_url)
                store.set_meta('created_at', utc_now())
                store.add_list_page(list_url)
            ready = True
            if 'da-nang' not in list_url:
                print('Cảnh báo: URL không chứa da-nang; chỉ theo phạm vi danh sách đã chọn.')
            print('Dữ liệu thuộc website/người đăng; chỉ dùng học tập/nội bộ, giữ URL làm nguồn.')
            if args.resume:
                retried = store.reset_failed()
                print(f'Đưa {retried} tin failed đủ điều kiện về pending.')
            print(f'Danh sách: {list_url}\nTrạng thái: {state}\nCSV: {output}')
            print(f'Khoảng chờ >= {args.delay:g}s/request. Giới hạn trang/tin là tổng done trong DB, kể cả resume.')
            print('CSV đi kèm .ady-owner.json; chuyển cả hai file. CSV cũ chưa có sidecar: chọn --output mới.')
            throttle = Throttle(args.delay)
            http = HttpFetcher()
            crawler = Crawler(http, throttle, mode=args.fetcher)
            if args.fetcher != 'http':
                crawler.browser = BrowserFetcher(crawler.robots_allows)
                crawler.browser.channel = args.browser_channel
                crawler.browser.experimental_skip_gtm_bootstrap = args.experimental_skip_gtm_bootstrap
                crawler.browser.log = crawler.log
            reason, stats = run(store, crawler, args.max_pages, args.max_items, not args.no_contact_name,
                                state.with_suffix('.last_error.html'))
            print(f'\n{reason}\nThống kê lần chạy: {stats}; tổng done: {store.count("done")}')
            code = 3 if reason.startswith('DỪNG') else 0
    except KeyboardInterrupt:
        print('Bị ngắt bằng Ctrl+C; dữ liệu đã commit vẫn còn, dùng --resume để tiếp tục.')
        code = 130
    except Exception as error:
        print(f'DỪNG: {type(error).__name__}: {error}. Dữ liệu đã commit vẫn giữ trong {state}.')
        code = 3
    finally:
        # Cleanup không được che lỗi chính hoặc Ctrl+C; mã lỗi chính có ưu tiên.
        resource = crawler if crawler is not None else http
        if resource is not None:
            try:
                resource.close()
            except (Exception, KeyboardInterrupt) as error:
                print(f'Lỗi cleanup crawler: {type(error).__name__}: {error}')
                if code == 0:
                    code = 3
        if store is not None:
            try:
                if ready and not args.export_only:
                    if not export_csv(store, output) and code == 0:
                        code = 4
            except (Exception, KeyboardInterrupt) as error:
                print(f'Không xuất được CSV: {type(error).__name__}: {error}; SQLite vẫn giữ dữ liệu.')
                if code == 0:
                    code = 4
            finally:
                try:
                    store.close()
                except (Exception, KeyboardInterrupt) as error:
                    print(f'Lỗi đóng SQLite: {type(error).__name__}: {error}')
                    if code == 0:
                        code = 3
    return code


if __name__ == '__main__':
    sys.exit(main())
