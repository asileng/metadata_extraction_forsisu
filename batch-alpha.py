#!/usr/bin/env python3
"""
PDF文献元数据批量提取工具 Alpha版
多级分流架构 + 高并发处理 + 超时控制

架构设计：
- Tier 1 (快车道): 有文本层 + 正则匹配DOI → 直接验证
- Tier 2 (中车道): 有文本层 + 无DOI → DeepSeek合并处理
- Tier 3 (慢车道): 扫描件(文字<100字) → OCR/视觉模型

性能优化：
- ThreadPoolExecutor 并发处理 (max_workers=20)
- 指数退避处理429错误
- 每10条自动保存
- 完整错误类型追踪
- 单文件超时控制 (30秒)

文件归类：
- 成功文件 -> 完成/
- 失败文件 -> 失败/{error_type}/
"""

import os
import re
import time
import threading
import shutil
from typing import List, Dict, Optional, Tuple, Set
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import json

import fitz  # PyMuPDF
import requests
import pandas as pd
import customtkinter as ctk
from tkinter import filedialog, messagebox

from llm_enhancer_alpha import LLMEnhancerAlpha

# --- 常量配置 ---
APP_VERSION = "4.2-alpha"
DEFAULT_EMAIL = "kodwabeamer@gmail.com"
DEFAULT_KEYWORDS = "sign language, deaf, fingerspelling, asl, csl, gloss recognition, ASL, sign, deaf, mute, multimodal, embodiment, iconicity, gesture, metaphor, 手势, 手语, 模态, 聋, 隐喻"

# API配置
OPENALEX_API_URL = "https://api.openalex.org/works/https://doi.org/"
CROSSREF_API_URL = "https://api.crossref.org/works/"
SEMANTIC_SCHOLAR_API_URL = "https://api.semanticscholar.org/graph/v1/paper/"
API_TIMEOUT = 15  # 超时15秒

# 性能配置
SAVE_INTERVAL = 10  # 每10条保存
MAX_WORKERS = 20  # 并发数
TEXT_THRESHOLD = 100  # 扫描件判定阈值（字符数）
DEFAULT_FILE_TIMEOUT = 20  # 默认单文件处理超时时间（秒）
DEFAULT_PAGE_COUNT = 3  # 默认截取页数

# --- 设置外观 ---
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


@dataclass
class ProcessingResult:
    """处理结果数据类"""
    filename: str = ""
    title: str = "N/A"
    language: str = "N/A"
    authors: str = "N/A"
    year: str = "N/A"
    journal: str = "N/A"
    volume_issue: str = "N/A"
    doi: str = "N/A"
    status: str = "失败"
    error_type: str = ""
    source_folder: str = ""
    tier: str = ""  # 记录走的是哪个车道


