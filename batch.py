import os
import re
import time
import threading
import io
import shutil
from typing import List, Dict, Optional, Tuple, Set
from pathlib import Path

import fitz  # PyMuPDF
import requests
import pandas as pd
import pytesseract
from PIL import Image
import customtkinter as ctk
from tkinter import filedialog, messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed
import json

# --- 常量配置 ---
APP_VERSION = "3.1"
DEFAULT_TESSERACT_PATH = r"D:\Tesseract\tesseract.exe"
DEFAULT_EMAIL = "kodwabeamer@gmail.com"
DEFAULT_KEYWORDS = "sign language, deaf, fingerspelling, asl, csl, gloss recognition"
OPENALEX_API_URL = "https://api.openalex.org/works/https://doi.org/"
CROSSREF_API_URL = "https://api.crossref.org/works/"
SEMANTIC_SCHOLAR_API_URL = "https://api.semanticscholar.org/graph/v1/paper/"
API_TIMEOUT = 10
OCR_TEXT_THRESHOLD = 100
SAVE_INTERVAL = 10
MAX_WORKERS = 3  # API 并发数

# --- 设置外观 ---
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


class MultiSourceMetadataFetcher:
    """多数据源元数据获取器"""

    def __init__(self, email: str, session: requests.Session):
        self.email = email
        self.session = session
        self.enabled_sources = ['openalex', 'crossref', 'semantic_scholar']  # 默认全部启用

    def set_enabled_sources(self, sources: List[str]):
        """设置启用的数据源"""
        self.enabled_sources = sources

    def fetch_from_openalex(self, doi: str) -> Optional[Dict]:
        """从 OpenAlex 获取元数据"""
        try:
            resp = self.session.get(
                f"{OPENALEX_API_URL}{doi}",
                params={'mailto': self.email},
                timeout=API_TIMEOUT
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def fetch_from_crossref(self, doi: str) -> Optional[Dict]:
        """从 Crossref 获取元数据"""
        try:
            resp = self.session.get(
                f"{CROSSREF_API_URL}{doi}",
                headers={'User-Agent': f'PaperMetadataTool/1.0 (mailto:{self.email})'},
                timeout=API_TIMEOUT
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get('message')
        except Exception:
            pass
        return None

    def fetch_from_semantic_scholar(self, doi: str) -> Optional[Dict]:
        """从 Semantic Scholar 获取元数据"""
        try:
            resp = self.session.get(
                f"{SEMANTIC_SCHOLAR_API_URL}DOI:{doi}",
                params={'fields': 'title,authors,year,venue,volume,issue,journal,externalIds'},
                timeout=API_TIMEOUT
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def fetch_metadata(self, doi: str) -> Optional[Dict]:
        """从多个数据源获取元数据，按优先级顺序尝试"""
        metadata = None

        # 按优先级顺序尝试各个数据源
        for source in self.enabled_sources:
            if source == 'openalex':
                metadata = self.fetch_from_openalex(doi)
            elif source == 'crossref':
                metadata = self.fetch_from_crossref(doi)
            elif source == 'semantic_scholar':
                metadata = self.fetch_from_semantic_scholar(doi)

            if metadata:
                metadata['_source'] = source  # 标记数据来源
                break

        return metadata

    def normalize_metadata(self, metadata: Dict, source: str) -> Dict:
        """标准化不同数据源的元数据格式"""
        if source == 'openalex':
            return self._normalize_openalex(metadata)
        elif source == 'crossref':
            return self._normalize_crossref(metadata)
        elif source == 'semantic_scholar':
            return self._normalize_semantic_scholar(metadata)
        return {}

    def _normalize_openalex(self, metadata: Dict) -> Dict:
        """标准化 OpenAlex 数据格式"""
        authors = [
            a['author']['display_name']
            for a in metadata.get('authorships', [])
        ]

        biblio = metadata.get('biblio', {})
        volume = biblio.get('volume')
        issue = biblio.get('issue')

        # 提取纯 DOI（去除 https://doi.org/ 前缀）
        doi = metadata.get('doi', '')
        if doi.startswith('https://doi.org/'):
            doi = doi[16:]

        return {
            'title': metadata.get('title', ''),
            'authors': authors,
            'year': metadata.get('publication_year'),
            'language': metadata.get('language'),
            'journal': (
                metadata.get('primary_location', {})
                .get('source', {})
                .get('display_name', '')
            ) if metadata.get('primary_location') else '',
            'volume': volume,
            'issue': issue,
            'doi': doi,
            'type': metadata.get('type', ''),
            'concepts': [c['display_name'] for c in metadata.get('concepts', [])]
        }

    def _normalize_crossref(self, metadata: Dict) -> Dict:
        """标准化 Crossref 数据格式"""
        authors = []
        for author in metadata.get('author', []):
            given = author.get('given', '')
            family = author.get('family', '')
            if given and family:
                authors.append(f"{given} {family}")
            elif family:
                authors.append(family)

        return {
            'title': ' '.join(metadata.get('title', [])),
            'authors': authors,
            'year': metadata.get('published-print', {}).get('date-parts', [[None]])[0][0],
            'language': metadata.get('language', ''),
            'journal': metadata.get('container-title', [''])[0],
            'volume': metadata.get('volume'),
            'issue': metadata.get('issue'),
            'doi': metadata.get('DOI', ''),
            'type': metadata.get('type', ''),
            'concepts': []  # Crossref 不提供概念
        }

    def _normalize_semantic_scholar(self, metadata: Dict) -> Dict:
        """标准化 Semantic Scholar 数据格式"""
        authors = [author.get('name', '') for author in metadata.get('authors', [])]

        return {
            'title': metadata.get('title', ''),
            'authors': authors,
            'year': metadata.get('year'),
            'language': '',
            'journal': metadata.get('venue', ''),
            'volume': metadata.get('volume'),
            'issue': metadata.get('issue'),
            'doi': metadata.get('externalIds', {}).get('DOI', ''),
            'type': 'article',  # 假设都是文章
            'concepts': []  # Semantic Scholar 可能有关键词但未在 API 中返回
        }


class PaperMetadataExtractor:
    """PDF 文献元数据提取器"""

    def __init__(self, email: str, keywords: str, tesseract_path: str,
                 enable_ocr: bool = True, enabled_sources: List[str] = None):
        self.email = email
        self.keywords = keywords
        self.kw_pattern = keywords.replace(", ", "|")
        self.tesseract_path = tesseract_path
        self.enable_ocr = enable_ocr
        self.session = requests.Session()

        # 配置 Tesseract
        pytesseract.pytesseract.tesseract_cmd = tesseract_path

        # 初始化多源元数据获取器
        self.metadata_fetcher = MultiSourceMetadataFetcher(email, self.session)
        if enabled_sources:
            self.metadata_fetcher.set_enabled_sources(enabled_sources)

    def extract_text_from_pdf(self, file_path: str) -> str:
        """从 PDF 提取文本，优先文本层，失败则 OCR"""
        try:
            doc = fitz.open(file_path)
            text = "".join([doc[p].get_text() for p in range(min(2, len(doc)))])

            # 文本量不足时启用 OCR
            if self.enable_ocr and len(text.strip()) < OCR_TEXT_THRESHOLD:
                try:
                    pix = doc[0].get_pixmap(dpi=300)
                    img = Image.open(io.BytesIO(pix.tobytes()))
                    ocr_text = pytesseract.image_to_string(img, lang='eng')
                    text = ocr_text if len(ocr_text) > len(text) else text
                except Exception as e:
                    pass  # OCR 失败继续使用原文本

            doc.close()
            return text
        except Exception as e:
            raise Exception(f"PDF 文本提取失败：{str(e)}")

    def extract_doi(self, text: str) -> Optional[str]:
        """从文本中提取 DOI"""
        doi_match = re.search(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', text, re.IGNORECASE)
        return doi_match.group(0).rstrip('.') if doi_match else None

    def fetch_metadata(self, doi: str) -> Optional[Dict]:
        """从多数据源获取元数据（返回标准化格式）"""
        raw_metadata = self.metadata_fetcher.fetch_metadata(doi)
        if not raw_metadata:
            return None

        source = raw_metadata.get('_source', 'unknown')
        normalized = self.metadata_fetcher.normalize_metadata(raw_metadata, source)
        normalized['_source'] = source  # 保留数据来源
        normalized['_raw'] = raw_metadata  # 保留原始数据供调试
        return normalized

    def format_apa_authors(self, author_list: List[str]) -> str:
        """APA 格式转换器"""
        formatted = []
        for name in author_list:
            parts = name.split()
            if len(parts) >= 2:
                formatted.append(f"{parts[-1]}, {parts[0][0]}.")
            else:
                formatted.append(name)

        count = len(formatted)
        if count == 0:
            return "Unknown"
        elif count == 1:
            return formatted[0]
        elif count == 2:
            return f"{formatted[0]}, & {formatted[1]}"
        elif count == 3:
            return f"{formatted[0]}, {formatted[1]}, & {formatted[2]}"
        else:
            base = ", ".join(formatted[:3])
            return f"{base}, & {formatted[3]} et al."

    def is_sign_language_related(self, title: str, concepts: List[str]) -> bool:
        """判断是否手语相关文献"""
        concepts_text = " ".join(concepts) if concepts else ""
        text = f"{title} {concepts_text}".lower()
        return bool(re.search(self.kw_pattern, text))

    def process_pdf(self, file_path: str) -> Dict:
        """处理单个 PDF 文件"""
        filename = os.path.basename(file_path)
        current_dir = os.path.dirname(file_path)

        row = {
            "文件名": filename,
            "DOI": "N/A",
            "状态": "失败",
            "来源文件夹": os.path.basename(current_dir)
        }

        try:
            # 提取文本
            text = self.extract_text_from_pdf(file_path)

            # 提取 DOI
            doi = self.extract_doi(text)
            if not doi:
                row["error"] = "无 DOI"
                return row

            row["DOI"] = doi

            # 获取元数据（标准化格式）
            metadata = self.fetch_metadata(doi)
            if not metadata:
                row["DOI"] = "N/A"
                row["error"] = "无 API"
                return row

            # 检查文献类型
            pub_type = metadata.get('type', '').lower()
            if pub_type in ['book', 'book-series', 'monograph']:
                row["error"] = "跳过 (书籍)"
                return row

            # 检查是否手语相关
            title = metadata.get('title', '')
            concepts = metadata.get('concepts', [])

            if not self.is_sign_language_related(title, concepts):
                row["error"] = "跳过 (非手语)"
                return row

            # 提取元数据
            authors = metadata.get('authors', [])
            volume = metadata.get('volume')
            issue = metadata.get('issue')

            # 格式化期卷数字符串
            if volume and issue:
                issue_volume = f"{volume}({issue})"
            elif volume:
                issue_volume = volume
            elif issue:
                issue_volume = f"({issue})"
            else:
                issue_volume = "N/A"

            # 判断语种
            language = metadata.get('language', '')
            if language == 'zh':
                language_display = "中文"
            elif language:
                language_display = "英文"  # 默认英文
            else:
                language_display = "N/A"

            row.update({
                "文献名称": title,
                "作者名称": self.format_apa_authors(authors),
                "语种": language_display,
                "出版年份": metadata.get('year'),
                "发表期刊名称": metadata.get('journal', 'N/A'),
                "期卷数": issue_volume,
                "卷": volume or "N/A",
                "期": issue or "N/A",
                "数据来源": metadata.get('_source', 'unknown'),
                "状态": "成功"
            })

        except Exception as e:
            row["error"] = f"处理异常：{str(e)}"

        return row


class PaperToolGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title(f"文献元数据自动化获取工具 v{APP_VERSION}")
        self.geometry("950x750")
        self.minsize(900, 700)

        # 运行状态变量
        self.is_running = False
        self.stop_requested = False
        self.selected_src_folders: List[str] = []
        self.extractor: Optional[PaperMetadataExtractor] = None

        # --- UI 布局配置 ---
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(7, weight=1)

        self._setup_ui()

    def _setup_ui(self):
        """初始化 UI 组件"""
        # 1. 目标文件夹选择
        self.label_path = ctk.CTkLabel(self, text="PDF 源文件夹:")
        self.label_path.grid(row=0, column=0, padx=20, pady=10, sticky="e")

        self.entry_path = ctk.CTkEntry(
            self,
            placeholder_text="请选择一个或多个包含 PDF 的文件夹...",
            width=450
        )
        self.entry_path.grid(row=0, column=1, padx=10, pady=10, sticky="ew")

        self.btn_browse_src = ctk.CTkButton(
            self,
            text="选择源目录 (可多选)",
            command=self.browse_src_folder
        )
        self.btn_browse_src.grid(row=0, column=2, padx=20, pady=10)

        self.btn_clear_src = ctk.CTkButton(
            self,
            text="清空已选",
            fg_color="#c0392b",
            hover_color="#a93226",
            width=80,
            command=self.clear_src_folders
        )
        self.btn_clear_src.grid(row=0, column=4, padx=5, pady=10)

        self.label_folder_count = ctk.CTkLabel(
            self,
            text="(未选择)",
            text_color="gray"
        )
        self.label_folder_count.grid(row=0, column=3, padx=5, pady=10, sticky="w")

        # 日志框
        self.log_box = ctk.CTkTextbox(self, height=250)
        self.log_box.grid(row=7, column=0, columnspan=5, padx=20, pady=10, sticky="nsew")

        # 2. 结果保存位置
        self.label_out = ctk.CTkLabel(self, text="结果保存位置:")
        self.label_out.grid(row=1, column=0, padx=20, pady=10, sticky="e")

        self.entry_output_path = ctk.CTkEntry(
            self,
            placeholder_text="请选择 CSV 结果保存的完整路径...",
            width=450
        )
        self.entry_output_path.grid(row=1, column=1, padx=10, pady=10, sticky="ew")

        self.btn_browse_out = ctk.CTkButton(
            self,
            text="设置保存位置",
            fg_color="#1f538d",
            command=self.browse_output_file
        )
        self.btn_browse_out.grid(row=1, column=2, padx=20, pady=10)

        # 分类结果目录
        self.label_classify_folder = ctk.CTkLabel(self, text="归类结果目录:")
        self.label_classify_folder.grid(row=2, column=0, padx=20, pady=5, sticky="e")

        self.entry_classify_folder = ctk.CTkEntry(
            self,
            placeholder_text="可选：所有成功/失败 PDF 统一输出目录",
            width=450
        )
        self.entry_classify_folder.grid(row=2, column=1, padx=10, pady=5, sticky="ew")

        self.btn_browse_classify = ctk.CTkButton(
            self,
            text="选择归类目录",
            fg_color="#1f538d",
            command=self.browse_classify_folder
        )
        self.btn_browse_classify.grid(row=2, column=2, padx=20, pady=5)

        # 3. 配置项
        self.label_email = ctk.CTkLabel(self, text="OpenAlex Email:")
        self.label_email.grid(row=3, column=0, padx=20, pady=5, sticky="e")

        self.entry_email = ctk.CTkEntry(self, placeholder_text=DEFAULT_EMAIL)
        self.entry_email.insert(0, DEFAULT_EMAIL)
        self.entry_email.grid(row=3, column=1, padx=10, pady=5, sticky="ew")

        self.ocr_switch = ctk.CTkSwitch(self, text="启用 Tesseract OCR")
        self.ocr_switch.select()
        self.ocr_switch.grid(row=3, column=2, padx=20, pady=5)

        self.subfolder_switch = ctk.CTkSwitch(self, text="递归搜索子目录")
        self.subfolder_switch.grid(row=3, column=3, padx=10, pady=5)

        self.classify_switch = ctk.CTkSwitch(self, text="处理后自动归类文件")
        self.classify_switch.grid(row=3, column=4, padx=10, pady=5)

        # 4. 关键词配置
        self.label_kw = ctk.CTkLabel(self, text="手语关键词:")
        self.label_kw.grid(row=4, column=0, padx=20, pady=5, sticky="e")

        self.entry_kw = ctk.CTkEntry(self)
        self.entry_kw.insert(0, DEFAULT_KEYWORDS)
        self.entry_kw.grid(row=4, column=1, padx=10, pady=5, sticky="ew")

        # 5. 数据源配置
        self.label_sources = ctk.CTkLabel(self, text="数据源 (逗号分隔):")
        self.label_sources.grid(row=5, column=0, padx=20, pady=5, sticky="e")

        self.entry_sources = ctk.CTkEntry(self)
        self.entry_sources.insert(0, "openalex,crossref,semantic_scholar")
        self.entry_sources.grid(row=5, column=1, padx=10, pady=5, sticky="ew")

        # 6. Tesseract 路径
        self.label_tess = ctk.CTkLabel(self, text="Tesseract 路径:")
        self.label_tess.grid(row=6, column=0, padx=20, pady=5, sticky="e")

        self.entry_tess = ctk.CTkEntry(self)
        self.entry_tess.insert(0, DEFAULT_TESSERACT_PATH)
        self.entry_tess.grid(row=6, column=1, padx=10, pady=5, sticky="ew")

        # 6. 进度条
        self.progress_bar = ctk.CTkProgressBar(self)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=8, column=0, columnspan=5, padx=20, pady=10, sticky="ew")

        self.progress_label = ctk.CTkLabel(self, text="", text_color="gray")
        self.progress_label.grid(row=8, column=5, padx=10, pady=10, sticky="w")

        # 7. 控制按钮
        self.btn_start = ctk.CTkButton(
            self,
            text="开始执行任务",
            fg_color="green",
            hover_color="darkgreen",
            height=40,
            command=self.start_task
        )
        self.btn_start.grid(row=9, column=1, pady=20)

        self.btn_stop = ctk.CTkButton(
            self,
            text="停止",
            fg_color="red",
            state="disabled",
            command=self.stop_task
        )
        self.btn_stop.grid(row=9, column=2, pady=20)

    # --- 界面逻辑 ---
    def clear_src_folders(self):
        """清空已选择的文件夹列表"""
        self.selected_src_folders = []
        self.entry_path.delete(0, "end")
        self.entry_path.insert(0, "请选择一个或多个包含 PDF 的文件夹...")
        self.label_folder_count.configure(text="(未选择)")
        self.log("已清空已选择的文件夹列表")

    def add_src_folders(self, folder_paths: List[str]):
        """添加多个源文件夹到列表中，去重，并更新 UI"""
        added = 0
        for path in folder_paths:
            if not path:
                continue
            path = os.path.abspath(path)
            if os.path.isdir(path):
                if path not in self.selected_src_folders:
                    self.selected_src_folders.append(path)
                    self.log(f"已添加文件夹：{path}")
                    added += 1
                else:
                    self.log(f"该文件夹已添加：{path}")
            else:
                self.log(f"不是有效目录，已忽略：{path}")

        if self.selected_src_folders:
            display_count = min(3, len(self.selected_src_folders))
            self.entry_path.delete(0, "end")
            display_text = "; ".join(self.selected_src_folders[:display_count])
            if len(self.selected_src_folders) > display_count:
                display_text += f" 等{len(self.selected_src_folders)}个文件夹"
            self.entry_path.insert(0, display_text)
            self.label_folder_count.configure(text=f"(已选{len(self.selected_src_folders)}个文件夹)")

            if added == len(self.selected_src_folders) and not self.entry_output_path.get():
                suggested_out = os.path.join(self.selected_src_folders[0], "Result_Metadata.csv")
                self.entry_output_path.delete(0, "end")
                self.entry_output_path.insert(0, suggested_out)

    def browse_src_folder(self):
        """支持多选文件夹（兼容单选），并添加到目标列表"""
        import tkinter as tk

        temp_tk = tk.Tk()
        temp_tk.withdraw()

        try:
            folder_paths = None
            try:
                folder_paths = filedialog.askdirectory(
                    title="选择 PDF 源文件夹 (可多选)",
                    initialdir=os.path.expanduser("~"),
                    mustexist=True,
                    multiple=True
                )
            except Exception:
                folder_paths = filedialog.askdirectory(
                    title="选择 PDF 源文件夹",
                    initialdir=os.path.expanduser("~"),
                    mustexist=True
                )

            if not folder_paths:
                return

            if isinstance(folder_paths, (tuple, list)):
                self.add_src_folders(folder_paths)
            else:
                self.add_src_folders([folder_paths])

        except Exception as e:
            self.log(f"选择文件夹出错：{e}")
        finally:
            temp_tk.destroy()

    def browse_output_file(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            title="选择保存结果的文件名"
        )
        if path:
            self.entry_output_path.delete(0, "end")
            self.entry_output_path.insert(0, path)

    def browse_classify_folder(self):
        path = filedialog.askdirectory(
            title="选择归类输出根目录",
            initialdir=os.path.expanduser("~"),
            mustexist=False
        )
        if path:
            self.entry_classify_folder.delete(0, "end")
            self.entry_classify_folder.insert(0, path)

    def log(self, message: str):
        """线程安全的日志输出方法"""

        def _log():
            self.log_box.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
            self.log_box.see("end")

        if threading.current_thread() is not threading.main_thread():
            self.after(0, _log)
        else:
            _log()

    def update_progress(self, value: float, current: int, total: int, filename: str = ""):
        """更新进度条和标签"""

        def _update():
            self.progress_bar.set(value / 100)
            self.progress_label.configure(text=f"{current}/{total} - {filename[:50]}")

        if threading.current_thread() is not threading.main_thread():
            self.after(0, _update)
        else:
            _update()

    # --- 核心任务逻辑 ---
    def process_logic(self):
        target_dirs = self.selected_src_folders.copy()
        output_file = self.entry_output_path.get()
        email = self.entry_email.get().strip()
        keywords = self.entry_kw.get()
        tesseract_path = self.entry_tess.get().strip()

        # 预检查
        if not target_dirs or not output_file:
            messagebox.showerror("错误", "请同时指定源文件夹和结果保存位置！")
            self.reset_ui()
            return

        if not email:
            email = DEFAULT_EMAIL

        # 验证文件夹
        valid_dirs = []
        for dir_path in target_dirs:
            if os.path.isdir(dir_path):
                valid_dirs.append(dir_path)
            else:
                self.log(f"警告：文件夹不存在 - {dir_path}")

        if not valid_dirs:
            messagebox.showerror("错误", "所有选中的文件夹都不存在！")
            self.reset_ui()
            return

        # 额外选项
        recurse = self.subfolder_switch.get()
        classify = self.classify_switch.get()
        classify_folder = self.entry_classify_folder.get().strip()

        if classify and not classify_folder:
            messagebox.showerror("错误", "已选自动归类，请先指定归类结果目录！")
            self.reset_ui()
            return

        # 解析数据源
        sources_text = self.entry_sources.get().strip() if hasattr(self, 'entry_sources') else ""
        if sources_text:
            enabled_sources = [s.strip() for s in sources_text.split(',') if s.strip()]
        else:
            enabled_sources = ['openalex', 'crossref', 'semantic_scholar']  # 默认

        # 初始化提取器
        self.extractor = PaperMetadataExtractor(
            email=email,
            keywords=keywords,
            tesseract_path=tesseract_path,
            enable_ocr=self.ocr_switch.get(),
            enabled_sources=enabled_sources
        )

        # 收集 PDF 文件
        pdf_files = self.collect_pdf_files(valid_dirs, recurse)
        total = len(pdf_files)
        self.log(f"共找到 {total} 个 PDF 文件（来自 {len(valid_dirs)} 个文件夹）")

        # 续传读取
        results = []
        done_files: Set[str] = set()
        if os.path.exists(output_file):
            try:
                old_df = pd.read_csv(output_file)
                results = old_df.to_dict('records')
                done_files = set(old_df['文件名'].astype(str).tolist())
                self.log(f"检测到已有结果文件，已跳过 {len(done_files)} 个。")
            except Exception as e:
                self.log(f"读取旧文件失败：{e}，将重新开始。")

        # 处理文件
        processed = 0
        for i, (current_dir, filename) in enumerate(pdf_files):
            if self.stop_requested:
                self.log("停止请求已响应。正在保存当前数据...")
                break

            if filename in done_files:
                continue

            file_path = os.path.join(current_dir, filename)

            # 处理 PDF
            row = self.extractor.process_pdf(file_path)

            # 归类文件
            if classify and classify_folder:
                self.classify_file(file_path, row.get("状态", "失败"), classify_folder)

            results.append(row)
            processed += 1

            # 更新进度
            progress = (i + 1) / total * 100
            self.update_progress(progress, i + 1, total, filename)

            # 定期保存
            if (i + 1) % SAVE_INTERVAL == 0:
                self.save_results(results, output_file)

        # 最终保存
        self.save_results(results, output_file)
        self.log(f"任务完成！处理：{processed} 个文件，结果已存至：{output_file}")

        # 写入日志
        self.write_task_log(output_file, results, valid_dirs, stopped=self.stop_requested)

        self.reset_ui()

    def collect_pdf_files(self, target_dirs: List[str], recurse: bool) -> List[Tuple[str, str]]:
        """收集所有 PDF 文件"""
        pdf_files = []
        skip_dirs = {'完成', '失败', 'success', 'failed'}

        for target_dir in target_dirs:
            if recurse:
                for root, dirs, files in os.walk(target_dir):
                    dirs[:] = [d for d in dirs if d not in skip_dirs]
                    for f in files:
                        if f.lower().endswith('.pdf'):
                            pdf_files.append((root, f))
            else:
                try:
                    files = [f for f in os.listdir(target_dir) if f.lower().endswith('.pdf')]
                    pdf_files.extend([(target_dir, f) for f in files])
                except Exception as e:
                    self.log(f"读取目录失败 {target_dir}: {e}")

        return pdf_files

    def classify_file(self, file_path: str, status: str, classify_folder: str):
        """归类文件到成功/失败目录"""
        try:
            filename = os.path.basename(file_path)
            dest_folder = os.path.join(
                classify_folder,
                "完成" if status == "成功" else "失败"
            )
            os.makedirs(dest_folder, exist_ok=True)
            dest_path = os.path.join(dest_folder, filename)

            if os.path.exists(dest_path):
                os.remove(dest_path)

            shutil.copy2(file_path, dest_path)
            self.log(f"已复制归类 {filename} -> {dest_folder}")
        except Exception as exc:
            self.log(f"归类失败 {os.path.basename(file_path)}: {exc}")

    def save_results(self, results: List[Dict], output_file: str):
        """保存结果到 CSV"""
        try:
            df = pd.DataFrame(results)
            df.to_csv(output_file, index=False, encoding='utf-8-sig')
        except Exception as e:
            self.log(f"保存结果失败：{e}")

    def write_task_log(self, output_file: str, results: List[Dict],
                       folders: List[str], stopped: bool = False):
        """每次执行完成后写入任务摘要日志"""
        total = len(results)
        success_count = sum(1 for r in results if r.get("状态") == "成功")
        failed_count = total - success_count
        timestamp = time.strftime("%Y-%m-%d %H:%M")
        status = "已终止" if stopped else "已完成"
        folder_list = "; ".join(folders)
        log_path = os.path.splitext(output_file)[0] + "_task_log.txt"

        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {status}：\n")
                f.write(f"  处理目录：{folder_list}\n")
                f.write(f"  总文件：{total}，成功：{success_count}，失败：{failed_count}\n")
                f.write(f"  结果文件：{output_file}\n")
                f.write("------------------------------\n")
            self.log(f"已写入任务日志：{log_path}")
        except Exception as e:
            self.log(f"写入任务日志失败：{e}")

    def start_task(self):
        self.is_running = True
        self.stop_requested = False
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        threading.Thread(target=self.process_logic, daemon=True).start()

    def stop_task(self):
        self.stop_requested = True
        self.btn_stop.configure(state="disabled")
        self.log("正在停止任务...")

    def reset_ui(self):
        self.is_running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.update_progress(0, 0, 1, "")


if __name__ == "__main__":
    app = PaperToolGUI()
    app.mainloop()
