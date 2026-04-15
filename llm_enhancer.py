#!/usr/bin/env python3
"""
LLM增强处理模块 - 用于PDF内容分析和元数据提取
提取自 pdf_meta_extract.py，封装为类以供批量处理使用
"""

import json
import re
import time
import random
import base64
import io
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Union
from openai import OpenAI
import httpx


class LLMEnhancer:
    """LLM增强处理类 - 用于PDF内容分析和元数据提取"""

    def __init__(self,
                 api_key: str = "sk-2d79adf725ec41d3babf30bef712d01b",
                 base_url: str = "https://api.deepseek.com",
                 kimi_api_key: str = "sk-H2kSJ6dNld4gFJuy4qUimoWDv0DgEqQuK3gU1K2nN4Kyn9C1",
                 kimi_base_url: str = "https://api.moonshot.cn/v1",
                 max_retries: int = 3,
                 retry_delay: float = 2.0):
        """
        初始化LLM增强处理器

        Args:
            api_key: DeepSeek API密钥
            base_url: DeepSeek API基础URL
            kimi_api_key: Kimi API密钥（用于视觉分析）
            kimi_base_url: Kimi API基础URL
            max_retries: 最大重试次数
            retry_delay: 重试间隔时间（秒）
        """
        # 设置超时：连接5秒，读取60秒
        timeout = httpx.Timeout(5.0, read=60.0)
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self.kimi_client = OpenAI(api_key=kimi_api_key, base_url=kimi_base_url, timeout=timeout)
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def check_sign_language_relevance(self, title: str, extracted_text: str, keywords: str) -> tuple[bool, str]:
        """
        使用LLM判断论文是否与手语相关

        Args:
            title: 论文标题
            extracted_text: 提取的文本内容

        Returns:
            tuple: (是否手语相关: bool, 文档类型: str)
        """
        is_related = False
        doc_type = "期刊"


        prompt = f"""根据标题和内容判断，
        1、文本是书籍还是期刊;
        只回答“书籍”或“期刊”。
        2、文本内容是否与关键词相关。
        正例关键词：{keywords}
        排除：纯口语语言学、听力医学等无关领域。
        只要正例及其相关语义词汇在文本中，则认为文本是相关的。
        只回答"0"或"1",其中0代表不相关，1代表相关。
        标题: {title}
        内容: {extracted_text[0:300]}
        你最终输出内容为一个元组（a,b）,其中a是代表是否与手语相关的数字，b代表是书籍或是期刊的字符串。"""
        for attempt in range(self.max_retries):
            try:
                response = self.client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=5
                )

                content = response.choices[0].message.content.strip()
                is_related = "1" in content and "0" not in content
                doc_type = "期刊" if "期刊" in content else ("书籍" if "书籍" in content else "未知")

            except Exception as e:
                if attempt < self.max_retries - 1:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)
                else:
                    print(f"  ✗ 手语相关性判断失败: {e}")

        return (is_related, doc_type)
    def extract_metadata_with_llm(self,
                                  extracted_text: str,
                                  file_name: str,
                                  doc_type: str = "book",
                                  prefix: str = "") -> Dict[str, Optional[str]]:
        """
        使用LLM提取元数据

        Args:
            extracted_text: 提取的文本内容
            file_name: 文件名
            doc_type: 文档类型（"book" 或 "paper"）
            prefix: 日志前缀

        Returns:
            dict: 提取的元数据
        """
        if not extracted_text:
            return {}

        best_meta = {}
        best_missing_count = float('inf')

        for attempt in range(self.max_retries):
            try:
                # 根据文档类型选择提示词
                prompt = self._get_metadata_prompt(extracted_text, file_name, doc_type)

                response = self.client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=500
                )

                content = response.choices[0].message.content

                # 尝试解析JSON
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    # 如果JSON不完整，尝试找到有效的JSON部分
                    json_match = re.search(r'\{.*\}', content, re.DOTALL)
                    if json_match:
                        data = json.loads(json_match.group())
                    else:
                        data = {}
                doc_type = self._get_doc_type(data)
                # 根据文档类型转换数据格式
                if doc_type == "paper":
                    # 解析期卷信息，统一格式为期（卷）
                    issue_volume = "N/A"
                    volume_issue = data.get("volume_issue")
                    if volume_issue and volume_issue != "N/A":
                        issue_num = None
                        volume_num = None

                        # 格式1：英文格式 "5(2)" 或 "5:2"
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
                            # 提取卷号
                            vol_match = re.search(r'(\d+)\s*卷', volume_issue)
                            if vol_match:
                                volume_num = vol_match.group(1)
                            # 提取期号
                            issue_match = re.search(r'(\d+)\s*期', volume_issue)
                            if issue_match:
                                issue_num = issue_match.group(1)
                        else:
                            # 其他格式，尝试提取数字
                            numbers = re.findall(r'\d+', volume_issue)
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

                    current_meta = {
                        "论文题目": data.get("title"),
                        "作者": ", ".join(data.get("authors", [])) if data.get("authors") else None,
                        "来源（出版社/期刊）": data.get("journal"),
                        "年份": str(data.get("year")) if data.get("year") else None,
                        "期（卷）": issue_volume,
                        "DOI": data.get("doi")
                    }
                    is_valid, missing = self._validate_paper_metadata(current_meta)
                else:
                    current_meta = {
                        "论文题目": data.get("title"),
                        "作者": ", ".join(data.get("authors", [])) if data.get("authors") else None,
                        "年份": str(data.get("publish_year")) if data.get("publish_year") else None,
                        "来源（出版社/期刊）": data.get("publisher"),
                        "ISBN": data.get("isbn"),
                    }
                    is_valid, missing = self._validate_book_metadata(current_meta)

                # 如果所有字段都完整，直接返回
                if is_valid:
                    print(f"{prefix}  ✓ 第 {attempt + 1} 次尝试：所有字段完整")
                    return current_meta

                # 记录缺失字段最少的结果
                missing_count = len(missing)
                if missing_count < best_missing_count:
                    best_missing_count = missing_count
                    best_meta = current_meta
                    print(f"{prefix}  ⚠ 第 {attempt + 1} 次尝试：缺失 {missing_count} 个字段 {missing}")

                # 如果还有重试机会，继续尝试
                if attempt < self.max_retries - 1:
                    if self.retry_delay > 0:
                        print(f"{prefix}  🔄 字段不完整，{self.retry_delay}秒后重试...")
                        time.sleep(self.retry_delay)
                    else:
                        print(f"{prefix}  🔄 字段不完整，立即重试...")

            except Exception as e:
                if attempt < self.max_retries - 1:
                    if self.retry_delay > 0:
                        print(f"  [重试 {attempt + 1}/{self.max_retries}] API调用失败: {e}, {self.retry_delay}秒后重试...")
                        time.sleep(self.retry_delay)
                    else:
                        print(f"  [重试 {attempt + 1}/{self.max_retries}] API调用失败: {e}, 立即重试...")
                else:
                    print(f"  ✗ API调用最终失败: {e}")

        # 返回最好的结果（即使不完整）
        if best_meta:
            print(f"{prefix}  ⚠ 经过 {self.max_retries} 次尝试，仍有 {best_missing_count} 个字段缺失")

        return best_meta

    def extract_metadata_with_vision(self,
                                     pdf_path: str,
                                     file_name: str,
                                     missing_fields: List[str],
                                     prefix: str = "") -> Dict[str, str]:
        """
        使用视觉模型从PDF页眉页脚提取元数据

        Args:
            pdf_path: PDF文件路径
            file_name: 文件名
            missing_fields: 缺失的字段列表
            prefix: 日志前缀

        Returns:
            dict: 提取的元数据
        """
        try:
            import fitz  # PyMuPDF
            from PIL import Image

            # 打开PDF并获取第一页
            doc = fitz.open(pdf_path)
            page = doc[0]
            rect = page.rect

            # 提取页眉（顶部15%）和页脚（底部15%）
            header_rect = fitz.Rect(0, 0, rect.width, rect.height * 0.15)
            footer_rect = fitz.Rect(0, rect.height * 0.85, rect.width, rect.height)

            # 获取页眉页脚的图像
            pix_header = page.get_pixmap(clip=header_rect, matrix=fitz.Matrix(2, 2))
            pix_footer = page.get_pixmap(clip=footer_rect, matrix=fitz.Matrix(2, 2))

            # 转换为PIL图像
            img_h = Image.frombytes("RGB", [pix_header.width, pix_header.height], pix_header.samples)
            img_f = Image.frombytes("RGB", [pix_footer.width, pix_footer.height], pix_footer.samples)

            # 合并图像
            dst = Image.new('RGB', (img_h.width, img_h.height + img_f.height))
            dst.paste(img_h, (0, 0))
            dst.paste(img_f, (0, img_h.height))

            # 转换为base64
            buffered = io.BytesIO()
            dst.save(buffered, format="PNG")
            image_data = buffered.getvalue()
            image_url = f"data:image/png;base64,{base64.b64encode(image_data).decode('utf-8')}"

            doc.close()

        except Exception as e:
            print(f"{prefix} ⚠️ 视觉预处理失败: {e}")
            return {}

        # 构建提示词
        field_desc = ", ".join(missing_fields) if missing_fields else "文件名，论文标题, 作者, 发表时间, 发表期刊，期卷"

        for attempt in range(self.max_retries):
            try:
                completion = self.kimi_client.chat.completions.create(
                    model="kimi-k2.5",
                    messages=[
                        {
                            "role": "system",
                            "content": """你是 Kimi，一个学术文献处理专家。接下来我需要你对一些文件进行视觉识别和元信息提取，给你的文件包含了论文的页脚和页眉。其中你需要处理的数据可能包括论文标题、作者、发表年份、DOI，发表期刊、期卷信息。你需要以json格式输出，他们的具体输出格式如下:
                            "title": "论文题目（必填）",
                            "authors": ["作者1", "作者2"]（必填，至少一个作者）,
                            "journal": "来源（出版社/期刊）（必填）",
                            "year": 年份（必填，数字格式）,
                            "volume_issue": "期卷信息（期：卷）"  # 最终输出格式为期（卷）
                            你并不一定每次都要找全部信息，重点是找寻、补全缺少的元数据。
                            """
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": image_url}},
                                {
                                    "type": "text",
                                    "text": f"""当前元数据缺少以下字段：{field_desc}。文件名是{file_name}。
                                     请注意，作者，发表时间等信息有时也会在文件名中出现，请同时从中尝试提取信息。"
                                     请在图片中寻找这些缺失的元信息信息并按照系统提示此返回 JSON 格式。
                                     如果图片中没有，请留空。"""
                                }
                            ]
                        }
                    ],
                    response_format={"type": "json_object"},
                    temperature=1.0  # 视觉模型建议固定为 1.0
                )

                result = json.loads(completion.choices[0].message.content)
                print(f"{prefix} 👁️ Kimi视觉补全: {result}")
                return result

            except Exception as e:
                err_msg = str(e)
                # 检查是否为 429 频率限制或服务器过载
                if "429" in err_msg or "overloaded" in err_msg.lower():
                    wait_time = (2 ** attempt) + random.uniform(0, 1)
                    print(f"{prefix} ⏳ Kimi 服务器繁忙/限速 (429)，等待 {wait_time:.1f}s 后进行第 {attempt + 1} 次重试...")
                    time.sleep(wait_time)
                else:
                    # 其他错误（如 400 格式错误）直接跳出
                    print(f"{prefix} ⚠️ Kimi 识别发生非重试错误: {e}")
                    break

        return {}

    def _get_metadata_prompt(self, extracted_text: str, file_name: str, doc_type: str) -> str:
        """生成元数据提取提示词"""
        if doc_type == "paper":
            return f"""你是一个学术文献处理助手。我会给你一段从论文前几页提取的文本。
            请从中提取以下信息，并以 JSON 格式返回：
            {{
              "title": "论文标题（必填）",
              "authors": ["作者1", "作者2"]（必填，至少一个作者）,
              "doi": "DOI号（必填）",
              "journal": "期刊名（必填）",
              "year": 发表年份（必填，数字格式）,
              "volume_issue": "期卷信息（期：卷）"
            }}

            重要要求：
            1. 所有字段都必须填写，禁止使用xxxx或unknown，实在不行留空。
            2. 期卷信息提取说明：请寻找类似于 'Volume 5, Issue 2'、'Vol. 5, No. 2'、'5(2)' 或 '5:2' 的表达，并统一提取到 volume_issue 字段中，格式为期（卷），如7（6）。
            3. 如果字段在文本中找不到，请仔细查找。文件名 {file_name} 也是重要参考。
            4. 底部扫描：优先查看页面最底部，那里通常包含来源（出版社/期刊）、年份和期（卷）信息。
            5. DOI 必须以 10. 开头，年份为4位数字。

            直接返回 JSON 对象，不要包含其他文本。

            文本内容：
            {extracted_text}"""
        else:
            return f"""你是一个学术文献处理助手。我会给你一段从书籍前几页提取的文本。
            请从中提取以下信息，并以 JSON 格式返回：
            {{
              "title": "著作标题（必填）",
              "authors": ["作者1", "作者2"]（必填，至少一个作者）,
              "year": 出版年份（必填，数字格式）,
              "publisher": "出版社名称（必填）",
              "isbn": "ISBN号（必填）"
            }}

            重要要求：
            1. 所有字段都必须填写，不可以xxxx或者unknown进行无意义填充，实在不行只能留空
            2. 如果某个字段在文本中找不到，请根据上下文合理推断或搜索相关信息。注意，{file_name}中可能包含论文名称和作者，可以参考。
            3. ISBN 必须是标准格式（如：978-7-111-12345-6）
            4. 出版年份必须是4位数字
            5. 作者必须是真实的人名列表

            直接返回 JSON 对象，不要包含其他文本。

            文本内容：
            {extracted_text}"""

    def _validate_paper_metadata(self, meta: Dict[str, Optional[str]]) -> tuple[bool, List[str]]:
        """验证论文元数据是否完整"""
        required_fields = {
            "论文题目": meta.get("论文题目"),
            "作者": meta.get("作者"),
            "来源（出版社/期刊）": meta.get("来源（出版社/期刊）"),
            "年份": meta.get("年份"),
            "期（卷）": meta.get("期（卷）"),
        }

        missing_fields = [field for field, value in required_fields.items() if not value or value == "None"]

        return len(missing_fields) == 0, missing_fields

    def _validate_book_metadata(self, meta: Dict[str, Optional[str]]) -> tuple[bool, List[str]]:
        """验证书籍元数据是否完整"""
        required_fields = {
            "论文题目": meta.get("论文题目"),
            "作者": meta.get("作者"),
            "年份": meta.get("年份"),
            "来源（出版社/期刊）": meta.get("来源（出版社/期刊）"),
            "ISBN": meta.get("ISBN"),
        }

        missing_fields = [field for field, value in required_fields.items() if not value or value == "None"]

        return len(missing_fields) == 0, missing_fields

    def analyze_content(self,
                       extracted_text: str,
                       analysis_type: str = "summary",
                       custom_prompt: Optional[str] = None) -> Dict[str, str]:
        """
        使用LLM分析PDF内容

        Args:
            extracted_text: 提取的文本内容
            analysis_type: 分析类型（"summary", "keywords", "custom"）
            custom_prompt: 自定义提示词（当analysis_type为"custom"时使用）

        Returns:
            dict: 分析结果
        """
        if not extracted_text:
            return {"error": "没有提供文本内容"}

        # 根据分析类型选择提示词
        if analysis_type == "summary":
            prompt = f"""请对以下学术文献内容进行摘要，提取主要观点和结论：

            文本内容：
            {extracted_text[:2000]}  # 限制长度以避免token超限

            请以JSON格式返回：
            {{
                "summary": "内容摘要",
                "key_points": ["要点1", "要点2"],
                "conclusion": "主要结论"
            }}"""
        elif analysis_type == "keywords":
            prompt = f"""请从以下学术文献中提取关键词：

            文本内容：
            {extracted_text[:2000]}

            请以JSON格式返回：
            {{
                "keywords": ["关键词1", "关键词2"],
                "key_phrases": ["关键短语1", "关键短语2"]
            }}"""
        elif analysis_type == "custom" and custom_prompt:
            prompt = custom_prompt.format(text=extracted_text[:2000])
        else:
            return {"error": "无效的分析类型或缺少自定义提示词"}

        try:
            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1000
            )

            content = response.choices[0].message.content.strip()

            # 尝试解析JSON
            try:
                result = json.loads(content)
                return result
            except json.JSONDecodeError:
                # 如果解析失败，返回原始内容
                return {"raw_content": content}

        except Exception as e:
            print(f"  ✗ 内容分析失败: {e}")
            return {"error": str(e)}

    def extract_doi_from_text(self, text: str) -> Optional[str]:
        """从文本中提取DOI"""
        if not text:
            return None
        doi_pattern = r'10\.\d{4,}/[-._;()/:A-Z0-9]+'
        match = re.search(doi_pattern, text, re.IGNORECASE)
        return match.group(0) if match else None


