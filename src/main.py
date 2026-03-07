# -*- coding: utf-8 -*-
import requests as req
from lxml import etree
from ebooklib import epub
from tqdm import tqdm
import json
import time
import random
import os
import concurrent.futures
from typing import Callable, Optional, Dict, List
from dataclasses import dataclass
from enum import Enum

class SaveMode(Enum):
    SINGLE_TXT = 1
    SPLIT_TXT = 2
    EPUB = 3
    HTML = 4
    LATEX = 5

@dataclass
class Config:
    kg: int = 0
    kgf: str = '　'
    delay: List[int] = None
    save_path: str = './downloads'
    save_mode: SaveMode = SaveMode.SINGLE_TXT
    space_mode: str = 'halfwidth'
    xc: int = 5  # Giảm xuống 5 để tránh bị chặn IP trên GitHub Actions

    def __post_init__(self):
        if self.delay is None:
            self.delay = [50, 150]

class NovelDownloader:
    def __init__(self, config: Config, progress_callback: Optional[Callable] = None, log_callback: Optional[Callable] = None):
        self.config = config
        self.progress_callback = progress_callback or self._default_progress
        self.log_callback = log_callback or print

        self.headers_lib = [
            {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/93.0.4577.63 Safari/537.36'},
            {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:91.0) Gecko/20100101 Firefox/91.0'},
            {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/93.0.4577.63 Safari/537.36 Edg/93.0.961.47'}
        ]
        self.headers = random.choice(self.headers_lib)
        self.headers['Referer'] = 'https://fanqienovel.com/'

        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.data_dir = os.path.join(self.script_dir, 'data')
        self.bookstore_dir = os.path.join(self.data_dir, 'bookstore')
        self.cookie_path = os.path.join(self.data_dir, 'cookie.json')

        self._setup_directories()
        self._init_cookie()

        self.zj = {}
        self.cs = 0

    def _setup_directories(self):
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.bookstore_dir, exist_ok=True)
        os.makedirs(self.config.save_path, exist_ok=True)

    def _init_cookie(self):
        """Khởi tạo cookie nhanh chóng, không chạy vòng lặp vô tận"""
        if os.path.exists(self.cookie_path):
            with open(self.cookie_path, 'r', encoding='UTF-8') as f:
                self.cookie = json.load(f)
        else:
            self.log_callback('Đang tạo cookie mới...')
            # Tạo một novel_web_id ngẫu nhiên (19 chữ số)
            random_id = "".join([str(random.randint(0, 9)) for _ in range(19)])
            self.cookie = f'novel_web_id={random_id}'
            with open(self.cookie_path, 'w', encoding='UTF-8') as f:
                json.dump(self.cookie, f)
        
        self.headers['Cookie'] = self.cookie
        self.log_callback('Khởi tạo Cookie thành công')

    def _default_progress(self, current: int, total: int, desc: str = '', chapter_title: str = None):
        if not hasattr(self, '_pbar'):
            self._pbar = tqdm(total=total, desc=desc)
        self._pbar.update(1)

    def _sanitize_filename(self, filename: str) -> str:
        return "".join([c for c in filename if c.isalnum() or c in (' ', '.', '_')]).strip()

    def _get_chapter_list(self, novel_id: int):
        """Lấy danh sách chương từ API Fanqie"""
        url = f"https://novel.snssdk.com/api/novel/book/directory/list/v1/?device_platform=web&parent_enter_from=novel_detail&book_id={novel_id}"
        try:
            res = req.get(url, headers=self.headers)
            data = res.json()
            if data['code'] != 0: return 'err', {}, []
            
            book_info = data['data']['book_info']
            chapters = {c['title']: c['item_id'] for c in data['data']['directory']}
            return book_info['book_name'], chapters, [book_info['status_text']]
        except:
            return 'err', {}, []

    def _download_chapter_content(self, chapter_id: str):
        """Tải nội dung chi tiết một chương"""
        url = f"https://novel.snssdk.com/api/novel/book/reader/full/v1/?device_platform=web&item_id={chapter_id}"
        try:
            res = req.get(url, headers=self.headers)
            data = res.json()
            # Fanqie trả về HTML trong JSON, cần parse để lấy text
            content_html = data['data']['content']
            soup = etree.HTML(content_html)
            paragraphs = soup.xpath('//p/text()')
            return "\n".join(paragraphs)
        except:
            return 'err'

    def download_novel(self, novel_id: int):
        name, chapters, status = self._get_chapter_list(novel_id)
        if name == 'err':
            self.log_callback("Không tìm thấy truyện!")
            return

        safe_name = self._sanitize_filename(name)
        self.log_callback(f'Bắt đầu tải: {name}')
        
        results = {}
        chapter_list = list(chapters.items())
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.xc) as executor:
            future_to_title = {executor.submit(self._download_chapter_content, cid): title for title, cid in chapter_list}
            
            for future in tqdm(concurrent.futures.as_completed(future_to_title), total=len(chapter_list), desc="Tiến độ"):
                title = future_to_title[future]
                content = future.result()
                if content != 'err':
                    results[title] = content
                time.sleep(random.uniform(self.config.delay[0]/1000, self.config.delay[1]/1000))

        # Lưu file
        self._save_to_txt(safe_name, results, chapter_list)

    def _save_to_txt(self, name: str, results: dict, original_order: list):
        output_path = os.path.join(self.config.save_path, f'{name}.txt')
        with open(output_path, 'w', encoding='UTF-8') as f:
            for title, _ in original_order:
                if title in results:
                    f.write(f"\n{title}\n\n")
                    f.write(results[title])
                    f.write("\n\n" + "="*30 + "\n")
        self.log_callback(f"Đã lưu tại: {output_path}")

if __name__ == "__main__":
    import sys
    # Lấy ID truyện từ tham số dòng lệnh (cho GitHub Actions)
    novel_id = sys.argv[1] if len(sys.argv) > 1 else "7024797615359265799"
    
    cfg = Config()
    downloader = NovelDownloader(cfg)
    downloader.download_novel(int(novel_id))
