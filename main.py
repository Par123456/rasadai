import os
import json
import time
import logging
import cloudscraper
import html
import re
import random
import tempfile
import trafilatura
import concurrent.futures
import feedparser
from urllib.parse import quote, unquote, urlparse, urlunparse
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from gnews import GNews
from ddgs import DDGS
from dateutil import parser
import hashlib

# --- CONFIGURATION ---
CONFIG = {
    'SEARCH_QUERY': 'Iran AND (Israel OR USA OR nuclear OR conflict OR sanctions OR currency OR IRGC)',
    # Focused queries (cleaner signal than one mega-query)
    'SEARCH_QUERIES': [
        'Iran (Israel OR Gaza OR Hezbollah OR Houthis) (attack OR strike OR missile OR drone)',
        'Iran (nuclear OR IAEA OR enrichment OR sanctions)',
        'Iran (dollar OR rial OR currency OR IRGC OR economy)',
    ],
    'TARGET_SOURCES': [
        'iranintl.com', 'bbc.com/persian', 'radiofarda.com', 'independentpersian.com',
        'dw.com/fa', 'presstv.ir', 'tasnimnews.com', 'farsnews.ir', 'irna.ir', 'mehrnews.com'
    ],
    # Prefer quality Persian/intel sources first
    'PRIORITY_SITES': [
        'bbc.com/persian', 'radiofarda.com', 'iranintl.com',
        'independentpersian.com', 'dw.com/fa'
    ],
    'SOURCE_PRIORITY': {
        'bbc.com': 10, 'radiofarda.com': 10, 'iranintl.com': 9,
        'independentpersian.com': 8, 'dw.com': 8,
        'reuters.com': 9, 'apnews.com': 8, 'aljazeera.com': 7,
        'theguardian.com': 7, 'nytimes.com': 7,
        'tasnimnews.com': 4, 'farsnews.ir': 4, 'irna.ir': 4,
        'mehrnews.com': 4, 'presstv.ir': 3,
    },
    'FILES': {
        'NEWS': 'news.json',
        'MARKET': 'market.json',
        'DAILY_SUMMARY': 'daily_summary.json'
    },
    'TELEGRAM': {
        'BOT_TOKEN': os.environ.get('TG_BOT_TOKEN'),
        'CHANNEL_ID': os.environ.get('TG_CHANNEL_ID')
    },
    'PROXY_URL': 'https://raw.githubusercontent.com/itsyebekhe/MTProtoNexus/refs/heads/gh-pages/extracted_proxies.json',
    'TIMEOUT': 12,              # scrape timeout
    'AI_TIMEOUT': 45,
    'MAX_WORKERS': 3,           # safer for scrape + AI rate limits
    'MAX_CANDIDATES': 15,       # hard cap before process_item
    'MAX_TEXT_CHARS': 1800,     # enough for AI, cheaper to extract
    'MIN_TEXT_LEN': 100,
    'MIN_AI_URGENCY_HINT': 5,   # skip AI if cheap hint below this
    'POLLINATIONS_KEY': os.environ.get('POLLINATIONS_API_KEY'),
    'AI_RETRIES': 3,
    'MIN_TELEGRAM_URGENCY': 7,
    'MAX_NEWS_AGE_HOURS': 18,
    'HISTORY_SIZE': 300,
    'RESOLVE_GOOGLE_URLS': False,  # True = try redirect; False = drop Google links
}

