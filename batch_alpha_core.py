#!/usr/bin/env python3
"""
PDF文献元数据批量提取工具 - 核心模块
从batch-alpha.py提取的无GUI核心类
"""

import os
import re
import time
import threading
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, field
import json

import fitz  # PyMuPDF
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

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
        """线程安全的日志输出，同时输出到控制台和回调"""
        prefix = f"[T{thread_id}] " if thread_id else ""
        formatted_msg = f"[{time.strftime('%H:%M:%S')}]{prefix}{message}"
        print(formatted_msg)  # 控制台输出
        if self.log_callback:
            self.log_callback(formatted_msg)  # 回调输出

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


# 导出主要类
__all__ = [
    'ProcessingResult',
    'MultiSourceMetadataFetcher',
    'PaperMetadataExtractorAlpha',
    'DEFAULT_EMAIL',
    'DEFAULT_KEYWORDS',
    'MAX_WORKERS',
    'SAVE_INTERVAL',
    'DEFAULT_FILE_TIMEOUT',
    'DEFAULT_PAGE_COUNT'
]