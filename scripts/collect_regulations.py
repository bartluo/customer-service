"""从国家税务总局政策法规库采集法规原文（第二条采集路径）。

用法（项目根目录）：
    python scripts/collect_regulations.py --target 100
    python scripts/collect_regulations.py --channels law,rule --limit 20

为什么要有这个脚本：
  的文件投放采集器（InboxCollector）需要人工下载后再导入。
  数量上不去（G3 要求 100 份），所以这里对接官方公开接口自动采集。

数据来源与合法性说明：
  · 站点：国家税务总局政策法规库 https://fgk.chinatax.gov.cn/
  · 接口：站点自身前端使用的公开接口（返回 JSON），非破解、非绕过登录。
  · 内容：国家公开发布的法律法规与税务公告，属公开政务信息。
  · 礼貌抓取：请求之间固定间隔、失败重试、只取元数据与正文，不并发轰炸。

输出：
  <out>/NNN_标题.txt          正文（首行文号 + 第二行标题 + 空行 + 条文正文）
  <out>/NNN_标题.meta.json    元数据（来源 URL、文号、发布日期，供追溯）

为什么首行要写文号：导入流水线的解析器从正文首行读文号，
文号缺失的法规会被标为待复核，不会进入检索结果。
"""

from __future__ import annotations

import argparse
import html
import io
import json
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent

LIST_API = "https://www.chinatax.gov.cn/getFileListByCodeId"
DETAIL_HOST = "https://fgk.chinatax.gov.cn"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# 官方栏目 → channelId（取自各栏目页面内联的 channelId 变量）
CHANNELS: dict[str, dict[str, str]] = {
    "law": {
        "name": "法律",
        "channel_id": "d34fa7ad03f84f4caed12f5c2beae099",
        "collection": "cn_tax_admin",
    },
    "admin": {
        "name": "行政法规",
        "channel_id": "e1cd1569d1ea4a25a11041248925a081",
        "collection": "cn_tax_admin",
    },
    "rule": {
        "name": "税务规范性文件",
        "channel_id": "470b437b304f434396500a1e2edc7f28",
        "collection": "cn_tax_admin",
    },
    "finance": {
        "name": "财税文件",
        "channel_id": "2cb303fdee614232b79552d52bb057d6",
        "collection": "cn_finance_doc",
    },
    "statecouncil": {
        "name": "国务院文件",
        "channel_id": "fa1726b47078490fa0a4522194185e8d",
        "collection": "cn_tax_admin",
    },
    # 其他文件栏目里有不少跨部门联合发文（社保费、残保金这类由人社部/财政部/总局联合发的），
    # 只在"税务规范性文件"里翻会漏掉它们。
    "other": {
        "name": "其他文件",
        "channel_id": "4c1a5be62f6d44d48f386f630dcebbc5",
        "collection": "cn_tax_admin",
    },
}

# 非财税栏目里只保留标题含这些词的条目（法律/行政法规栏目混着矿产资源法一类）
TAX_TITLE_KEYWORDS = (
    "税", "发票", "会计", "征管", "征收", "财政", "预算", "审计",
    "海关", "进出口", "关税", "企业所得", "个人所得",
)

TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)

# 正文短到这个程度基本可以判定"网页上没内容"（只剩页脚）。
MIN_BODY_CHARS = 80

# 判断网页正文是不是"只是发布通知、真正内容在附件里"。
# 为什么要单独判：不少总局公告本身很短但有完整条款
# （"对增值税小规模纳税人免征增值税的公告"只有 233 字，却是一条真规则），
# 一律按字数砍会把它们误杀；而"现予公布《XX目录》，附件：xxx.xls"这类
# 进库只是噪声。
_CLAUSE_RE = re.compile(
    r"(第[一二三四五六七八九十百]+条)|([一二三四五六七八九十]+、)|(（[一二三四五六七八九十]+）)"
)
_ATTACHMENT_HINT_RE = re.compile(r"(附件[:：]|详见附件|予以发布|予以公布|现予发布|现予公布)")


def looks_like_attachment_only(body: str) -> bool:
    """正文只是发布通知、内容在附件里 → 不入库。"""

    if len(body) > 400:
        return False
    return bool(_ATTACHMENT_HINT_RE.search(body)) and not _CLAUSE_RE.search(body)