class MultiSourceMetadataFetcher:
    """多数据源元数据获取器"""

    def __init__(self, email: str, session: requests.Session):
        self.email = email
        self.session = session
        self.enabled_sources = ['openalex', 'crossref', 'semantic_scholar']

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
        """从 Crossref 获取元数据（最终验证首选）"""
        try:
            resp = self.session.get(
                f"{CROSSREF_API_URL}{doi}",
                headers={'User-Agent': f'PaperMetadataTool/4.0 (mailto:{self.email})'},
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

    def validate_doi(self, doi: str) -> Tuple[Optional[Dict], str]:
        """
        验证DOI并获取元数据（逻辑6：最终验证）
        优先使用CrossRef验证
        """
        # 优先CrossRef
        metadata = self.fetch_from_crossref(doi)
        if metadata:
            return self._normalize_crossref(metadata), 'crossref'

        # 备选OpenAlex
        metadata = self.fetch_from_openalex(doi)
        if metadata:
            return self._normalize_openalex(metadata), 'openalex'

        # 备选Semantic Scholar
        metadata = self.fetch_from_semantic_scholar(doi)
        if metadata:
            return self._normalize_semantic_scholar(metadata), 'semantic_scholar'

        return None, ''

    def _normalize_openalex(self, metadata: Dict) -> Dict:
        """标准化 OpenAlex 数据格式"""
        authors = [a['author']['display_name'] for a in metadata.get('authorships', [])]
        biblio = metadata.get('biblio', {})
        volume = biblio.get('volume')
        issue = biblio.get('issue')

        doi = metadata.get('doi', '')
        if doi.startswith('https://doi.org/'):
            doi = doi[16:]

        issue_volume = self._format_issue_volume(issue, volume)

        return {
            '论文题目': metadata.get('title', ''),
            '作者': authors,
            '年份': metadata.get('publication_year'),
            '语种': metadata.get('language', ''),
            '来源（出版社/期刊）': (
                metadata.get('primary_location', {})
                .get('source', {})
                .get('display_name', '')
            ) if metadata.get('primary_location') else '',
            '期（卷）': issue_volume,
            'DOI': doi
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

        volume = metadata.get('volume')
        issue = metadata.get('issue')
        issue_volume = self._format_issue_volume(issue, volume)

        return {
            '论文题目': ' '.join(metadata.get('title', [])),
            '作者': authors,
            '年份': metadata.get('published-print', {}).get('date-parts', [[None]])[0][0],
            '语种': metadata.get('language', ''),
            '来源（出版社/期刊）': metadata.get('container-title', [''])[0],
            '期（卷）': issue_volume,
            'DOI': metadata.get('DOI', '')
        }

    def _normalize_semantic_scholar(self, metadata: Dict) -> Dict:
        """标准化 Semantic Scholar 数据格式"""
        authors = [author.get('name', '') for author in metadata.get('authors', [])]
        volume = metadata.get('volume')
        issue = metadata.get('issue')
        issue_volume = self._format_issue_volume(issue, volume)

        return {
            '论文题目': metadata.get('title', ''),
            '作者': authors,
            '年份': metadata.get('year'),
            '语种': '',
            '来源（出版社/期刊）': metadata.get('venue', ''),
            '期（卷）': issue_volume,
            'DOI': metadata.get('externalIds', {}).get('DOI', '')
        }

    def _format_issue_volume(self, issue: Optional[str], volume: Optional[str]) -> str:
        """格式化期卷为 期（卷）"""
        if issue and volume:
            return f"{issue}({volume})"
        elif issue:
            return f"{issue}()"
        elif volume:
            return f"()({volume})"
        return "N/A"


class PaperMetadataExtractorAlpha:
    """PDF 文献元数据提取器 - Alpha版（多级分流）"""

    def __init__(self, email: str, keywords: str,
                 enabled_sources: List[str] = None,
                 max_workers: int = MAX_WORKERS,
                 file_timeout: int = DEFAULT_FILE_TIMEOUT,
                 page_count: int = DEFAULT_PAGE_COUNT,
                 log_callback=None):
        self.email = email
        self.keywords = keywords
        self.session = requests.Session()
        self.max_workers = max_workers
        self.file_timeout = file_timeout
        self.page_count = page_count
        self.log_callback = log_callback

        # 初始化组件
        self.metadata_fetcher = MultiSourceMetadataFetcher(email, self.session)
        if enabled_sources:
            self.metadata_fetcher.set_enabled_sources(enabled_sources)

        # 传递日志回调给LLM增强器
        self.llm_enhancer = LLMEnhancerAlpha(log_callback=log_callback)

        # 统计信息
        self.stats = {
            'tier1_count': 0,
            'tier2_count': 0,
            'tier3_count': 0,
            'success_count': 0,
            'skipped_count': 0,
            'failed_count': 0,
            'timeout_count': 0
        }
        self.stats_lock = threading.Lock()

    def log(self, message: str, thread_id: str = ""):
        """线程安全的日志输出，同时输出到控制台和GUI回调"""
        prefix = f"[T{thread_id}] " if thread_id else ""
        formatted_msg = f"[{time.strftime('%H:%M:%S')}]{prefix}{message}"
        print(formatted_msg)  # 控制台输出
        if self.log_callback:
            self.log_callback(formatted_msg)  # GUI回调

    def extract_pages_from_pdf(self, file_path: str) -> str:
        """
        从PDF截取前N页，保存为临时文件并返回路径
        用于降低内存消耗，加速处理大型PDF
        """
        try:
            from pypdf import PdfReader, PdfWriter

            temp_dir = os.path.dirname(file_path)
            temp_filename = f"temp_alpha_{os.path.basename(file_path)}"
            temp_path = os.path.join(temp_dir, temp_filename)

            # 读取原始PDF
            reader = PdfReader(file_path)
            total_pages = len(reader.pages)

            # 计算实际截取页数
            actual_pages = min(self.page_count, total_pages)

            # 创建新的PDF
            writer = PdfWriter()
            for i in range(actual_pages):
                writer.add_page(reader.pages[i])

            # 写入临时文件
            with open(temp_path, 'wb') as output_file:
                writer.write(output_file)

            return temp_path

        except Exception as e:
            self.log(f"页面截取失败，使用原文件: {str(e)[:50]}")
            return file_path

    def detect_language(self, text: str) -> str:
        """检测文本语言"""
        if not text:
            return "N/A"
        if any('\u4e00' <= c <= '\u9fff' for c in text):
            return "中文"
        return "英文"

    def format_apa_authors(self, author_list: List[str]) -> str:
        """
        APA格式作者转换
        规则：
        - 1人：Johnston, T.
        - 2人：Johnston, T., & Cresdee, D.
        - 3人：Johnston, T., Cresdee, D., & Schembri, A.
        - 4人以上：Johnston, T., Cresdee, D., Schembri, A., et al.
        """
        if not author_list:
            return "N/A"

        formatted = []
        for name in author_list:
            if not name:
                continue
            parts = name.strip().split()
            if len(parts) >= 2:
                # 姓在前，名取首字母
                formatted.append(f"{parts[-1]}, {parts[0][0]}.")
            elif len(parts) == 1:
                formatted.append(parts[0])

        count = len(formatted)
        if count == 0:
            return "N/A"
        elif count == 1:
            return formatted[0]
        elif count == 2:
            return f"{formatted[0]}, & {formatted[1]}"
        elif count == 3:
            return f"{formatted[0]}, {formatted[1]}, & {formatted[2]}"
        else:
            return f"{formatted[0]}, {formatted[1]}, {formatted[2]}, et al."

    def extract_text_fitz(self, pdf_path: str, max_pages: int = 3) -> Tuple[str, bool]:
        """
        使用fitz快速提取文本（用于分流判断）
        Returns: (text, has_text_layer)
        """
        try:
            doc = fitz.open(pdf_path)
            text_parts = []
            has_real_text = False

            for i in range(min(max_pages, len(doc))):
                page = doc[i]
                text = page.get_text()
                text_parts.append(text)
                if len(text.strip()) > TEXT_THRESHOLD:
                    has_real_text = True

            doc.close()
            return "".join(text_parts), has_real_text

        except Exception as e:
            return "", False

    def process_pdf(self, file_path: str, thread_id: str = "") -> ProcessingResult:
        """
        处理单个PDF - 多级分流实现

        分流逻辑：
        Tier 1: 有文本层 + 正则匹配DOI → API验证
        Tier 2: 有文本层 + 无DOI → DeepSeek合并处理
        Tier 3: 扫描件(文字<100字) → OCR/视觉模型

        超时控制：单文件处理超过 file_timeout 秒则标记为超时
        """
        filename = os.path.basename(file_path)
        current_dir = os.path.dirname(file_path)

        result = ProcessingResult(
            filename=filename,
            source_folder=os.path.basename(current_dir)
        )

        # ===== 超时控制 =====
        start_time = time.time()
        temp_file_path = None  # 用于追踪临时文件

        def check_timeout() -> bool:
            """检查是否超时，超时返回True"""
            return (time.time() - start_time) > self.file_timeout

        def cleanup_temp_file():
            """清理临时文件"""
            nonlocal temp_file_path
            if temp_file_path and os.path.exists(temp_file_path) and temp_file_path != file_path:
                try:
                    os.remove(temp_file_path)
                except:
                    pass

        def handle_timeout():
            """处理超时情况"""
            result.status = "失败"
            result.error_type = "超时"
            result.tier = result.tier or "超时"
            with self.stats_lock:
                self.stats['failed_count'] += 1
                self.stats['timeout_count'] += 1
            elapsed = time.time() - start_time
            self.log(f"⏱️ 处理超时 ({elapsed:.1f}s): {filename}", thread_id)
            cleanup_temp_file()

        try:
            self.log(f"开始处理: {filename}", thread_id)

            # ===== 页面截取：降低内存消耗 =====
            temp_file_path = self.extract_pages_from_pdf(file_path)
            actual_file_path = temp_file_path if temp_file_path != file_path else file_path

            # ===== 逻辑1: 识别层 - 判断是否有文本层 =====
            text, has_text_layer = self.extract_text_fitz(actual_file_path, max_pages=self.page_count)
            text_length = len(text.strip())

            # 超时检查点1
            if check_timeout():
                handle_timeout()
                return result

            # ===== 分流判断 =====
            if has_text_layer and text_length >= TEXT_THRESHOLD:
                # 有文本层，进入Tier 1或Tier 2
                doi = self.llm_enhancer.extract_doi_from_text(text)

                if doi:
                    # ===== Tier 1: 快车道 - 有DOI直接验证 =====
                    result.tier = "Tier1"
                    with self.stats_lock:
                        self.stats['tier1_count'] += 1

                    self.log(f"[Tier1] DOI正则匹配成功: {doi}", thread_id)

                    # 逻辑6: 最终验证
                    metadata, source = self.metadata_fetcher.validate_doi(doi)

                    # 超时检查点2
                    if check_timeout():
                        handle_timeout()
                        return result

                    if metadata:
                        self._fill_result(result, metadata, text)
                        result.status = "成功"
                        result.doi = doi
                        with self.stats_lock:
                            self.stats['success_count'] += 1
                        self.log(f"[Tier1] API验证成功: {source}", thread_id)
                        cleanup_temp_file()
                        return result
                    else:
                        # DOI无效，降级到Tier 2
                        result.error_type = "DOI无效"
                        self.log(f"[Tier1->Tier2] DOI验证失败，降级处理", thread_id)

                # ===== Tier 2: 中车道 - DeepSeek合并处理 =====
                result.tier = "Tier2"
                with self.stats_lock:
                    self.stats['tier2_count'] += 1

                self.log(f"[Tier2] 开始DeepSeek合并分析", thread_id)

                # 逻辑3: DeepSeek合并（相关性+元数据）
                metadata, error_type = self.llm_enhancer.combined_analysis(
                    text, filename, self.keywords, f"[T{thread_id}]"
                )

                # 超时检查点3
                if check_timeout():
                    handle_timeout()
                    return result

                if error_type == "不相关跳过":
                    result.status = "跳过"
                    result.error_type = "不相关跳过"
                    with self.stats_lock:
                        self.stats['skipped_count'] += 1
                    self.log(f"[Tier2] 不相关，跳过", thread_id)
                    cleanup_temp_file()
                    return result

                if metadata and metadata.get("is_related"):
                    self._fill_result(result, metadata, text)
                    result.status = "成功"
                    with self.stats_lock:
                        self.stats['success_count'] += 1
                    self.log(f"[Tier2] DeepSeek提取成功", thread_id)
                    cleanup_temp_file()
                    return result

                # Tier 2失败，降级到视觉层
                result.error_type = error_type or "LLM提取失败"
                self.log(f"[Tier2->Tier3] 提取失败，尝试视觉层", thread_id)

            # ===== Tier 3: 慢车道 - 扫描件处理 =====
            result.tier = "Tier3"
            with self.stats_lock:
                self.stats['tier3_count'] += 1

            self.log(f"[Tier3] 扫描件处理开始", thread_id)

            # 尝试OCR提取（使用截取后的文件）
            if text_length < TEXT_THRESHOLD:
                ocr_text, ocr_error = self.llm_enhancer.ocr_extract_text(actual_file_path, pages=[0, 1, 2])

                # 超时检查点4
                if check_timeout():
                    handle_timeout()
                    return result

                if ocr_text and len(ocr_text) > TEXT_THRESHOLD:
                    # OCR成功，尝试从OCR文本提取DOI
                    doi = self.llm_enhancer.extract_doi_from_text(ocr_text)
                    if doi:
                        metadata, source = self.metadata_fetcher.validate_doi(doi)
                        if metadata:
                            self._fill_result(result, metadata, ocr_text)
                            result.status = "成功"
                            result.doi = doi
                            with self.stats_lock:
                                self.stats['success_count'] += 1
                            self.log(f"[Tier3] OCR+DOI验证成功", thread_id)
                            cleanup_temp_file()
                            return result

                    # OCR文本进行LLM分析
                    metadata, error_type = self.llm_enhancer.combined_analysis(
                        ocr_text, filename, self.keywords, f"[T{thread_id}]"
                    )
                    if metadata and metadata.get("is_related"):
                        self._fill_result(result, metadata, ocr_text)
                        result.status = "成功"
                        with self.stats_lock:
                            self.stats['success_count'] += 1
                        self.log(f"[Tier3] OCR+LLM提取成功", thread_id)
                        cleanup_temp_file()
                        return result

            # 超时检查点5 - 视觉层前最后检查
            if check_timeout():
                handle_timeout()
                return result

            # 逻辑4&5: Kimi视觉层（使用原始文件，因为需要完整图像）
            self.log(f"[Tier3] 尝试视觉模型", thread_id)
            metadata, error_type = self.llm_enhancer.extract_metadata_with_vision(
                file_path, filename, f"[T{thread_id}]"
            )

            if error_type == "不相关跳过":
                result.status = "跳过"
                result.error_type = "不相关跳过"
                with self.stats_lock:
                    self.stats['skipped_count'] += 1
                cleanup_temp_file()
                return result

            if metadata and metadata.get("is_related"):
                self._fill_result(result, metadata, "")
                result.status = "成功"
                with self.stats_lock:
                    self.stats['success_count'] += 1
                self.log(f"[Tier3] 视觉提取成功", thread_id)
                cleanup_temp_file()
                return result

            # 全部失败
            result.error_type = error_type or "视觉失败"
            result.status = "失败"
            with self.stats_lock:
                self.stats['failed_count'] += 1
            self.log(f"[Tier3] 处理失败: {result.error_type}", thread_id)

        except Exception as e:
            result.status = "失败"
            result.error_type = f"处理异常: {str(e)[:50]}"
            with self.stats_lock:
                self.stats['failed_count'] += 1
            self.log(f"处理异常: {str(e)}", thread_id)

        finally:
            # 确保临时文件被清理
            cleanup_temp_file()

        return result

    def _fill_result(self, result: ProcessingResult, metadata: Dict, text: str):
        """填充结果数据"""
        authors = metadata.get("作者", metadata.get("authors", []))
        if isinstance(authors, str):
            authors = [a.strip() for a in authors.split(",")]

        result.title = metadata.get("论文题目") or metadata.get("title") or "N/A"
        result.authors = self.format_apa_authors(authors)
        result.year = str(metadata.get("年份") or metadata.get("year") or "N/A")
        result.journal = metadata.get("来源（出版社/期刊）") or metadata.get("journal") or "N/A"
        result.volume_issue = metadata.get("期（卷）") or metadata.get("volume_issue") or "N/A"
        result.doi = metadata.get("DOI") or metadata.get("doi") or "N/A"
        result.language = self.detect_language(text) if text else "N/A"


class PaperToolGUIAlpha(ctk.CTk):
    """文献元数据自动化获取工具 - Alpha版GUI"""

    def __init__(self):
        super().__init__()

        self.title(f"文献元数据自动化获取工具 v{APP_VERSION} (高并发版)")
        self.geometry("1000x900")
        self.minsize(950, 850)

        # 运行状态变量
        self.is_running = False
        self.stop_requested = False
        self.selected_src_folders: List[str] = []
        self.extractor: Optional[PaperMetadataExtractorAlpha] = None
        self.results: List[Dict] = []
        self.results_lock = threading.Lock()

        # --- UI 布局配置 ---
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(12, weight=1)

        self._setup_ui()

    def _setup_ui(self):
        """初始化 UI 组件"""
        # 版本信息标签
        self.label_version = ctk.CTkLabel(
            self,
            text="🚀 Alpha版 - 多级分流架构 + 20线程并发",
            text_color="green",
            font=("", 12, "bold")
        )
        self.label_version.grid(row=0, column=0, columnspan=5, padx=20, pady=5)

        # 1. 目标文件夹选择
        self.label_path = ctk.CTkLabel(self, text="PDF 源文件夹:")
        self.label_path.grid(row=1, column=0, padx=20, pady=10, sticky="e")

        self.entry_path = ctk.CTkEntry(
            self,
            placeholder_text="请选择一个或多个包含 PDF 的文件夹...",
            width=450
        )
        self.entry_path.grid(row=1, column=1, padx=10, pady=10, sticky="ew")

        self.btn_browse_src = ctk.CTkButton(
            self,
            text="选择源目录 (可多选)",
            command=self.browse_src_folder
        )
        self.btn_browse_src.grid(row=1, column=2, padx=10, pady=10)

        self.btn_clear_src = ctk.CTkButton(
            self,
            text="清空已选",
            fg_color="#c0392b",
            hover_color="#a93226",
            width=80,
            command=self.clear_src_folders
        )
        self.btn_clear_src.grid(row=1, column=4, padx=5, pady=10)

        self.label_folder_count = ctk.CTkLabel(
            self,
            text="(未选择)",
            text_color="gray"
        )
        self.label_folder_count.grid(row=1, column=3, padx=5, pady=10, sticky="w")

        # 2. 结果保存位置
        self.label_out = ctk.CTkLabel(self, text="结果保存位置:")
        self.label_out.grid(row=2, column=0, padx=20, pady=10, sticky="e")

        self.entry_output_path = ctk.CTkEntry(
            self,
            placeholder_text="请选择 CSV 结果保存的完整路径...",
            width=450
        )
        self.entry_output_path.grid(row=2, column=1, padx=10, pady=10, sticky="ew")

        self.btn_browse_out = ctk.CTkButton(
            self,
            text="设置保存位置",
            fg_color="#1f538d",
            command=self.browse_output_file
        )
        self.btn_browse_out.grid(row=2, column=2, padx=10, pady=10)

        # 3. 分类结果目录
        self.label_classify_folder = ctk.CTkLabel(self, text="归类结果目录:")
        self.label_classify_folder.grid(row=3, column=0, padx=20, pady=5, sticky="e")

        self.entry_classify_folder = ctk.CTkEntry(
            self,
            placeholder_text="可选：所有成功/失败 PDF 统一输出目录",
            width=450
        )
        self.entry_classify_folder.grid(row=3, column=1, padx=10, pady=5, sticky="ew")

        self.btn_browse_classify = ctk.CTkButton(
            self,
            text="选择归类目录",
            fg_color="#1f538d",
            command=self.browse_classify_folder
        )
        self.btn_browse_classify.grid(row=3, column=2, padx=10, pady=5)

        # 4. 配置项 - 第一行
        self.label_email = ctk.CTkLabel(self, text="OpenAlex Email:")
        self.label_email.grid(row=4, column=0, padx=20, pady=5, sticky="e")

        self.entry_email = ctk.CTkEntry(self, placeholder_text=DEFAULT_EMAIL)
        self.entry_email.insert(0, DEFAULT_EMAIL)
        self.entry_email.grid(row=4, column=1, padx=10, pady=5, sticky="ew")

        # 并发数设置
        self.label_workers = ctk.CTkLabel(self, text="并发线程数:")
        self.label_workers.grid(row=4, column=2, padx=10, pady=5, sticky="e")

        self.entry_workers = ctk.CTkEntry(self, width=50, placeholder_text="20")
        self.entry_workers.insert(0, "20")
        self.entry_workers.grid(row=4, column=3, padx=5, pady=5, sticky="w")

        # 5. 开关选项
        self.subfolder_switch = ctk.CTkSwitch(self, text="递归搜索子目录")
        self.subfolder_switch.select()
        self.subfolder_switch.grid(row=5, column=1, padx=10, pady=5, sticky="w")

        self.classify_switch = ctk.CTkSwitch(self, text="处理后自动归类文件")
        self.classify_switch.grid(row=5, column=2, padx=10, pady=5, sticky="w")

        # 5.5 性能参数配置行
        self.label_timeout = ctk.CTkLabel(self, text="超时时间(秒):")
        self.label_timeout.grid(row=5, column=3, padx=5, pady=5, sticky="e")

        self.entry_timeout = ctk.CTkEntry(self, width=50, placeholder_text="20")
        self.entry_timeout.insert(0, str(DEFAULT_FILE_TIMEOUT))
        self.entry_timeout.grid(row=5, column=4, padx=5, pady=5, sticky="w")

        self.label_pages = ctk.CTkLabel(self, text="截取页数:")
        self.label_pages.grid(row=5, column=5, padx=5, pady=5, sticky="e")

        self.entry_pages = ctk.CTkEntry(self, width=50, placeholder_text="3")
        self.entry_pages.insert(0, str(DEFAULT_PAGE_COUNT))
        self.entry_pages.grid(row=5, column=6, padx=5, pady=5, sticky="w")

        # 6. 关键词配置
        self.label_kw = ctk.CTkLabel(self, text="手语关键词:")
        self.label_kw.grid(row=6, column=0, padx=20, pady=5, sticky="e")

        self.entry_kw = ctk.CTkEntry(self)
        self.entry_kw.insert(0, DEFAULT_KEYWORDS)
        self.entry_kw.grid(row=6, column=1, padx=10, pady=5, sticky="ew")

        # 7. 数据源配置
        self.label_sources = ctk.CTkLabel(self, text="数据源 (逗号分隔):")
        self.label_sources.grid(row=7, column=0, padx=20, pady=5, sticky="e")

        self.entry_sources = ctk.CTkEntry(self)
        self.entry_sources.insert(0, "crossref,openalex,semantic_scholar")
        self.entry_sources.grid(row=7, column=1, padx=10, pady=5, sticky="ew")

        # 8. 统计信息框
        self.stats_frame = ctk.CTkFrame(self)
        self.stats_frame.grid(row=8, column=0, columnspan=7, padx=20, pady=10, sticky="ew")

        self.label_stats = ctk.CTkLabel(
            self.stats_frame,
            text="📊 统计: 等待开始...",
            font=("", 11)
        )
        self.label_stats.pack(padx=10, pady=5)

        # 9. 进度条
        self.progress_bar = ctk.CTkProgressBar(self)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=9, column=0, columnspan=5, padx=20, pady=10, sticky="ew")

        self.progress_label = ctk.CTkLabel(self, text="", text_color="gray")
        self.progress_label.grid(row=9, column=5, padx=10, pady=10, sticky="w")

        # 10. 控制按钮
        self.btn_start = ctk.CTkButton(
            self,
            text="开始执行任务 (高并发)",
            fg_color="green",
            hover_color="darkgreen",
            height=45,
            command=self.start_task
        )
        self.btn_start.grid(row=10, column=1, pady=15, sticky="ew")

        self.btn_stop = ctk.CTkButton(
            self,
            text="停止",
            fg_color="red",
            state="disabled",
            command=self.stop_task
        )
        self.btn_stop.grid(row=10, column=2, pady=15, sticky="ew")

        # 11. 日志框
        self.log_box = ctk.CTkTextbox(self, height=280)
        self.log_box.grid(row=12, column=0, columnspan=5, padx=20, pady=10, sticky="nsew")

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
                suggested_out = os.path.join(self.selected_src_folders[0], "Result_Metadata_Alpha.csv")
                self.entry_output_path.delete(0, "end")
                self.entry_output_path.insert(0, suggested_out)

    def browse_src_folder(self):
        """支持多选文件夹"""
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
        """线程安全的日志输出方法，同时输出到控制台和GUI"""
        # 控制台输出
        print(message)

        # GUI输出
        def _log():
            self.log_box.insert("end", f"{message}\n")
            self.log_box.see("end")

        if threading.current_thread() is not threading.main_thread():
            self.after(0, _log)
        else:
            _log()

    def update_progress(self, value: float, current: int, total: int, filename: str = ""):
        """更新进度条和标签"""
        def _update():
            self.progress_bar.set(value / 100)
            self.progress_label.configure(text=f"{current}/{total} - {filename[:40]}")

        if threading.current_thread() is not threading.main_thread():
            self.after(0, _update)
        else:
            _update()

    def update_stats(self):
        """更新统计信息"""
        def _update():
            if self.extractor:
                stats = self.extractor.stats
                self.label_stats.configure(
                    text=f"📊 Tier1: {stats['tier1_count']} | Tier2: {stats['tier2_count']} | Tier3: {stats['tier3_count']} | "
                         f"✓成功: {stats['success_count']} | ⏭跳过: {stats['skipped_count']} | ⏱超时: {stats['timeout_count']} | ✗失败: {stats['failed_count']}"
                )

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

        # 预检查
        if not target_dirs or not output_file:
            messagebox.showerror("错误", "请同时指定源文件夹和结果保存位置！")
            self.after(10, self.reset_ui)
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
            self.after(10, self.reset_ui)
            return

        # 额外选项
        recurse = self.subfolder_switch.get()
        classify = self.classify_switch.get()
        classify_folder = self.entry_classify_folder.get().strip()

        if classify and not classify_folder:
            messagebox.showerror("错误", "已选自动归类，请先指定归类结果目录！")
            self.after(10, self.reset_ui)
            return

        # 获取并发数
        max_workers = int(self.entry_workers.get()) if self.entry_workers.get().isdigit() else MAX_WORKERS
        max_workers = min(max_workers, 50)  # 最大限制50

        # 获取超时时间和截取页数
        file_timeout = int(self.entry_timeout.get()) if self.entry_timeout.get().isdigit() else DEFAULT_FILE_TIMEOUT
        file_timeout = max(5, min(file_timeout, 120))  # 限制在5-120秒

        page_count = int(self.entry_pages.get()) if self.entry_pages.get().isdigit() else DEFAULT_PAGE_COUNT
        page_count = max(1, min(page_count, 20))  # 限制在1-20页

        # 解析数据源
        sources_text = self.entry_sources.get().strip()
        if sources_text:
            enabled_sources = [s.strip() for s in sources_text.split(',') if s.strip()]
        else:
            enabled_sources = ['crossref', 'openalex', 'semantic_scholar']

        # 初始化提取器，传入GUI日志回调
        self.extractor = PaperMetadataExtractorAlpha(
            email=email,
            keywords=keywords,
            enabled_sources=enabled_sources,
            max_workers=max_workers,
            file_timeout=file_timeout,
            page_count=page_count,
            log_callback=self.log  # 传入GUI的log方法
        )

        # 收集 PDF 文件
        pdf_files = self.collect_pdf_files(valid_dirs, recurse)
        total = len(pdf_files)
        self.log(f"共找到 {total} 个 PDF 文件（来自 {len(valid_dirs)} 个文件夹）")
        self.log(f"并发线程: {max_workers} | 超时: {file_timeout}s | 截取页数: {page_count}")

        # 续传读取
        self.results = []
        done_files: Set[str] = set()
        if os.path.exists(output_file):
            try:
                old_df = pd.read_csv(output_file)
                self.results = old_df.to_dict('records')
                done_files = set(old_df['文件名称'].astype(str).tolist())
                self.log(f"检测到已有结果文件，已跳过 {len(done_files)} 个。")
            except Exception as e:
                self.log(f"读取旧文件失败：{e}，将重新开始。")

        # 过滤已处理的文件
        pending_files = [(d, f) for d, f in pdf_files if f not in done_files]
        self.log(f"待处理文件数: {len(pending_files)}")

        # 使用线程池并发处理
        processed = 0
        start_time = time.time()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交所有任务
            future_to_file = {}
            for i, (current_dir, filename) in enumerate(pending_files):
                if self.stop_requested:
                    break
                file_path = os.path.join(current_dir, filename)
                thread_id = str(i % 100).zfill(2)  # 简化的线程ID
                future = executor.submit(self.extractor.process_pdf, file_path, thread_id)
                future_to_file[future] = (current_dir, filename, file_path)

            # 收集结果
            for future in as_completed(future_to_file):
                if self.stop_requested:
                    self.log("停止请求已响应。正在保存当前数据...")
                    break

                current_dir, filename, file_path = future_to_file[future]

                try:
                    result = future.result()

                    # 转换为字典
                    row = {
                        "文件名称": result.filename,
                        "论文题目": result.title,
                        "语种": result.language,
                        "作者": result.authors,
                        "年份": result.year,
                        "来源（出版社/期刊）": result.journal,
                        "期（卷）": result.volume_issue,
                        "DOI": result.doi,
                        "状态": result.status,
                        "error_type": result.error_type,
                        "来源文件夹": result.source_folder,
                        "处理车道": result.tier
                    }

                    # 归类文件
                    if classify and classify_folder:
                        self.classify_file(file_path, result.status, classify_folder, result.error_type)

                    with self.results_lock:
                        self.results.append(row)
                    processed += 1

                    # 更新进度
                    progress = processed / len(pending_files) * 100
                    self.update_progress(progress, processed, len(pending_files), filename)
                    self.update_stats()

                    # 定期保存
                    if processed % SAVE_INTERVAL == 0:
                        self.save_results(self.results, output_file)

                except Exception as e:
                    self.log(f"处理异常 {filename}: {str(e)}")
                    error_type = f"处理异常: {str(e)[:50]}"
                    with self.results_lock:
                        self.results.append({
                            "文件名称": filename,
                            "状态": "失败",
                            "error_type": error_type,
                            "来源文件夹": os.path.basename(current_dir)
                        })
                    # 异常文件也归类
                    if classify and classify_folder:
                        self.classify_file(file_path, "失败", classify_folder, error_type)

        # 最终保存
        self.save_results(self.results, output_file)

        # 计算耗时
        elapsed_time = time.time() - start_time
        avg_time = elapsed_time / processed if processed > 0 else 0

        self.log(f"任务完成！处理：{processed} 个文件")
        self.log(f"总耗时：{elapsed_time:.1f}秒，平均：{avg_time:.2f}秒/文件")
        self.log(f"结果已存至：{output_file}")

        # 写入日志
        self.write_task_log(output_file, self.results, valid_dirs,
                           elapsed_time, stopped=self.stop_requested)

        self.after(10, self.reset_ui)

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

    def classify_file(self, file_path: str, status: str, classify_folder: str, error_type: str = ""):
        """
        归类文件到成功/失败目录
        成功文件 -> 完成/
        失败文件 -> 失败/{error_type}/
        """
        try:
            filename = os.path.basename(file_path)

            if status == "成功":
                # 成功文件统一放入"完成"目录
                dest_folder = os.path.join(classify_folder, "完成")
            else:
                # 失败文件按错误类型分类
                # 清理错误类型名称，移除不合法字符
                safe_error_type = error_type if error_type else "未知错误"
                safe_error_type = re.sub(r'[<>:"/\\|?*]', '_', safe_error_type)
                safe_error_type = safe_error_type[:50]  # 限制长度
                dest_folder = os.path.join(classify_folder, "失败", safe_error_type)

            os.makedirs(dest_folder, exist_ok=True)
            dest_path = os.path.join(dest_folder, filename)

            if os.path.exists(dest_path):
                os.remove(dest_path)

            shutil.copy2(file_path, dest_path)
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
                       folders: List[str], elapsed_time: float,
                       stopped: bool = False):
        """每次执行完成后写入任务摘要日志"""
        total = len(results)
        success_count = sum(1 for r in results if r.get("状态") == "成功")
        skipped_count = sum(1 for r in results if r.get("状态") == "跳过")
        failed_count = total - success_count - skipped_count
        timestamp = time.strftime("%Y-%m-%d %H:%M")
        status = "已终止" if stopped else "已完成"
        folder_list = "; ".join(folders)
        log_path = os.path.splitext(output_file)[0] + "_task_log.txt"

        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {status}：\n")
                f.write(f"  处理目录：{folder_list}\n")
                f.write(f"  总文件：{total}，成功：{success_count}，跳过：{skipped_count}，失败：{failed_count}\n")
                f.write(f"  总耗时：{elapsed_time:.1f}秒\n")
                if self.extractor:
                    stats = self.extractor.stats
                    f.write(f"  分流统计 - Tier1: {stats['tier1_count']}, Tier2: {stats['tier2_count']}, Tier3: {stats['tier3_count']}\n")
                f.write(f"  结果文件：{output_file}\n")
                f.write("------------------------------\n")
            self.log(f"已写入任务日志：{log_path}")
        except Exception as e:
            self.log(f"写入任务日志失败：{e}")

    def start_task(self):
        self.is_running = True
        self.stop_requested = False
        self.results = []
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        threading.Thread(target=self.process_logic, daemon=True).start()

    def stop_task(self):
        self.stop_requested = True
        self.btn_stop.configure(state="disabled")
        self.log("正在停止任务...")

    def reset_ui(self):
        """重置UI状态"""
        def _reset():
            self.is_running = False
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.update_progress(0, 0, 1, "")

        if threading.current_thread() is not threading.main_thread():
            self.after(10, _reset)
        else:
            _reset()


if __name__ == "__main__":
    app = PaperToolGUIAlpha()
    app.mainloop()
