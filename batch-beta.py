from email.mime import text
import os
import re
import time
import threading
import shutil
from typing import List, Dict, Optional, Tuple, Set
from pathlib import Path

import fitz  # PyMuPDF
import requests
import pandas as pd
import customtkinter as ctk
from tkinter import filedialog, messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pypdf import PdfReader, PdfWriter  # 添加pypdf用于页面截取
import pymupdf4llm  # 添加pymupdf4llm用于OCR
from llm_enhancer import LLMEnhancer
enhancer = LLMEnhancer()

# --- 常量配置 ---
APP_VERSION = "3.1"
DEFAULT_TESSERACT_PATH = r"D:\Tesseract\tesseract.exe"
DEFAULT_EMAIL = "kodwabeamer@gmail.com"
DEFAULT_KEYWORDS = "sign language, deaf, fingerspelling, asl, csl, gloss recognition, ASL, sign, deaf, mute, multimodal, embodiment, iconicity，gesture，metaphor，手势，手语，模态，聋，隐喻"
OPENALEX_API_URL = "https://api.openalex.org/works/https://doi.org/"
CROSSREF_API_URL = "https://api.crossref.org/works/"
SEMANTIC_SCHOLAR_API_URL = "https://api.semanticscholar.org/graph/v1/paper/"
API_TIMEOUT = 10
OCR_TEXT_THRESHOLD = 60
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

    def fetch_metadata(self, doi: str) -> Tuple[Optional[Dict], str]:
        """从多个数据源获取元数据，按优先级顺序尝试，返回 (metadata, source)"""
        for source in self.enabled_sources:
            metadata = None
            if source == 'openalex':
                metadata = self.fetch_from_openalex(doi)
            elif source == 'crossref':
                metadata = self.fetch_from_crossref(doi)
            elif source == 'semantic_scholar':
                metadata = self.fetch_from_semantic_scholar(doi)

            if metadata:
                return metadata, source

        return None, ''

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

        # 统一期卷格式为期（卷）
        issue_volume = "N/A"
        if issue and volume:
            issue_volume = f"{issue}({volume})"
        elif issue:
            issue_volume = f"{issue}()"
        elif volume:
            issue_volume = f"()({volume})"

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

        # 统一期卷格式为期（卷）
        volume = metadata.get('volume')
        issue = metadata.get('issue')
        issue_volume = "N/A"
        if issue and volume:
            issue_volume = f"{issue}({volume})"
        elif issue:
            issue_volume = f"{issue}()"
        elif volume:
            issue_volume = f"()({volume})"

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

        # 统一期卷格式为期（卷）
        volume = metadata.get('volume')
        issue = metadata.get('issue')
        issue_volume = "N/A"
        if issue and volume:
            issue_volume = f"{issue}({volume})"
        elif issue:
            issue_volume = f"{issue}()"
        elif volume:
            issue_volume = f"()({volume})"

        return {
            '论文题目': metadata.get('title', ''),
            '作者': authors,
            '年份': metadata.get('year'),
            '语种': '',  # Semantic Scholar 不提供语言信息
            '来源（出版社/期刊）': metadata.get('venue', ''),
            '期（卷）': issue_volume,
            'DOI': metadata.get('externalIds', {}).get('DOI', '')
        }