PROXY_NAMES = [
    "کوروش", "داریوش", "کاوه", "رستم", "آرش", "سیاوش", "بابک",
    "خشایار", "سورنا", "آریوبرزن", "میترا", "آناهیتا", "فریدون",
    "جمشید", "زال", "بهرام", "شاپور", "آرتابان", "پیروز", "مازیار",
    "تهمینه", "گردآفرید", "سهراب", "آتوسا", "رکسانا", "ماندانا"
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()


class IranNewsRadar:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
        self.scraper.headers.update({
            'Accept-Language': 'en-US,en;q=0.9,fa;q=0.8',
            'Cache-Control': 'no-cache',
        })
        self.api_key = CONFIG['POLLINATIONS_KEY']
        self.existing_news = self._load_existing_news()

        self.seen_urls = set()
        self.seen_titles = set()
        self.recent_title_hashes = set()
        self.failed_hosts = set()

        for item in self.existing_news:
            if item.get('url'):
                self.seen_urls.add(self._clean_url(item['url']))
            if item.get('title_en'):
                nt = self._normalize_text(item['title_en'])
                self.seen_titles.add(nt)
                self.recent_title_hashes.add(self._title_hash(item['title_en']))
            if item.get('title_fa'):
                nt = self._normalize_text(item['title_fa'])
                self.seen_titles.add(nt)
                self.recent_title_hashes.add(self._title_hash(item['title_fa']))

        # keep recent hashes bounded
        if len(self.recent_title_hashes) > 200:
            self.recent_title_hashes = set(list(self.recent_title_hashes)[-150:])

        self.gnews_en = GNews(language='en', country='US', period='4h', max_results=5)

    # ───────────────────────── helpers ─────────────────────────

    def _load_previous_daily_summary(self):
        path = CONFIG['FILES']['DAILY_SUMMARY']
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

    def _clean_url(self, url):
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
            return clean.rstrip('/')
        except Exception:
            return url

    def _normalize_text(self, text):
        if not text:
            return ""
        text = text.replace('ي', 'ی').replace('ك', 'ک').replace('\u200c', ' ')
        clean = re.sub(r'[^\w\s]', '', text.lower())
        return re.sub(r'\s+', '', clean)

    def _title_hash(self, title):
        return hashlib.md5(self._normalize_text(title).encode('utf-8')).hexdigest()

    def _get_tokens(self, text):
        if not text:
            return set()
        stop_words = {
            'a', 'an', 'the', 'and', 'or', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by',
            'is', 'are', 'was', 'news', 'report', 'breaking',
            'از', 'به', 'در', 'که', 'و', 'این', 'آن', 'را', 'برای', 'با', 'است', 'شد',
            'شده', 'می', 'بر', 'یک', 'خود', 'تا', 'کرد', 'نیز'
        }
        text = text.replace('ي', 'ی').replace('ك', 'ک').replace('\u200c', ' ')
        clean = re.sub(r'[^\w\s]', '', text.lower())
        return set(clean.split()) - stop_words

    def _is_duplicate_fuzzy(self, new_title, comparison_pool):
        norm_title = self._normalize_text(new_title)
        if norm_title in self.seen_titles:
            return True
        new_tokens = self._get_tokens(new_title)
        if len(new_tokens) < 3:
            return False
        # Only compare against recent items (faster)
        pool = comparison_pool[:60] if len(comparison_pool) > 60 else comparison_pool
        for item in pool:
            existing_title = item.get('title_en') or item.get('title_fa') or item.get('title', '')
            existing_tokens = self._get_tokens(existing_title)
            if not existing_tokens:
                continue
            inter = new_tokens.intersection(existing_tokens)
            union = new_tokens.union(existing_tokens)
            if union and (len(inter) / len(union)) > 0.5:
                return True
        return False

    def _load_existing_news(self):
        if not os.path.exists(CONFIG['FILES']['NEWS']):
            return []
        try:
            with open(CONFIG['FILES']['NEWS'], 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    def _domain_score(self, url, publisher=""):
        try:
            host = urlparse(url or '').netloc.lower().replace('www.', '')
            for domain, score in CONFIG['SOURCE_PRIORITY'].items():
                if domain in host:
                    return score
        except Exception:
            pass
        pub = (publisher or '').lower()
        for domain, score in CONFIG['SOURCE_PRIORITY'].items():
            if domain.split('.')[0] in pub:
                return score
        return 3

    def _cheap_urgency_hint(self, title, publisher=""):
        t = (title or '').lower()
        score = 3
        high = [
            'attack', 'strike', 'missile', 'killed', 'nuclear', 'drone', 'war',
            'حمله', 'موشک', 'هسته‌ای', 'پهپاد', 'کشته', 'انفجار', 'تشدید'
        ]
        mid = [
            'sanction', 'dollar', 'currency', 'irgc', 'protest',
            'تحریم', 'دلار', 'ارز', 'سپاه', 'اعتراض'
        ]
        if any(w in t for w in high):
            score += 3
        if any(w in t for w in mid):
            score += 2
        if self._domain_score('', publisher) >= 8:
            score += 1
        return min(score, 9)

    def _generate_news_id(self, clean_url):
        return hashlib.md5((clean_url or str(time.time())).encode('utf-8')).hexdigest()[:10]

    def _get_fallback_image(self, text_or_tag):
        t = str(text_or_tag).lower()
        if any(w in t for w in ['ship', 'navy', 'sea', 'strait', 'hormuz', 'دریایی', 'کشتی', 'خلیج']):
            return 'https://images.unsplash.com/photo-1509316975850-ff9c5deb0cd9?auto=format&fit=crop&w=1200&q=80'
        if any(w in t for w in ['missile', 'strike', 'war', 'army', 'military', 'نظامی', 'موشک', 'پهپاد', 'حمله']):
            return 'https://images.unsplash.com/photo-1585829365295-ab7cd400c167?auto=format&fit=crop&w=1200&q=80'
        if any(w in t for w in ['nuclear', 'atomic', 'iaea', 'هسته‌ای', 'غنی‌سازی']):
            return 'https://images.unsplash.com/photo-1581092160607-ee22621dd758?auto=format&fit=crop&w=1200&q=80'
        if any(w in t for w in ['currency', 'dollar', 'economy', 'تومان', 'دلار', 'تحریم', 'ارز']):
            return 'https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?auto=format&fit=crop&w=1200&q=80'
        return 'https://images.unsplash.com/photo-1504711434969-e33886168f5c?auto=format&fit=crop&w=1200&q=80'

    # ───────────────────────── proxies & market ─────────────────────────

    def fetch_best_proxies(self):
        try:
            resp = self.scraper.get(CONFIG['PROXY_URL'], timeout=10)
            if resp.status_code != 200:
                return []
            data = resp.json()
            online = [p for p in data if p.get('status') == 'Online']
            online.sort(key=lambda x: x.get('latency') if x.get('latency') is not None else 99999)
            return online[:9]
        except Exception:
            return []

    def fetch_market_rates(self):
        data = {"usd": "نامشخص", "oil": "نامشخص", "updated": "--:--"}
        try:
            resp = self.scraper.get("https://alanchand.com/en/currencies-price/usd", timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'lxml')
                usd = soup.find('input', attrs={'data-curr': 'tmn'})
                if usd:
                    val = usd.get('data-price') or usd.get('value')
                    if val:
                        data["usd"] = f"{int(int(val.replace(',', '')) / 10):,}"
        except Exception:
            pass
        try:
            resp = self.scraper.get("https://oilprice.com/oil-price-charts/46", timeout=10)
            soup = BeautifulSoup(resp.text, 'lxml')
            oil = soup.select_one(".last_price")
            if oil:
                data["oil"] = oil.get_text().strip()
        except Exception:
            pass
        data["updated"] = time.strftime("%H:%M")
        return data

    # ───────────────────────── news search ─────────────────────────

    def fetch_gnews(self):
        results = []
        try:
            results = self.gnews_en.get_news(CONFIG['SEARCH_QUERY']) or []
        except Exception as e:
            logger.error(f"GNews Error: {e}")
        return results

    def fetch_duckduckgo(self, query, region='wt-wt', max_results=8):
        results = []
        try:
            ddgs = DDGS()
            ddg_gen = ddgs.news(
                query=query, region=region, safesearch="off",
                timelimit="d", max_results=max_results
            )
            for r in ddg_gen:
                results.append({
                    'title': r.get('title'),
                    'url': r.get('url'),
                    'publisher': {'title': r.get('source')},
                    'published date': r.get('date'),
                    'description': r.get('body'),
                    'image': r.get('image')
                })
        except Exception as e:
            logger.error(f"DDG Error ({query[:40]}): {e}")
        return results

    def fetch_bing_rss(self, query):
        results = []
        try:
            encoded_query = quote(query)
            url = f"https://www.bing.com/news/search?q={encoded_query}&format=rss"
            feed = feedparser.parse(url)
            for entry in feed.entries:
                publisher = "Bing News"
                if hasattr(entry, 'news_source'):
                    publisher = entry.news_source
                elif hasattr(entry, 'source') and hasattr(entry.source, 'title'):
                    publisher = entry.source.title

                final_link = entry.link
                if "apiclick.aspx" in final_link:
                    match = re.search(r'[?&]url=([^&]+)', final_link)
                    if match:
                        final_link = unquote(match.group(1))

                image_url = None
                try:
                    if hasattr(entry, 'news_image'):
                        raw_url = entry.news_image
                        image_url = raw_url.replace('{0}', '700').replace('{1}', '400') if '{0}' in raw_url else raw_url
                except Exception:
                    pass

                results.append({
                    'title': entry.title,
                    'url': final_link,
                    'publisher': {'title': publisher},
                    'published date': entry.published,
                    'description': entry.summary if hasattr(entry, 'summary') else entry.title,
                    'image': image_url
                })
        except Exception as e:
            logger.error(f"Bing RSS Error: {e}")
        return results

    def fetch_manual_url(self, url):
        try:
            resp = self.scraper.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, 'lxml')
            title = soup.title.string if soup.title else "Unknown Title"
            og_title = soup.find("meta", property="og:title")
            if og_title:
                title = og_title.get("content")
            publisher = "Manual Source"
            og_site = soup.find("meta", property="og:site_name")
            if og_site:
                publisher = og_site.get("content")
            image = None
            og_image = soup.find("meta", property="og:image")
            if og_image:
                image = og_image.get("content")
            return [{
                'title': title,
                'url': url,
                'publisher': {'title': publisher},
                'published date': datetime.now(timezone.utc).isoformat(),
                'description': "Manual Submission",
                'image': image
            }]
        except Exception as e:
            logger.error(f"Manual Fetch Error: {e}")
            return []

    def get_combined_news(self):
        """Parallel multi-source search."""
        all_entries = []
        futs = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            futs.append(ex.submit(self.fetch_gnews))
            futs.append(ex.submit(self.fetch_bing_rss, CONFIG['SEARCH_QUERY']))

            for q in CONFIG.get('SEARCH_QUERIES', [CONFIG['SEARCH_QUERY']]):
                futs.append(ex.submit(self.fetch_duckduckgo, q, 'wt-wt', 6))

            for domain in CONFIG.get('PRIORITY_SITES', [])[:5]:
                if any(x in domain for x in ['bbc', 'radiofarda', 'dw', 'iranintl', 'independent']):
                    q = f"site:{domain} ایران"
                else:
                    q = f"site:{domain} Iran"
                futs.append(ex.submit(self.fetch_duckduckgo, q, 'wt-wt', 5))

            for fut in concurrent.futures.as_completed(futs):
                try:
                    batch = fut.result() or []
                    all_entries.extend(batch)
                except Exception as e:
                    logger.warning(f"Search worker failed: {e}")

        logger.info(f"Raw search hits: {len(all_entries)}")
        return all_entries

    # ───────────────────────── URL resolve ─────────────────────────

    def _resolve_final_url(self, url, raw_title=None):
        if not url:
            return None
        if "news.google.com" not in url:
            return url

        if not CONFIG.get('RESOLVE_GOOGLE_URLS', False):
            # Prefer dropping fragile Google links; other sources already give real URLs
            return None

        try:
            resp = self.scraper.head(url, allow_redirects=True, timeout=5)
            if "news.google.com" not in resp.url and "consent.google.com" not in resp.url:
                return resp.url
        except Exception:
            pass
        return None

    # ───────────────────────── content grab ─────────────────────────

    def scrape_article_data(self, final_url, fallback_snippet, raw_image=None):
        """Trafilatura-first, soup only as last resort. Shorter text for AI."""
        if not final_url or final_url.lower().endswith('.pdf'):
            return fallback_snippet, self._get_fallback_image(fallback_snippet)

        host = urlparse(final_url).netloc.lower()
        if host in self.failed_hosts:
            return fallback_snippet, self._get_fallback_image(fallback_snippet)

        extracted_text = fallback_snippet
        extracted_image = raw_image
        max_chars = CONFIG.get('MAX_TEXT_CHARS', 1800)

        try:
            downloaded = trafilatura.fetch_url(final_url)
            if downloaded:
                text = trafilatura.extract(
                    downloaded,
                    include_comments=False,
                    include_tables=False,
                    favor_precision=True,
                )
                if text and len(text.strip()) > CONFIG.get('MIN_TEXT_LEN', 100):
                    extracted_text = re.sub(r'\s+', ' ', text).strip()[:max_chars]

                try:
                    meta = trafilatura.extract_metadata(downloaded)
                    if meta and not extracted_image and getattr(meta, 'image', None):
                        extracted_image = meta.image
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"trafilatura failed {final_url}: {e}")
            self.failed_hosts.add(host)

        # Soup only if text still weak or image missing
        need_soup = (
            extracted_text == fallback_snippet
            or len(extracted_text) < CONFIG.get('MIN_TEXT_LEN', 100)
            or not extracted_image
        )
        if need_soup:
            try:
                resp = self.scraper.get(final_url, timeout=CONFIG['TIMEOUT'])
                soup = BeautifulSoup(resp.text, 'lxml')
                if extracted_text == fallback_snippet or len(extracted_text) < CONFIG.get('MIN_TEXT_LEN', 100):
                    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe']):
                        tag.decompose()
                    paras = [
                        p.get_text(strip=True)
                        for p in soup.find_all('p')
                        if len(p.get_text(strip=True)) > 40
                    ]
                    clean = ' '.join(paras[:12])
                    if len(clean) > CONFIG.get('MIN_TEXT_LEN', 100):
                        extracted_text = clean[:max_chars]
                if not extracted_image:
                    og = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
                    if og and og.get('content'):
                        extracted_image = og['content']
            except Exception as e:
                logger.warning(f"Soup fallback failed {final_url}: {e}")
                self.failed_hosts.add(host)

        if not extracted_image or not isinstance(extracted_image, str) or extracted_image.startswith('data:'):
            extracted_image = self._get_fallback_image(extracted_text)

        return extracted_text, extracted_image

    # ───────────────────────── AI analysis ─────────────────────────

    def analyze_with_ai(self, headline, full_text, source_name):
        if not self.api_key:
            return None

        is_regime = any(x in source_name.lower() for x in ['tasnim', 'fars', 'irna', 'presstv', 'mehr'])
        regime_instruction = ""
        if is_regime:
            regime_instruction = "CRITICAL: The source is Iranian State Media. Expose propaganda. "

        system_prompt = (
            "تو یک تحلیل‌گر ارشد و تیزبین ژئوپلیتیک، مسلط به ادبیات کانال‌های تحلیلی تلگرام فارسی (مانند تحلیل‌گران مستقل و اپوزیسیون ایرانی) هستی.\n"
            "وظیفه تو تبدیل اخبار خام به تحلیل‌های کوتاه، ضربتی، کاملاً انسانی، به فارسی روان و بدون «بوی هوش مصنوعی» است.\n\n"
            "🔴 قوانین حیاتی نگارش و انسانی‌سازی (مهم - حتماً رعایت شود):\n"
            "۱. **روانی، شفافیت و سادگی زبان (مهم):**\n"
            " - از کلمات قلم‌به‌سلم، پیچیده و عجیب دانشگاهی (مثل: 'گره مشخص'، 'تعمیم روایت'، 'شکل‌گیری محاسبات') مطلقاً استفاده نکن.\n"
            " - **ممنوعیت ترجمه تحت‌اللفظی:** عبارات انگلیسی را کلمه به کلمه ترجمه نکن (مثلاً اصطلاح 'drone threat' را 'تهدید پهپادی' بنویس، نه 'تهدید پرنده'!).\n"
            " - جملات باید بسیار روان، صریح و شفاف باشند تا مخاطب با یک‌بار خواندن متوجه اصل ماجرا شود.\n\n"
            "۲. **ممنوعیت مطلق عبارت‌های کلیشه‌ای رباتیک:**\n"
            " استفاده از این عبارات مطلقاً ممنوع است: ('به نظر می‌رسد'، 'نشان‌دهنده این است که'، 'لازم به ذکر است'، 'در نهایت'، 'پیامدهای عمیق'، 'ابعاد جدیدی از'، 'در این راستا'، 'شایان ذکر است').\n\n"
            "۳. **تنوع در ساختار جملات:**\n"
            " جملات نباید همه با یک فرمول شروع شوند. گاهی با یک فعل حاد، گاهی با یک آمار، و گاهی با یک ارزیابی مستقیم شروع کن.\n\n"
            "۴. **تعداد نقطه‌نظرات شناور:**\n"
            " بخش summary می‌تواند بین ۲ تا ۴ مورد باشد. اگر خبر کوتاه است ۲ نکته عمیق و روان کافیست، برای خبرهای مهم ۴ نکته بنویس. خودت را به ۳ نقطه اجباری محدود نکن.\n\n"
            "۵. **حذف القاب رسمی و حاکمیتی:**\n"
            " از القاب مانند (آیت‌الله، سردار، شهید، حجت‌الاسلام، عمومی) استفاده نکن. فقط نام و سمت رسمی.\n\n"
            "۶. **تغییر لحن بر اساس اهمیت (Urgency):**\n"
            " - اگر خبر نظامی/فوریت بالاست (۸ تا ۱۰): لحن ضربتی، کوتاه و صریح باشد.\n"
            " - اگر خبر اقتصادی/سیاسی است (۴ تا ۷): لحن تحلیلی و افشاگرانه باشد.\n\n"
            "قواعد امتیازبندی فوریت (Urgency Score 1-10):\n"
            "- 9-10: درگیری مستقیم نظامی، کشته شدن مقامات ارشد، ضربه به تاسیسات اتمی/نظامی.\n"
            "- 7-8: تحریم‌های خفه کننده جدید، سقوط شدید ارزی، اعتراضات سراسری، حملات نیابتی سنگین.\n"
            "- 4-6: تحرکات دیپلماتیک مهم، تنش‌های لفظی مسئولان، مانورهای منطقه‌ای.\n"
            "- 1-3: اظهارات routine، دیدارهای تشریفاتی.\n\n"
            "فرمت خروجی باید دکارتی و دقیقاً ساختار JSON زیر باشد:\n"
            "{\n"
            ' "title_fa": "تیتر جذاب، روان، غیرتکراری و بدون کلمات خنثی (حداکثر ۱۰ کلمه)",\n'
            ' "summary": ["نکته تحلیلی ۱ به فارسی روان و بدون کلمات اضافه", "نکته تحلیلی ۲ با تمرکز بر واقعیت پشت خبر"],\n'
            ' "impact": "تأثیر عملیاتی یا اقتصادی خبر در یک جمله کوتاه، روان و ضربتی",\n'
            ' "tag": "کلمه کلیدی اصلی (مثلاً: نظامی، ارز، تحریم، نیابتی)",\n'
            ' "urgency": عدد بین 1 تا 10,\n'
            ' "sentiment": عدد بین -1.0 تا 1.0\n'
            "}"
        )

        current_text = full_text
        for attempt in range(CONFIG['AI_RETRIES']):
            try:
                if attempt > 0:
                    current_text = headline + " " + full_text[:800]
                resp = self.scraper.post(
                    "https://gen.pollinations.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={
                        "model": "openai",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": f"{regime_instruction}SOURCE: {source_name}\nHEADLINE: {headline}\nTEXT: {current_text}"}
                        ],
                        "temperature": 0.25
                    },
                    timeout=CONFIG.get('AI_TIMEOUT', 45)
                )
                if resp.status_code == 200:
                    raw = resp.json()['choices'][0]['message']['content']
                    clean = re.sub(r'```json\s*|```', '', raw).strip()
                    data = json.loads(clean)
                    if 'title_fa' in data and 'summary' in data:
                        return data
                time.sleep(1)
            except Exception as e:
                logger.error(f"AI Attempt {attempt+1} failed: {e}")
                time.sleep(2)
        return None

    def generate_daily_summary(self):
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        todays_items = [
            item for item in self.existing_news
            if datetime.fromtimestamp(item.get("timestamp", 0), timezone.utc) >= today_start
        ]
        if len(todays_items) < 3:
            return None
        todays_items.sort(key=lambda x: x.get("urgency", 0), reverse=True)
        news_context = []
        for item in todays_items[:20]:
            news_context.append(
                f"Title: {item.get('title_en')}\nSource: {item.get('source')}\n"
                f"Urgency: {item.get('urgency')}\nTag: {item.get('tag')}\n"
                f"Impact: {item.get('impact')}\nSummary: {' '.join(item.get('summary', []))}"
            )
        news_block = "\n\n".join(news_context)
        previous_summary = self._load_previous_daily_summary()
        previous_block = ""
        if previous_summary:
            previous_block = (
                f"Previous Strategic Assessment:\nThemes: {previous_summary.get('themes')}\n"
                f"Strategic Assessment: {previous_summary.get('strategic_assessment')}\n"
                f"Market Impact: {previous_summary.get('market_impact')}\n"
                f"Risk Level: {previous_summary.get('risk_level')}"
            )
        return self.analyze_daily_summary_with_ai(news_block, previous_block)

    def analyze_daily_summary_with_ai(self, news_block, previous_block):
        if not self.api_key:
            return None
        system_prompt = """
You are a senior geopolitical intelligence analyst aligned with the Iranian nationalist opposition.
This is a rolling daily strategic assessment.
You will receive:
1) All today's news events
2) The previous run's strategic summary (if available)
Your job:
- Detect evolution compared to previous assessment.
- Identify new escalation or de-escalation signals.
- Strip away regime propaganda and expose their true vulnerabilities.
- Provide highly analytical predictive intelligence based on geopolitical realities.
OUTPUT LANGUAGE: Persian (Farsi)
STRICT OUTPUT JSON:
{
  "date": "YYYY-MM-DD HH:MM",
  "executive_tldr": "1 punchy sentence summarizing the day's geopolitical reality",
  "themes": [3-5 bullet points],
  "regime_vulnerabilities": {
    "regime_internal_friction": "1 sentence exposing IRGC vs Government infighting or purges",
    "infrastructure_vulnerability": "1 sentence on energy shortages, cyber-attacks, or systemic failures",
    "sanctions_evasion_watch": "1 sentence on oil smuggling or banking evasion exposed today"
  },
  "proxy_network_status": "1 sentence analyzing the health/actions of regional proxies (Hezbollah, Houthis, etc.)",
  "opposition_momentum": "1 sentence on diaspora actions, internal strikes, or civil disobedience",
  "regime_narrative": "1 concise sentence explaining what propaganda state media is pushing today",
  "predicted_regime_response": "1 sentence predicting their next move (e.g., proxy attack, internal crackdown, diplomatic deception)",
  "forecast": {
    "most_likely_scenario": "1 paragraph predicting the realistic outcome over the next 3-7 days",
    "regime_worst_case_scenario": "1 paragraph detailing the specific events that could fracture regime stability this week",
    "flashpoint_indicator": "The specific trigger event/red line that signals immediate severe escalation"
  },
  "probability_matrix": {
    "military_escalation_percent": "integer (0-100)",
    "economic_shock_percent": "integer (0-100)",
    "domestic_unrest_percent": "integer (0-100)",
    "regime_defection_risk_percent": "integer (0-100)"
  },
  "key_figures_in_focus": ["Name 1 - Reason", "Name 2 - Reason"],
  "strategic_assessment": "1-2 paragraphs of hardline, realistic geopolitical analysis",
  "market_impact": "1 paragraph on economic vulnerabilities and sanctions impact",
  "currency_outlook": "جهش دلار | نوسان بالا | ثبات شکننده",
  "risk_level": "integer (1-10)",
  "change_from_previous": "افزایش | کاهش | بدون تغییر"
}
"""
        try:
            resp = self.scraper.post(
                "https://gen.pollinations.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": "openai",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"TODAY NEWS:\n{news_block}\n\nPREVIOUS SUMMARY:\n{previous_block}"}
                    ],
                    "temperature": 0.2
                },
                timeout=60
            )
            if resp.status_code == 200:
                raw = resp.json()['choices'][0]['message']['content']
                clean = re.sub(r'```json\s*|```', '', raw).strip()
                return json.loads(clean)
        except Exception as e:
            logger.error(f"Daily Summary AI Error: {e}")
        return None

    # ───────────────────────── process item ─────────────────────────

    def process_item(self, entry):
        raw_title = entry.get('title', '').rsplit(' - ', 1)[0].strip()
        publisher = entry.get('publisher', {}).get('title', 'Unknown')

        final_url = self._resolve_final_url(entry.get('url'), raw_title)
        if not final_url:
            return None

        clean_final_url = self._clean_url(final_url)

        if not os.environ.get('MANUAL_URL'):
            if clean_final_url in self.seen_urls:
                return None
            th = self._title_hash(raw_title)
            if th in self.recent_title_hashes or self._normalize_text(raw_title) in self.seen_titles:
                return None
            if self._is_duplicate_fuzzy(raw_title, self.existing_news):
                return None

        # Cheap urgency gate – skip heavy AI for weak items (still scrape lightly if needed)
        hint = self._cheap_urgency_hint(raw_title, publisher)
        logger.info(f"Processing (hint={hint}, score={self._domain_score(final_url, publisher)}): {publisher} | {raw_title[:40]}...")

        snippet = entry.get('description', raw_title)
        text, photo_url = self.scrape_article_data(final_url, snippet, raw_image=entry.get('image'))

        # Skip AI if hint too low and text is thin
        if hint < CONFIG.get('MIN_AI_URGENCY_HINT', 5) and len(text) < 200:
            logger.info(f"Skip AI (low hint/thin text): {raw_title[:40]}")
            return None

        ai = self.analyze_with_ai(raw_title, text, publisher)
        if not ai:
            return None

        try:
            urgency_val = int(ai.get('urgency', 3))
        except Exception:
            urgency_val = 3
        try:
            ts = parser.parse(entry.get('published date')).timestamp()
        except Exception:
            ts = time.time()

        news_id = self._generate_news_id(clean_final_url)
        return {
            "id": news_id,
            "title_fa": ai.get('title_fa', raw_title),
            "title_en": raw_title,
            "summary": ai.get('summary', [snippet]),
            "impact": ai.get('impact', '...'),
            "tag": ai.get('tag', 'General'),
            "urgency": urgency_val,
            "sentiment": ai.get('sentiment', 0),
            "source": publisher,
            "url": final_url,
            "clean_url": clean_final_url,
            "image": photo_url,
            "timestamp": ts
        }

    # ───────────────────────── telegram ─────────────────────────

    def send_digest_to_telegram(self, items):
        token = CONFIG['TELEGRAM']['BOT_TOKEN']
        chat_id = CONFIG['TELEGRAM']['CHANNEL_ID']
        if not token or not chat_id or not items:
            return

        items.sort(key=lambda x: x.get('urgency', 3), reverse=True)

        market_html = ""
        try:
            with open(CONFIG['FILES']['MARKET'], 'r') as f:
                mkt = json.load(f)
            market_html = (
                f"<table bordered striped>\n"
                f" <tr><th>💵 دلار</th><th>🛢 نفت</th><th>⏱ زمان</th></tr>\n"
                f" <tr><td align='center'>{mkt.get('usd', '---')}</td>"
                f"<td align='center'>{mkt.get('oil', '---')}</td>"
                f"<td align='center'>{mkt.get('updated', '--:--')}</td></tr>\n"
                f"</table>\n\n"
            )
        except Exception:
            market_html = ""

        proxies = self.fetch_best_proxies()[:4]
        proxy_html = ""
        if proxies:
            proxy_items = []
            names_pool = random.sample(PROXY_NAMES, min(len(proxies), len(PROXY_NAMES)))
            for i, p in enumerate(proxies):
                proxy_name = names_pool[i]
                latency = p.get('latency', '?')
                raw_tg_url = p.get('tg_url', '#')
                clean_tg_url = html.unescape(raw_tg_url).replace('&amp;', '&')
                proxy_items.append(f"<li>🛡 <a href='{clean_tg_url}'>{proxy_name}</a> (<code>{latency}ms</code>)</li>")
            proxy_html = (
                "<details>\n"
                "<summary>🌐 <b>پروکسی‌های فعال تلگرام (کلیک کنید)</b></summary>\n"
                "<ul>" + "".join(proxy_items) + "</ul>\n"
                "</details>\n\n"
            )

        preview_url = ""
        for item in items:
            img = item.get('image', '')
            if img and isinstance(img, str) and not img.startswith('data:'):
                preview_url = img
                break
        if not preview_url and items:
            preview_url = items[0].get('url', '')
        hidden_preview = f"<a href='{preview_url}'>&#8205;</a>" if preview_url else ""

        def to_farsi_num(num):
            return str(num).translate(str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹'))

        try:
            from zoneinfo import ZoneInfo
            ir_tz = ZoneInfo("Asia/Tehran")
        except ImportError:
            ir_tz = timezone(timedelta(hours=3, minutes=30))
        now_ir = datetime.now(ir_tz)
        ir_time_str = to_farsi_num(now_ir.strftime("%H:%M"))

        header = (
            f"{hidden_preview}"
            f"<h1>🚨 رادار اخبار مهم ایران</h1>\n"
            f"<p>⏱ <b>زمان بروزرسانی:</b> {ir_time_str} (به وقت تهران)</p>\n"
            f"{market_html}"
            f"<hr/>\n\n"
        )

        base_site = "https://itsyebekhe.github.io/rasadai/"
        headlines_text = "<p>📌 <b>سرخط مهم‌ترین اخبار:</b></p>\n<ul>"
        for i, item in enumerate(items, 1):
            title = html.escape(str(item.get('title_fa', item.get('title_en'))))
            source = html.escape(str(item.get('source', 'Unknown')))
            news_id = item.get('id', '')
            deep_link = f"{base_site}?id={news_id}" if news_id else item.get('url', '#')
            urgency = item.get('urgency', 3)
            icon = "🔥" if urgency >= 9 else ("🚨" if urgency >= 7 else "🔹")
            headlines_text += f"<li>{icon} <a href='{deep_link}'>{title}</a> (<i>{source}</i>)</li>"
        headlines_text += "</ul>\n<hr/>\n\n"

        items_html = ""
        all_tags = set()
        for i, item in enumerate(items, 1):
            title = html.escape(str(item.get('title_fa', item.get('title_en'))))
            source = html.escape(str(item.get('source', 'Unknown')))
            impact = html.escape(str(item.get('impact', '')))
            news_id = item.get('id', '')
            deep_link = f"{base_site}?id={news_id}" if news_id else item.get('url', '#')
            summary_raw = item.get('summary', [])
            if isinstance(summary_raw, str):
                summary_raw = [summary_raw]
            safe_summary = "".join([f"<li>{html.escape(str(s))}</li>" for s in summary_raw])
            tag = str(item.get('tag', 'General')).replace(' ', '_')
            all_tags.add(f"#{html.escape(tag)}")
            is_open = " open" if i == 1 else ""
            items_html += (
                f"<details{is_open}>\n"
                f" <summary><b>{to_farsi_num(i)}. {title}</b></summary>\n"
                f" <p>📝 <b>تحلیل خبر:</b></p>\n"
                f" <ul>{safe_summary}</ul>\n"
                f" <p>🎯 <b>اثرگذاری:</b> {impact}</p>\n"
                f" <p>🔗 <a href='{deep_link}'>مطالعه گزارش کامل در داشبورد</a> | "
                f"<a href='{item.get('url')}'>منبع اصلی ({source})</a></p>\n"
                f"</details>\n<hr/>\n"
            )

        tags_html = f"<p>{' '.join(all_tags)}</p>\n"
        footer = f"<footer>{proxy_html}🆔 @RasadAIOfficial</footer>"
        full_rich_html = header + headlines_text + items_html + tags_html + footer

        inline_keyboard = {
            "inline_keyboard": [[
                {"text": "📊 مشاهده داشبورد و رادار زنده", "url": base_site},
                {"text": "🛡 پروکسی‌های فعال تلگرام", "url": "https://itsyebekhe.github.io/MTProtoNexus/"}
            ]]
        }

        api_url = f"https://api.telegram.org/bot{token}/sendRichMessage"
        payload = {
            "chat_id": chat_id,
            "rich_message": {"html": full_rich_html[:30000], "is_rtl": True},
            "reply_markup": inline_keyboard
        }
        try:
            resp = self.scraper.post(api_url, json=payload, timeout=20)
            if resp.status_code == 200:
                logger.info(">>> Rich Message & Deep Links successfully posted to Telegram.")
            else:
                logger.error(f"Telegram Rich Message Failed: Status {resp.status_code} | {resp.text}")
        except Exception as e:
            logger.error(f"TG Send Error: {e}")

    # ───────────────────────── save ─────────────────────────

    def _atomic_json_dump(self, file_path, data):
        dir_name = os.path.dirname(file_path) or '.'
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile('w', dir=dir_name, delete=False, encoding='utf-8') as tf:
                json.dump(data, tf, indent=4, ensure_ascii=False)
                temp_name = tf.name
            os.replace(temp_name, file_path)
        except Exception as e:
            logger.error(f"Atomic dump failed for {file_path}: {e}")
            if temp_name and os.path.exists(temp_name):
                os.remove(temp_name)

    def save_news(self, new_items):
        try:
            all_news = new_items + self.existing_news
            seen_u = set()
            unique_news = []
            for item in all_news:
                u = self._clean_url(item.get('url'))
                if u and u not in seen_u:
                    seen_u.add(u)
                    unique_news.append(item)
            unique_news.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
            final_list = unique_news[:CONFIG['HISTORY_SIZE']]
            self._atomic_json_dump(CONFIG['FILES']['NEWS'], final_list)
            logger.info(">>> news.json updated successfully.")
            return final_list
        except Exception as e:
            logger.error(f"Save Failed: {e}")
            return self.existing_news

    def save_daily_summary(self, summary):
        if not summary:
            return
        try:
            self._atomic_json_dump(CONFIG['FILES']['DAILY_SUMMARY'], summary)
            logger.info(">>> daily_summary.json updated successfully.")
        except Exception as e:
            logger.error(f"Failed to save daily summary: {e}")

    def generate_scheduled_bulletin(self):
        now_utc = datetime.now(timezone.utc)
        tehran_time = now_utc.astimezone(timezone(timedelta(hours=3, minutes=30)))
        hour = tehran_time.hour
        if 6 <= hour < 12:
            edition_key, edition_title = "morning", "بولتـن صبحگاهی"
        elif 12 <= hour < 18:
            edition_key, edition_title = "midday", "بولتـن نیمروزی"
        else:
            edition_key, edition_title = "evening", "بولتـن شبانگاهی (جمع‌بندی روز)"

        top_items = sorted(self.existing_news, key=lambda x: x.get('urgency', 0), reverse=True)[:5]
        if not top_items:
            return None

        news_text = "\n".join([
            f"- {item.get('title_fa')}: {' '.join(item.get('summary', []))}"
            for item in top_items
        ])
        system_prompt = f"""
تو سردبیر ارشد بخش اخبار فوری هستی. برای "{edition_title}" یک خلاصه خبر ۳ دقیقه‌ای روان، ضربتی و بسیار جذاب به فارسی بنویس.
خروجی باید JSON زیر باشد:
{{
  "edition": "{edition_key}",
  "title": "{edition_title}",
  "time": "{tehran_time.strftime('%H:%M')}",
  "date": "{tehran_time.strftime('%Y/%m/%d')}",
  "bullets": ["نکته ۱", "نکته ۲", "نکته ۳", "نکته ۴"],
  "bottom_line": "نتیجه‌گیری در یک جمله کوتاه"
}}
"""
        try:
            resp = self.scraper.post(
                "https://gen.pollinations.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": "openai",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": news_text}
                    ],
                    "temperature": 0.2
                },
                timeout=45
            )
            if resp.status_code == 200:
                raw = resp.json()['choices'][0]['message']['content']
                clean = re.sub(r'```json\s*|```', '', raw).strip()
                data = json.loads(clean)
                self._atomic_json_dump('bulletins.json', data)
                logger.info(f">>> Scheduled Bulletin ({edition_title}) generated successfully.")
                return data
        except Exception as e:
            logger.error(f"Bulletin Generation Error: {e}")
        return None

    def generate_special_topic_report(self):
        if len(self.existing_news) < 5:
            return None
        tag_clusters = {}
        for item in self.existing_news[:30]:
            tag = item.get('tag', 'عمومی')
            tag_clusters.setdefault(tag, []).append(item)
        top_tag = max(tag_clusters, key=lambda k: len(tag_clusters[k]))
        cluster_items = tag_clusters[top_tag]
        if len(cluster_items) < 2:
            return None

        cluster_context = "\n---\n".join([
            f"منبع: {i.get('source')}\nتیتر: {i.get('title_fa')}\nتحلیل: {i.get('impact')}\nخلاصه: {' '.join(i.get('summary', []))}"
            for i in cluster_items[:6]
        ])
        system_prompt = """
تو تیم تحریریه پرونده‌های ویژه خبری هستی. بر اساس گزارش‌های ورودی که همگی درباره یک موضوع پرخبر امروز هستند، یک «پرونده ویژه اختصاصی» به فارسی روان، جذاب و تحلیل‌گرایانه بنویس.
خروجی باید JSON زیر باشد:
{
  "topic_tag": "موضوع پرونده",
  "headline": "تیتر اصلی و جذاب پرونده ویژه",
  "lead_paragraph": "مقدمه و اصل ماجرا در دو جمله بسیار روان",
  "key_findings": [
    "یافته و زاویه دید ۱",
    "یافته و زاویه دید ۲",
    "یافته و زاویه دید ۳"
  ],
  "regime_vs_reality": "مقایسه ادعای رسانه‌های حکومتی با واقعیت میدانی در یک پاراگراف",
  "strategic_outlook": "پیش‌بینی ادامه روند این پرونده در هفته آینده"
}
"""
        try:
            resp = self.scraper.post(
                "https://gen.pollinations.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": "openai",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"موضوع پرخبر: {top_tag}\n\nگزارش‌های هم‌زمان:\n{cluster_context}"}
                    ],
                    "temperature": 0.25
                },
                timeout=60
            )
            if resp.status_code == 200:
                raw = resp.json()['choices'][0]['message']['content']
                clean = re.sub(r'```json\s*|```', '', raw).strip()
                data = json.loads(clean)
                self._atomic_json_dump('special_reports.json', data)
                logger.info(f">>> Special Report on ({top_tag}) generated successfully.")
                return data
        except Exception as e:
            logger.error(f"Special Report Error: {e}")
        return None

    # ───────────────────────── main run ─────────────────────────

    def run(self):
        logger.info(">>> Radar Started (optimized search + extract)...")

        with open(CONFIG['FILES']['MARKET'], 'w', encoding='utf-8') as f:
            json.dump(self.fetch_market_rates(), f, ensure_ascii=False)

        manual_url = os.environ.get('MANUAL_URL')

        # --- 1. FETCH ---
        if manual_url and manual_url.strip():
            logger.info(f"!!! MANUAL MODE: {manual_url} !!!")
            results = self.fetch_manual_url(manual_url)
            candidates = results
        else:
            results = self.get_combined_news()
            candidates = []
            seen_batch_titles = set()
            cutoff_date = datetime.now(timezone.utc) - timedelta(hours=CONFIG['MAX_NEWS_AGE_HOURS'])

            for item in results:
                # Age filter
                try:
                    p_date = item.get('published date')
                    if p_date:
                        dt = parser.parse(p_date)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt < cutoff_date:
                            continue
                except Exception:
                    pass

                raw_url = item.get('url', '')
                clean_u = self._clean_url(raw_url)
                if clean_u in self.seen_urls:
                    continue

                t = item.get('title', '').rsplit(' - ', 1)[0]
                norm_t = self._normalize_text(t)
                th = self._title_hash(t)

                if norm_t in self.seen_titles or norm_t in seen_batch_titles:
                    continue
                if th in self.recent_title_hashes:
                    continue
                if self._is_duplicate_fuzzy(t, self.existing_news):
                    continue

                seen_batch_titles.add(norm_t)
                candidates.append(item)

            # Source priority + hard cap
            candidates.sort(
                key=lambda x: self._domain_score(
                    x.get('url'),
                    x.get('publisher', {}).get('title', '')
                ),
                reverse=True
            )
            candidates = candidates[:CONFIG.get('MAX_CANDIDATES', 15)]

        logger.info(
            f"Total Fetched: {len(results)} | Candidates (new/recent/capped): {len(candidates)}"
        )

        # --- 2. PROCESS (high-score first already ordered) ---
        new_processed_items = []
        if candidates:
            with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG['MAX_WORKERS']) as exc:
                futures = {exc.submit(self.process_item, i): i for i in candidates}
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        res = fut.result()
                        if res:
                            new_processed_items.append(res)
                            self.seen_urls.add(res['clean_url'])
                            self.recent_title_hashes.add(self._title_hash(res.get('title_en', '')))
                    except Exception as e:
                        logger.error(f"process_item worker error: {e}")

        # --- 3. SAVE & TELEGRAM ---
        if new_processed_items:
            self.existing_news = self.save_news(new_processed_items)

            telegram_items = []
            min_urgency = CONFIG['MIN_TELEGRAM_URGENCY']
            for item in new_processed_items:
                urgency = item.get('urgency', 0)
                tag = str(item.get('tag', '')).lower()
                is_conflict = any(w in tag for w in [
                    'war', 'conflict', 'military', 'strike', 'attack', 'nuclear',
                    'نظامی', 'حمله', 'هسته‌ای', 'نیابتی'
                ])
                if urgency >= min_urgency or (urgency >= 6 and is_conflict):
                    telegram_items.append(item)

            if telegram_items:
                logger.info(f"Sending {len(telegram_items)} items to Telegram.")
                self.send_digest_to_telegram(telegram_items)
            else:
                logger.info("New items saved, but urgency too low for Telegram.")
        else:
            logger.info(">>> No valid new items found.")

        # --- 4. DAILY / BULLETIN / SPECIAL ---
        daily_summary = self.generate_daily_summary()
        if daily_summary:
            self.save_daily_summary(daily_summary)

        self.generate_scheduled_bulletin()
        self.generate_special_topic_report()

        logger.info(
            f">>> Done. New={len(new_processed_items)} | "
            f"Failed hosts this run={len(self.failed_hosts)}"
        )


if __name__ == "__main__":
    IranNewsRadar().run()
