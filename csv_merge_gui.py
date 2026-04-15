import os
import pandas as pd
import customtkinter as ctk
from tkinter import filedialog, messagebox
import threading
import time
from typing import List, Dict, Optional, Tuple
from pathlib import Path

# --- 设置外观 ---
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


class CSVMergeToolGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("CSV 表格对齐合并工具")
        self.geometry("850x750")
        self.minsize(800, 700)

        # 运行状态变量
        self.is_running = False
        self.stop_requested = False
        self.source_csv_path: Optional[str] = None
        self.target_csv_path: Optional[str] = None
        self.target_headers: List[str] = []

        # --- UI 布局配置 ---
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(6, weight=1)  # 因为增加了一行，权重行改为6

        self._setup_ui()

    def _setup_ui(self):
        """初始化 UI 组件"""
        # 1. 源CSV文件选择
        self.label_source = ctk.CTkLabel(self, text="源 CSV 文件 (batch.py输出):")
        self.label_source.grid(row=0, column=0, padx=20, pady=10, sticky="e")

        self.entry_source = ctk.CTkEntry(
            self,
            placeholder_text="请选择 batch.py 输出的 CSV 文件...",
            width=450
        )
        self.entry_source.grid(row=0, column=1, padx=10, pady=10, sticky="ew")

        self.btn_browse_source = ctk.CTkButton(
            self,
            text="选择源文件",
            command=self.browse_source_file
        )
        self.btn_browse_source.grid(row=0, column=2, padx=20, pady=10)

        # 2. 目标CSV文件选择
        self.label_target = ctk.CTkLabel(self, text="目标 CSV 文件 (主表格):")
        self.label_target.grid(row=1, column=0, padx=20, pady=10, sticky="e")

        self.entry_target = ctk.CTkEntry(
            self,
            placeholder_text="请选择要追加写入的主表格 CSV 文件...",
            width=450
        )
        self.entry_target.grid(row=1, column=1, padx=10, pady=10, sticky="ew")

        self.btn_browse_target = ctk.CTkButton(
            self,
            text="选择目标文件",
            command=self.browse_target_file
        )
        self.btn_browse_target.grid(row=1, column=2, padx=20, pady=10)

        # 目标表格表头预览
        self.btn_preview_headers = ctk.CTkButton(
            self,
            text="预览表头",
            fg_color="#1f538d",
            command=self.preview_target_headers
        )
        self.btn_preview_headers.grid(row=1, column=3, padx=10, pady=10)

        # 3. 输出选项
        self.label_output = ctk.CTkLabel(self, text="输出方式:")
        self.label_output.grid(row=2, column=0, padx=20, pady=10, sticky="e")

        # 输出选项框架
        self.output_frame = ctk.CTkFrame(self)
        self.output_frame.grid(row=2, column=1, padx=10, pady=10, sticky="ew", columnspan=3)

        self.radio_overwrite = ctk.CTkRadioButton(
            self.output_frame,
            text="直接覆盖原目标文件"
        )
        self.radio_overwrite.pack(side="left", padx=10, pady=5)
        self.radio_overwrite.select()  # 默认选择覆盖

        self.radio_newfile = ctk.CTkRadioButton(
            self.output_frame,
            text="另存为新文件"
        )
        self.radio_newfile.pack(side="left", padx=10, pady=5)

        self.entry_newfile = ctk.CTkEntry(
            self.output_frame,
            placeholder_text="新文件路径...",
            width=250,
            state="disabled"  # 默认禁用
        )
        self.entry_newfile.pack(side="left", padx=10, pady=5)

        self.btn_browse_newfile = ctk.CTkButton(
            self.output_frame,
            text="选择",
            width=60,
            state="disabled",
            command=self.browse_newfile
        )
        self.btn_browse_newfile.pack(side="left", padx=5, pady=5)

        # 绑定单选按钮事件
        self.output_mode = "overwrite"  # 使用简单的字符串变量跟踪选择
        self.radio_overwrite.configure(command=lambda: self.on_output_option_changed("overwrite"))
        self.radio_newfile.configure(command=lambda: self.on_output_option_changed("newfile"))

        # 4. 处理人和方向配置
        self.label_processor = ctk.CTkLabel(self, text="处理人:")
        self.label_processor.grid(row=3, column=0, padx=20, pady=10, sticky="e")

        self.entry_processor = ctk.CTkEntry(
            self,
            placeholder_text="熊羿成",
            width=200
        )
        self.entry_processor.grid(row=3, column=1, padx=10, pady=10, sticky="w")
        self.entry_processor.insert(0, "熊羿成")  # 设置默认值

        self.label_direction = ctk.CTkLabel(self, text="方向:")
        self.label_direction.grid(row=3, column=2, padx=20, pady=10, sticky="e")

        self.entry_direction = ctk.CTkEntry(
            self,
            placeholder_text="手语语言学",
            width=200
        )
        self.entry_direction.grid(row=3, column=3, padx=10, pady=10, sticky="w")
        self.entry_direction.insert(0, "手语语言学")  # 设置默认值

        # 5. 列名映射预览区域
        self.label_mapping = ctk.CTkLabel(self, text="列名映射预览:")
        self.label_mapping.grid(row=4, column=0, padx=20, pady=10, sticky="ne")

        self.mapping_text = ctk.CTkTextbox(self, height=150)
        self.mapping_text.grid(row=4, column=1, padx=10, pady=10, sticky="nsew", columnspan=3)
        self.mapping_text.insert("1.0", "选择源文件和目标文件后，将显示列名映射关系...")
        self.mapping_text.configure(state="disabled")

        # 6. 日志框
        self.log_box = ctk.CTkTextbox(self, height=250)
        self.log_box.grid(row=5, column=0, columnspan=4, padx=20, pady=10, sticky="nsew")

        # 7. 进度条
        self.progress_bar = ctk.CTkProgressBar(self)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=6, column=0, columnspan=4, padx=20, pady=10, sticky="ew")

        self.progress_label = ctk.CTkLabel(self, text="", text_color="gray")
        self.progress_label.grid(row=6, column=4, padx=10, pady=10, sticky="w")

        # 8. 控制按钮
        self.btn_start = ctk.CTkButton(
            self,
            text="开始合并",
            fg_color="green",
            hover_color="darkgreen",
            height=40,
            command=self.start_merge
        )
        self.btn_start.grid(row=7, column=1, pady=20)

        self.btn_stop = ctk.CTkButton(
            self,
            text="停止",
            fg_color="red",
            state="disabled",
            command=self.stop_merge
        )
        self.btn_stop.grid(row=7, column=2, pady=20)

        self.btn_clear = ctk.CTkButton(
            self,
            text="清空",
            fg_color="gray",
            command=self.clear_all
        )
        self.btn_clear.grid(row=7, column=3, pady=20)

    # --- 文件选择逻辑 ---
    def browse_source_file(self):
        """选择源 CSV 文件"""
        file_path = filedialog.askopenfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            title="选择源 CSV 文件"
        )
        if file_path:
            self.source_csv_path = file_path
            self.entry_source.delete(0, "end")
            self.entry_source.insert(0, file_path)
            self.log(f"已选择源文件: {file_path}")
            self.update_mapping_preview()

    def browse_target_file(self):
        """选择目标 CSV 文件"""
        file_path = filedialog.askopenfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            title="选择目标 CSV 文件"
        )
        if file_path:
            self.target_csv_path = file_path
            self.entry_target.delete(0, "end")
            self.entry_target.insert(0, file_path)
            self.log(f"已选择目标文件: {file_path}")
            self.load_target_headers()
            self.update_mapping_preview()

    def browse_newfile(self):
        """选择新文件保存路径"""
        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            title="选择新文件保存路径"
        )
        if file_path:
            self.entry_newfile.delete(0, "end")
            self.entry_newfile.insert(0, file_path)

    def on_output_option_changed(self, mode):
        """输出选项变更处理
        Args:
            mode: "overwrite" 或 "newfile"
        """
        self.output_mode = mode
        if mode == "newfile":
            self.entry_newfile.configure(state="normal")
            self.btn_browse_newfile.configure(state="normal")
        else:
            self.entry_newfile.configure(state="disabled")
            self.btn_browse_newfile.configure(state="disabled")

    def preview_target_headers(self):
        """预览目标表格的表头"""
        if not self.target_csv_path or not os.path.exists(self.target_csv_path):
            messagebox.showwarning("警告", "请先选择目标 CSV 文件")
            return

        try:
            df = pd.read_csv(self.target_csv_path, encoding='utf-8-sig', nrows=0)
            headers = list(df.columns)

            preview_window = ctk.CTkToplevel(self)
            preview_window.title("目标表格表头预览")
            preview_window.geometry("500x400")

            textbox = ctk.CTkTextbox(preview_window, width=480, height=350)
            textbox.pack(padx=10, pady=10)

            header_text = "目标表格表头 (共{}列):\n\n".format(len(headers))
            for i, header in enumerate(headers, 1):
                header_text += f"{i:2d}. {header}\n"

            textbox.insert("1.0", header_text)
            textbox.configure(state="disabled")

            self.log(f"已预览目标表格表头，共 {len(headers)} 列")

        except Exception as e:
            self.log(f"预览表头失败: {str(e)}")
            messagebox.showerror("错误", f"读取目标文件失败:\n{str(e)}")

    def load_target_headers(self):
        """加载目标表格的表头"""
        if not self.target_csv_path or not os.path.exists(self.target_csv_path):
            self.target_headers = []
            return

        try:
            df = pd.read_csv(self.target_csv_path, encoding='utf-8-sig', nrows=0)
            self.target_headers = list(df.columns)
            self.log(f"已加载目标表格表头，共 {len(self.target_headers)} 列")
        except Exception as e:
            self.log(f"加载表头失败: {str(e)}")
            self.target_headers = []

    def update_mapping_preview(self):
        """更新列名映射预览"""
        if not self.source_csv_path or not self.target_csv_path:
            return

        self.mapping_text.configure(state="normal")
        self.mapping_text.delete("1.0", "end")

        try:
            # 读取源文件的前几行来了解结构
            df_source = pd.read_csv(self.source_csv_path, encoding='utf-8-sig')
            source_columns = list(df_source.columns)

            mapping_text = "列名映射关系:\n\n"
            mapping_text += f"源文件列 ({len(source_columns)}列):\n"
            for col in source_columns[:15]:  # 只显示前15列
                mapping_text += f"  • {col}\n"

            if len(source_columns) > 15:
                mapping_text += f"  ... 还有 {len(source_columns) - 15} 列\n"

            mapping_text += f"\n目标文件列 ({len(self.target_headers)}列):\n"
            for col in self.target_headers[:15]:  # 只显示前15列
                mapping_text += f"  • {col}\n"

            if len(self.target_headers) > 15:
                mapping_text += f"  ... 还有 {len(self.target_headers) - 15} 列\n"

            self.mapping_text.insert("1.0", mapping_text)

        except Exception as e:
            self.mapping_text.insert("1.0", f"读取文件失败: {str(e)}")

        self.mapping_text.configure(state="disabled")

    # --- 日志和进度 ---
    def log(self, message: str):
        """线程安全的日志输出方法"""
        def _log():
            self.log_box.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
            self.log_box.see("end")

        if threading.current_thread() is not threading.main_thread():
            self.after(0, _log)
        else:
            _log()

    def update_progress(self, value: float, current: int, total: int, message: str = ""):
        """更新进度条和标签"""
        def _update():
            self.progress_bar.set(value / 100)
            self.progress_label.configure(text=f"{current}/{total} - {message}")

        if threading.current_thread() is not threading.main_thread():
            self.after(0, _update)
        else:
            _update()

    def clear_all(self):
        """清空所有输入"""
        self.source_csv_path = None
        self.target_csv_path = None
        self.target_headers = []

        self.entry_source.delete(0, "end")
        self.entry_source.insert(0, "请选择 batch.py 输出的 CSV 文件...")

        self.entry_target.delete(0, "end")
        self.entry_target.insert(0, "请选择要追加写入的主表格 CSV 文件...")

        self.entry_newfile.delete(0, "end")
        self.entry_newfile.insert(0, "新文件路径...")

        # 重置处理人和方向为默认值
        self.entry_processor.delete(0, "end")
        self.entry_processor.insert(0, "熊羿成")

        self.entry_direction.delete(0, "end")
        self.entry_direction.insert(0, "手语语言学")

        self.mapping_text.configure(state="normal")
        self.mapping_text.delete("1.0", "end")
        self.mapping_text.insert("1.0", "选择源文件和目标文件后，将显示列名映射关系...")
        self.mapping_text.configure(state="disabled")

        self.log_box.delete("1.0", "end")
        self.progress_bar.set(0)
        self.progress_label.configure(text="")

        self.log("已清空所有输入")

    # --- 核心合并逻辑 ---
    def merge_logic(self):
        """合并逻辑的主函数"""
        source_path = self.source_csv_path
        target_path = self.target_csv_path

        if not source_path or not target_path:
            messagebox.showerror("错误", "请同时选择源文件和目标文件！")
            self.after(10, self.reset_ui)
            return

        if not os.path.exists(source_path):
            messagebox.showerror("错误", f"源文件不存在:\n{source_path}")
            self.after(10, self.reset_ui)
            return

        if not os.path.exists(target_path):
            messagebox.showerror("错误", f"目标文件不存在:\n{target_path}")
            self.after(10, self.reset_ui)
            return

        # 确定输出路径
        if self.output_mode == "newfile":
            output_path = self.entry_newfile.get().strip()
            if not output_path:
                messagebox.showerror("错误", "请选择新文件保存路径！")
                self.after(10, self.reset_ui)
                return
        else:
            output_path = target_path  # 直接覆盖

        self.log(f"开始合并: {source_path} -> {target_path}")
        self.log(f"输出路径: {output_path}")

        try:
            # 读取源数据
            self.update_progress(10, 1, 4, "读取源文件...")
            df_source = pd.read_csv(source_path, encoding='utf-8-sig')
            self.log(f"读取源文件成功，共 {len(df_source)} 行，{len(df_source.columns)} 列")

            # 过滤掉状态为"失败"的行
            if '状态' in df_source.columns:
                original_count = len(df_source)
                # 将状态列转换为字符串并去除空格，然后过滤掉值为"失败"的行
                # 注意：NaN会变成字符串'nan'，不会被过滤
                df_source = df_source[df_source['状态'].astype(str).str.strip() == '成功']
                filtered_count = len(df_source)
                removed_count = original_count - filtered_count
                if removed_count > 0:
                    self.log(f"过滤掉 {removed_count} 条状态为'失败'的记录")
                else:
                    self.log("没有状态为'失败'的记录需要过滤")
            else:
                self.log("警告：源文件中没有'状态'列，无法过滤失败记录")

            # 读取目标数据
            self.update_progress(30, 2, 4, "读取目标文件...")
            if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                df_target = pd.read_csv(target_path, encoding='utf-8-sig')
                self.log(f"读取目标文件成功，共 {len(df_target)} 行，{len(df_target.columns)} 列")
                self.log(f"目标文件列名: {list(df_target.columns)}")
            else:
                df_target = pd.DataFrame()
                self.log("目标文件为空或不存在，将创建新文件")

            # 获取处理人和方向配置
            processor = self.entry_processor.get().strip()
            direction = self.entry_direction.get().strip()

            if not processor:
                processor = "熊羿成"  # 默认值
            if not direction:
                direction = "手语语言学"  # 默认值

            self.log(f"使用配置: 处理人={processor}, 方向={direction}")

            # 转换源数据到目标格式
            self.update_progress(50, 3, 4, "转换数据格式...")
            df_new = self.transform_source_to_target(df_source, df_target, processor, direction)
            self.log(f"转换完成，生成 {len(df_new)} 条新记录")

            # 合并数据
            self.update_progress(70, 4, 4, "合并数据...")
            if not df_target.empty:
                df_combined = pd.concat([df_target, df_new], ignore_index=True)
                self.log(f"合并后总记录数: {len(df_combined)}")
            else:
                df_combined = df_new

            # 聚合重复记录（根据文件名称）
            if "文件名称" in df_combined.columns:
                before_aggregate = len(df_combined)
                df_combined = self.aggregate_duplicates(df_combined, "文件名称")
                after_aggregate = len(df_combined)
                if before_aggregate != after_aggregate:
                    self.log(f"聚合处理: {before_aggregate} 条记录聚合为 {after_aggregate} 条记录")
                self.log(f"聚合后列名: {list(df_combined.columns)}")

            # 保存结果
            self.log(f"最终DataFrame列名: {list(df_combined.columns)}")
            self.log(f"最终DataFrame形状: {df_combined.shape}")
            self.update_progress(90, 5, 5, "保存结果...")
            df_combined.to_csv(output_path, index=False, encoding='utf-8-sig')

            self.update_progress(100, 5, 5, "完成！")
            self.log(f"✅ 合并完成！共添加 {len(df_new)} 条新记录")
            self.log(f"✅ 最终文件: {output_path}")
            self.log(f"✅ 总记录数: {len(df_combined)}")

            messagebox.showinfo("成功", f"合并完成！\n\n添加了 {len(df_new)} 条新记录\n总记录数: {len(df_combined)}\n文件已保存至: {output_path}")

        except Exception as e:
            self.log(f"❌ 合并失败: {str(e)}")
            messagebox.showerror("错误", f"合并过程中出错:\n{str(e)}")

        self.after(10, self.reset_ui)

    def transform_source_to_target(self, df_source: pd.DataFrame, df_target: pd.DataFrame,
                                  processor: str = "熊羿成", direction: str = "手语语言学") -> pd.DataFrame:
        """将源数据转换为目标格式
        Args:
            df_source: 源数据DataFrame
            df_target: 目标数据DataFrame
            processor: 处理人姓名
            direction: 研究方向
        """
        # 如果目标表格为空，使用默认列名
        if df_target.empty:
            target_columns = [
                "文件名称", "论文题目", "语种", "作者", "年份",
                "来源（出版社/期刊）", "期（卷）", "类型（专著/论文）",
                "方向", "是否译文", "处理人"
            ]
        else:
            target_columns = list(df_target.columns)

        self.log(f"目标列: {target_columns}")

        # 再次过滤掉状态为"失败"的行（防御性编程）
        if '状态' in df_source.columns:
            df_source = df_source[df_source['状态'].astype(str).str.strip() == '成功']

        # 调试日志：检查源数据列
        self.log(f"转换调试 - 源数据列: {list(df_source.columns)}")
        # 检查batch.py输出的标准列名
        expected_columns = ['文件名称', '论文题目', '语种', '作者', '年份', '来源（出版社/期刊）', '期（卷）', '状态', '来源文件夹']
        missing_columns = [col for col in expected_columns if col not in df_source.columns]
        if missing_columns:
            self.log(f"转换调试 - 警告：源数据缺少以下列: {missing_columns}")

        # 检查关键列
        if '文件名称' in df_source.columns:
            self.log(f"转换调试 - '文件名称'列唯一值数量: {df_source['文件名称'].nunique()}")
            # 检查空值情况
            null_count = df_source['文件名称'].isnull().sum()
            empty_count = (df_source['文件名称'].astype(str).str.strip() == '').sum()
            self.log(f"转换调试 - '文件名称'列空值: {null_count}, 空字符串: {empty_count}")
            # 如果唯一值少，显示样本
            if df_source['文件名称'].nunique() <= 5:
                unique_files = df_source['文件名称'].unique()
                self.log(f"转换调试 - '文件名称'样本: {unique_files[:5]}")
        else:
            self.log(f"转换调试 - 警告：源数据中没有'文件名称'列！尝试使用其他列名...")
            # 检查可能的替代列名
            possible_filename_cols = ['文件名', 'file_name', 'filename', '文献名称', '论文题目']
            for col in possible_filename_cols:
                if col in df_source.columns:
                    self.log(f"转换调试 - 使用替代列 '{col}' 作为文件名称")
                    break

        new_rows = []

        for _, row in df_source.iterrows():
            # 使用 batch.py 输出的语种字段，若没有则根据标题判断
            language = row.get('语种', '')
            # 处理可能的NaN值
            if pd.isna(language) or (isinstance(language, str) and language.strip() in ['', 'N/A']):
                title = str(row.get('论文题目', row.get('文献名称', '')))
                author = str(row.get('作者', row.get('作者名称', '')))
                language = "中文" if any('\u4e00' <= c <= '\u9fff' for c in (title + author)) else "英语"
            elif isinstance(language, str):
                language = language.strip()

            # 处理期刊和期卷信息
            # 源文件有'来源（出版社/期刊）'和'期（卷）'列
            journal = row.get('来源（出版社/期刊）', 'N/A')
            issue_volume = row.get('期（卷）', 'N/A')

            # 处理可能的NaN值
            if pd.isna(journal) or (isinstance(journal, str) and journal.strip() in ['', 'N/A']):
                journal = ''
            elif isinstance(journal, str):
                journal = journal.strip()

            if pd.isna(issue_volume) or (isinstance(issue_volume, str) and issue_volume.strip() in ['', 'N/A']):
                issue_volume = ''
            elif isinstance(issue_volume, str):
                issue_volume = issue_volume.strip()

            # 构建新行，确保包含所有目标列
            new_row = {}

            # 设置已知的映射字段
            # 首先获取文件名，优先使用'文件名称'列，兼容'文件名'列
            filename = row.get('文件名称', row.get('文件名', ''))
            if pd.isna(filename) or (isinstance(filename, str) and filename.strip() == ''):
                # 生成基于文献名称和作者的唯一文件名
                title = str(row.get('论文题目', row.get('文献名称', '未知标题'))).strip()
                author = str(row.get('作者', row.get('作者名称', '未知作者'))).strip()
                # 取前50个字符，避免过长
                title_short = title[:50] if len(title) > 50 else title
                author_short = author[:30] if len(author) > 30 else author

                # 确保文件名不为空
                if title_short or author_short:
                    if title_short and author_short:
                        filename = f"{title_short}_{author_short}"
                    elif title_short:
                        filename = title_short
                    else:
                        filename = author_short
                else:
                    filename = f"文献_{_}"  # 使用行索引作为后备

            column_mapping = {
                "文件名称": filename,
                "论文题目": row.get('论文题目', row.get('文献名称', '')),
                "语种": language,
                "作者": row.get('作者', row.get('作者名称', '')),
                "年份": row.get('年份', row.get('出版年份', '')),
                "来源（出版社/期刊）": journal,
                "期（卷）": issue_volume,
                "类型（专著/论文）": "论文",
                "方向": direction,
                "是否译文": "否",
                "处理人": processor,
            }

            # 创建大小写不敏感的column_mapping（键转为小写）
            column_mapping_lower = {k.lower(): v for k, v in column_mapping.items()}

            # 将映射字段添加到新行
            for target_col in target_columns:
                target_col_lower = target_col.lower()
                if target_col_lower in column_mapping_lower:
                    new_row[target_col] = column_mapping_lower[target_col_lower]
                else:
                    # 对于目标表格中但映射中没有的列，留空
                    new_row[target_col] = ''

            new_rows.append(new_row)

        df_new = pd.DataFrame(new_rows, columns=target_columns)
        return df_new

    def aggregate_duplicates(self, df: pd.DataFrame, key_column: str = "文件名称") -> pd.DataFrame:
        """聚合重复的行，基于指定的键列
        Args:
            df: 输入DataFrame
            key_column: 用于识别重复的键列
        Returns:
            聚合后的DataFrame
        """
        if key_column not in df.columns:
            return df

        # 调试日志：检查输入数据
        self.log(f"聚合调试 - 输入数据形状: {df.shape}")
        self.log(f"聚合调试 - '{key_column}'列唯一值数量: {df[key_column].nunique()}")
        if df[key_column].nunique() < 10:  # 如果唯一值较少，显示具体值
            unique_values = df[key_column].unique()
            self.log(f"聚合调试 - '{key_column}'列唯一值: {unique_values}")

        # 检查空值或缺失值
        null_count = df[key_column].isnull().sum()
        empty_count = (df[key_column].astype(str).str.strip() == '').sum()
        self.log(f"聚合调试 - '{key_column}'列空值数量: {null_count}, 空字符串数量: {empty_count}")

        # 分组并聚合
        def aggregate_group(group):
            # 创建一个字典来存储聚合结果
            aggregated = {}
            for col in group.columns:
                if col == key_column:
                    # 键列使用第一个值
                    aggregated[col] = group[col].iloc[0]
                else:
                    # 收集所有非空值
                    non_empty = group[col].dropna()
                    if len(non_empty) == 0:
                        aggregated[col] = ''
                    elif len(non_empty) == 1:
                        aggregated[col] = non_empty.iloc[0]
                    else:
                        # 多个不同的非空值，用分号分隔
                        # 去重并过滤空字符串
                        unique_values = non_empty.astype(str).str.strip().unique()
                        unique_values = [v for v in unique_values if v and v != '' and v.lower() != 'nan']
                        if len(unique_values) == 0:
                            aggregated[col] = ''
                        elif len(unique_values) == 1:
                            aggregated[col] = unique_values[0]
                        else:
                            # 用分号分隔，按出现顺序
                            aggregated[col] = '; '.join(str(v) for v in unique_values if str(v).strip())
            return pd.Series(aggregated)

        # 按键列分组并聚合
        self.log(f"聚合调试 - 开始分组，键列: '{key_column}'")

        # 方法1: 使用groupby + apply
        # 首先检查分组情况
        group_sizes = df[key_column].value_counts()
        self.log(f"聚合调试 - 分组数量: {len(group_sizes)}")
        self.log(f"聚合调试 - 总记录数: {len(df)}")

        # 如果分组数量少，显示每个分组的大小
        if len(group_sizes) <= 10:
            for name, size in group_sizes.items():
                self.log(f"聚合调试 - 分组 '{name}': {size} 行")
        else:
            # 显示前10个分组
            self.log(f"聚合调试 - 前10个分组:")
            for name, size in group_sizes.head(10).items():
                self.log(f"聚合调试 - 分组 '{name}': {size} 行")

        # 检查是否有空值或重复的分组键
        unique_values = df[key_column].unique()
        self.log(f"聚合调试 - 唯一值样本 (前5个): {unique_values[:5] if len(unique_values) > 0 else '空'}")

        # 简单的聚合方法：删除完全重复的行（基于所有列）
        before_dedup = len(df)
        df_dedup = df.drop_duplicates(subset=[key_column], keep='first')
        after_dedup = len(df_dedup)

        self.log(f"聚合调试 - 去重前: {before_dedup} 条记录")
        self.log(f"聚合调试 - 去重后: {after_dedup} 条记录")
        self.log(f"聚合调试 - 移除了 {before_dedup - after_dedup} 条重复记录")

        # 如果去重后记录数不变，尝试显示原因
        if before_dedup == after_dedup:
            self.log(f"聚合调试 - 没有重复记录，所有'{key_column}'值都不同")
        else:
            self.log(f"聚合调试 - 有重复记录，重复的'{key_column}'值被合并")

        # 重置索引
        df_dedup.reset_index(drop=True, inplace=True)

        self.log(f"聚合调试 - 最终数据形状: {df_dedup.shape}")
        self.log(f"聚合调试 - 最终'{key_column}'列唯一值数量: {df_dedup[key_column].nunique()}")

        return df_dedup

    def start_merge(self):
        """开始合并任务"""
        self.is_running = True
        self.stop_requested = False
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        threading.Thread(target=self.merge_logic, daemon=True).start()

    def stop_merge(self):
        """停止合并任务"""
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
    app = CSVMergeToolGUI()
    app.mainloop()