class PaperMetadataExtractor:
    """PDF 文献元数据提取器"""

    def __init__(self, email: str, keywords: str, 
                 enable_ocr: bool = True, enabled_sources: List[str] = None,
                 enable_page_extraction: bool = True, extract_first_n_pages: int = 6):
        self.email = email
        self.keywords = keywords
        self.kw_pattern = keywords.replace(", ", "|")
        self.enable_ocr = enable_ocr
        self.session = requests.Session()

        # 页面截取设置（默认截取前6页）
        self.enable_page_extraction = True
        self.extract_first_n_pages = 6

        # 初始化多源元数据获取器
        self.metadata_fetcher = MultiSourceMetadataFetcher(email, self.session)
        if enabled_sources:
            self.metadata_fetcher.set_enabled_sources(enabled_sources)

        # 初始化文档类型属性
        self.doc_type = "未知"

    def log(self, message: str):
        """日志输出方法"""
        print(f"[{time.strftime('%H:%M:%S')}] {message}")

    def extract_pages_from_pdf(self, file_path: str, start_page: int = 1, end_page: int = 6) -> str:
        """从PDF截取指定页码范围，保存为临时文件并返回路径"""
        try:
            # 创建临时文件
            temp_dir = os.path.dirname(file_path)
            temp_filename = f"temp_{os.path.basename(file_path)}_pages_{start_page}_{end_page}.pdf"
            temp_path = os.path.join(temp_dir, temp_filename)

            # 读取原始PDF
            reader = PdfReader(file_path)
            total_pages = len(reader.pages)

            # 调整页码范围
            actual_end_page = min(end_page, total_pages)
            actual_start_page = max(1, min(start_page, actual_end_page))

            # 创建新的PDF写入器
            writer = PdfWriter()

            # 添加指定页码范围（pypdf使用0-based索引）
            for i in range(actual_start_page - 1, actual_end_page):
                writer.add_page(reader.pages[i])

            # 写入临时文件
            with open(temp_path, 'wb') as output_file:
                writer.write(output_file)

            self.log(f"已截取PDF前{actual_end_page}页: {os.path.basename(file_path)}")
            return temp_path

        except Exception as e:
            self.log(f"截取PDF页面失败，将使用原文件: {str(e)}")
            return file_path

    def extract_text_from_pdf(self, file_path: str, quick_scan: bool = False) -> str:
        """
        从 PDF 提取文本

        Args:
            file_path: PDF 文件路径
            quick_scan: True=快速扫描（前3页，用于判断相关性）
                       False=完整扫描（所有页面，用于元数据提取）
        """
        temp_file_path = None
        text = ""  # 初始化 text

        try:
            # 如果需要页面截取，先截取前N页
            if self.enable_page_extraction and self.extract_first_n_pages > 0:
                temp_file_path = self.extract_pages_from_pdf(file_path, 1, self.extract_first_n_pages)
                actual_file_path = temp_file_path
            else:
                actual_file_path = file_path

            doc = fitz.open(actual_file_path)
            total_pages = len(doc)

            # 确定要提取的页数
            pages_to_extract = 3 if quick_scan else total_pages

            # 优先使用 pymupdf4llm（OCR 效果更好）
            if self.enable_ocr:
                try:
                    page_list = list(range(min(pages_to_extract, total_pages)))
                    md_text = pymupdf4llm.to_markdown(actual_file_path, pages=page_list)
                    if md_text:
                        text = md_text
                        mode = "快速扫描" if quick_scan else "完整扫描"
                        self.log(f"{mode}使用pymupdf4llm提取{len(page_list)}页，共{len(text)}字符")
                except Exception as e:
                    self.log(f"pymupdf4llm提取失败: {str(e)}，尝试传统方法")

            # 如果 pymupdf4llm 未启用或失败，使用传统方法
            if not text.strip():
                pages_to_read = min(pages_to_extract, total_pages)
                text = "".join([doc[p].get_text() for p in range(pages_to_read)])
                mode = "快速扫描" if quick_scan else "完整扫描"
                self.log(f"{mode}使用传统方法提取{pages_to_read}页，共{len(text)}字符")

            doc.close()

            # 清理临时文件
            if temp_file_path and os.path.exists(temp_file_path) and temp_file_path != file_path:
                try:
                    os.remove(temp_file_path)
                except:
                    pass

            return text

        except Exception as e:
            # 清理临时文件
            if temp_file_path and os.path.exists(temp_file_path) and temp_file_path != file_path:
                try:
                    os.remove(temp_file_path)
                except:
                    pass
            raise Exception(f"PDF 文本提取失败：{str(e)}")

    def extract_doi(self, text: str) -> Optional[str]:
        """从文本中提取 DOI"""
        doi_match = re.search(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', text, re.IGNORECASE)
        return doi_match.group(0).rstrip('.') if doi_match else None

    def fetch_metadata(self, doi: str) -> Optional[Dict]:
        """从多数据源获取元数据（返回标准化格式）"""
        raw_metadata, source = self.metadata_fetcher.fetch_metadata(doi)
        if not raw_metadata:
            return None

        normalized = self.metadata_fetcher.normalize_metadata(raw_metadata, source)
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

    def detect_language(self, text: str) -> str:
        """检测文本语言"""
        if not text:
            return "N/A"

        # 简单判断：包含中文字符则为中文
        if any('\u4e00' <= c <= '\u9fff' for c in text):
            return "中文"
        else:
            return "英文"

    def is_sign_language_related(self, title: str, concepts: List[str], extracted_text:str) -> bool:
        """判断是否手语相关文献"""
        concepts_text = " ".join(concepts) if concepts else ""
        text = f"{title} {concepts_text}".lower()
        result1 =  bool(re.search(self.kw_pattern, text))
        result2, _ = enhancer.check_sign_language_relevance(title, extracted_text, self.keywords)
        result = max(result1, result2)
        return result
    
    def first_scan(self, file_path: str, keywords: List[str], extracted_text: str) -> bool:
        """初步扫描文本是否包含关键词"""
        result = enhancer.check_sign_language_relevance(file_path, extracted_text, self.keywords)
        return result

    def process_pdf(self, file_path: str) -> Dict:
        """处理单个 PDF 文件 - 漏斗策略实现"""
        filename = os.path.basename(file_path)
        current_dir = os.path.dirname(file_path)

        row = {
            "文件名称": filename,
            "论文题目": "N/A",
            "语种": "N/A",
            "作者": "N/A",
            "年份": "N/A",
            "来源（出版社/期刊）": "N/A",
            "期（卷）": "N/A",
            "状态": "失败",
            "来源文件夹": os.path.basename(current_dir)
        }

        try:
            # ===== 第一阶段：首次扫描，判断相关性和文档类型 =====
            self.log(f"开始处理: {filename}")

            # 使用pymupdf4llm进行首次扫描（前3页）
            first_scan_text = self.extract_text_from_pdf(file_path, quick_scan=True)

            # 判断是否为相关文献和文档类型
            is_related, doc_type = enhancer.check_sign_language_relevance(
                "", first_scan_text[:500], self.keywords
            )
            self.doc_type = doc_type

            self.log(f"首次扫描结果 - 相关文献: {is_related}, 文档类型: {doc_type}")

            # 只处理期刊和相关文献
            if not is_related:
                row["error"] = f"跳过 ({'非手语相关'})"
                return row
            if doc_type == "书籍":
                row["error"] = f"跳过 ({'书籍而非期刊'})"
                return row

            # ===== 第二阶段：DOI提取漏斗策略 =====
            # 根据要求重构：移除LLM文本提取DOI步骤，直接从正则跳到视觉模型

            # 1. 正则表达式提取DOI
            doi = self.extract_doi(first_scan_text)
            vision_doi_result = {}  # 保存视觉模型DOI提取的完整结果

            # 2. 如果正则提取失败，直接使用视觉模型从页眉页脚提取（跳过LLM文本提取）
            if not doi:
                self.log("正则提取DOI失败，直接尝试视觉模型提取")
                # 使用视觉模型提取DOI（同时会提取其他元数据）
                vision_doi_result = enhancer.extract_metadata_with_vision(
                    file_path, filename, ["DOI"], "[视觉DOI提取]"
                )
                # 视觉模型返回的键名可能是小写，需要兼容处理
                doi = vision_doi_result.get("DOI", "")
                if not doi:
                    doi = vision_doi_result.get("doi", "")

            # 如果有DOI，记录成功；否则继续处理（无DOI的文献也可以提取元数据）
            if doi:
                self.log(f"DOI提取成功: {doi}")
            else:
                self.log("DOI提取为空，将直接使用视觉模型/LLM提取元数据")

            # ===== 第三阶段：元数据获取漏斗策略 =====

            # 1. 通过DOI搜索元数据（如果有DOI的话）
            metadata = {}
            if doi:
                metadata = self.fetch_metadata(doi)

            # 2. 如果DOI搜索失败/无DOI，优先复用视觉模型DOI提取阶段的结果
            if not metadata and vision_doi_result:
                # 检查是否有有效元数据（至少有标题或作者）
                if vision_doi_result.get("title") or vision_doi_result.get("authors"):
                    self.log("复用视觉模型DOI提取阶段的元数据")
                    metadata = vision_doi_result

            # 3. 如果仍然没有元数据，使用LLM从完整文本提取
            if not metadata:
                self.log("尝试LLM文本提取元数据")
                # 获取完整文本（所有页面）
                full_text = self.extract_text_from_pdf(file_path, quick_scan=False)
                # 使用LLM提取元数据
                metadata = enhancer.extract_metadata_with_llm(
                    full_text, filename, "paper", "[元数据LLM提取]"
                )

            # 4. 如果LLM提取失败，使用视觉模型提取
            if not metadata:
                self.log("LLM文本提取元数据失败，尝试视觉模型提取")
                # 使用视觉模型提取元数据
                metadata = enhancer.extract_metadata_with_vision(
                    file_path, filename,
                    ["title", "authors", "journal", "year", "volume_issue"],
                    "[元数据视觉提取]"
                )

            if not metadata:
                row["error"] = "元数据提取失败（三级漏斗均失败）"
                return row

            # ===== 第四阶段：整理和输出结果 =====

            # 提取并整理元数据（兼容大小写不同的键名）
            # 统一键名：优先使用中文键名，英文键名作为兼容
            authors = metadata.get("作者", metadata.get("authors", metadata.get("Authors", [])))
            if isinstance(authors, str):
                authors = [a.strip() for a in authors.split(",")]

            year = metadata.get("年份") or metadata.get("发表时间") or metadata.get("year") or metadata.get("发表年份") or metadata.get("Year")
            journal = metadata.get("来源（出版社/期刊）") or metadata.get("发表期刊") or metadata.get("journal") or metadata.get("Journal")

            # 获取期卷信息，兼容多种键名格式
            volume_issue_raw = (metadata.get("期（卷）") or
                               metadata.get("期卷") or
                               metadata.get("volume_issue") or
                               metadata.get("期卷信息") or
                               metadata.get("volume") or
                               metadata.get("issue") or
                               "N/A")

            # 获取论文题目
            title = metadata.get("论文题目") or metadata.get("论文标题") or metadata.get("title") or ""

            # 判断语种
            language = self.detect_language(first_scan_text)

            # 解析期卷信息，统一格式为期（卷）
            issue_volume = "N/A"  # 期（卷）格式

            if volume_issue_raw and volume_issue_raw != "N/A":
                issue_num = None
                volume_num = None

                # 格式1：英文格式 "5(2)" 或 "5:2" - 假设前面是卷，括号内是期
                if "(" in volume_issue_raw and ")" in volume_issue_raw:
                    parts = volume_issue_raw.split("(")
                    volume_num = parts[0].strip()
                    issue_num = parts[1].rstrip(")").strip()
                elif ":" in volume_issue_raw:
                    parts = volume_issue_raw.split(":")
                    volume_num = parts[0].strip()
                    issue_num = parts[1].strip()
                # 格式2：中文格式 "第12卷第3期" 或 "12卷3期"
                elif "卷" in volume_issue_raw and "期" in volume_issue_raw:
                    # 提取卷号
                    vol_match = re.search(r'(\d+)\s*卷', volume_issue_raw)
                    if vol_match:
                        volume_num = vol_match.group(1)
                    # 提取期号
                    issue_match = re.search(r'(\d+)\s*期', volume_issue_raw)
                    if issue_match:
                        issue_num = issue_match.group(1)
                else:
                    # 其他格式，尝试提取数字
                    numbers = re.findall(r'\d+', volume_issue_raw)
                    if len(numbers) >= 2:
                        volume_num = numbers[0]
                        issue_num = numbers[1]
                    elif len(numbers) == 1:
                        volume_num = numbers[0]

                # 格式化为 期（卷）
                if issue_num and volume_num:
                    issue_volume = f"{issue_num}({volume_num})"
                elif issue_num:
                    issue_volume = f"{issue_num}()"
                elif volume_num:
                    issue_volume = f"()({volume_num})"

            row.update({
                "文件名称": filename,
                "论文题目": title,
                "语种": language,
                "作者": self.format_apa_authors(authors),
                "年份": year,
                "来源（出版社/期刊）": journal or "N/A",
                "期（卷）": issue_volume,
                "状态": "成功"
            })

            self.log(f"处理完成: {filename} - 成功提取元数据")

        except Exception as e:
            row["error"] = f"处理异常：{str(e)}"
            self.log(f"处理 {filename} 失败: {str(e)}")

        return row

class PaperToolGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title(f"文献元数据自动化获取工具 v{APP_VERSION}")
        self.geometry("950x850")
        self.minsize(900, 800)

        # 运行状态变量
        self.is_running = False
        self.stop_requested = False
        self.selected_src_folders: List[str] = []
        self.extractor: Optional[PaperMetadataExtractor] = None
        self.output_mode = "overwrite"  # 输出模式

        # --- UI 布局配置 ---
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(10, weight=1)  # 更新为日志框所在的行号

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
        self.log_box.grid(row=10, column=0, columnspan=5, padx=20, pady=10, sticky="nsew")

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

        # 3. 配置项 - 第一行
        self.label_email = ctk.CTkLabel(self, text="OpenAlex Email:")
        self.label_email.grid(row=3, column=0, padx=20, pady=5, sticky="e")

        self.entry_email = ctk.CTkEntry(self, placeholder_text=DEFAULT_EMAIL)
        self.entry_email.insert(0, DEFAULT_EMAIL)
        self.entry_email.grid(row=3, column=1, padx=10, pady=5, sticky="ew")

        self.ocr_switch = ctk.CTkSwitch(self, text="启用OCR")
        self.ocr_switch.select()
        self.ocr_switch.grid(row=3, column=2, padx=20, pady=5)

        self.subfolder_switch = ctk.CTkSwitch(self, text="递归搜索子目录")
        self.subfolder_switch.select()  # 默认开启递归搜索
        self.subfolder_switch.grid(row=3, column=3, padx=10, pady=5)

        self.classify_switch = ctk.CTkSwitch(self, text="处理后自动归类文件")
        self.classify_switch.grid(row=3, column=4, padx=10, pady=5)

        # 4. 页面截取配置 - 第二行
        self.page_extract_frame = ctk.CTkFrame(self)
        self.page_extract_frame.grid(row=4, column=1, padx=10, pady=5, sticky="ew", columnspan=1)

        self.page_extract_switch = ctk.CTkSwitch(self.page_extract_frame, text="截取前")
        self.page_extract_switch.select()  # 默认启用
        self.page_extract_switch.pack(side="left", padx=5, pady=5)
        self.page_extract_switch.configure(command=self.on_page_extract_changed)

        self.entry_page_count = ctk.CTkEntry(self.page_extract_frame, width=50, placeholder_text="6")
        self.entry_page_count.pack(side="left", padx=5, pady=5)
        self.entry_page_count.insert(0, "6")

        self.label_pages = ctk.CTkLabel(self.page_extract_frame, text="页")
        self.label_pages.pack(side="left", padx=5, pady=5)

        # 5. 关键词配置 - 第三行
        self.label_kw = ctk.CTkLabel(self, text="手语关键词:")
        self.label_kw.grid(row=5, column=0, padx=20, pady=5, sticky="e")

        self.entry_kw = ctk.CTkEntry(self)
        self.entry_kw.insert(0, DEFAULT_KEYWORDS)
        self.entry_kw.grid(row=5, column=1, padx=10, pady=5, sticky="ew")

        # 6. 数据源配置 - 第四行
        self.label_sources = ctk.CTkLabel(self, text="数据源 (逗号分隔):")
        self.label_sources.grid(row=6, column=0, padx=20, pady=5, sticky="e")

        self.entry_sources = ctk.CTkEntry(self)
        self.entry_sources.insert(0, "openalex,crossref,semantic_scholar")
        self.entry_sources.grid(row=6, column=1, padx=10, pady=5, sticky="ew")



        # 8. 进度条
        self.progress_bar = ctk.CTkProgressBar(self)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=8, column=0, columnspan=5, padx=20, pady=10, sticky="ew")

        self.progress_label = ctk.CTkLabel(self, text="", text_color="gray")
        self.progress_label.grid(row=8, column=5, padx=10, pady=10, sticky="w")

        # 9. 控制按钮
        self.btn_start = ctk.CTkButton(
            self,
            text="开始执行任务",
            fg_color="green",
            hover_color="darkgreen",
            height=40,
            command=self.start_task
        )
        self.btn_start.grid(row=9, column=1, pady=20, sticky="ew")

        self.btn_stop = ctk.CTkButton(
            self,
            text="停止",
            fg_color="red",
            state="disabled",
            command=self.stop_task
        )
        self.btn_stop.grid(row=9, column=2, pady=20, sticky="ew")

    def on_page_extract_changed(self):
        """页面截取开关变更处理"""
        if self.page_extract_switch.get():
            self.entry_page_count.configure(state="normal")
        else:
            self.entry_page_count.configure(state="disabled")

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
            enable_ocr=self.ocr_switch.get(),
            enabled_sources=enabled_sources,
            enable_page_extraction=self.page_extract_switch.get(),
            extract_first_n_pages=int(self.entry_page_count.get()) if self.entry_page_count.get().isdigit() else 6
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

        # 使用after确保UI更新在主线程执行
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
        """重置UI状态 - 线程安全版本"""
        def _reset():
            self.is_running = False
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.update_progress(0, 0, 1, "")

        # 确保在主线程中执行UI更新
        if threading.current_thread() is not threading.main_thread():
            self.after(10, _reset)
        else:
            _reset()


if __name__ == "__main__":
    app = PaperToolGUI()
    app.mainloop()
