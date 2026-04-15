#!/usr/bin/env python3
"""
PDF文献元数据批量提取工具 - 命令行版本
基于Alpha版的多级分流架构 + 高并发处理

用法:
  python batch-cli.py --input-dir /path/to/pdfs --output /path/to/output.csv
  或
  python batch-cli.py -i /path/to/pdf1.pdf /path/to/pdf2.pdf -o results.csv
"""

import os
import sys
import argparse
import time
import threading
from typing import List, Dict, Tuple, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd

# 导入Alpha版的核心类和常量
from batch_alpha_core import (
    PaperMetadataExtractorAlpha,
    MultiSourceMetadataFetcher,
    ProcessingResult,
    DEFAULT_EMAIL,
    DEFAULT_KEYWORDS,
    MAX_WORKERS,
    SAVE_INTERVAL,
    DEFAULT_FILE_TIMEOUT,
    DEFAULT_PAGE_COUNT
)

def setup_argparse():
    """设置命令行参数解析"""
    parser = argparse.ArgumentParser(
        description="PDF文献元数据批量提取工具 - 命令行版本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 处理单个文件夹
  python batch-cli.py -i /path/to/pdfs -o results.csv

  # 处理多个文件夹
  python batch-cli.py -i /path/to/folder1 /path/to/folder2 -o results.csv

  # 处理单个文件
  python batch-cli.py -i document.pdf -o results.csv

  # 使用自定义配置
  python batch-cli.py -i /path/to/pdfs -o results.csv \\
    --email your-email@example.com \\
    --keywords "sign language, deaf, 手语" \\
    --workers 10 \\
    --timeout 30 \\
    --pages 5
        """
    )

    # 输入参数
    parser.add_argument(
        '-i', '--input',
        nargs='+',
        required=True,
        help='输入PDF文件或文件夹路径（支持多个）'
    )

    parser.add_argument(
        '-o', '--output',
        required=True,
        help='输出CSV文件路径'
    )

    # 配置参数
    parser.add_argument(
        '--email',
        default=DEFAULT_EMAIL,
        help=f'OpenAlex邮箱地址（默认: {DEFAULT_EMAIL}）'
    )

    parser.add_argument(
        '--keywords',
        default=DEFAULT_KEYWORDS,
        help=f'手语关键词，逗号分隔（默认: {DEFAULT_KEYWORDS[:50]}...）'
    )

    parser.add_argument(
        '--sources',
        default='crossref,openalex,semantic_scholar',
        help='数据源，逗号分隔（默认: crossref,openalex,semantic_scholar）'
    )

    parser.add_argument(
        '--workers',
        type=int,
        default=MAX_WORKERS,
        help=f'并发线程数（默认: {MAX_WORKERS}）'
    )

    parser.add_argument(
        '--timeout',
        type=int,
        default=DEFAULT_FILE_TIMEOUT,
        help=f'单文件处理超时时间（秒，默认: {DEFAULT_FILE_TIMEOUT}）'
    )

    parser.add_argument(
        '--pages',
        type=int,
        default=DEFAULT_PAGE_COUNT,
        help=f'PDF截取页数（默认: {DEFAULT_PAGE_COUNT}）'
    )

    parser.add_argument(
        '--classify-dir',
        help='归类目录（成功/失败文件分类存放，可选）'
    )

    parser.add_argument(
        '--recursive',
        action='store_true',
        help='递归搜索子目录'
    )

    parser.add_argument(
        '--skip-existing',
        action='store_true',
        help='跳过输出文件中已存在的记录'
    )

    parser.add_argument(
        '--verbose',
        action='store_true',
        help='显示详细日志'
    )

    return parser.parse_args()

def log_message(message: str, verbose: bool = False, force: bool = False):
    """日志输出函数"""
    if verbose or force:
        print(f"[{time.strftime('%H:%M:%S')}] {message}")

def collect_pdf_files(input_paths: List[str], recursive: bool) -> List[Tuple[str, str]]:
    """收集所有PDF文件"""
    pdf_files = []
    skip_dirs = {'完成', '失败', 'success', 'failed'}

    for input_path in input_paths:
        if os.path.isfile(input_path) and input_path.lower().endswith('.pdf'):
            # 单个PDF文件
            pdf_files.append((os.path.dirname(input_path), os.path.basename(input_path)))
            log_message(f"添加文件: {input_path}", verbose=True)

        elif os.path.isdir(input_path):
            # 文件夹
            if recursive:
                for root, dirs, files in os.walk(input_path):
                    dirs[:] = [d for d in dirs if d not in skip_dirs]
                    for f in files:
                        if f.lower().endswith('.pdf'):
                            pdf_files.append((root, f))
            else:
                try:
                    files = [f for f in os.listdir(input_path) if f.lower().endswith('.pdf')]
                    pdf_files.extend([(input_path, f) for f in files])
                except Exception as e:
                    log_message(f"读取目录失败 {input_path}: {e}", force=True)

        else:
            log_message(f"警告：路径不存在或不是PDF文件: {input_path}", force=True)

    return pdf_files

def classify_file(file_path: str, status: str, classify_dir: str, error_type: str = ""):
    """归类文件到成功/失败目录"""
    try:
        import shutil
        import re

        filename = os.path.basename(file_path)

        if status == "成功":
            # 成功文件统一放入"完成"目录
            dest_folder = os.path.join(classify_dir, "完成")
        else:
            # 失败文件按错误类型分类
            safe_error_type = error_type if error_type else "未知错误"
            safe_error_type = re.sub(r'[<>:"/\\|?*]', '_', safe_error_type)
            safe_error_type = safe_error_type[:50]  # 限制长度
            dest_folder = os.path.join(classify_dir, "失败", safe_error_type)

        os.makedirs(dest_folder, exist_ok=True)
        dest_path = os.path.join(dest_folder, filename)

        if os.path.exists(dest_path):
            os.remove(dest_path)

        shutil.copy2(file_path, dest_path)
        log_message(f"已归类文件: {filename} -> {dest_folder}", verbose=True)

    except Exception as exc:
        log_message(f"归类失败 {os.path.basename(file_path)}: {exc}", force=True)

def save_results(results: List[Dict], output_file: str):
    """保存结果到CSV"""
    try:
        df = pd.DataFrame(results)
        df.to_csv(output_file, index=False, encoding='utf-8-sig')
        log_message(f"已保存 {len(results)} 条记录到: {output_file}", force=True)
    except Exception as e:
        log_message(f"保存结果失败：{e}", force=True)

def main():
    """主函数"""
    args = setup_argparse()

    log_message("=" * 60, force=True)
    log_message("PDF文献元数据批量提取工具 - 命令行版本", force=True)
    log_message("=" * 60, force=True)

    # 收集PDF文件
    log_message(f"正在收集PDF文件...", force=True)
    pdf_files = collect_pdf_files(args.input, args.recursive)

    if not pdf_files:
        log_message("错误：未找到任何PDF文件！", force=True)
        sys.exit(1)

    log_message(f"共找到 {len(pdf_files)} 个PDF文件", force=True)

    # 解析数据源
    enabled_sources = [s.strip() for s in args.sources.split(',') if s.strip()]

    # 初始化提取器
    log_message(f"初始化提取器...", force=True)
    log_message(f"  邮箱: {args.email}", force=True)
    log_message(f"  并发线程: {args.workers}", force=True)
    log_message(f"  超时时间: {args.timeout}秒", force=True)
    log_message(f"  截取页数: {args.pages}", force=True)
    log_message(f"  数据源: {', '.join(enabled_sources)}", force=True)

    extractor = PaperMetadataExtractorAlpha(
        email=args.email,
        keywords=args.keywords,
        enabled_sources=enabled_sources,
        max_workers=args.workers,
        file_timeout=args.timeout,
        page_count=args.pages,
        log_callback=lambda msg: log_message(msg, args.verbose)
    )

    # 读取已有结果（如果启用跳过已存在）
    results = []
    done_files: Set[str] = set()

    if args.skip_existing and os.path.exists(args.output):
        try:
            old_df = pd.read_csv(args.output)
            results = old_df.to_dict('records')
            done_files = set(old_df['文件名称'].astype(str).tolist())
            log_message(f"检测到已有结果文件，已跳过 {len(done_files)} 个文件", force=True)
        except Exception as e:
            log_message(f"读取旧文件失败：{e}，将重新开始", force=True)

    # 过滤已处理的文件
    pending_files = [(d, f) for d, f in pdf_files if f not in done_files]

    if not pending_files:
        log_message("所有文件都已处理完成！", force=True)
        sys.exit(0)

    log_message(f"待处理文件数: {len(pending_files)}", force=True)

    # 并发处理
    processed = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        # 提交所有任务
        future_to_file = {}
        for i, (current_dir, filename) in enumerate(pending_files):
            file_path = os.path.join(current_dir, filename)
            thread_id = str(i % 100).zfill(2)  # 简化的线程ID
            future = executor.submit(extractor.process_pdf, file_path, thread_id)
            future_to_file[future] = (current_dir, filename, file_path)

        # 收集结果
        for future in as_completed(future_to_file):
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
                if args.classify_dir:
                    classify_file(file_path, result.status, args.classify_dir, result.error_type)

                results.append(row)
                processed += 1

                # 显示进度
                progress = processed / len(pending_files) * 100
                log_message(f"[{processed:4d}/{len(pending_files)}] {progress:5.1f}% - {filename[:50]}", force=True)

                # 显示统计信息
                if args.verbose and extractor.stats:
                    stats = extractor.stats
                    log_message(f"  统计: Tier1:{stats['tier1_count']} Tier2:{stats['tier2_count']} Tier3:{stats['tier3_count']} "
                               f"✓:{stats['success_count']} ⏭:{stats['skipped_count']} ✗:{stats['failed_count']}",
                               verbose=True)

                # 定期保存
                if processed % SAVE_INTERVAL == 0:
                    save_results(results, args.output)

            except Exception as e:
                log_message(f"处理异常 {filename}: {str(e)}", force=True)
                error_type = f"处理异常: {str(e)[:50]}"
                results.append({
                    "文件名称": filename,
                    "状态": "失败",
                    "error_type": error_type,
                    "来源文件夹": os.path.basename(current_dir)
                })
                # 异常文件也归类
                if args.classify_dir:
                    classify_file(file_path, "失败", args.classify_dir, error_type)

    # 最终保存
    save_results(results, args.output)

    # 计算耗时
    elapsed_time = time.time() - start_time
    avg_time = elapsed_time / processed if processed > 0 else 0

    log_message("=" * 60, force=True)
    log_message(f"任务完成！", force=True)
    log_message(f"处理文件数: {processed}/{len(pending_files)}", force=True)
    log_message(f"总耗时: {elapsed_time:.1f}秒", force=True)
    log_message(f"平均时间: {avg_time:.2f}秒/文件", force=True)
    log_message(f"结果文件: {args.output}", force=True)

    # 显示最终统计
    if extractor.stats:
        stats = extractor.stats
        log_message(f"最终统计:", force=True)
        log_message(f"  Tier1 (快车道): {stats['tier1_count']}", force=True)
        log_message(f"  Tier2 (中车道): {stats['tier2_count']}", force=True)
        log_message(f"  Tier3 (慢车道): {stats['tier3_count']}", force=True)
        log_message(f"  成功: {stats['success_count']}", force=True)
        log_message(f"  跳过: {stats['skipped_count']}", force=True)
        log_message(f"  失败: {stats['failed_count']}", force=True)
        log_message(f"  超时: {stats['timeout_count']}", force=True)

    log_message("=" * 60, force=True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n程序被用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n程序运行出错: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)