#!/usr/bin/env python3
"""
AB-pdf Skill - PDF 文件处理

此脚本是 AB-pdf Skill 的统一入口，支持多种 PDF 处理操作。

Usage:
    python main.py <input_file> [--action <action>] [--output <output_file>] [--data <json_data>]

Actions:
    extract_text   - 提取 PDF 文本内容
    extract_forms  - 提取表单字段信息
    fill_form      - 填充表单字段（需要 --data 参数）
    convert_images - 转换为图片

Example:
    python main.py ./input/document.pdf --action extract_text
    python main.py ./input/form.pdf --action fill_form --data '{"name": "张三", "email": "test@example.com"}'
    python main.py ./input/document.pdf --action convert_images --output ./output/
"""

import argparse
import json
import sys
import os
from pathlib import Path
from typing import Dict, Any, List, Optional

# 尝试导入 PDF 处理库
try:
    from pypdf import PdfReader, PdfWriter
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

try:
    import pypdfium2 as pdfium
    HAS_PDFIUM = True
except ImportError:
    HAS_PDFIUM = False

try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False


def main():
    parser = argparse.ArgumentParser(
        description='AB-pdf: PDF 文件处理技能',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py ./input/document.pdf --action extract_text
  python main.py ./input/form.pdf --action extract_forms
  python main.py ./input/form.pdf --action fill_form --data '{"name": "张三"}'
  python main.py ./input/document.pdf --action convert_images --output ./output/
        """
    )

    parser.add_argument('input_file', help='输入 PDF 文件路径')
    parser.add_argument('--action',
                        choices=['extract_text', 'extract_forms', 'fill_form', 'convert_images'],
                        default='extract_text',
                        help='执行的操作类型 (default: extract_text)')
    parser.add_argument('--output', help='输出文件或目录路径')
    parser.add_argument('--data', help='JSON 格式的数据（用于表单填充）')
    parser.add_argument('--limit', type=int, default=0,
                        help='限制输出字符数（0表示不限制，用于extract_text）')
    parser.add_argument('--verbose', '-v', action='store_true', help='显示详细输出')

    args = parser.parse_args()

    # 验证输入文件
    input_path = Path(args.input_file)
    if not input_path.exists():
        print(json.dumps({
            "status": "error",
            "error": f"输入文件不存在: {args.input_file}",
            "action": args.action
        }, ensure_ascii=False))
        sys.exit(1)

    # 验证文件类型
    if input_path.suffix.lower() != '.pdf':
        print(json.dumps({
            "status": "error",
            "error": f"输入文件不是 PDF 格式: {input_path.suffix}",
            "action": args.action
        }, ensure_ascii=False))
        sys.exit(1)

    try:
        result = process_pdf(args)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result.get("status") == "success" else 1)
    except Exception:
        print(json.dumps({
            "status": "error",
            "error": "PDF 处理失败",
            "action": args.action
        }, ensure_ascii=False))
        sys.exit(1)


def process_pdf(args) -> Dict[str, Any]:
    """根据 action 处理 PDF"""
    action = args.action

    # 获取 limit 参数（默认 0 表示不限制）
    limit = getattr(args, 'limit', 0)

    if action == 'extract_text':
        return extract_text(args.input_file, args.verbose, limit)
    elif action == 'extract_forms':
        return extract_forms(args.input_file, args.verbose)
    elif action == 'fill_form':
        if not args.data:
            return {
                "status": "error",
                "error": "fill_form 操作需要 --data 参数",
                "action": action
            }
        try:
            data = json.loads(args.data)
        except json.JSONDecodeError as e:
            return {
                "status": "error",
                "error": f"无效的 JSON 数据: {e}",
                "action": action
            }
        return fill_form(args.input_file, data, args.output, args.verbose)
    elif action == 'convert_images':
        return convert_images(args.input_file, args.output, args.verbose)
    else:
        return {
            "status": "error",
            "error": f"未知操作: {action}",
            "action": action
        }


def extract_text(input_file: str, verbose: bool = False, limit: int = 0) -> Dict[str, Any]:
    """
    提取 PDF 文本内容

    Args:
        input_file: PDF 文件路径
        verbose: 是否显示详细输出
        limit: 限制输出字符数（0表示不限制）

    Returns:
        包含提取文本的字典
    """
    if not HAS_PDFPLUMBER and not HAS_PYPDF:
        return {
            "status": "error",
            "error": "缺少必要的库：请安装 pdfplumber 或 pypdf",
            "action": "extract_text"
        }

    try:
        all_text = []
        page_count = 0

        # 优先使用 pdfplumber（更好的文本提取效果）
        if HAS_PDFPLUMBER:
            if verbose:
                print(f"[INFO] 使用 pdfplumber 提取文本", file=sys.stderr)

            with pdfplumber.open(input_file) as pdf:
                page_count = len(pdf.pages)
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text()
                    if text:
                        all_text.append(f"=== 第 {i+1} 页 ===\n{text}")
        else:
            # 回退到 pypdf
            if verbose:
                print(f"[INFO] 使用 pypdf 提取文本", file=sys.stderr)

            reader = PdfReader(input_file)
            page_count = len(reader.pages)
            for i, page in enumerate(reader.pages):
                text = page.extract_text()
                if text:
                    all_text.append(f"=== 第 {i+1} 页 ===\n{text}")

        combined_text = "\n\n".join(all_text)

        # 应用字符限制
        output_text = combined_text
        truncated = False
        if limit > 0 and len(combined_text) > limit:
            output_text = combined_text[:limit]
            truncated = True

        return {
            "status": "success",
            "action": "extract_text",
            "output": output_text,
            "pages": page_count,
            "char_count": len(output_text),
            "truncated": truncated
        }

    except Exception:
        return {
            "status": "error",
            "error": "提取文本失败",
            "action": "extract_text"
        }


def extract_forms(input_file: str, verbose: bool = False) -> Dict[str, Any]:
    """
    提取 PDF 表单字段信息

    Args:
        input_file: PDF 文件路径
        verbose: 是否显示详细输出

    Returns:
        包含表单字段信息的字典
    """
    if not HAS_PYPDF:
        return {
            "status": "error",
            "error": "缺少必要的库：请安装 pypdf",
            "action": "extract_forms"
        }

    try:
        reader = PdfReader(input_file)
        fields = {}

        # 获取表单字段
        if reader.get_fields():
            for field_name, field_data in reader.get_fields().items():
                field_info = {
                    "name": field_name,
                    "type": field_data.get("/FT", "unknown"),
                    "value": field_data.get("/V", ""),
                    "default_value": field_data.get("/DV", ""),
                    "flags": str(field_data.get("/Ff", ""))
                }
                fields[field_name] = field_info

        if verbose:
            print(f"[INFO] 发现 {len(fields)} 个表单字段", file=sys.stderr)

        return {
            "status": "success",
            "action": "extract_forms",
            "output": {
                "field_count": len(fields),
                "fields": fields
            },
            "has_forms": len(fields) > 0
        }

    except Exception:
        return {
            "status": "error",
            "error": "提取表单失败",
            "action": "extract_forms"
        }


def fill_form(input_file: str, data: Dict[str, Any], output_file: Optional[str] = None, verbose: bool = False) -> Dict[str, Any]:
    """
    填充 PDF 表单字段

    Args:
        input_file: PDF 文件路径
        data: 要填充的数据字典
        output_file: 输出文件路径
        verbose: 是否显示详细输出

    Returns:
        包含填充结果的字典
    """
    if not HAS_PYPDF:
        return {
            "status": "error",
            "error": "缺少必要的库：请安装 pypdf",
            "action": "fill_form"
        }

    try:
        reader = PdfReader(input_file)
        writer = PdfWriter()

        # 复制所有页面
        for page in reader.pages:
            writer.add_page(page)

        # 克隆读者中的表单字段
        writer.clone_reader_document_root(reader)

        # 填充表单字段
        filled_count = 0
        for field_name, value in data.items():
            try:
                writer.update_page_form_field_values(
                    writer.pages[0],
                    {field_name: str(value)}
                )
                filled_count += 1
                if verbose:
                    print(f"[INFO] 填充字段 '{field_name}': {value}", file=sys.stderr)
            except Exception as e:
                if verbose:
                    print(
                        f"[WARN] 填充字段失败: error_type={type(e).__name__}",
                        file=sys.stderr,
                    )

        # 确定输出路径
        if not output_file:
            input_path = Path(input_file)
            output_file = str(input_path.parent / f"{input_path.stem}_filled.pdf")

        # 保存文件
        with open(output_file, 'wb') as f:
            writer.write(f)

        return {
            "status": "success",
            "action": "fill_form",
            "output": {
                "filled_fields": filled_count,
                "output_file": output_file
            },
            "files": [output_file]
        }

    except Exception:
        return {
            "status": "error",
            "error": "填充表单失败",
            "action": "fill_form"
        }


def convert_images(input_file: str, output_dir: Optional[str] = None, verbose: bool = False) -> Dict[str, Any]:
    """
    将 PDF 转换为图片

    Args:
        input_file: PDF 文件路径
        output_dir: 输出目录路径
        verbose: 是否显示详细输出

    Returns:
        包含转换结果的字典
    """
    if not HAS_PDFIUM:
        return {
            "status": "error",
            "error": "缺少必要的库：请安装 pypdfium2 和 Pillow",
            "action": "convert_images"
        }

    try:
        # 确定输出目录
        if not output_dir:
            input_path = Path(input_file)
            output_dir = str(input_path.parent / "images")

        # 创建输出目录
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        if verbose:
            print(f"[INFO] 将 PDF 转换为图片，输出目录: {output_dir}", file=sys.stderr)

        document = pdfium.PdfDocument(input_file)
        if len(document) > 500:
            document.close()
            raise ValueError("PDF页数超过限制（最多 500 页）")
        output_files = []
        for i, page in enumerate(document):
            image = page.render(scale=200 / 72).to_pil()
            output_file = str(output_path / f"page_{i+1:03d}.png")
            image.save(output_file, 'PNG')
            output_files.append(output_file)
            if verbose:
                print(f"[INFO] 保存图片: {output_file}", file=sys.stderr)
        document.close()

        return {
            "status": "success",
            "action": "convert_images",
            "output": {
                "page_count": len(output_files),
                "output_dir": output_dir
            },
            "files": output_files
        }

    except Exception:
        return {
            "status": "error",
            "error": "转换为图片失败",
            "action": "convert_images"
        }


if __name__ == "__main__":
    main()