# --------------------------------------------------------------------------
# 附件解析：不少"管理办法""总体方案"的正文不在网页上，而在附件里
# --------------------------------------------------------------------------

# 详情页里的附件链接形如 href="5194570/files/XX办法.docx"（相对路径）
_ATTACHMENT_LINK_RE = re.compile(
    r'href="([^"]+\.(?:docx?|pdf|wps))"', re.IGNORECASE
)

# 单个附件最大下载体积。超过就放弃——正常法规附件不会这么大，
# 过大的多半是整册扫描件或数据集，解析出来也不是条文。
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def find_attachment_urls(page_html: str, page_url: str) -> list[str]:
    """从详情页里取出附件下载地址（相对路径拼成绝对地址）。"""

    urls: list[str] = []
    for match in _ATTACHMENT_LINK_RE.finditer(page_html):
        href = match.group(1).strip()
        if not href or href.lower().startswith("javascript:"):
            continue
        absolute = href if href.lower().startswith("http") else urllib.parse.urljoin(page_url, href)
        if absolute not in urls:
            urls.append(absolute)
    return urls


def extract_docx_text(data: bytes) -> str:
    """从 .docx 抽正文。

    为什么用标准库而不是 python-docx：docx 本身就是一个 zip 包，
    正文在 word/document.xml 里，解压后去掉标签即可。
    这样解析附件不需要给项目新增任何依赖，部署到别的机器也不用装东西。
    """

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")

    # 段落结束 → 换行；制表符保留；其余标签去掉
    xml = re.sub(r"</w:p\s*>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_pdf_text(data: bytes) -> str:
    """从 .pdf 抽正文。

    PDF 解析需要第三方库（pypdf / pdfplumber），不是标准库能做的。
    这里做成"有就用、没有就跳过"：不装库时返回空串，
    由调用方把它记进跳过清单，而不是让整批采集崩掉。
    """

    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore[import-not-found]
        except ImportError:
            return ""

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(part.strip() for part in pages if part.strip())


# Word 97-2003 的"第X条/第X章/一、"这类结构行很短，会被长度阈值滤掉，
# 单独用这个模式把它们捞回来——丢了它们整份办法就切不出条文。
_STRUCTURAL_MARKER_RE = re.compile(
    r"^第[一二三四五六七八九十百零〇\d]+[条章节款项]$"
    r"|^[一二三四五六七八九十]+、$"
    r"|^（[一二三四五六七八九十\d]+）$"
)

# 正文片段：中英文、数字、常见中英标点
_TEXT_RUN_RE = re.compile(
    r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffefA-Za-z0-9，。；：（）《》、%\.\-—/]{2,}"
)


def _is_binary_noise(piece: str) -> bool:
    """判断片段是不是二进制头部被误读出来的伪文字。

    OLE 容器的头部按 UTF-16 解出来常是"同几个字反复出现"（如"橢橢糱糱"），
    真正的条文不会长这样。
    """

    if len(piece) < 2:
        return True
    return len(piece) <= 6 and len(set(piece)) <= 2


# "第一章湯"：结构标记后面直接粘了个乱码字符。正常写法是"第一章 总则"（有空格）。
_GLUED_MARKER_RE = re.compile(r"^第[一二三四五六七八九十百零〇\d]+[条章节款项][^\s（(]")


def _looks_like_byte_swapped_ascii(piece: str) -> bool:
    """判断片段是不是 ASCII 字符串按 UTF-16 错位解出来的伪中文。

    Word 文档里嵌的 XML（如 "settings.xml"）被按 UTF-16LE 解码后，
    会变成"湯整瑮呟灹獥"这种看着像中文的东西混进正文。
    判定方法：把每个字符还原成两个字节并交换前后顺序——
    如果还原出来大部分是可打印 ASCII，说明原文本来就是英文字符串，
    这个片段不是正文。

    判据必须是"**全部**可打印"，不能放宽到 80%：
    "第一条"这种短标记错位后是 ",{\\x00Nag"（含不可打印字节），
    用 80% 阈值会被误杀，整份办法就切不出条文了。
    错位的 XML 字符串则全是可打印 ASCII，能被准确识别。
    """

    if len(piece) < 4:
        return False
    swapped = bytearray()
    for char in piece:
        code = ord(char)
        if code > 0xFFFF:
            return False
        swapped.append(code & 0xFF)
        swapped.append((code >> 8) & 0xFF)
    return all(32 <= byte < 127 for byte in swapped)