def test_llm_enhancer():
    """测试LLM增强器功能"""
    print("=" * 60)
    print("🔬 测试 LLMEnhancer 类")
    print("=" * 60)

    # 创建实例
    enhancer = LLMEnhancer()

    # 测试文本
    test_text = """
    Toward a Sign Language-Friendly Questionnaire Design
    Marta Bosch-Baliarda, Olga Soler Vilageliu and Pilar Orero

    Abstract
    The United Nations Convention on the Rights of Persons with Disabilities requests "Nothing about us without us."
    User-centered methodological research is the way to comply with this convention. Interaction with the deaf community
    must be in their language; hence sign language questionnaires are one of the tools to gather data.
    """

    # 测试1：手语相关性判断
    print("\n📋 测试1：手语相关性判断")
    result = enhancer.check_sign_language_relevance(
        title="Toward a Sign Language-Friendly Questionnaire Design",
        extracted_text=test_text
    )
    print(f"结果: {result}")

    # 测试2：论文元数据提取
    print("\n📋 测试2：论文元数据提取")
    meta = enhancer.extract_metadata_with_llm(
        extracted_text=test_text,
        file_name="test_paper.pdf",
        doc_type="paper",
        prefix="[TEST]"
    )
    print(f"提取的元数据: {json.dumps(meta, ensure_ascii=False, indent=2)}")

    # 测试3：内容摘要
    print("\n📋 测试3：内容摘要")
    summary = enhancer.analyze_content(
        extracted_text=test_text,
        analysis_type="summary"
    )
    print(f"内容摘要: {json.dumps(summary, ensure_ascii=False, indent=2)}")

    # 测试4：关键词提取
    print("\n📋 测试4：关键词提取")
    keywords = enhancer.analyze_content(
        extracted_text=test_text,
        analysis_type="keywords"
    )
    print(f"关键词: {json.dumps(keywords, ensure_ascii=False, indent=2)}")

    print("\n✅ 测试完成！")


if __name__ == "__main__":
    test_llm_enhancer()