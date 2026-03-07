# -*- coding: utf-8 -*-
import requests as req
from ebooklib import epub
from tqdm import tqdm
import json
import time
import random
import os
import concurrent.futures
from typing import Callable, Optional, Dict, List
from dataclasses import dataclass, field
from enum import Enum


class SaveMode(Enum):
    SINGLE_TXT = 1
    SPLIT_TXT = 2
    EPUB = 3


@dataclass
class Config:
    kg: int = 0
    kgf: str = '　'
    delay: List[int] = None
    save_path: str = './downloads'
    save_mode: SaveMode = SaveMode.SINGLE_TXT
    xc: int = 5  # Giảm xuống 5 để tránh bị chặn IP trên GitHub Actions

    def __post_init__(self):
        if self.delay is None:
            self.delay = [200, 800]  # Tăng delay để tránh bị chặn


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

        # Load charset
        charset_path = os.path.join(self.script_dir, 'charset.json')
        with open(charset_path, 'r', encoding='UTF-8') as f:
            self.charset = json.load(f)

        self._setup_directories()
        self.cookie = self._load_or_create_cookie()

    def _setup_directories(self):
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.bookstore_dir, exist_ok=True)
        os.makedirs(self.config.save_path, exist_ok=True)

    # =========================================================
    # COOKIE - FIX: Không loop vô tận, giới hạn 50 lần thử
    # =========================================================
    def _load_or_create_cookie(self) -> str:
        """Load cookie từ file hoặc tạo mới, KHÔNG loop vô tận"""
        if os.path.exists(self.cookie_path):
            try:
                with open(self.cookie_path, 'r', encoding='UTF-8') as f:
                    cookie = json.load(f)
                self.log(f'Dùng cookie cũ: {cookie[:40]}...')
                return cookie
            except Exception:
                pass

        return self._generate_cookie()

    def _generate_cookie(self) -> str:
        """
        FIX CHÍNH: Tạo cookie với giới hạn số lần thử rõ ràng.
        Code gốc loop hàng TỶ lần -> timeout GitHub Actions.
        """
        bas = 1000000000000000000
        max_attempts = 50  # Chỉ thử 50 lần
        start = random.randint(bas * 6, bas * 8)

        self.log(f'Đang tạo cookie (tối đa {max_attempts} lần thử)...')

        # Thử lấy chapter test để validate cookie
        test_chapter_id = self._get_test_chapter_id()

        for attempt in range(max_attempts):
            i = start + attempt
            cookie = f'novel_web_id={i}'
            try:
                if test_chapter_id:
                    result = self._fetch_chapter_raw(test_chapter_id, cookie)
                    if result and len(result) > 200:
                        self.log(f'Cookie hợp lệ sau {attempt + 1} lần thử.')
                        self._save_cookie(cookie)
                        return cookie
                else:
                    # Không có chapter test -> dùng luôn
                    self._save_cookie(cookie)
                    return cookie
            except Exception:
                continue

        # Fallback: dùng cookie ngẫu nhiên thay vì loop mãi
        cookie = f'novel_web_id={random.randint(bas * 7, bas * 8)}'
        self.log(f'Dùng cookie ngẫu nhiên (không validate được).')
        self._save_cookie(cookie)
        return cookie

    def _save_cookie(self, cookie: str):
        try:
            with open(self.cookie_path, 'w', encoding='UTF-8') as f:
                json.dump(cookie, f)
        except Exception:
            pass

    def _get_test_chapter_id(self) -> Optional[str]:
        """Lấy 1 chapter ID để test cookie - dùng truyện cố định"""
        try:
            test_novel_id = '7143038691944959011'
            _, chapters, _ = self._get_chapter_list(test_novel_id)
            if chapters:
                return list(chapters.values())[0]
        except Exception:
            pass
        return None

    # =========================================================
    # API
    # =========================================================
    def _get_chapter_list(self, novel_id: str):
        """Lấy danh sách chương"""
        url = f'https://fanqienovel.com/api/reader/directory/detail?bookId={novel_id}'
        try:
            resp = req.get(url, headers=self.headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()

            if data.get('code') != 0:
                self.log(f'API trả về lỗi code={data.get("code")}: {data.get("msg", "")}')
                return 'err', {}, []

            book_data = data.get('data', {})
            name = book_data.get('bookName', 'Unknown')
            status = [book_data.get('bookStatus', '?')]

            chapters = {}
            for volume in book_data.get('chapterListWithVolume', []):
                for ch in volume.get('chapterList', []):
                    title = ch.get('chapterTitle', '').strip()
                    ch_id = str(ch.get('chapterId', ''))
                    if title and ch_id:
                        chapters[title] = ch_id

            self.log(f'Tìm thấy truyện: 《{name}》- {len(chapters)} chương')
            return name, chapters, status

        except req.Timeout:
            self.log('Timeout khi lấy danh sách chương (20s)')
            return 'err', {}, []
        except Exception as e:
            self.log(f'Lỗi lấy danh sách chương: {e}')
            return 'err', {}, []

    def _fetch_chapter_raw(self, chapter_id: str, cookie: str) -> Optional[str]:
        """Gọi API lấy nội dung chapter thô"""
        url = f'https://fanqienovel.com/api/reader/full?itemId={chapter_id}'
        headers = {**self.headers, 'Cookie': cookie}
        try:
            resp = req.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            if data.get('code') != 0:
                return None
            return data.get('data', {}).get('chapterData', {}).get('content', '')
        except Exception:
            return None

    def _download_chapter_content(self, chapter_id: str) -> Optional[str]:
        """Download và decode nội dung 1 chương"""
        raw = self._fetch_chapter_raw(chapter_id, self.cookie)
        if not raw:
            return None
        return self._decode_content(raw)

    def _decode_content(self, content: str) -> str:
        """Decode ký tự đặc biệt FanQie"""
        result = []
        for char in content:
            code = ord(char)
            decoded = False
            for code_range in self.CODE:
                if code_range[0] <= code <= code_range[1]:
                    idx = code - code_range[0]
                    if idx < len(self.charset):
                        result.append(self.charset[idx])
                        decoded = True
                        break
            if not decoded:
                result.append(char)
        return ''.join(result)

    # =========================================================
    # DOWNLOAD
    # =========================================================
    def _download_chapter(self, title: str, chapter_id: str, existing: Dict) -> Optional[str]:
        """Download 1 chương, bỏ qua nếu đã có"""
        if title in existing:
            return existing[title]

        retries = 3
        for attempt in range(retries):
            try:
                content = self._download_chapter_content(chapter_id)
                if content:
                    time.sleep(random.randint(self.config.delay[0], self.config.delay[1]) / 1000)
                    return content
                time.sleep(2)
            except Exception as e:
                if attempt == retries - 1:
                    self.log(f'  ✗ Thất bại [{title}]: {e}')
                time.sleep(2)
        return None

    def download_novel(self, novel_id: str) -> str:
        """Download toàn bộ truyện theo ID"""
        novel_id = str(novel_id).strip()
        self.log(f'\n{"="*50}')
        self.log(f'Bắt đầu tải truyện ID: {novel_id}')

        name, chapters, status = self._get_chapter_list(novel_id)
        if name == 'err':
            self.log('Không lấy được thông tin truyện. Kiểm tra lại ID.')
            return 'err'

        safe_name = self._sanitize_filename(name)
        self.log(f'Truyện: 《{name}》| Trạng thái: {status[0]} | Tổng chương: {len(chapters)}')

        # Load chapter đã tải trước (resume)
        json_path = os.path.join(self.bookstore_dir, f'{safe_name}.json')
        existing = {}
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='UTF-8') as f:
                existing = json.load(f)
            skip = sum(1 for t in chapters if t in existing)
            self.log(f'Resume: đã có {skip}/{len(chapters)} chương, tải tiếp phần còn lại.')

        chapter_list = list(chapters.items())
        total = len(chapter_list)
        completed = 0
        content = dict(existing)  # giữ chương cũ

        with tqdm(total=total, desc='Tải chương', unit='ch') as pbar:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.xc) as executor:
                futures = {
                    executor.submit(self._download_chapter, title, ch_id, existing): title
                    for title, ch_id in chapter_list
                }
                for future in concurrent.futures.as_completed(futures):
                    title = futures[future]
                    try:
                        result = future.result()
                        if result:
                            content[title] = result
                    except Exception as e:
                        self.log(f'  ✗ Lỗi [{title}]: {e}')

                    completed += 1
                    pbar.update(1)

                    # Lưu định kỳ mỗi 10 chương
                    if completed % 10 == 0:
                        with open(json_path, 'w', encoding='UTF-8') as f:
                            json.dump(content, f, ensure_ascii=False)

        # Lưu JSON cuối
        with open(json_path, 'w', encoding='UTF-8') as f:
            json.dump(content, f, ensure_ascii=False, indent=2)
        self.log(f'Đã lưu dữ liệu JSON: {json_path}')

        # Xuất file
        success_count = sum(1 for t in chapters if t in content)
        self.log(f'Tải xong: {success_count}/{total} chương')

        if self.config.save_mode == SaveMode.SINGLE_TXT:
            return self._save_single_txt(safe_name, chapters, content)
        elif self.config.save_mode == SaveMode.SPLIT_TXT:
            return self._save_split_txt(safe_name, chapters, content)
        elif self.config.save_mode == SaveMode.EPUB:
            return self._save_epub(name, safe_name, chapters, content)
        return 's'

    # =========================================================
    # SAVE
    # =========================================================
    def _save_single_txt(self, name: str, chapters: Dict, content: Dict) -> str:
        out = os.path.join(self.config.save_path, f'{name}.txt')
        with open(out, 'w', encoding='UTF-8') as f:
            for title in chapters:  # Giữ đúng thứ tự chương
                if title not in content:
                    continue
                f.write(f'\n{title}\n')
                f.write(content[title])
                f.write('\n')
        self.log(f'✓ Đã lưu TXT: {out}')
        return 's'

    def _save_split_txt(self, name: str, chapters: Dict, content: Dict) -> str:
        out_dir = os.path.join(self.config.save_path, name)
        os.makedirs(out_dir, exist_ok=True)
        for i, title in enumerate(chapters, 1):
            if title not in content:
                continue
            safe_title = self._sanitize_filename(title)
            path = os.path.join(out_dir, f'{i:04d}_{safe_title}.txt')
            with open(path, 'w', encoding='UTF-8') as f:
                f.write(f'{title}\n\n{content[title]}')
        self.log(f'✓ Đã lưu các chương vào: {out_dir}')
        return 's'

    def _save_epub(self, name: str, safe_name: str, chapters: Dict, content: Dict) -> str:
        book = epub.EpubBook()
        book.set_title(name)
        book.set_language('zh')

        epub_chapters = []
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
            epub_chapters.append(ch)

        book.toc = epub_chapters
        book.spine = ['nav'] + epub_chapters
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        out = os.path.join(self.config.save_path, f'{safe_name}.epub')
        epub.write_epub(out, book)
        self.log(f'✓ Đã lưu EPUB: {out}')
        return 's'

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        for c in r'\/:*?"<>|':
            name = name.replace(c, '_')
        return name.strip()


# =========================================================
# ENTRY POINT - Dùng cho GitHub Actions
# =========================================================
if __name__ == '__main__':
    import sys
    import argparse

    parser = argparse.ArgumentParser(description='FanQie Novel Downloader')
    parser.add_argument('--id', type=str, required=True, help='ID truyện FanQie')
    parser.add_argument('--mode', type=str, default='txt',
                        choices=['txt', 'split', 'epub'], help='Định dạng lưu')
    parser.add_argument('--output', type=str, default='./downloads', help='Thư mục lưu')
    parser.add_argument('--threads', type=int, default=5, help='Số luồng tải')
    args = parser.parse_args()

    mode_map = {'txt': SaveMode.SINGLE_TXT, 'split': SaveMode.SPLIT_TXT, 'epub': SaveMode.EPUB}

    config = Config(
        save_path=args.output,
        save_mode=mode_map[args.mode],
        xc=args.threads,
        delay=[200, 600],
    )

    downloader = NovelDownloader(config)
    result = downloader.download_novel(args.id)

    if result == 's':
        print('\n✅ Tải thành công!')
        sys.exit(0)
    else:
        print('\n❌ Tải thất bại!')
        sys.exit(1)
    