def extract_legacy_doc_text(data: bytes) -> str:
    """从 Word 97-2003 二进制 .doc 里抽正文。

    为什么不用第三方库：Python 生态里没有轻量的 .doc 解析器
    （python-docx 只支持 docx，textract 要拖一堆重依赖）。
    而政府网站上的老文件恰恰大量是这种格式——更要命的是它们常被
    命名成 .docx，光看扩展名会当成新版文档，解析时报
    "没有 word/document.xml" 直接失败。

    做法：这类文件的正文以 UTF-16LE 存在，按 UTF-16LE 解整份文件、
    扫出连续的可读片段即可。粗暴但有效——实测《残疾人就业保障金
    征收使用管理办法》能完整抽出 4000 余字，条款结构（第X条、
    第X章）都在。
    """

    decoded = data.decode("utf-16-le", errors="ignore")
    lines: list[str] = []
    for chunk in _TEXT_RUN_RE.findall(decoded):
        piece = chunk.strip()
        if not piece or _is_binary_noise(piece):
            continue
        if _GLUED_MARKER_RE.match(piece) or _looks_like_byte_swapped_ascii(piece):
            continue
        # 长片段直接收；短片段只收结构标记（第X条、一、）
        if len(piece) >= 4 or _STRUCTURAL_MARKER_RE.match(piece):
            lines.append(piece)
    return "\n".join(lines)


# 文件头标识：靠内容判断格式，不能靠扩展名——官网把 .doc 写成 .docx 是常事
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def extract_attachment(data: bytes, url: str) -> tuple[str, str]:
    """按文件实际格式解析附件。返回 (正文, 格式说明)。"""

    if data.startswith(b"%PDF"):
        return extract_pdf_text(data), "pdf"

    if data.startswith(_OLE_MAGIC):
        # 老式 OLE 容器：可能是 .doc、.xls、.wps
        text = extract_legacy_doc_text(data)
        # 表格类（.xls）抽出来的多是零散数字，用"有没有条文特征"筛一下
        if text and (len(text) >= 200 or "条" in text or "。" in text):
            return text, "doc(97-2003)"
        return "", "ole-未识别"

    if data.startswith(_ZIP_MAGIC):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile:
            return "", "zip-损坏"
        if "word/document.xml" in names:
            return extract_docx_text(data), "docx"
        if any(name.startswith("xl/") for name in names):
            # Excel 附件（口径表、税率表）结构化程度低，先不硬抽
            return "", "xlsx-暂不支持"
        return "", "zip-未知"

    return "", "未知格式"


def fetch_attachment_text(page_html: str, page_url: str, sleep: float = 0.4) -> str:
    """下载并解析页面上的附件，返回拼好的正文。取不到返回空串。"""

    urls = find_attachment_urls(page_html, page_url)
    if not urls:
        return ""

    parts: list[str] = []
    formats: list[str] = []
    for url in urls:
        lowered = url.lower()
        try:
            data = _fetch_bytes(url)
        except Exception:  # noqa: BLE001 - 附件拿不到不影响其它文件
            continue
        if not data or len(data) > MAX_ATTACHMENT_BYTES:
            continue
        try:
            text, fmt = extract_attachment(data, lowered)
        except Exception:  # noqa: BLE001 - 单个附件解析失败不拖垮整批
            text, fmt = "", "解析异常"
        if text.strip():
            parts.append(text.strip())
            formats.append(fmt)
        else:
            formats.append(fmt or "空")
        time.sleep(sleep)

    if formats:
        print(f"        附件格式：{'、'.join(formats)}")
    return "\n\n".join(parts)


