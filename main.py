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

# --- CONFIGURATION ---
CONFIG = {
    'SEARCH_QUERY': 'Iran AND (Israel OR USA OR nuclear OR conflict OR sanctions OR currency OR IRGC)',
    'TARGET_SOURCES': [
        'iranintl.com', 'bbc.com/persian', 'radiofarda.com', 'independentpersian.com',
        'dw.com/fa', 'presstv.ir', 'tasnimnews.com', 'farsnews.ir', 'irna.ir', 'mehrnews.com'
    ],
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
    'TIMEOUT': 20,
    'MAX_WORKERS': 4,
    'POLLINATIONS_KEY': os.environ.get('POLLINATIONS_API_KEY'),
    'AI_RETRIES': 3,
    'MIN_TELEGRAM_URGENCY': 7,
    'MAX_NEWS_AGE_HOURS': 24, # Drop news older than this
    'HISTORY_SIZE': 300       # Keep last 300 items in history
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
        self.scraper = cloudscraper.create_scraper(browser='chrome') 
        self.api_key = CONFIG['POLLINATIONS_KEY']
        self.existing_news = self._load_existing_news()
        
        self.seen_urls = set()
        self.seen_titles = set()
        
        # Populate history sets
        for item in self.existing_news:
            if item.get('url'):
                self.seen_urls.add(self._clean_url(item['url']))
            if item.get('title_en'):
                self.seen_titles.add(self._normalize_text(item['title_en']))
            if item.get('title_fa'):
                self.seen_titles.add(self._normalize_text(item['title_fa']))
        
        self.gnews_en = GNews(language='en', country='US', period='4h', max_results=5)

    def _load_previous_daily_summary(self):
        path = CONFIG['FILES']['DAILY_SUMMARY']
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return None

    def _clean_url(self, url):
        """Removes query parameters to prevent duplicates based on ?utm_source etc."""
        if not url: return ""
        try:
            parsed = urlparse(url)
            # Rebuild url without query params
            clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
            return clean.rstrip('/')
        except:
            return url

    def _normalize_text(self, text):
        if not text: 
            return ""
        # 1. Unify Persian/Arabic letter variants (ی/ي, ک/ك)
        text = text.replace('ي', 'ی').replace('ك', 'ک')
        # 2. Convert ZWNJ (\u200c) to standard space to treat split words uniformly
        text = text.replace('\u200c', ' ')
        # 3. Lowercase and remove all punctuation/special characters
        clean = re.sub(r'[^\w\s]', '', text.lower())
        # 4. Collapse whitespace
        return re.sub(r'\s+', '', clean)

    def _get_tokens(self, text):
        if not text: 
            return set()
        
        # Stopwords: English + Persian
        stop_words = {
            # English
            'a', 'an', 'the', 'and', 'or', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'is', 'are', 'was', 'news', 'report', 'breaking',
            # Persian
            'از', 'به', 'در', 'که', 'و', 'این', 'آن', 'را', 'برای', 'با', 'است', 'شد', 'شده', 'می', 'بر', 'یک', 'خود', 'تا', 'کرد', 'برای', 'نیز'
        }
        
        # Normalize characters and handle ZWNJ
        text = text.replace('ي', 'ی').replace('ك', 'ک').replace('\u200c', ' ')
        clean = re.sub(r'[^\w\s]', '', text.lower())
        words = set(clean.split())
        
        return words - stop_words

    def _is_duplicate_fuzzy(self, new_title, comparison_pool):
        norm_title = self._normalize_text(new_title)
        if norm_title in self.seen_titles: return True
        
        new_tokens = self._get_tokens(new_title)
        if len(new_tokens) < 3: return False # Too short to judge

        for item in comparison_pool:
            existing_title = item.get('title_en', item.get('title', ''))
            existing_tokens = self._get_tokens(existing_title)
            
            if not existing_tokens: continue
            
            intersection = new_tokens.intersection(existing_tokens)
            union = new_tokens.union(existing_tokens)
            
            if not union: continue
            similarity = len(intersection) / len(union)
            
            # If 50% similar words, it's a duplicate
            if similarity > 0.5:
                return True
        return False

    def _load_existing_news(self):
        if not os.path.exists(CONFIG['FILES']['NEWS']): return []
        try:
            with open(CONFIG['FILES']['NEWS'], 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except: return []

    # --- PROXIES & MARKET ---
    def fetch_best_proxies(self):
        try:
            resp = self.scraper.get(CONFIG['PROXY_URL'], timeout=10)
            if resp.status_code != 200: return []
            data = resp.json()
            online = [p for p in data if p.get('status') == 'Online']
            online.sort(key=lambda x: x.get('latency') if x.get('latency') is not None else 99999)
            return online[:9]
        except: return []

    def fetch_market_rates(self):
        data = {"usd": "نامشخص", "oil": "نامشخص", "updated": "--:--"}
        try:
            resp = self.scraper.get("https://alanchand.com/en/currencies-price/usd", timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                usd = soup.find('input', attrs={'data-curr': 'tmn'})
                if usd:
                    val = usd.get('data-price') or usd.get('value')
                    if val: data["usd"] = f"{int(int(val.replace(',', '')) / 10):,}"
        except: pass
        try:
            resp = self.scraper.get("https://oilprice.com/oil-price-charts/46", timeout=10)
            soup = BeautifulSoup(resp.text, 'html.parser')
            oil = soup.select_one(".last_price")
            if oil: data["oil"] = oil.get_text().strip()
        except: pass
        data["updated"] = time.strftime("%H:%M")
        return data

    # --- NEWS FETCHING ---
    def fetch_gnews(self):
        results = []
        try:
            results = self.gnews_en.get_news(CONFIG['SEARCH_QUERY'])
        except Exception as e:
            logger.error(f"GNews Error: {e}")
        return results

    def fetch_duckduckgo(self, query, region='wt-wt'):
        results = []
        try:
            ddgs = DDGS()
            # Changed timelimit to 'd' (day)
            ddg_gen = ddgs.news(query=query, region=region, safesearch="off", timelimit="d", max_results=10)
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
            logger.error(f"DDG Error ({query}): {e}")
        return results

    def fetch_bing_rss(self, query):
        results = []
        try:
            encoded_query = quote(query)
            url = f"https://www.bing.com/news/search?q={encoded_query}&format=rss"
            feed = feedparser.parse(url)
            
            for entry in feed.entries:
                publisher = "Bing News"
                if hasattr(entry, 'news_source'): publisher = entry.news_source
                elif hasattr(entry, 'source') and hasattr(entry.source, 'title'): publisher = entry.source.title

                final_link = entry.link
                if "apiclick.aspx" in final_link:
                    match = re.search(r'[?&]url=([^&]+)', final_link)
                    if match: final_link = unquote(match.group(1))

                image_url = None
                try:
                    if hasattr(entry, 'news_image'):
                        raw_url = entry.news_image
                        if '{0}' in raw_url:
                            image_url = raw_url.replace('{0}', '700').replace('{1}', '400')
                        else:
                            image_url = raw_url
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

    # --- MANUAL URL ---
    def fetch_manual_url(self, url):
        try:
            resp = self.scraper.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')
            title = "Unknown Title"
            if soup.title: title = soup.title.string
            og_title = soup.find("meta", property="og:title")
            if og_title: title = og_title.get("content")
            
            publisher = "Manual Source"
            og_site = soup.find("meta", property="og:site_name")
            if og_site: publisher = og_site.get("content")
            
            image = None
            og_image = soup.find("meta", property="og:image")
            if og_image: image = og_image.get("content")

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
        all_entries = []
        all_entries.extend(self.fetch_gnews())
        all_entries.extend(self.fetch_bing_rss(CONFIG['SEARCH_QUERY']))
        all_entries.extend(self.fetch_duckduckgo(CONFIG['SEARCH_QUERY'], region='wt-wt'))
        
        # Reduced external sites to prevent timeout, focus on quality
        for domain in CONFIG['TARGET_SOURCES'][:5]: 
            try:
                query = f"site:{domain} Iran"
                if any(x in domain for x in ['tasnim', 'fars', 'irna', 'bbc.com', 'radiofarda']):
                    query = f"site:{domain} ایران"
                site_res = self.fetch_duckduckgo(query, region='wt-wt')
                all_entries.extend(site_res)
                time.sleep(0.5) 
            except: pass
        return all_entries

    # --- PROCESSING ---
    def _resolve_final_url(self, url, raw_title=None):
        if not url: return None
        
        # If it's not a Google News URL, return it immediately
        if "news.google.com" not in url: 
            return url
            
        # 1. Try standard redirect first (sometimes Google lets it through)
        try:
            resp = self.scraper.get(url, allow_redirects=True, timeout=8, stream=True)
            # Make sure we didn't just get redirected to a Google Consent or Error page
            if "news.google.com" not in resp.url and "consent.google.com" not in resp.url:
                return resp.url
        except Exception:
            pass

        # 2. WORKAROUND: If Google blocked us, use Bing RSS to find the real article link
        if raw_title:
            logger.info(f"GNews blocked URL resolution. Searching Bing for: {raw_title[:40]}...")
            
            # Use our existing Bing RSS function to search the exact title
            bing_results = self.fetch_bing_rss(raw_title)
            
            if bing_results:
                # Take the URL of the first matching result from Bing
                bing_url = bing_results[0]['url']
                logger.info(f"Bing Workaround Success! Found: {bing_url}")
                return bing_url

        # Fallback to the original Google URL if everything fails
        return url

    def scrape_article_text(self, final_url, fallback_snippet):
        """Extracts main content using Trafilatura with a BeautifulSoup fallback."""
        if not final_url or final_url.lower().endswith('.pdf'):
            return fallback_snippet

        try:
            # 1. Primary Method: Trafilatura (Best for removing ads, navs, and boilerplate)
            downloaded = trafilatura.fetch_url(final_url)
            if downloaded:
                extracted_text = trafilatura.extract(
                    downloaded, 
                    include_links=False, 
                    include_comments=False,
                    output_format='txt'
                )
                if extracted_text and len(extracted_text.strip()) > 120:
                    clean_text = re.sub(r'\s+', ' ', extracted_text).strip()
                    return clean_text[:2500]

            # 2. Fallback Method: BeautifulSoup
            resp = self.scraper.get(final_url, timeout=12)
            soup = BeautifulSoup(resp.text, 'html.parser')
            for tag in soup(["script", "style", "nav", "footer", "header", "form", "iframe"]):
                tag.extract()
            
            article_body = soup.find('div', class_=re.compile(r'(article|story|body|content|entry)'))
            text = article_body.get_text(separator=' ').strip() if article_body else " ".join([p.get_text().strip() for p in soup.find_all('p')])
            clean_text = re.sub(r'\s+', ' ', text)
            
            return clean_text[:2500] if len(clean_text) > 100 else fallback_snippet

        except Exception as e:
            logger.warning(f"Extraction failed for {final_url}: {e}")
            return fallback_snippet

    def analyze_with_ai(self, headline, full_text, source_name):
        if not self.api_key: return None
        
        is_regime = any(x in source_name.lower() for x in ['tasnim', 'fars', 'irna', 'presstv', 'mehr'])
        
        regime_instruction = ""
        if is_regime:
            regime_instruction = "CRITICAL: The source is Iranian State Media. Expose propaganda. "

        system_prompt = (
            "تو یک تحلیل‌گر ارشد و تیزبین ژئوپلیتیک، مسلط به ادبیات کانال‌های تحلیلی تلگرام فارسی (مانند تحلیل‌گران مستقل و اپوزیسیون ایرانی) هستی.\n"
            "وظیفه تو تبدیل اخبار خام به تحلیل‌های کوتاه، ضربتی، کاملاً انسانی، به فارسی روان و بدون «بوی هوش مصنوعی» است.\n\n"

            "🔴 قوانین حیاتی نگارش و انسانی‌سازی (مهم - حتماً رعایت شود):\n"
            "۱. **روانی، شفافیت و سادگی زبان (مهم):**\n"
            "   - از کلمات قلم‌به‌سلم، پیچیده و عجیب دانشگاهی (مثل: 'گره مشخص'، 'تعمیم روایت'، 'شکل‌گیری محاسبات') مطلقاً استفاده نکن.\n"
            "   - **ممنوعیت ترجمه تحت‌اللفظی:** عبارات انگلیسی را کلمه به کلمه ترجمه نکن (مثلاً اصطلاح 'drone threat' را 'تهدید پهپادی' بنویس، نه 'تهدید پرنده'!).\n"
            "   - جملات باید بسیار روان، صریح و شفاف باشند تا مخاطب با یک‌بار خواندن متوجه اصل ماجرا شود.\n\n"

            "۲. **ممنوعیت مطلق عبارت‌های کلیشه‌ای رباتیک:**\n"
            "   استفاده از این عبارات مطلقاً ممنوع است: ('به نظر می‌رسد'، 'نشان‌دهنده این است که'، 'لازم به ذکر است'، 'در نهایت'، 'پیامدهای عمیق'، 'ابعاد جدیدی از'، 'در این راستا'، 'شایان ذکر است').\n\n"

            "۳. **تنوع در ساختار جملات:**\n"
            "   جملات نباید همه با یک فرمول شروع شوند. گاهی با یک فعل حاد، گاهی با یک آمار، و گاهی با یک ارزیابی مستقیم شروع کن.\n\n"

            "۴. **تعداد نقطه‌نظرات شناور:**\n"
            "   بخش summary می‌تواند بین ۲ تا ۴ مورد باشد. اگر خبر کوتاه است ۲ نکته عمیق و روان کافیست، برای خبرهای مهم ۴ نکته بنویس. خودت را به ۳ نقطه اجباری محدود نکن.\n\n"

            "۵. **حذف القاب رسمی و حاکمیتی:**\n"
            "   از القاب مانند (آیت‌الله، سردار، شهید، حجت‌الاسلام، عمومی) استفاده نکن. فقط نام و سمت رسمی.\n\n"

            "۶. **تغییر لحن بر اساس اهمیت (Urgency):**\n"
            "   - اگر خبر نظامی/فوریت بالاست (۸ تا ۱۰): لحن ضربتی، کوتاه و صریح باشد.\n"
            "   - اگر خبر اقتصادی/سیاسی است (۴ تا ۷): لحن تحلیلی و افشاگرانه باشد.\n\n"

            "قواعد امتیازبندی فوریت (Urgency Score 1-10):\n"
            "- 9-10: درگیری مستقیم نظامی، کشته شدن مقامات ارشد، ضربه به تاسیسات اتمی/نظامی.\n"
            "- 7-8: تحریم‌های خفه کننده جدید، سقوط شدید ارزی، اعتراضات سراسری، حملات نیابتی سنگین.\n"
            "- 4-6: تحرکات دیپلماتیک مهم، تنش‌های لفظی مسئولان، مانورهای منطقه‌ای.\n"
            "- 1-3: اظهارات routine، دیدارهای تشریفاتی.\n\n"

            "فرمت خروجی باید دکارتی و دقیقاً ساختار JSON زیر باشد:\n"
            "{\n"
            '  "title_fa": "تیتر جذاب، روان، غیرتکراری و بدون کلمات خنثی (حداکثر ۱۰ کلمه)",\n'
            '  "summary": ["نکته تحلیلی ۱ به فارسی روان و بدون کلمات اضافه", "نکته تحلیلی ۲ با تمرکز بر واقعیت پشت خبر"],\n'
            '  "impact": "تأثیر عملیاتی یا اقتصادی خبر در یک جمله کوتاه، روان و ضربتی",\n'
            '  "tag": "کلمه کلیدی اصلی (مثلاً: نظامی، ارز، تحریم، نیابتی)",\n'
            '  "urgency": عدد بین 1 تا 10,\n'
            '  "sentiment": عدد بین -1.0 تا 1.0\n'
            "}"
        )

        current_text = full_text

        for attempt in range(CONFIG['AI_RETRIES']):
            try:
                if attempt > 0: current_text = headline + " " + full_text[:800]
                
                resp = self.scraper.post(
                    "https://gen.pollinations.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={
                        "model": "openai",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": f"SOURCE: {source_name}\nHEADLINE: {headline}\nTEXT: {current_text}"}
                        ],
                        "temperature": 0.25 
                    }, timeout=45
                )
                
                if resp.status_code == 200:
                    raw = resp.json()['choices'][0]['message']['content']
                    clean = re.sub(r'```json\s*|```', '', raw).strip()
                    data = json.loads(clean)
                    if 'title_fa' in data and 'summary' in data: return data
                time.sleep(1)
            except Exception as e:
                logger.error(f"AI Attempt {attempt+1} failed: {e}")
                time.sleep(2)

        return None

    def generate_daily_summary(self):
        """
        Generate a rolling strategic daily summary.
        Uses:
        - All today's news
        - Previous run's daily summary (if exists)
        """

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
        todays_items = [
            item for item in self.existing_news
            if datetime.fromtimestamp(item.get("timestamp", 0), timezone.utc) >= today_start
        ]

        if len(todays_items) < 3:
            return None

        todays_items.sort(key=lambda x: x.get("urgency", 0), reverse=True)

        # Build structured context
        news_context = []
        for item in todays_items[:20]:  # limit context size
            news_context.append(
                f"""
Title: {item.get('title_en')}
Source: {item.get('source')}
Urgency: {item.get('urgency')}
Tag: {item.get('tag')}
Impact: {item.get('impact')}
Summary: {' '.join(item.get('summary', []))}
"""
            )

        news_block = "\n\n".join(news_context)

        previous_summary = self._load_previous_daily_summary()
        previous_block = ""

        if previous_summary:
            previous_block = f"""
Previous Strategic Assessment:
Themes: {previous_summary.get('themes')}
Strategic Assessment: {previous_summary.get('strategic_assessment')}
Market Impact: {previous_summary.get('market_impact')}
Risk Level: {previous_summary.get('risk_level')}
"""

        return self.analyze_daily_summary_with_ai(news_block, previous_block)

    def process_item(self, entry):
        # We extract the title and strip publisher names (e.g. " - BBC News") for cleaner Bing searching
        raw_title = entry.get('title', '').rsplit(' - ', 1)[0].strip()
        publisher = entry.get('publisher', {}).get('title', 'Unknown')
        
        # Pass the raw_title to the resolver to enable the Bing workaround
        final_url = self._resolve_final_url(entry.get('url'), raw_title)
        clean_final_url = self._clean_url(final_url)

        if not os.environ.get('MANUAL_URL'):
            if clean_final_url in self.seen_urls:
                return None
            if self._is_duplicate_fuzzy(raw_title, self.existing_news):
                return None

        logger.info(f"Processing: {publisher} | {raw_title[:20]}...")
        
        snippet = entry.get('description', raw_title)
        
        # Now final_url should be a direct website link, allowing scrape_article_text to actually work!
        text = self.scrape_article_text(final_url, snippet)
        
        ai = self.analyze_with_ai(raw_title, text, publisher)
        if not ai: return None
        
        try: urgency_val = int(ai.get('urgency', 3))
        except: urgency_val = 3

        try: ts = parser.parse(entry.get('published date')).timestamp()
        except: ts = time.time()

        return {
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
            "image": entry.get('image'),
            "timestamp": ts
        }

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
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "openai",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": f"""
TODAY NEWS:
{news_block}

PREVIOUS SUMMARY:
{previous_block}
"""
                        }
                    ],
                    "temperature": 0.2
                },
                timeout=60
            )

            if resp.status_code == 200:
                raw = resp.json()['choices'][0]['message']['content']
                clean = re.sub(r'```json\s*|```', '', raw).strip()
                data = json.loads(clean)
                return data

        except Exception as e:
            logger.error(f"Daily Summary AI Error: {e}")

        return None
    
    def send_digest_to_telegram(self, items):
        token = CONFIG['TELEGRAM']['BOT_TOKEN']
        chat_id = CONFIG['TELEGRAM']['CHANNEL_ID']
        if not token or not chat_id or not items: 
            return

        # 1. Sort items by urgency (highest urgency first)
        items.sort(key=lambda x: x.get('urgency', 3), reverse=True)

        # 2. Build Rich Market Data Table
        market_html = ""
        try:
            with open(CONFIG['FILES']['MARKET'], 'r') as f: 
                mkt = json.load(f)
            market_html = (
                f"<table bordered striped>\n"
                f"  <tr><th>💵 دلار</th><th>🛢 نفت</th><th>⏱ زمان</th></tr>\n"
                f"  <tr><td align='center'>{mkt.get('usd', '---')}</td>"
                f"<td align='center'>{mkt.get('oil', '---')}</td>"
                f"<td align='center'>{mkt.get('updated', '--:--')}</td></tr>\n"
                f"</table>\n\n"
            )
        except Exception: 
            market_html = ""

        # 3. Build Active Proxies List (FIXED LINK FORMATTING)
        proxies = self.fetch_best_proxies()[:4] 
        proxy_html = ""
        if proxies:
            proxy_items = []
            names_pool = random.sample(PROXY_NAMES, min(len(proxies), len(PROXY_NAMES)))
            for i, p in enumerate(proxies):
                proxy_name = names_pool[i]
                latency = p.get('latency', '?')
                
                # FIX: Unescape &amp; back to literal & for Telegram deep links
                raw_tg_url = p.get('tg_url', '#')
                clean_tg_url = html.unescape(raw_tg_url).replace('&amp;', '&')
                
                proxy_items.append(
                    f"<li>🛡 <a href='{clean_tg_url}'>{proxy_name}</a> (<code>{latency}ms</code>)</li>"
                )
            
            proxy_html = (
                "<details>\n"
                "<summary>🌐 <b>پروکسی‌های فعال تلگرام (کلیک کنید)</b></summary>\n"
                "<ul>" + "".join(proxy_items) + "</ul>\n"
                "</details>\n\n"
            )

        # 4. Find Link Preview Image
        preview_url = ""
        for item in items:
            img = item.get('image', '')
            if img and isinstance(img, str) and not img.startswith('data:'):
                preview_url = img
                break
        if not preview_url and items:
            preview_url = items[0].get('url', '')

        hidden_preview = f"<a href='{preview_url}'>&#8205;</a>" if preview_url else ""

        # 5. Header Section
        # --- TIME CALCULATOR (Iran Standard Time: Asia/Tehran) ---
        try:
            from zoneinfo import ZoneInfo
            ir_tz = ZoneInfo("Asia/Tehran")
        except ImportError:
            # Fallback if zoneinfo is not available (Iran is permanently at UTC+3:30)
            ir_tz = timezone(timedelta(hours=3, minutes=30))

        now_ir = datetime.now(ir_tz)
        
        # Format time and convert digits to Farsi (e.g. 14:30 -> ۱۴:۳۰)
        ir_time_str = to_farsi_num(now_ir.strftime("%H:%M"))
        ir_date_str = to_farsi_num(now_ir.strftime("%Y/%m/%d"))

        # 5. Header Section with Iran Time
        header = (
            f"{hidden_preview}"
            f"<h1>🚨 رادار اخبار مهم ایران</h1>\n"
            f"<p>⏱ <b>زمان بروزرسانی:</b> {ir_time_str} (به وقت تهران) | 📅 {ir_date_str}</p>\n"
            f"{market_html}"
            f"<hr/>\n\n"
        )

        # Helper function for Farsi Numbers
        def to_farsi_num(num):
            return str(num).translate(str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹'))

        # 6. Build Collapsible Analysis Blocks (<details><summary>)
        items_html = ""
        all_tags = set()

        for i, item in enumerate(items, 1):
            title = html.escape(str(item.get('title_fa', item.get('title_en'))))
            url = item.get('url', '#')
            source = html.escape(str(item.get('source', 'Unknown')))
            
            is_regime = any(x in source.lower() for x in ['tasnim', 'fars', 'irna', 'presstv', 'mehr'])
            if is_regime: 
                source += " 🚫"

            urgency = item.get('urgency', 3)
            icon = "🔥" if urgency >= 9 else ("🚨" if urgency >= 7 else "🔹")

            impact = html.escape(str(item.get('impact', '')))
            
            summary_raw = item.get('summary', [])
            if isinstance(summary_raw, str): 
                summary_raw = [summary_raw]
            
            safe_summary = "".join([f"<li>{html.escape(str(s))}</li>" for s in summary_raw])
            
            tag = str(item.get('tag', 'General')).replace(' ', '_')
            all_tags.add(f"#{html.escape(tag)}")

            # First item expanded (<details open>), remaining items collapsed (<details>)
            is_open = " open" if i == 1 else ""

            items_html += (
                f"<details{is_open}>\n"
                f"  <summary>{icon} <b>{to_farsi_num(i)}. {title}</b> (<i>{source}</i>)</summary>\n"
                f"  <p><b>📝 تحلیل خبر:</b></p>\n"
                f"  <ul>{safe_summary}</ul>\n"
                f"  <p>🎯 <b>اثرگذاری:</b> {impact}</p>\n"
                f"  <p>🔗 <a href='{url}'>مشاهده منبع خبر</a></p>\n"
                f"</details>\n"
                f"<hr/>\n"
            )

        # 7. Tags & Footer
        tags_html = f"<p>{' '.join(all_tags)}</p>\n"
        footer = (
            f"<footer>"
            f"{proxy_html}"
            f"🆔 @RasadAIOfficial<br/>"
            f"📊 <a href='https://itsyebekhe.github.io/rasadai/'>مشاهده پایگاه داده رادار</a>"
            f"</footer>"
        )

        full_rich_html = header + items_html + tags_html + footer

        # 8. Send Rich Message Payload via API
        api_url = f"https://api.telegram.org/bot{token}/sendRichMessage"
        
        # Max limit for sendRichMessage is 32,768 UTF-8 characters
        if len(full_rich_html) > 30000:
            logger.warning("Rich message exceeds 30k chars, truncating old items...")
            full_rich_html = full_rich_html[:30000] + "<footer>...</body>"

        payload = {
            "chat_id": chat_id,
            "rich_message": {
                "html": full_rich_html,
                "is_rtl": True  # Enforces Persian Right-To-Left Rendering!
            }
        }

        try:
            resp = self.scraper.post(api_url, json=payload, timeout=20)
            if resp.status_code == 200:
                logger.info(">>> Rich Message successfully posted to Telegram.")
            else:
                logger.error(f"Telegram Rich Message Failed: Status {resp.status_code} | {resp.text}")
        except Exception as e:
            logger.error(f"TG Send Error: {e}")

    def _atomic_json_dump(self, file_path, data):
        """Safely writes JSON to a temp file first, then atomically replaces target file."""
        dir_name = os.path.dirname(file_path) or '.'
        try:
            with tempfile.NamedTemporaryFile('w', dir=dir_name, delete=False, encoding='utf-8') as tf:
                json.dump(data, tf, indent=4, ensure_ascii=False)
                temp_name = tf.name
            os.replace(temp_name, file_path)  # Safe atomic swap
        except Exception as e:
            logger.error(f"Atomic dump failed for {file_path}: {e}")
            if 'temp_name' in locals() and os.path.exists(temp_name):
                os.remove(temp_name)

    def save_news(self, new_items):
        """Merges new items with old items and saves safely."""
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
            
            # --- USE ATOMIC DUMP HERE ---
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
            # --- USE ATOMIC DUMP HERE ---
            self._atomic_json_dump(CONFIG['FILES']['DAILY_SUMMARY'], summary)
            logger.info(">>> daily_summary.json updated successfully.")
        except Exception as e:
            logger.error(f"Failed to save daily summary: {e}")

    def run(self):
        logger.info(">>> Radar Started...")
        
        # Update Market Data
        with open(CONFIG['FILES']['MARKET'], 'w') as f: 
            json.dump(self.fetch_market_rates(), f)

        manual_url = os.environ.get('MANUAL_URL')
        
        # --- 1. FETCHING ---
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
                # 1. Check Date
                try:
                    p_date = item.get('published date')
                    if p_date:
                        dt = parser.parse(p_date)
                        # Make naive datetime aware (assume UTC if missing)
                        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                        if dt < cutoff_date: continue # SKIP OLD NEWS
                except: pass # If date parse fails, assume recent

                # 2. Check Deduplication
                raw_url = item.get('url', '')
                clean_u = self._clean_url(raw_url)
                if clean_u in self.seen_urls: continue

                t = item.get('title', '').rsplit(' - ', 1)[0]
                norm_t = self._normalize_text(t)
                
                if norm_t in self.seen_titles: continue
                if norm_t in seen_batch_titles: continue
                if self._is_duplicate_fuzzy(t, self.existing_news): continue

                seen_batch_titles.add(norm_t)
                candidates.append(item)

        logger.info(f"Total Fetched: {len(results)} | Candidates (New & Recent): {len(candidates)}")

        # --- 2. PROCESSING ---
        new_processed_items = []
        if candidates:
            with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG['MAX_WORKERS']) as exc:
                futures = {exc.submit(self.process_item, i): i for i in candidates}
                for fut in concurrent.futures.as_completed(futures):
                    res = fut.result()
                    if res:
                        new_processed_items.append(res)
                        # Add to seen immediately so we don't double process if logic expands
                        self.seen_urls.add(res['clean_url'])

        # --- 3. SAVING & SENDING ---
        if new_processed_items:
            # SAVE FIRST to prevent duplicates if sending fails
            self.existing_news = self.save_news(new_processed_items)
            
            # Prepare Telegram List
            telegram_items = []
            min_urgency = CONFIG['MIN_TELEGRAM_URGENCY']
            
            for item in new_processed_items:
                urgency = item.get('urgency', 0)
                tag = str(item.get('tag', '')).lower()
                is_conflict = any(w in tag for w in ['war', 'conflict', 'military', 'strike', 'attack', 'nuclear'])
                
                if urgency >= min_urgency:
                    telegram_items.append(item)
                elif urgency >= 6 and is_conflict:
                    telegram_items.append(item)

            if telegram_items:
                logger.info(f"Sending {len(telegram_items)} items to Telegram.")
                self.send_digest_to_telegram(telegram_items)
            else:
                logger.info("New items saved, but urgency too low for Telegram.")
        else:
            logger.info(">>> No valid new items found.")

        # --- 4. GENERATE ROLLING DAILY SUMMARY ---
        daily_summary = self.generate_daily_summary()
        if daily_summary:
            self.save_daily_summary(daily_summary)

if __name__ == "__main__":
    IranNewsRadar().run()
