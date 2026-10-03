"""把外部法规数据整理成导入流水线能吃的格式。

用法：
    python scripts/prepare_regulation_data.py --bundle <regulation_bundle.json> --out <目录>

为什么需要这个脚本：
  外部数据（这里来自旧项目导出的 regulation_bundle.json）有三个问题，
  直接导入会污染知识库，必须先清洗：
    1. 混入海外法规。同一文件里有 225 份，185 份是欧盟/美国/东南亚/中东法规，
       中国财税系统用不上，导进去会让"问中国增值税"召回德国 VAT。
    2. 网页噪声。税务总局法规库页面正文里混着「字体：大小中」「分享到：收藏」
       「扫一扫在手机打开当前页」这类页面元素，会被当成条文切进库里。
    3. 重复条目。同一部法规可能有「（2014年修订）」和「（2014年修订）」两种标点写法。

清洗规则都写在这里而不是 import 命令里，是为了让"哪条数据被丢弃、为什么"可追溯。
"""

from __future__ import annotations
import argparse
import json
import pathlib
import re
import sys


# ---------- 网页噪声清洗 ----------

# 税务总局法规库页面元素。这些出现在正文里但不是法规内容，必须剔除。
NOISE_PATTERNS = (
    r"^字体：?$",
    r"^【大】$",
    r"^【中】$",
    r"^【小】$",
    r"^分享到：?$",
    r"^收藏$",
    r"^订阅$",
    r"^打印$",
    r"^关闭$",
    r"^已推送.*$",
    r"^扫一扫在手机打开当前页.*$",
    r"^语音播报：?$",
    r"^进入.*查看$",
    r"^全选有效$",
    r"^发文机关：?$",
    r"^成文日期：?$",
    r"^发布日期：?$",
    r"^有效性：?$",
    r"^全文有效$",
    r"^\($",
    r"^\)$",
    r"^注释$",
    r"^\(共\s*\d+\s*条\)$",
    r"^\d+$",  # 孤立数字行
)

# 只删真正的方括号标签。绝不能用"短行"当噪声：
# 法规正文里 "第一条"、"（一）销售货物收入;" 都是短行，删掉会导致整部法规没有条号。
TAG_NOISE_RE = re.compile(r"^[【\[［][^】\]］]{0,20}[】\]］]$")

# 页面元素与法条混排时，用它切掉"标题行到正文开始"之间的东西
HEADER_NOISE_BLOCK = re.compile(
    r"(字体[:：]?|【大】|【中】|【小】|分享到[:：]?|收藏|已推送|扫一扫在手机打开当前页|语音播报|"
    r"打印|关闭|订阅|发文机关[:：]?|成文日期[:：]?|发布日期[:：]?|全文有效|"
    r"有效性[:：]?|进入\s*中\s*查看)"
)


def clean_web_noise(text: str) -> str:
    """剔除网页噪声，保留法规正文。"""

    lines = text.splitlines()
    cleaned: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            cleaned.append("")
            continue
        # 整行都是噪声
        if TAG_NOISE_RE.match(line) or any(re.match(pattern, line) for pattern in NOISE_PATTERNS):
            continue
        # 行内含噪声片段：切掉噪声片段，保留其余
        if HEADER_NOISE_BLOCK.search(line):
            # 只保留第一个噪声片段之前的内容
            m = HEADER_NOISE_BLOCK.search(line)
            before = line[: m.start()].strip()
            if before:
                cleaned.append(before)
            continue
        cleaned.append(line)

    result = "\n".join(cleaned)
    # 压缩连续空行
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def normalize_title(title: str) -> str:
    """标题归一化：用于去重比较。全角半角标点统一。"""

    return re.sub(r"[\s\(\)（）—\-－_、，,]+", "", title or "")


def is_truncated(content: str) -> bool:
    """判断内容是否被截断。被截断的标出来，让人知道这份法规不完整。"""

    return "（以下省略" in content or content.rstrip().endswith("...")


def pick_best_duplicate(duplicates: list[dict]) -> dict:
    """同一法规的多份副本里，选内容最长的那份。"""

    return max(duplicates, key=lambda d: len(d.get("content", "")))


def main() -> int:
    parser = argparse.ArgumentParser(description="整理外部法规数据")
    parser.add_argument("--bundle", required=True, help="regulation_bundle.json 路径")
    parser.add_argument("--out", required=True, help="输出的 .txt 目录")
    parser.add_argument("--jurisdiction", default="CN", help="只保留该辖区的法规")
    args = parser.parse_args()

    bundle_path = pathlib.Path(args.bundle).expanduser().resolve()
    out_dir = pathlib.Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    documents = data.get("documents", [])

    # 1. 只保留目标辖区
    in_scope = [d for d in documents if d.get("jurisdiction") == args.jurisdiction]
    print(f"总文档 {len(documents)}，{args.jurisdiction} 辖区 {len(in_scope)}")

    # 2. 去重（按归一化标题）
    groups: dict[str, list[dict]] = {}
    for document in in_scope:
        key = normalize_title(document.get("title", ""))
        groups.setdefault(key, []).append(document)

    # 3. 清洗噪声并写出
    written = 0
    skipped_empty = 0
    truncated_count = 0
    missing_source = 0

    for group in groups.values():
        best = pick_best_duplicate(group)
        content = clean_web_noise(best.get("content", ""))
        if not content:
            skipped_empty += 1
            continue

        title = best.get("title", "").strip()
        # 文件名用序号 + 标题，保证排序稳定且可读
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)[:60]
        filename = f"{written + 1:03d}_{safe_title}.txt"

        # 头部保留元数据行，导入脚本的解析器会从正文首行读文号
        header_lines = []
        if best.get("document_number"):
            header_lines.append(str(best["document_number"]))
        header_lines.append(title)

        body = "\n".join(header_lines) + "\n\n" + content
        (out_dir / filename).write_text(body, encoding="utf-8")
        written += 1

        # 元数据单独存一份 sidecar，导入脚本按同名 .meta.json 读取来源与日期。
        # 为什么单独存：法规正文里塞来源 URL 会污染条文切分（URL 会被当成正文一行），
        # 而合规又要求来源可追溯，所以正文与元数据分开，导入时再合并。
        sidecar = {
            "filename": filename,
            "title": title,
            "document_number": best.get("document_number"),
            "source": best.get("source"),
            "source_url": best.get("source_url"),
            "effective_date": best.get("effective_date"),
            "expiry_date": best.get("expiry_date"),
            "collection": best.get("collection"),
            "content_length": len(content),
            "truncated": is_truncated(content),
        }
        (out_dir / f"{pathlib.Path(filename).stem}.meta.json").write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        if is_truncated(content):
            truncated_count += 1
        if not best.get("source_url"):
            missing_source += 1

    print(f"去重后 {len(groups)} 组，写出 {written} 份")
    print(f"  内容为空被丢弃：{skipped_empty}")
    print(f"  内容被截断（需补全）：{truncated_count}")
    print(f"  缺来源 URL：{missing_source}")
    print(f"输出目录：{out_dir}")

    # 4. 输出一份清单，方便人工核对哪些进了、哪些没进
    manifest = [path.name for path in sorted(out_dir.glob("*.txt"))]
    (out_dir / "_清单.txt").write_text("\n".join(manifest), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
