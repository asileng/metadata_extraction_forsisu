#!/usr/bin/env python3
"""
LLM增强处理模块 Alpha版 - 高并发优化版本
- 合并相关性和元数据提取提示词
- 指数退避处理429错误
- 超时控制15s
- 统一的视觉模型接口
"""

import json
import re
import time
import random
import base64
import io
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Union, Tuple
from openai import OpenAI
import httpx


class LLMEnhancerAlpha:
    """LLM增强处理类 - Alpha优化版本"""

    def __init__(self,
                 api_key: str = "sk-2d79adf725ec41d3babf30bef712d01b",
                 base_url: str = "https://api.deepseek.com",
                 kimi_api_key: str = "sk-H2kSJ6dNld4gFJuy4qUimoWDv0DgEqQuK3gU1K2nN4Kyn9C1",
                 kimi_base_url: str = "https://api.moonshot.cn/v1",
                 max_retries: int = 3,
                 timeout: float = 15.0,
                 log_callback=None):
        """
        初始化LLM增强处理器

        Args:
            api_key: DeepSeek API密钥
            base_url: DeepSeek API基础URL
            kimi_api_key: Kimi API密钥（用于视觉分析）
            kimi_base_url: Kimi API基础URL
            max_retries: 最大重试次数
            timeout: API超时时间（秒）
            log_callback: 日志回调函数，用于将日志输出到GUI
        """
        # 设置超时：连接5秒，读取timeout秒
        http_timeout = httpx.Timeout(5.0, read=timeout)
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=http_timeout)
        self.kimi_client = OpenAI(api_key=kimi_api_key, base_url=kimi_base_url, timeout=http_timeout)
        self.max_retries = max_retries
        self.timeout = timeout
        self.log_callback = log_callback

    def log(self, message: str):
        """输出日志到控制台和GUI回调"""
        print(message)
        if self.log_callback:
            self.log_callback(message)

    def _exponential_backoff(self, attempt: int, base_delay: float = 1.0) -> float:
        """计算指数退避等待时间"""
        return (2 ** attempt) * base_delay + random.uniform(0, 0.5)

    def _call_with_retry(self, client, model: str, messages: list,
                         temperature: float = 0.3, max_tokens: int = 500,
                         response_format: dict = None) -> Tuple[Optional[str], Optional[str]]:
        """
        带重试的API调用，处理429错误

        Returns:
            Tuple[content, error_type]: 成功返回(content, None)，失败返回(None, error_type)
        """
        last_error = None

        for attempt in range(self.max_retries):
            try:
                kwargs = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens
                }
                if response_format:
                    kwargs["response_format"] = response_format

                response = client.chat.completions.create(**kwargs)
                return response.choices[0].message.content, None

            except Exception as e:
                err_msg = str(e)
                last_error = err_msg

                # 检查是否为429频率限制
                if "429" in err_msg or "rate" in err_msg.lower() or "limit" in err_msg.lower():
                    wait_time = self._exponential_backoff(attempt)
                    self.log(f"  ⏳ API限速(429)，等待 {wait_time:.1f}s 后重试 ({attempt+1}/{self.max_retries})")
                    time.sleep(wait_time)
                elif "timeout" in err_msg.lower():
                    self.log(f"  ⏱️ API超时，重试 ({attempt+1}/{self.max_retries})")
                    time.sleep(1)
                elif attempt < self.max_retries - 1:
                    time.sleep(1)
                else:
                    break

        return None, f"API调用失败: {last_error[:50]}" if last_error else "API调用失败"

    def combined_analysis(self,
                          extracted_text: str,
                          file_name: str,
                          keywords: str,
                          prefix: str = "") -> Tuple[Dict, str]:
        """
        合并相关性和元数据提取（Tier 2 核心方法）

        Args:
            extracted_text: 提取的文本内容
            file_name: 文件名
            keywords: 关键词列表
            prefix: 日志前缀

        Returns:
            Tuple[metadata, error_type]:
                - metadata: 提取的元数据字典
                - error_type: 错误类型（成功则为空字符串）
        """
        if not extracted_text or len(extracted_text.strip()) < 50:
            return {}, "文本不足"

        prompt = f"""你是一个学术文献处理专家。请分析以下文本，完成两个任务：

## 任务1：相关性判断
判断文献是否与手语相关。
正例关键词：{keywords}
排除：纯口语语言学、听力医学等无关领域。

## 任务2：元数据提取
提取以下字段：
- title: 论文标题
- authors: 作者列表（数组格式）
- year: 发表年份（4位数字）
- journal: 期刊/来源名称
- volume_issue: 期卷信息（格式：期(卷)，如 3(12)）
- doi: DOI号（必须以10.开头）

## 输出格式（严格JSON）
{{
  "is_sign_language_related": true/false,
  "doc_type": "期刊" 或 "书籍",
  "title": "论文标题",
  "authors": ["作者1", "作者2"],
  "year": 2024,
  "journal": "期刊名",
  "volume_issue": "期(卷)",
  "doi": "10.xxxx/xxxx"
}}

## 重要规则
1. 如果 is_sign_language_related 为 false，其他字段可为空
2. 所有字段禁止使用 "unknown"、"xxxx" 等占位符，找不到留空
3. 期卷信息统一格式为 期(卷)，如从 "Vol.5, No.2" 转换为 "2(5)"
4. 年份必须是4位数字

文件名：{file_name}
文本内容：
{extracted_text[:3000]}
"""

        content, error_type = self._call_with_retry(
            self.client,
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=600
        )

        if error_type:
            return {}, error_type

        # 解析JSON
        try:
            # 尝试提取JSON
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = json.loads(content)

            # 处理结果
            is_related = data.get("is_sign_language_related", False)
            doc_type = data.get("doc_type", "期刊")

            if not is_related:
                return {
                    "is_related": False,
                    "doc_type": doc_type
                }, "不相关跳过"

            # 格式化元数据
            metadata = {
                "is_related": True,
                "doc_type": doc_type,
                "论文题目": data.get("title", ""),
                "作者": data.get("authors", []),
                "年份": data.get("year"),
                "来源（出版社/期刊）": data.get("journal", ""),
                "期（卷）": self._normalize_volume_issue(data.get("volume_issue", "")),
                "DOI": data.get("doi", "")
            }

            self.log(f"{prefix} ✓ DeepSeek提取成功")
            return metadata, ""

        except json.JSONDecodeError as e:
            self.log(f"{prefix} ✗ JSON解析失败: {e}")
            return {}, "JSON解析失败"

    def extract_doi_from_text(self, text: str) -> Optional[str]:
        """从文本中提取DOI（正则方法）"""
        if not text:
            return None
        doi_pattern = r'10\.\d{4,9}/[-._;()/:A-Z0-9]+'
        match = re.search(doi_pattern, text, re.IGNORECASE)
        if match:
            doi = match.group(0).rstrip('.')  # 移除末尾可能的句号
            return doi
        return None

    def extract_metadata_with_vision(self,
                                     pdf_path: str,
                                     file_name: str,
                                     prefix: str = "") -> Tuple[Dict, str]:
        """
        使用视觉模型从PDF页眉页脚提取元数据（Tier 3）

        Args:
            pdf_path: PDF文件路径
            file_name: 文件名
            prefix: 日志前缀

        Returns:
            Tuple[metadata, error_type]
        """
        try:
            import fitz
            from PIL import Image

            doc = fitz.open(pdf_path)
            page = doc[0]
            rect = page.rect

            # 提取页眉（顶部15%）和页脚（底部15%）
            header_rect = fitz.Rect(0, 0, rect.width, rect.height * 0.15)
            footer_rect = fitz.Rect(0, rect.height * 0.85, rect.width, rect.height)

            pix_header = page.get_pixmap(clip=header_rect, matrix=fitz.Matrix(2, 2))
            pix_footer = page.get_pixmap(clip=footer_rect, matrix=fitz.Matrix(2, 2))

            img_h = Image.frombytes("RGB", [pix_header.width, pix_header.height], pix_header.samples)
            img_f = Image.frombytes("RGB", [pix_footer.width, pix_footer.height], pix_footer.samples)

            # 合并图像
            dst = Image.new('RGB', (img_h.width, img_h.height + img_f.height))
            dst.paste(img_h, (0, 0))
            dst.paste(img_f, (0, img_h.height))

            buffered = io.BytesIO()
            dst.save(buffered, format="PNG")
            image_data = buffered.getvalue()
            image_url = f"data:image/png;base64,{base64.b64encode(image_data).decode('utf-8')}"

            doc.close()

        except Exception as e:
            return {}, f"视觉预处理失败: {str(e)[:50]}"

        # 构建提示词 - 同时判断相关性和提取元数据
        messages = [
            {
                "role": "system",
                "content": """你是学术文献处理专家。分析论文页眉页脚图片，判断相关性并提取元数据。

输出JSON格式：
{
  "is_sign_language_related": true/false,
  "doc_type": "期刊" 或 "书籍",
  "title": "论文标题",
  "authors": ["作者1", "作者2"],
  "year": 年份,
  "journal": "期刊名",
  "volume_issue": "期(卷)",
  "doi": "DOI号"
}

相关关键词：sign language, deaf, fingerspelling, ASL, CSL, 手语, 聋, 手势
期卷格式：期(卷)，如 3(12)"""
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {
                        "type": "text",
                        "text": f"""文件名：{file_name}
请分析这个论文的页眉页脚：
1. 判断是否与手语相关
2. 提取论文标题、作者、年份、期刊、期卷、DOI
如果某个信息不存在，留空即可。"""
                    }
                ]
            }
        ]

        content, error_type = self._call_with_retry(
            self.kimi_client,
            model="kimi-k2.5",
            messages=messages,
            temperature=1.0,
            max_tokens=500,
            response_format={"type": "json_object"}
        )

        if error_type:
            return {}, error_type

        try:
            data = json.loads(content)

            is_related = data.get("is_sign_language_related", True)  # 视觉层默认相关

            metadata = {
                "is_related": is_related,
                "doc_type": data.get("doc_type", "期刊"),
                "论文题目": data.get("title", ""),
                "作者": data.get("authors", []),
                "年份": data.get("year"),
                "来源（出版社/期刊）": data.get("journal", ""),
                "期（卷）": self._normalize_volume_issue(data.get("volume_issue", "")),
                "DOI": data.get("doi", "")
            }

            self.log(f"{prefix} 👁️ Kimi视觉提取成功")
            return metadata, "" if is_related else "不相关跳过"

        except json.JSONDecodeError as e:
            return {}, f"视觉JSON解析失败"

    def ocr_extract_text(self, pdf_path: str, pages: List[int] = None) -> Tuple[str, str]:
        """
        使用pymupdf4llm进行OCR提取（Tier 3 扫描件处理）

        Args:
            pdf_path: PDF文件路径
            pages: 要提取的页码列表（默认前3页）

        Returns:
            Tuple[extracted_text, error_type]
        """
        if pages is None:
            pages = [0, 1, 2]

        try:
            import pymupdf4llm

            md_text = pymupdf4llm.to_markdown(pdf_path, pages=pages)
            if md_text:
                return md_text, ""
            return "", "OCR无输出"

        except Exception as e:
            return "", f"OCR失败: {str(e)[:50]}"

    def _normalize_volume_issue(self, volume_issue: str) -> str:
        """标准化期卷格式为 期(卷)"""
        if not volume_issue or volume_issue == "N/A":
            return ""

        issue_num = None
        volume_num = None

        # 格式1：英文格式 "5(2)" 或 "5:2" - 假设前面是卷，括号内是期
        if "(" in volume_issue and ")" in volume_issue:
            parts = volume_issue.split("(")
            volume_num = parts[0].strip()
            issue_num = parts[1].rstrip(")").strip()
        elif ":" in volume_issue:
            parts = volume_issue.split(":")
            volume_num = parts[0].strip()
            issue_num = parts[1].strip()
        # 格式2：中文格式 "第12卷第3期" 或 "12卷3期"
        elif "卷" in volume_issue and "期" in volume_issue:
            vol_match = re.search(r'(\d+)\s*卷', volume_issue)
            if vol_match:
                volume_num = vol_match.group(1)
            issue_match = re.search(r'(\d+)\s*期', volume_issue)
            if issue_match:
                issue_num = issue_match.group(1)
        # 格式3：纯数字
        else:
            numbers = re.findall(r'\d+', volume_issue)
            if len(numbers) >= 2:
                volume_num = numbers[0]
                issue_num = numbers[1]
            elif len(numbers) == 1:
                volume_num = numbers[0]

        # 格式化为 期（卷）
        if issue_num and volume_num:
            return f"{issue_num}({volume_num})"
        elif issue_num:
            return f"{issue_num}()"
        elif volume_num:
            return f"()({volume_num})"
        return volume_issue

    def check_sign_language_relevance(self, title: str, extracted_text: str, keywords: str) -> Tuple[bool, str]:
        """
        单独的相关性判断（兼容旧接口）
        """
        # 快速正则检查
        kw_pattern = keywords.replace(", ", "|").replace("，", "|")
        text = f"{title} {extracted_text}".lower()
        if re.search(kw_pattern, text, re.IGNORECASE):
            return True, "期刊"

        # 使用LLM判断
        prompt = f"""判断以下文本是否与手语相关。
关键词：{keywords}
排除：纯口语语言学、听力医学。

只回答JSON格式：
{{"is_related": true/false, "doc_type": "期刊"或"书籍"}}

文本：{extracted_text[:500]}"""

        content, _ = self._call_with_retry(
            self.client,
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=50
        )

        if content:
            try:
                data = json.loads(re.search(r'\{[\s\S]*\}', content).group())
                return data.get("is_related", False), data.get("doc_type", "期刊")
            except:
                pass

        return False, "期刊"


# 全局实例（兼容旧代码）
enhancer = LLMEnhancerAlpha()


if __name__ == "__main__":
    # 测试
    print("=" * 60)
    print("测试 LLMEnhancerAlpha")
    print("=" * 60)

    test_enhancer = LLMEnhancerAlpha()

    # 测试DOI提取
    test_text = "DOI: 10.1016/j.csl.2023.101234 and some other text"
    doi = test_enhancer.extract_doi_from_text(test_text)
    print(f"DOI提取测试: {doi}")

    # 测试合并分析
    test_paper = """
    Toward a Sign Language-Friendly Questionnaire Design
    Marta Bosch-Baliarda, Olga Soler Vilageliu and Pilar Orero
    Published in: Journal of Deaf Studies, Volume 28, Issue 3, 2023

    Abstract: The United Nations Convention on the Rights of Persons with Disabilities...
    """

    metadata, error = test_enhancer.combined_analysis(
        test_paper,
        "test.pdf",
        "sign language, deaf, ASL",
        "[TEST]"
    )
    print(f"合并分析结果: {json.dumps(metadata, ensure_ascii=False, indent=2)}")
    print(f"错误类型: {error}")