def _fetch(url: str, data: dict | None = None, retries: int = 3) -> str:
    """GET/POST 取回文本，带重试。失败抛最后一次异常。"""

    last: Exception | None = None
    for attempt in range(retries):
        try:
            if data is None:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            else:
                body = urllib.parse.urlencode(data).encode("utf-8")
                request = urllib.request.Request(
                    url,
                    data=body,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                raw = response.read()
            return raw.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - 网络抖动需要重试
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求失败 {url}：{last}")


def _fetch_bytes(url: str, retries: int = 2) -> bytes:
    """取二进制内容（附件）。失败抛最后一次异常。"""

    # 附件地址里常带中文文件名，必须先做百分号编码再请求。
    # urllib 不做这件事，直接传非 ASCII 地址会在编码阶段就抛错。
    safe_url = urllib.parse.quote(url, safe=":/?&=#%+")

    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(safe_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                return response.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"附件下载失败 {url}：{last}")


def fetch_channel(channel_key: str, page: int, size: int = 20) -> dict:
    """取某个栏目的第 page 页列表。"""

    channel = CHANNELS[channel_key]
    text = _fetch(
        LIST_API,
        data={
            "codeId": "",
            "channelId": channel["channel_id"],
            "page": page,
            "size": size,
        },
    )
    payload = json.loads(text)
    return payload.get("results", {}).get("data", {}) or {}


def extract_list_meta(item: dict) -> dict[str, str]:
    """把列表接口返回的元数据集拍平成 {key: value}。

    官方在列表接口里就带了结构化元数据（发文字号、效力等级、时效性、税费类型、
    发文机构、废止日期），比从详情页 HTML 正则抠更可靠，所以优先用这里的值。

    空字段官方返回的是**字符串** "null"（不是 JSON null），
    直接存进去会在写库时炸成 invalid input syntax for type timestamp。
    这里统一清洗成真正的空值。
    """

    blank = {"", "null", "none", "undefined", "nan"}
    meta: dict[str, str] = {}
    for group in item.get("domainMetaList") or []:
        for field in group.get("resultList") or []:
            key = (field.get("key") or "").strip()
            value = (field.get("value") or "").strip()
            if key and value.lower() not in blank:
                meta.setdefault(key, value)
    return meta


def html_to_text(fragment: str) -> str:
    """把正文 HTML 转成带换行的纯文本。"""

    fragment = SCRIPT_RE.sub("", fragment)
    # 段落与换行标签先转成换行，避免所有条文糊成一整行
    fragment = re.sub(r"(?i)</p\s*>|<br\s*/?>|</div\s*>", "\n", fragment)
    fragment = TAG_RE.sub("", fragment)
    fragment = html.unescape(fragment)
    # 全角空格与不间断空格统一成普通空格；去掉零宽字符
    fragment = fragment.replace("\u3000", " ").replace("&nbsp;", " ").replace("\xa0", " ")
    fragment = fragment.replace("\u200b", "").replace("　", " ")
    lines = [line.strip() for line in fragment.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def _pick(pattern: str, page: str, group: int = 1, flags: int = re.DOTALL) -> str:
    match = re.search(pattern, page, flags)
    return html.unescape(TAG_RE.sub("", match.group(group))).strip() if match else ""


def parse_detail(page: str) -> dict:
    """从详情页 HTML 抽出标题、文号、成文日期、施行日期与正文。"""

    title = _pick(r"<h3[^>]*>(.*?)</h3>", page)
    if not title:
        title = _pick(r'<meta name="ArticleTitle" content="([^"]*)"', page)

    # 文号有两种模板：有的页面给 <h5 class="actfwzh">，有的只给裸 <h5>。
    # 都取 arctips 里的第一个 h5——第二个 h5（class="actbtsm"）是空的括号占位。
    document_number = _pick(r'<div class="arctips">\s*<h5[^>]*>(.*?)</h5>', page)
    if not document_number:
        document_number = _pick(r'<h5 class="actfwzh"[^>]*>(.*?)</h5>', page)
    written_date = _pick(r'<span class="date"[^>]*>.*?成文日期[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})', page)

    body_match = re.search(r'<div class="arc_cont">(.*?)</div>\s*</div>', page, re.DOTALL)
    if not body_match:
        body_match = re.search(r'<div class="arc_cont">(.*?)(?:<div class="|</body>)', page, re.DOTALL)
    body = html_to_text(body_match.group(1)) if body_match else ""

    # 注释块常写"自 XXXX 年 X 月 X 日起施行"，是施行日期的最佳线索
    note_match = re.search(r'<div class="zscont">(.*?)</div>', page, re.DOTALL)
    note = html_to_text(note_match.group(1)) if note_match else ""
    effective_date = ""
    date_match = re.search(
        r"自\s*([0-9]{4})\s*年\s*([0-9]{1,2})\s*月\s*([0-9]{1,2})\s*日起施行", note + body
    )
    if date_match:
        effective_date = (
            f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
        )

    return {
        "title": title,
        "document_number": document_number,
        "written_date": written_date,
        "effective_date": effective_date or None,
        "note": note,
        "body": body,
    }


def normalize_title(title: str) -> str:
    """标题归一化，用于去重比较。"""

    return re.sub(r"[\s\(\)（）—\-－_、，,《》]+", "", title or "")


def _read_sidecar(txt_path: pathlib.Path) -> dict:
    sidecar = txt_path.with_name(txt_path.stem + ".meta.json")
    if not sidecar.exists():
        return {}
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def prune_out_dir(out_dir: pathlib.Path) -> tuple[int, int]:
    """让输出目录回到"一份法规一个文件"的状态，并重新连续编号。

    为什么需要它：编号是按写入顺序给的，重刷（--force）时如果某些条目这次
    被跳过，旧的编号就会和新的错开，留下同名不同内容的残留文件。
    残留文件会被重复导入，所以每次重刷后必须收敛一次。

    去重规则：同一标题保留"有文号优先、其次正文更长"的那一份。
    """

    entries: dict[str, list[pathlib.Path]] = {}
    for txt_path in sorted(out_dir.glob("*.txt")):
        if txt_path.name.startswith("_"):
            continue
        title = _read_sidecar(txt_path).get("title") or txt_path.stem.split("_", 1)[-1]
        entries.setdefault(normalize_title(title), []).append(txt_path)

    removed = 0
    keepers: list[pathlib.Path] = []
    for paths in entries.values():
        if len(paths) > 1:
            def score(path: pathlib.Path) -> tuple[int, int]:
                meta = _read_sidecar(path)
                return (1 if meta.get("document_number") else 0, path.stat().st_size)

            paths = sorted(paths, key=score, reverse=True)
            for stale in paths[1:]:
                stale.unlink(missing_ok=True)
                stale.with_name(stale.stem + ".meta.json").unlink(missing_ok=True)
                removed += 1
        keepers.append(paths[0])

    # 两段式改名：先挪到临时前缀，再落到最终编号，避免互相覆盖
    keepers.sort(key=lambda p: p.name)
    for index, path in enumerate(keepers, start=1):
        target_stem = f"{index:03d}_{path.stem.split('_', 1)[-1]}"
        if path.stem != target_stem:
            path.rename(path.with_name(f"__tmp_{target_stem}.txt"))
            sidecar = path.with_name(path.stem + ".meta.json")
            if sidecar.exists():
                sidecar.rename(sidecar.with_name(f"__tmp_{target_stem}.meta.json"))

    for path in sorted(out_dir.glob("__tmp_*.txt")):
        path.rename(path.with_name(path.name.replace("__tmp_", "", 1)))
    for path in sorted(out_dir.glob("__tmp_*.meta.json")):
        path.rename(path.with_name(path.name.replace("__tmp_", "", 1)))

    # 文件名变了，sidecar 里的 filename 字段要跟着更新
    for txt_path in sorted(out_dir.glob("*.txt")):
        if txt_path.name.startswith("_"):
            continue
        sidecar_path = txt_path.with_name(txt_path.stem + ".meta.json")
        meta = _read_sidecar(txt_path)
        if meta and meta.get("filename") != txt_path.name:
            meta["filename"] = txt_path.name
            sidecar_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    return removed, len(keepers)


def load_known_titles() -> set[str]:
    """已入库的法规标题（避免重复采集）。"""

    try:
        sys.path.insert(0, str(REPO / "backend"))
        sys.path.insert(0, str(REPO / "scripts"))
        from build_vector_index import _load_env  # noqa: PLC0415

        _load_env()
        from sqlalchemy import select  # noqa: PLC0415

        from app.database.session import SessionLocal  # noqa: PLC0415
        from app.models.knowledge import Regulation  # noqa: PLC0415

        session = SessionLocal()
        try:
            rows = session.execute(select(Regulation.title)).scalars().all()
        finally:
            session.close()
        return {normalize_title(row) for row in rows}
    except Exception as exc:  # noqa: BLE001 - 拿不到数据库就只按本地文件去重
        print(f"  [提示] 未能读取数据库已入库标题（{exc}），仅按本地文件去重")
        return set()


def main() -> int:
    parser = argparse.ArgumentParser(description="从税务总局政策法规库采集法规原文")
    parser.add_argument("--out", default=str(REPO / "法规数据" / "待导入"), help="输出目录")
    parser.add_argument("--target", type=int, default=100, help="目标总份数（含已入库）")
    parser.add_argument(
        "--channels",
        default="law,admin,rule,statecouncil,finance",
        help="栏目顺序，逗号分隔：law,admin,rule,statecouncil,finance",
    )
    parser.add_argument("--max-pages", type=int, default=8, help="每个栏目最多翻几页")
    parser.add_argument("--sleep", type=float, default=0.4, help="每次请求之间的间隔秒数")
    parser.add_argument("--dry-run", action="store_true", help="只列清单不下载正文")
    parser.add_argument(
        "--match",
        default=None,
        help=(
            "标题关键词，逗号分隔。给了就只采标题含其中任一关键词的条目——"
            "按专题补数据时用（如 --match 专项附加扣除,综合所得）"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="本次最多采多少份（不填则按 --target 反推还差多少）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略本地已采集文件，按栏目重新拉一遍（用于修复字段抽取规则后重刷）",
    )
    args = parser.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    known = load_known_titles()
    # 本地已有文件也算已采过；--force 时不看本次输出目录，按栏目重刷一遍
    local_sources = [(REPO / "法规数据" / "已清洗")]
    if not args.force:
        local_sources.append(out_dir)
    for directory in local_sources:
        for existing in directory.glob("*.txt"):
            known.add(normalize_title(existing.stem.split("_", 1)[-1]))

    print(f"已入库/已采集标题 {len(known)} 条，目标总份数 {args.target}")
    need = args.limit if args.limit is not None else max(args.target - len(known), 0)
    match_keywords = [k.strip() for k in (args.match or "").split(",") if k.strip()]
    if match_keywords:
        print(f"标题关键词过滤：{'、'.join(match_keywords)}（本次最多采 {need} 份）")
    if need == 0 and not args.dry_run:
        print("已达目标份数，无需采集。")
        return 0

    picked: list[dict] = []
    seen: set[str] = set(known)

    for channel_key in [c.strip() for c in args.channels.split(",") if c.strip()]:
        if channel_key not in CHANNELS:
            print(f"  [跳过] 未知栏目 {channel_key}")
            continue
        channel = CHANNELS[channel_key]
        print(f"\n== 栏目：{channel['name']}")
        for page in range(1, args.max_pages + 1):
            try:
                data = fetch_channel(channel_key, page)
            except Exception as exc:  # noqa: BLE001
                print(f"  第 {page} 页取列表失败：{exc}")
                break
            items = data.get("results", []) or []
            if not items:
                break
            for item in items:
                title = (item.get("title") or item.get("subTitleHtml") or "").strip()
                url = (item.get("url") or "").strip()
                if not title or not url:
                    continue
                # 专题采集：标题必须命中关键词
                if match_keywords and not any(kw in title for kw in match_keywords):
                    continue
                # 非财税栏目要按标题关键词过滤
                if channel_key in {"law", "admin", "statecouncil"} and not any(
                    kw in title for kw in TAX_TITLE_KEYWORDS
                ):
                    continue
                key = normalize_title(title)
                if key in seen:
                    continue
                seen.add(key)
                picked.append(
                    {
                        "channel": channel_key,
                        "title": title,
                        "url": url,
                        "published": item.get("publishedTimeStr", ""),
                        "list_meta": extract_list_meta(item),
                    }
                )
                if len(picked) >= need:
                    break
            print(f"  第 {page} 页：累计候选 {len(picked)} 份")
            if len(picked) >= need:
                break
            time.sleep(args.sleep)
        if len(picked) >= need:
            break

    print(f"\n候选 {len(picked)} 份")
    for item in picked[:10]:
        print(f"  · {item['title']}（{item['published'][:10]}）")
    if len(picked) > 10:
        print(f"  …… 其余 {len(picked) - 10} 份")
    if args.dry_run:
        return 0

    written = 0
    skipped: list[tuple[str, str]] = []
    for item in picked:
        try:
            page_html = _fetch(item["url"].replace("http://www.chinatax.gov.cn", DETAIL_HOST))
            detail = parse_detail(page_html)
            body = detail["body"]
            # 正文没抓到 / 内容在附件里 → 先把附件下载下来解析，解析不出来才跳过
            from_attachment = False
            if len(body) < MIN_BODY_CHARS or looks_like_attachment_only(body):
                attachment_text = fetch_attachment_text(
                    page_html,
                    item["url"].replace("http://www.chinatax.gov.cn", DETAIL_HOST),
                    sleep=args.sleep,
                )
                if len(attachment_text) >= MIN_BODY_CHARS:
                    body = attachment_text
                    from_attachment = True
                else:
                    reason = (
                        f"正文在附件里且附件解析失败（网页正文 {len(body)} 字）"
                        if find_attachment_urls(page_html, item["url"])
                        else f"正文过短（{len(body)} 字）"
                    )
                    skipped.append((item["title"], reason))
                    time.sleep(args.sleep)
                    continue
            if not detail["title"]:
                detail["title"] = item["title"]

            # 列表接口的元数据优先于详情页正则：官方自己标的更准。
            list_meta = item.get("list_meta", {})
            document_number = list_meta.get("writtentext") or detail["document_number"]
            # 页面上文号可能被拆成两行（"国家税务总局公告" / "2018年第55号"），
            # 写进文件前并成一行，导入端才能当成一个完整文号识别出来。
            document_number = re.sub(r"[\r\n]+", "", document_number or "").strip()
            issuer = list_meta.get("writtendepartments") or list_meta.get("writtendepartment", "")
            effective_date = detail["effective_date"]
            # 时效性"已废止"的不要静悄悄进库：这里照收，但在元数据里留痕，
            # 由审核环节决定（导入后默认是待复核，不会进检索结果）。
            aging = list_meta.get("aging", "")

            written += 1
            safe_title = re.sub(r'[\\/:*?"<>|]', "_", detail["title"])[:60]
            filename = f"{written:03d}_{safe_title}.txt"
            header = []
            if document_number:
                header.append(document_number)
            header.append(detail["title"])
            content = "\n".join(header) + "\n\n" + body
            (out_dir / filename).write_text(content, encoding="utf-8")
            (out_dir / f"{pathlib.Path(filename).stem}.meta.json").write_text(
                json.dumps(
                    {
                        "filename": filename,
                        "title": detail["title"],
                        "document_number": document_number or None,
                        "issuer": issuer or None,
                        "hierarchy_level_hint": list_meta.get("effectlevel", ""),
                        "effect_status_hint": aging,
                        "tax_policy": list_meta.get("taxpolicy", ""),
                        "source": f"国家税务总局政策法规库（{CHANNELS[item['channel']]['name']}）",
                        "source_url": item["url"].replace("http://www.chinatax.gov.cn", DETAIL_HOST),
                        "publish_date": item["published"][:10] or None,
                        "written_date": detail["written_date"] or None,
                        "effective_date": effective_date,
                        "expiry_date": list_meta.get("abolishdate") or None,
                        "collection": CHANNELS[item["channel"]]["collection"],
                        "content_length": len(body),
                        "truncated": False,
                        "from_attachment": from_attachment,
                        "note": detail["note"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"  [写入] {filename}（{len(body)} 字）")
        except Exception as exc:  # noqa: BLE001
            skipped.append((item["title"], str(exc)))
        time.sleep(args.sleep)

    print(f"\n采集完成：写入 {written} 份，跳过 {len(skipped)} 份")
    for title, reason in skipped[:20]:
        print(f"  · 跳过 {title}：{reason}")

    if args.force:
        removed, kept = prune_out_dir(out_dir)
        print(f"重刷去重：删除重复/过期文件 {removed} 个，保留 {kept} 份并重新编号")

    print(f"输出目录：{out_dir}")

    manifest = sorted(path.name for path in out_dir.glob("*.txt"))
    (out_dir / "_清单.txt").write_text("\n".join(manifest), encoding="utf-8")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
