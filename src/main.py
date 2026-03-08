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
from typing import Callable, Optional, Dict, List
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
    save_path: str = '.'
    save_mode: SaveMode = SaveMode.SINGLE_TXT
    xc: int = 5  # Giảm luồng để tránh bị chặn IP trên GitHub Actions

    def __post_init__(self):
        if self.delay is None:
            self.delay = [300, 800]


class NovelDownloader:
    def __init__(self, config: Config, log_callback: Optional[Callable] = None):
        self.config = config
        self.log = log_callback or print

        self.headers_lib = [
            {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'},
            {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0'},
            {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'},
        ]
        self.headers = random.choice(self.headers_lib).copy()
        self.headers['Referer'] = 'https://fanqienovel.com/'

        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.data_dir = os.path.join(self.script_dir, 'data')
        self.bookstore_dir = os.path.join(self.data_dir, 'bookstore')
        self.cookie_path = os.path.join(self.data_dir, 'cookie.json')

        self.CODE = [[58344, 58715], [58345, 58716]]

        charset_path = os.path.join(self.script_dir, 'charset.json')
        with open(charset_path, 'r', encoding='UTF-8') as f:
            self.charset = json.load(f)

        self._setup_directories()
        self.cookie = self._load_or_create_cookie()

    def _setup_directories(self):
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.bookstore_dir, exist_ok=True)
        os.makedirs(self.config.save_path, exist_ok=True)

    # ──────────────────────────────────────────────
    # COOKIE — FIX: không loop vô tận
    # ──────────────────────────────────────────────
    def _load_or_create_cookie(self) -> str:
        if os.path.exists(self.cookie_path):
            try:
                with open(self.cookie_path, 'r', encoding='UTF-8') as f:
                    cookie = json.load(f)
                self.log(f'Dùng cookie cũ.')
                return cookie
            except Exception:
                pass
        return self._generate_cookie()

    def _generate_cookie(self) -> str:
        """
        FIX CHÍNH: Giới hạn 50 lần thử thay vì loop hàng tỷ lần.
        Code gốc dùng range(bas*6, bas*9) → timeout GitHub Actions.
        """
        bas = 1000000000000000000
        max_attempts = 50
        start = random.randint(bas * 6, bas * 8)

        self.log(f'Đang tạo cookie (tối đa {max_attempts} lần)...')

        for attempt in range(max_attempts):
            cookie = f'novel_web_id={start + attempt}'
            try:
                # Test nhanh với chapter cố định
                result = self._fetch_chapter_raw('7143038691944959011', cookie)
                if result and len(result) > 200:
                    self.log(f'Cookie hợp lệ sau {attempt + 1} lần.')
                    self._save_cookie(cookie)
                    return cookie
            except Exception:
                continue
            time.sleep(0.1)

        # Fallback — không loop mãi
        cookie = f'novel_web_id={random.randint(bas * 7, bas * 8)}'
        self.log('Dùng cookie ngẫu nhiên (fallback).')
        self._save_cookie(cookie)
        return cookie

    def _save_cookie(self, cookie: str):
        try:
            with open(self.cookie_path, 'w', encoding='UTF-8') as f:
                json.dump(cookie, f)
        except Exception:
            pass

    # ──────────────────────────────────────────────
    # API
    # ──────────────────────────────────────────────
    def _get_book_info(self, novel_id: str):
        """Lấy tên và trạng thái truyện"""
        for url in [
            f'https://fanqienovel.com/api/author/book/info?bookId={novel_id}',
            f'https://fanqienovel.com/page/{novel_id}',
        ]:
            try:
                resp = req.get(url, headers=self.headers, timeout=15)
                data = resp.json()
                if data.get('code') == 0:
                    d = data.get('data', {})
                    name = d.get('bookName') or d.get('book_name') or d.get('name') or 'Unknown'
                    status = d.get('bookStatus') or d.get('book_status') or '?'
                    if name != 'Unknown':
                        return name, status
            except Exception:
                continue
        return 'Unknown', '?'

    def _get_chapter_list(self, novel_id: str):
        url = f'https://fanqienovel.com/api/reader/directory/detail?bookId={novel_id}'
        try:
            resp = req.get(url, headers=self.headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()

            if data.get('code') != 0:
                self.log(f'API lỗi: code={data.get("code")} msg={data.get("msg","")}')
                return 'err', {}, []

            raw = data.get('data', {})

            # Cấu trúc MỚI: allItemIds — chỉ có list ID, không có tên chương
            if isinstance(raw, dict) and 'allItemIds' in raw:
                all_ids = [str(i).strip() for i in (raw.get('allItemIds') or []) if i]
                self.log(f'Tìm thấy {len(all_ids)} chương (dạng allItemIds)')

                name, book_status = self._get_book_info(novel_id)
                # Key = "Chương N|id" để giữ thứ tự và biết ID
                chapters = {f'Chương {i+1}|{ch_id}': ch_id for i, ch_id in enumerate(all_ids)}
                self.log(f'Truyện: 《{name}》| {len(chapters)} chương')
                return name, chapters, [book_status]

            # Cấu trúc CŨ: list volume
            elif isinstance(raw, list):
                name, book_status = self._get_book_info(novel_id)
                chapters = {}
                for item in raw:
                    if isinstance(item, dict):
                        for ch in (item.get('chapterList') or []):
                            if isinstance(ch, dict):
                                title = (ch.get('chapterTitle') or '').strip()
                                ch_id = str(ch.get('chapterId') or '')
                                if title and ch_id:
                                    chapters[title] = ch_id
                self.log(f'Truyện: 《{name}》| {len(chapters)} chương')
                return name, chapters, [book_status]

            # Cấu trúc dict thông thường
            elif isinstance(raw, dict):
                name = raw.get('bookName') or 'Unknown'
                status = [raw.get('bookStatus') or '?']
                chapters = {}
                for vol in (raw.get('chapterListWithVolume') or []):
                    if isinstance(vol, dict):
                        for ch in (vol.get('chapterList') or []):
                            if isinstance(ch, dict):
                                title = (ch.get('chapterTitle') or '').strip()
                                ch_id = str(ch.get('chapterId') or '')
                                if title and ch_id:
                                    chapters[title] = ch_id
                self.log(f'Truyện: 《{name}》| {len(chapters)} chương')
                return name, chapters, status

            else:
                self.log(f'Cấu trúc API lạ: {type(raw)}, keys={list(raw.keys()) if isinstance(raw,dict) else "N/A"}')
                return 'err', {}, []

        except req.Timeout:
            self.log('Timeout lấy danh sách chương')
            return 'err', {}, []
        except Exception as e:
            self.log(f'Lỗi lấy chương: {e}')
            import traceback
            self.log(traceback.format_exc())
            return 'err', {}, []

    def _fetch_chapter_raw(self, chapter_id: str, cookie: str):
        """Trả về (title, content) hoặc None"""
        url = f'https://fanqienovel.com/api/reader/full?itemId={chapter_id}'
        headers = {**self.headers, 'Cookie': cookie}
        try:
            resp = req.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            if data.get('code') != 0:
                return None
            ch_data = data.get('data', {}).get('chapterData', {})
            title = ch_data.get('chapterTitle') or ch_data.get('title') or ''
            content = ch_data.get('content') or ''
            return title.strip(), content
        except Exception:
            return None

    def _download_chapter_content(self, chapter_id: str):
        """Trả về (real_title, decoded_content) hoặc None"""
        result = self._fetch_chapter_raw(chapter_id, self.cookie)
        if not result:
            return None
        title, raw = result
        if not raw:
            return None
        return title, self._decode_content(raw)

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

    # ──────────────────────────────────────────────
    # DOWNLOAD
    # ──────────────────────────────────────────────
    def _download_chapter(self, title: str, ch_id: str, existing: Dict):
        """Trả về (real_title, content) hoặc None"""
        if title in existing:
            return title, existing[title]

        for attempt in range(3):
            try:
                result = self._download_chapter_content(ch_id)
                if result:
                    real_title, content = result
                    # Dùng tên thật từ API nếu có, không thì dùng tên tạm bỏ |id
                    final_title = real_title if real_title else title.split('|')[0]
                    time.sleep(random.randint(self.config.delay[0], self.config.delay[1]) / 1000)
                    return final_title, content
            except Exception:
                pass
            time.sleep(2)
        return None

    def download_novel(self, novel_id: str) -> str:
        novel_id = str(novel_id).strip()
        self.log(f'\n{"="*50}')
        self.log(f'ID truyện: {novel_id}')

        name, chapters, status = self._get_chapter_list(novel_id)
        if name == 'err':
            self.log('Không lấy được thông tin truyện. Kiểm tra lại ID.')
            return 'err'

        safe_name = self._sanitize_filename(name)
        self.log(f'Trạng thái: {status[0]} | Tổng: {len(chapters)} chương')

        # Resume — load chương đã tải
        json_path = os.path.join(self.bookstore_dir, f'{safe_name}.json')
        existing = {}
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='UTF-8') as f:
                existing = json.load(f)
            self.log(f'Resume: đã có {len(existing)}/{len(chapters)} chương.')

        chapter_list = list(chapters.items())
        total = len(chapter_list)
        content = dict(existing)
        completed = 0

        # Dùng dict có thứ tự để lưu (real_title -> content)
        ordered_content = {}  # giữ thứ tự chương đúng

        with tqdm(total=total, desc='Tải chương', unit='ch') as pbar:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.xc) as executor:
                futures = {
                    executor.submit(self._download_chapter, title, ch_id, existing): (i, title)
                    for i, (title, ch_id) in enumerate(chapter_list)
                }
                results_by_index = {}
                for future in concurrent.futures.as_completed(futures):
                    i, title = futures[future]
                    try:
                        result = future.result()
                        if result:
                            real_title, ch_content = result
                            results_by_index[i] = (real_title, ch_content)
                            content[title] = ch_content  # lưu tạm theo key cũ để resume
                    except Exception as e:
                        self.log(f'✗ [{title}]: {e}')

                    completed += 1
                    pbar.update(1)

                    if completed % 10 == 0:
                        with open(json_path, 'w', encoding='UTF-8') as f:
                            json.dump(content, f, ensure_ascii=False)

        # Sắp xếp lại đúng thứ tự và dùng tên thật
        for i in sorted(results_by_index.keys()):
            real_title, ch_content = results_by_index[i]
            ordered_content[real_title] = ch_content

        # Lưu JSON cuối (dùng tên thật)
        with open(json_path, 'w', encoding='UTF-8') as f:
            json.dump(ordered_content, f, ensure_ascii=False, indent=2)

        self.log(f'Hoàn thành: {len(ordered_content)}/{total} chương')

        if self.config.save_mode == SaveMode.SINGLE_TXT:
            return self._save_single_txt(safe_name, ordered_content)
        elif self.config.save_mode == SaveMode.EPUB:
            return self._save_epub(name, safe_name, chapters, content)
        return 's'

    # ──────────────────────────────────────────────
    # SAVE
    # ──────────────────────────────────────────────
    @staticmethod
    def _display_title(title: str) -> str:
        """Bỏ phần |id khỏi tên chương nếu có (dạng allItemIds)"""
        return title.split('|')[0] if '|' in title else title

    def _save_single_txt(self, name: str, content: Dict) -> str:
        out = os.path.join(self.config.save_path, f'{name}.txt')
        with open(out, 'w', encoding='UTF-8') as f:
            for title, ch_content in content.items():
                f.write(f'\n{title}\n')
                f.write(ch_content)
                f.write('\n')
        self.log(f'✓ Lưu TXT: {out}')
        return 's'

    def _save_epub(self, name: str, safe_name: str, chapters: Dict, content: Dict) -> str:
        book = epub.EpubBook()
        book.set_title(name)
        book.set_language('zh')

        epub_chs = []
        for i, title in enumerate(chapters):
            if title not in content:
                continue
            ch = epub.EpubHtml(title=title, file_name=f'ch_{i:04d}.xhtml', lang='zh')
            body = ''.join(
                f'<p>{p.strip()}</p>'
                for p in content[title].split('\n') if p.strip()
            )
            ch.content = f'<h1>{title}</h1>{body}'
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
        for c in r'\/:*?"<>|':
            name = name.replace(c, '_')
        return name.strip()


# ──────────────────────────────────────────────
# ENTRY POINT
# Cách dùng: python src/main.py <book_id> [txt|epub]
# ──────────────────────────────────────────────
if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Cách dùng: python src/main.py <book_id> [txt|epub]')
        sys.exit(1)

    novel_id = sys.argv[1]
    mode_arg = sys.argv[2] if len(sys.argv) > 2 else 'txt'

    save_mode = SaveMode.EPUB if mode_arg == 'epub' else SaveMode.SINGLE_TXT

    config = Config(
        save_path='.',        # Lưu ra thư mục gốc để actions/upload-artifact@v4 tìm thấy *.txt
        save_mode=save_mode,
        xc=5,
        delay=[300, 800],
    )

    downloader = NovelDownloader(config)
    result = downloader.download_novel(novel_id)

    sys.exit(0 if result == 's' else 1)
