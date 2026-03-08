# -*- coding: utf-8 -*-
import requests as req
from ebooklib import epub
from tqdm import tqdm
import json
import time
import random
import os
import sys
import concurrent.futures
from typing import Callable, Optional, Dict, List, Tuple
from dataclasses import dataclass
from enum import Enum


class SaveMode(Enum):
    SINGLE_TXT = 1
    EPUB = 2


@dataclass
class Config:
    kg: int = 0
    kgf: str = '　'
    delay: List[int] = None
    save_path: str = './downloads'
    save_mode: SaveMode = SaveMode.EPUB
    xc: int = 8  # Giảm luồng để tránh bị chặn

    def __post_init__(self):
        if self.delay is None:
            self.delay = [200, 500]  # Delay dài hơn để tránh rate limit


class NovelDownloader:
    def __init__(self, config: Config, log_callback: Optional[Callable] = None):
        self.config = config
        self.log = log_callback or print

        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://fanqienovel.com/',
        }

        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.data_dir = os.path.join(self.script_dir, 'data')
        self.bookstore_dir = os.path.join(self.data_dir, 'bookstore')
        self.cookie_path = os.path.join(self.data_dir, 'cookie.json')

        self.CODE = [[58344, 58715], [58345, 58716]]

        charset_path = os.path.join(self.script_dir, 'charset.json')
        with open(charset_path, 'r', encoding='UTF-8') as f:
            self.charset = json.load(f)

        self._setup_directories()
        self.cookie = self._load_cookie()
        # Gán cookie vào headers
        self.headers['Cookie'] = self.cookie

    def _setup_directories(self):
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.bookstore_dir, exist_ok=True)
        os.makedirs(self.config.save_path, exist_ok=True)

    # ──────────────────────────────────────────────
    # COOKIE: ưu tiên env → file → tạo mới
    # ──────────────────────────────────────────────
    def _load_cookie(self) -> str:
        # 1. Đọc từ biến môi trường (GitHub Secret)
        env_cookie = os.environ.get('FANQIE_COOKIE', '').strip()
        if env_cookie:
            self.log('✓ Dùng cookie từ GitHub Secret (FANQIE_COOKIE).')
            self._save_cookie(env_cookie)
            return env_cookie

        # 2. Đọc từ file cache
        if os.path.exists(self.cookie_path):
            try:
                with open(self.cookie_path, 'r', encoding='UTF-8') as f:
                    cookie = json.load(f)
                if cookie:
                    self.log('✓ Dùng cookie từ file cache.')
                    return cookie
            except Exception:
                pass

        # 3. Tạo cookie ngẫu nhiên (fallback — thường không hoạt động với nội dung có phí)
        self.log('⚠ Không tìm thấy cookie thật, tạo cookie ngẫu nhiên (có thể không tải được).')
        return self._generate_cookie()

    def _generate_cookie(self) -> str:
        bas = 1000000000000000000
        cookie = f'novel_web_id={random.randint(bas * 7, bas * 8)}'
        self._save_cookie(cookie)
        return cookie

    def _save_cookie(self, cookie: str):
        try:
            with open(self.cookie_path, 'w', encoding='UTF-8') as f:
                json.dump(cookie, f)
        except Exception:
            pass

    # ──────────────────────────────────────────────
    # API - LẤY THÔNG TIN TRUYỆN
    # ──────────────────────────────────────────────
    def _get_book_info(self, novel_id: str) -> Tuple[str, str, str]:
        urls = [
            f'https://fanqienovel.com/api/reader/info?bookId={novel_id}',
            f'https://fanqienovel.com/api/author/book/info?bookId={novel_id}',
        ]
        for url in urls:
            try:
                resp = req.get(url, headers=self.headers, timeout=15)
                data = resp.json()
                if data.get('code') == 0:
                    d = data.get('data', {})
                    name = (d.get('bookName') or d.get('book_name') or d.get('name') or '').strip()
                    author = (d.get('authorName') or d.get('author_name') or d.get('author') or '').strip()
                    status = str(d.get('bookStatus') or d.get('book_status') or '?')
                    if name:
                        return name, author, status
            except Exception:
                continue
        return '', '', '?'

    def _get_chapter_list(self, novel_id: str):
        url = f'https://fanqienovel.com/api/reader/directory/detail?bookId={novel_id}'
        try:
            resp = req.get(url, headers=self.headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()

            if data.get('code') != 0:
                self.log(f'API lỗi: code={data.get("code")}')
                return 'err', '', {}, []

            raw = data.get('data', {})
            name, author, status = self._get_book_info(novel_id)

            if isinstance(raw, dict) and 'allItemIds' in raw:
                all_ids = [str(i).strip() for i in (raw.get('allItemIds') or []) if i]
                self.log(f'Tìm thấy {len(all_ids)} chương')
                chapters = {str(i): ch_id for i, ch_id in enumerate(all_ids)}
                return name or 'Unknown', author, chapters, [status]

            elif isinstance(raw, list):
                chapters = {}
                idx = 0
                for item in raw:
                    if isinstance(item, dict):
                        for ch in (item.get('chapterList') or []):
                            if isinstance(ch, dict):
                                ch_id = str(ch.get('chapterId') or '')
                                if ch_id:
                                    chapters[str(idx)] = ch_id
                                    idx += 1
                return name or 'Unknown', author, chapters, [status]

            elif isinstance(raw, dict):
                if not name:
                    name = raw.get('bookName') or 'Unknown'
                chapters = {}
                idx = 0
                for vol in (raw.get('chapterListWithVolume') or []):
                    if isinstance(vol, dict):
                        for ch in (vol.get('chapterList') or []):
                            if isinstance(ch, dict):
                                ch_id = str(ch.get('chapterId') or '')
                                if ch_id:
                                    chapters[str(idx)] = ch_id
                                    idx += 1
                return name or 'Unknown', author, chapters, [status]

            else:
                self.log(f'Cấu trúc API lạ: {type(raw)}')
                return 'err', '', {}, []

        except req.Timeout:
            self.log('Timeout lấy danh sách chương')
            return 'err', '', {}, []
        except Exception as e:
            self.log(f'Lỗi: {e}')
            import traceback
            self.log(traceback.format_exc())
            return 'err', '', {}, []

    # ──────────────────────────────────────────────
    # API - TẢI CHƯƠNG (dùng API chính chủ Fanqie)
    # ──────────────────────────────────────────────
    def _fetch_chapter(self, chapter_id: str) -> Optional[Tuple[str, str]]:
        import re as re_module

        def clean_html(raw):
            raw = re_module.sub(r'<header>.*?</header>', '', raw, flags=re_module.DOTALL)
            raw = re_module.sub(r'<footer>.*?</footer>', '', raw, flags=re_module.DOTALL)
            raw = re_module.sub(r'</?article>', '', raw)
            raw = re_module.sub(r'<p id="\d+">', '\n', raw)
            raw = re_module.sub(r'</p>', '', raw)
            raw = re_module.sub(r'<[^>]+>', '', raw)
            return re_module.sub(r'\n{3,}', '\n\n', raw).strip()

        # API 1: fanqienovel.com chính chủ (cần cookie)
        apis = [
            f'https://fanqienovel.com/api/reader/full?itemIds={chapter_id}',
            f'https://fanqienovel.com/api/reader/chapter/full?chapterId={chapter_id}',
            f'https://fanqienovel.com/content/{chapter_id}',
        ]

        for api_url in apis:
            try:
                resp = req.get(api_url, headers=self.headers, timeout=15)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                if data.get('code') == 0:
                    d = data.get('data', {})
                    # Thử các key nội dung phổ biến
                    content_raw = (
                        d.get('chapterData') or
                        d.get('content') or
                        d.get('chapterContent') or
                        (d.get('chapterDataList') or [{}])[0].get('chapterData', '') if isinstance(d.get('chapterDataList'), list) else ''
                    )
                    title = (
                        d.get('chapterTitle') or
                        d.get('title') or
                        (d.get('chapterDataList') or [{}])[0].get('chapterTitle', '') if isinstance(d.get('chapterDataList'), list) else ''
                    )
                    if content_raw:
                        decoded = self._decode_content(clean_html(str(content_raw)))
                        return (title or '').strip(), decoded
            except Exception as e:
                self.log(f'DEBUG api err {chapter_id} [{api_url}]: {e}')
                continue

        return None

    def _decode_content(self, content: str) -> str:
        result = []
        for char in content:
            code = ord(char)
            decoded = False
            for r in self.CODE:
                if r[0] <= code <= r[1]:
                    idx = code - r[0]
                    if idx < len(self.charset):
                        result.append(self.charset[idx])
                        decoded = True
                        break
            if not decoded:
                result.append(char)
        return ''.join(result)

    def _download_chapter(self, idx: str, ch_id: str, existing: Dict) -> Optional[Tuple[int, str, str]]:
        if ch_id in existing:
            title, content = existing[ch_id]
            return int(idx), title, content

        for attempt in range(4):
            result = self._fetch_chapter(ch_id)
            if result:
                title, content = result
                time.sleep(random.randint(self.config.delay[0], self.config.delay[1]) / 1000)
                return int(idx), title, content
            wait = (attempt + 1) * 2
            self.log(f'⚠ Retry {attempt+1}/4 chương {ch_id}, chờ {wait}s...')
            time.sleep(wait)
        self.log(f'✗ Bỏ qua chương {ch_id} sau 4 lần thử')
        return None

    # ──────────────────────────────────────────────
    # DOWNLOAD MAIN
    # ──────────────────────────────────────────────
    def download_novel(self, novel_id: str) -> str:
        novel_id = str(novel_id).strip()
        self.log(f'\n{"="*50}')
        self.log(f'ID: {novel_id}')

        name, author, chapters, status = self._get_chapter_list(novel_id)
        if name == 'err':
            self.log('Không lấy được thông tin truyện.')
            return 'err'

        self.log(f'Truyện: 《{name}》')
        self.log(f'Tác giả: {author if author else "?"}')
        self.log(f'Trạng thái: {status[0]} | Tổng: {len(chapters)} chương')

        safe_name = self._sanitize_filename(name)

        json_path = os.path.join(self.bookstore_dir, f'{safe_name}.json')
        existing = {}
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='UTF-8') as f:
                    existing = json.load(f)
                self.log(f'Resume: đã có {len(existing)}/{len(chapters)} chương.')
            except Exception:
                pass

        total = len(chapters)
        completed = 0
        results = {}
        chapter_items = list(chapters.items())

        with tqdm(total=total, desc='Tải chương', unit='ch') as pbar:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.xc) as executor:
                futures = {
                    executor.submit(self._download_chapter, idx, ch_id, existing): (idx, ch_id)
                    for idx, ch_id in chapter_items
                }
                for future in concurrent.futures.as_completed(futures):
                    idx_str, ch_id_key = futures[future]
                    try:
                        result = future.result()
                        if result:
                            i, title, ch_content = result
                            results[i] = (title, ch_content)
                            existing[ch_id_key] = (title, ch_content)
                    except Exception as e:
                        self.log(f'✗ [{idx_str}] Lỗi: {e}')

                    completed += 1
                    pbar.update(1)

                    if completed % 20 == 0:
                        with open(json_path, 'w', encoding='UTF-8') as f:
                            json.dump(existing, f, ensure_ascii=False)

        with open(json_path, 'w', encoding='UTF-8') as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)

        ordered = [(results[i][0], results[i][1]) for i in sorted(results.keys())]
        self.log(f'Hoàn thành: {len(ordered)}/{total} chương')

        if self.config.save_mode == SaveMode.EPUB:
            return self._save_epub(name, author, safe_name, ordered)
        else:
            return self._save_txt(safe_name, ordered)

    # ──────────────────────────────────────────────
    # SAVE
    # ──────────────────────────────────────────────
    def _save_txt(self, safe_name: str, chapters: List[Tuple[str, str]]) -> str:
        out = os.path.join(self.config.save_path, f'{safe_name}.txt')
        with open(out, 'w', encoding='UTF-8') as f:
            for title, content in chapters:
                f.write(f'\n{title}\n\n')
                f.write(content)
                f.write('\n')
        self.log(f'✓ Lưu TXT: {out}')
        return 's'

    def _save_epub(self, name: str, author: str, safe_name: str,
                   chapters: List[Tuple[str, str]]) -> str:
        book = epub.EpubBook()
        book.set_title(name)
        book.set_language('zh')
        if author:
            book.add_author(author)

        epub_chs = []
        for i, (title, content) in enumerate(chapters):
            ch = epub.EpubHtml(
                title=title or f'Chương {i+1}',
                file_name=f'ch_{i:04d}.xhtml',
                lang='zh'
            )
            body = ''.join(
                f'<p>{p.strip()}</p>'
                for p in content.split('\n') if p.strip()
            )
            display_title = title or f'Chương {i+1}'
            ch.content = f'<h1>{display_title}</h1>{body}'
            book.add_item(ch)
            epub_chs.append(ch)

        book.toc = epub_chs
        book.spine = ['nav'] + epub_chs
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        out = os.path.join(self.config.save_path, f'{safe_name}.epub')
        epub.write_epub(out, book)
        self.log(f'✓ Lưu EPUB: {out}')
        return 's'

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        if not name:
            return 'Unknown'
        for c in r'\/:*?"<>|':
            name = name.replace(c, '_')
        return name.strip() or 'Unknown'


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────
if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Cách dùng: python src/main.py <book_id> [epub|txt]')
        sys.exit(1)

    novel_id = sys.argv[1]
    mode_arg = sys.argv[2] if len(sys.argv) > 2 else 'epub'
    save_mode = SaveMode.SINGLE_TXT if mode_arg == 'txt' else SaveMode.EPUB

    config = Config(
        save_path='./downloads',
        save_mode=save_mode,
        xc=8,
        delay=[200, 500],
    )

    os.makedirs('./downloads', exist_ok=True)

    downloader = NovelDownloader(config)
    result = downloader.download_novel(novel_id)
    sys.exit(0 if result == 's' else 1)
