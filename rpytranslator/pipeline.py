# -*- coding: utf-8 -*-
"""
翻译流水线：扫描 → 提取 → AI 翻译 → 生成 tl 文件。
CLI 与 GUI 共用；progress_cb(阶段, 消息) 用于界面刷新。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import engine, rpa_loader
from .extract import (
    DialogueUnit, ExtractionResult, decode_say_string, extract_rpy_file,
    extract_rpy_files,
)
from .verify import (
    build_missing_fix, runtime_report_path, write_verification_patch,
)
from .generator import write_translation_files
from .housekeeping import dedupe_translate_blocks, remove_stale_rpyc
from .patcher import PatchResult, apply_all
from .rpyc_loader import cleanup, decompile_rpyc_files
from .translator import PauseRequested, TranslationClient, TranslationConfig

DEFAULT_LANGUAGE = "schinese"
# 未翻译文本的自动补译轮数上限：每轮只重译上一轮仍然失败的文本，
# 全部完成后不再生成“未翻译报告”；达到上限仍有残留则保留报告供手动处理。
MAX_RETRY_ROUNDS = 5
LANGUAGE_LABELS = {
    "schinese": "简体中文",
    "tchinese": "繁体中文",
    "zh_cn": "简体中文",
    "zh_hans": "简体中文",
    "zh": "简体中文",
}


def _guess_language_name(code: str) -> str:
    return LANGUAGE_LABELS.get(code.lower(), code)


@dataclass
class PipelineResult:
    ok: bool = False
    game_dir: Path | None = None
    languages: list[str] = field(default_factory=list)
    has_chinese: bool = False
    dialogue_count: int = 0
    string_count: int = 0
    translated_count: int = 0
    skipped_count: int = 0
    output_files: list[Path] = field(default_factory=list)
    output_dir: Path | None = None
    post_patches: list[PatchResult] = field(default_factory=list)
    removed_rpyc: int = 0
    removed_dup_blocks: int = 0
    paused: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)


def _dedupe_by_text(units) -> tuple[list, dict[str, list[int]]]:
    """按文本去重，返回 (去重后单元列表, {文本: 原列表索引列表})。"""
    seen: dict[str, int] = {}
    unique: list = []
    groups: dict[str, list[int]] = {}
    for i, u in enumerate(units):
        key = u.what if isinstance(u, DialogueUnit) else u.text
        if key not in seen:
            seen[key] = len(unique)
            groups[key] = []
            unique.append(u)
        groups[key].append(i)
    return unique, groups


def _norm_filename(filename: str, game_dir) -> str:
    """把源文件路径规范化为相对 game 目录的 posix 路径。

    统一 .rpy 提取（绝对路径）、.rpyc 反编译（临时目录路径）的表示，
    保证按源文件分组时，不同子目录的同名文件（days/route_aelfric/day_6.rpy
    与 days/route_ulrich/day_6.rpy）不会混淆，生成的 tl 文件也不会互相覆盖。
    """
    p = Path(filename)
    try:
        return p.relative_to(game_dir).as_posix()
    except (ValueError, TypeError):
        pass
    # 含 game/ 前缀的绝对路径
    parts = p.parts
    if "game" in parts:
        return Path(*parts[parts.index("game") + 1:]).as_posix()
    # 反编译临时文件等：无法定位时退回文件名
    return p.name


# Ren'Py 编译 translate 块时插入的位置标记（unrpyc 反编译后残留），
# 如 `balto "@@p0@@p这样自我介绍可能不是最好的方式。"`。游戏运行时无害，
# 但会污染译文、干扰增量匹配，解析已有 tl 时统一剥离。decode_say_string
# 已内建该剥离逻辑，故此处只需复用 extract 模块的实现。


def _parse_untran_report(path: Path) -> list[str]:
    """解析「未翻译报告」：返回其中列出的未翻译文本清单。

    报告条目格式为 `文件名:行号  原文`（文件名与行号后为两个空格），
    标题行、错误详情行等不会被解析进来。
    """
    texts: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    except OSError:
        return texts
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("==") or s.startswith("以下文本"):
            continue
        m = re.match(r"^.+:\d+\s{2,}(.+)$", s)
        if m:
            texts.append(m.group(1).strip())
    return texts


# ---------------------------------------------------------------------------
# 断点续译缓存
# ---------------------------------------------------------------------------

# 缓存中“已翻译文本集合”的指纹标识键
_CACHE_FP_KEY = "__fp__"


def _progress_fingerprint(uniq_d, uniq_s) -> str:
    """基于本次待翻译文本全集计算的指纹。

    源脚本变化（游戏更新）会导致文本集合变化 → 指纹变化 → 旧缓存作废，
    避免把旧文本的译文错误套用到更新后的游戏上。
    """
    h = hashlib.md5()
    for u in uniq_d:
        h.update(("d:" + u.what + "\x00").encode("utf-8", "ignore"))
    for u in uniq_s:
        h.update(("s:" + u.text + "\x00").encode("utf-8", "ignore"))
    return h.hexdigest()


def _load_progress_cache(path: Path, fingerprint: str) -> tuple[dict[str, str], dict[str, str]]:
    """读取断点续译缓存；指纹不一致（源文本已变）时视为无效并删除。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get(_CACHE_FP_KEY) != fingerprint:
            try:
                path.unlink()
            except OSError:
                pass
            return {}, {}
        d, s = raw.get("dialogues", {}), raw.get("strings", {})
        if isinstance(d, dict) and isinstance(s, dict):
            return (
                {str(k): str(v) for k, v in d.items()},
                {str(k): str(v) for k, v in s.items()},
            )
    except (OSError, ValueError):
        pass
    return {}, {}


def _save_progress_cache(path: Path, fingerprint: str,
                         d_by_text: dict[str, str],
                         s_by_text: dict[str, str]) -> None:
    """把翻译进度实时写入缓存（供断点续译）。失败静默（不影响主流程）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {_CACHE_FP_KEY: fingerprint,
                "dialogues": d_by_text, "strings": s_by_text}
        path.write_text(json.dumps(data, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass


def _drop_progress_cache(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


_TRANS_RE = re.compile(r"translate\s+(\S+)\s+(\S+):\s*$")


def _parse_existing_tl(tl_dir: Path, language: str) -> tuple[dict[str, str], dict[str, str]]:
    """解析已有 tl/<语言> 下的翻译文件。

    返回 (对话 identifier→译文, 字符串 原文→译文)。增量汉化时据此保留
    上次已经翻译成功的内容，避免全量重写 tl 后丢失已有译文。
    只认语言一致的标准 Ren'Py 翻译块（本工具生成 / 官方格式均兼容）。
    """
    d_map: dict[str, str] = {}
    s_map: dict[str, str] = {}
    if not tl_dir.is_dir():
        return d_map, s_map
    for f in sorted(tl_dir.rglob("*.rpy")):
        try:
            lines = f.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        except OSError:
            continue
        i, n = 0, len(lines)
        while i < n:
            m = _TRANS_RE.match(lines[i].strip())
            if not m or m.group(1) != language:
                i += 1
                continue
            block_id = m.group(2)
            i += 1
            if block_id == "strings":
                # 字符串块：old/new 成对出现
                cur_old: str | None = None
                while i < n:
                    ln = lines[i].strip()
                    if not ln:
                        i += 1
                        continue
                    if _TRANS_RE.match(ln):
                        break
                    mo = re.match(r'old\s+"(.*)"\s*$', ln)
                    mn = re.match(r'new\s+"(.*)"\s*$', ln)
                    if mo:
                        cur_old = decode_say_string(mo.group(1))
                    elif mn and cur_old is not None:
                        s_map[cur_old] = decode_say_string(mn.group(1))
                        cur_old = None
                    i += 1
            else:
                # 对话块：跳过注释行（原文），取译文行首尾引号之间的内容
                while i < n:
                    ln = lines[i].strip()
                    if not ln:
                        i += 1
                        continue
                    if _TRANS_RE.match(ln):
                        break
                    if not ln.startswith("#"):
                        q = ln.find('"')
                        rq = ln.rfind('"')
                        if q != -1 and rq > q:
                            d_map[block_id] = decode_say_string(ln[q + 1:rq])
                        break
                    i += 1
                # 跳过本块其余行，直到下一个 translate 块
                while i < n and not _TRANS_RE.match(lines[i].strip()):
                    i += 1
    return d_map, s_map


def _pause_and_save(language, info, todo_d, todo_s, d_by_text, s_by_text,
                    result, log, progress_cb, cleanup_all):
    """用户暂停：已译条目已实时落盘（调用方 _flush_pt_cache），这里负责
    写「未完成段落标记」并按“已暂停”状态返回。

    标记文件 tl/.{语言}.暂停标记.txt 按原提取顺序列出全部未完成条目；
    下次 run_pipeline 会自动加载进度缓存跳过已译条目，等于从标记处
    顺序续译，且与暂停前的译文风格/术语表完全一致（同一套提示词）。
    """
    import datetime
    marker = info.game_dir / "tl" / f".{language}.暂停标记.txt"
    pend_d = [u for u in todo_d if d_by_text.get(u.what) in (None, u.what)]
    pend_s = [u for u in todo_s if s_by_text.get(u.text) in (None, u.text)]
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "汉化暂停标记（汉化工具自动生成，续译完成后自动删除）",
            f"游戏目录: {info.game_dir}",
            f"语言: {language}",
            f"暂停时间: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
            f"已完成并保存: {len(todo_d) - len(pend_d)} 条对话、"
            f"{len(todo_s) - len(pend_s)} 条字符串",
            f"未完成（按顺序续译）: {len(pend_d)} 条对话、"
            f"{len(pend_s)} 条字符串",
            "",
            "未完成对话（文件:行号 原文）:",
        ]
        lines += [f"  {u.filename}:{u.line}  {u.what}" for u in pend_d]
        lines += ["", "未完成字符串（文件:行号 原文）:"]
        lines += [f"  {u.filename}:{u.line}  {u.text}" for u in pend_s]
        marker.write_text("\n".join(lines), encoding="utf-8")
    except OSError as e:
        log(f"! 写暂停标记失败（不影响进度缓存）: {e}")
    done_d = len(todo_d) - len(pend_d)
    done_s = len(todo_s) - len(pend_s)
    log(f"已暂停：本次完成 {done_d} 条对话、{done_s} 条字符串，"
        f"剩余 {len(pend_d)} 条对话、{len(pend_s)} 条字符串已标记。"
        f"进度已保存：{marker.parent / f'.{language}.汉化进度.json'}"
        f"（未完成清单：{marker}）")
    log("重新打开程序点击「开始汉化」即可从标记处自动继续，无需从头开始。")
    cleanup_all()
    result.paused = True
    result.ok = False
    result.message = (f"已暂停：完成 {done_d + done_s} 条，"
                      f"剩余 {len(pend_d) + len(pend_s)} 条已标记，"
                      "可随时从断点继续")
    return result


def run_pipeline(
    game_path: str | Path,
    config: TranslationConfig | None = None,
    language: str = DEFAULT_LANGUAGE,
    client: TranslationClient | None = None,
    progress_cb=None,
    apply_font_patch: bool = True,
    apply_language_ui: bool = True,
    apply_verification: bool = True,
    extra_terms: list[str] | None = None,
    pause_event=None,
) -> PipelineResult:
    """执行完整汉化流程，返回结果统计。"""
    def log(msg: str):
        if progress_cb:
            progress_cb(msg)

    def cleanup_all():
        # 反编译临时目录 + .rpa 解包临时目录 统一清理
        cleanup(tmp_dir)
        cleanup(materialized_dir)

    result = PipelineResult()

    # 1. 扫描
    log("正在扫描游戏目录…")
    info = engine.scan_game(game_path)
    result.game_dir = info.game_dir
    result.languages = info.languages
    result.has_chinese = info.has_chinese
    if info.game_dir is None:
        result.message = "；".join(info.notes) or "未找到 Ren'Py 游戏"
        result.ok = False
        return result

    result.game_dir = info.game_dir

    # 1.1 脚本打包在 .rpa 归档中（game 目录无松散 .rpy/.rpyc）：
    #     物化 .rpyc（Ren'Py 实际加载的编译产物，identifier 与之对齐）
    #     到临时目录，再走常规“反编译 → 提取”流程。
    materialized_dir: Path | None = None
    if (not info.rpy_files and not info.rpyc_files) and info.rpa_files:
        import tempfile as _tempfile
        log(f"检测到脚本归档（{len(info.archive_scripts)} 个脚本文件），正在解包…")
        materialized_dir = Path(_tempfile.mkdtemp(prefix="rpy_archive_"))
        extracted = rpa_loader.extract_all_scripts_to(
            info.rpa_files, materialized_dir, prefer="rpyc")
        if extracted:
            rpyc_ok = [p for p in extracted
                       if p.suffix.lower() in (".rpyc", ".rpymc")]
            rpy_ok = [p for p in extracted
                      if p.suffix.lower() in (".rpy", ".rpym")]
            # 与松散文件规则一致：有 .rpyc 时以 .rpyc 反编译为准，
            # 避免同一文件双份提取导致 tl 中 translate identifier 重复。
            if rpyc_ok:
                info.rpyc_files = rpyc_ok
                info.rpy_files = []
            else:
                info.rpy_files = rpy_ok
                info.rpyc_files = []
            log(f"已从归档解出 {len(extracted)} 个脚本（临时目录），开始提取…")
        else:
            log("无法从 .rpa 归档中解出脚本文件")
            result.message = "无法从 .rpa 归档中解出脚本文件"
            result.ok = False
            return result

    report_path = info.game_dir / "tl" / f"{language}.未翻译报告.txt"
    # 断点续译缓存：翻译过程中实时保存 (原文→译文)；中断后重跑时跳过
    # 已翻译文本。Ren'Py 只加载 tl 下的 .rpy，.json 文件不会被当作翻译。
    progress_cache = info.game_dir / "tl" / f".{language}.汉化进度.json"
    # 说明：游戏“已含中文 tl”时的判断不能放在提取之前——否则游戏更新后
    # 只会看到旧 tl 而直接返回，永远发现不了新增剧情。真正的“已是最新 /
    # 需要补译更新部分”要在提取并做文本级 diff 之后判断（见 5.2）。

    # 2. 增量汉化检测：拖入已汉化过的游戏时，检查是否仍存有未翻译报告。
    #    有则本次只翻译报告中列出的文本，保留已有译文，完成后自动更新报告，
    #    循环此过程直到全部汉化完成（报告消失）。
    retry_texts: set[str] = set()
    incremental = False
    if report_path.exists():
        incremental = True
        retry_texts = set(_parse_untran_report(report_path))
        if retry_texts:
            log(f"检测到未翻译报告（{len(retry_texts)} 条未翻译文本）："
                f"本次将增量汉化——只翻译报告中列出的文本，并保留已有译文")
        else:
            # 报告已无内容，删除空报告
            try:
                report_path.unlink()
            except OSError:
                pass

    # 2.0 前置：确定 .rpyc 优先的文件清单。
    #     Ren'Py 运行时优先加载 .rpyc，翻译以 .rpyc 反编译内容为准；
    #     有对应 .rpyc 的 .rpy 不再提取，避免同一文件双份提取（.rpy +
    #     .rpyc 反编译）导致 tl 中同一 translate identifier 重复定义两次，
    #     Ren'Py 编译 tl 时报错并使该文件翻译全部失效。
    need_rpyc: list[Path] = []
    rpy_to_skip: set[str] = set()
    if info.rpyc_files:
        rpy_rel = {_norm_filename(str(p), info.game_dir): p for p in info.rpy_files}
        rpyc_rel = {_norm_filename(str(p), info.game_dir): p for p in info.rpyc_files}
        for rel, rpyc in rpyc_rel.items():
            need_rpyc.append(rpyc)
            rpy_equiv = rel.replace(".rpymc", ".rpy").replace(".rpyc", ".rpy")
            if rpy_equiv in rpy_rel:
                rpy_to_skip.add(rpy_equiv)

    # 2.1 提取（只提取没有对应 .rpyc 的 .rpy）
    log("正在提取游戏文本…")
    dialogues: list[DialogueUnit] = []
    strings: list = []
    skipped: list[str] = []

    rpy_to_extract = [
        p for p in info.rpy_files
        if _norm_filename(str(p), info.game_dir) not in rpy_to_skip
    ]
    if rpy_to_extract:
        r = extract_rpy_files(rpy_to_extract)
        for d in r.dialogues:
            d.filename = _norm_filename(d.filename, info.game_dir)
        for s in r.strings:
            s.filename = _norm_filename(s.filename, info.game_dir)
        dialogues.extend(r.dialogues)
        strings.extend(r.strings)
        skipped.extend(r.skipped)

    # 3. 反编译 .rpyc（无对应 .rpy 的 .rpyc 在这里提取）
    tmp_dir = None
    if need_rpyc:
        log(f"发现 {len(need_rpyc)} 个 .rpyc 文件"
            f"（其中 {len(rpy_to_skip)} 个优先于同名 .rpy），正在反编译…")
        tmp_dir, mapping = decompile_rpyc_files(need_rpyc, info.game_dir)
        if mapping:
            # 反编译文件的路径映射回原始相对路径，保证与 .rpy 提取一致
            src_by_decomp = {str(v): k for k, v in mapping.items()}
            decompiled = list(mapping.values())
            r = extract_rpy_files(decompiled)
            for d in r.dialogues:
                orig = src_by_decomp.get(d.filename)
                d.filename = (_norm_filename(str(orig), info.game_dir)
                              .replace(".rpymc", ".rpy").replace(".rpyc", ".rpy")
                              if orig else d.filename)
            for s in r.strings:
                orig = src_by_decomp.get(s.filename)
                s.filename = (_norm_filename(str(orig), info.game_dir)
                              .replace(".rpymc", ".rpy").replace(".rpyc", ".rpy")
                              if orig else s.filename)
            dialogues.extend(r.dialogues)
            strings.extend(r.strings)
            skipped.extend(r.skipped)

    # 3.1 全局 identifier 去重（防御）：跨提取批次（.rpy 与 .rpyc 反编译）
    #     可能产生相同 identifier，同一 identifier 只保留首次出现的单元，
    #     确保生成的 tl 中每个 translate identifier 唯一。
    seen_ids: set[str] = set()
    dedup_d: list[DialogueUnit] = []
    for d in dialogues:
        if d.identifier in seen_ids:
            continue
        seen_ids.add(d.identifier)
        dedup_d.append(d)
    if len(dedup_d) != len(dialogues):
        log(f"检测到 {len(dialogues) - len(dedup_d)} 个重复 identifier，已去重")
    dialogues = dedup_d

    # 4. 角色名（Character("名字") 的首个字符串参数）保留原文，
    #    不参与 AI 翻译、不生成翻译条目，Ren'Py 自然显示原文。
    kept_names = [u for u in strings if u.context == "character"]
    strings = [u for u in strings if u.context != "character"]
    if kept_names:
        log(f"角色名 {len(kept_names)} 个将保留原文（不翻译）："
            + "、".join(sorted({u.text for u in kept_names})[:20]))

    # 4.1 人名保护名单：Character 名 / xxxVars.name + 用户额外指定的专有名词。
    #     这些词在翻译前整体替换为占位符，翻译后还原，杜绝被 AI 翻译。
    protect_terms: list[str] = []
    for u in kept_names:
        t = (u.text or "").strip()
        if t and len(t) >= 2 and t not in protect_terms:
            protect_terms.append(t)
    for t in (extra_terms or []):
        t = t.strip()
        if t and t not in protect_terms:
            protect_terms.append(t)
    if protect_terms:
        log(f"已保护 {len(protect_terms)} 个人名/专有名词不被翻译："
            + "、".join(protect_terms[:20]))

    # 5. 按文本去重（省 API 调用）
    uniq_d, d_groups = _dedupe_by_text(dialogues)
    uniq_s, s_groups = _dedupe_by_text(strings)

    result.dialogue_count = len(dialogues)
    result.string_count = len(strings)
    log(f"提取完成：对话 {len(dialogues)} 条，字符串 {len(strings)} 条"
        f"（去重后 {len(uniq_d)} + {len(uniq_s)}）")

    if not dialogues and not strings:
        cleanup_all()
        result.message = "未提取到可翻译的文本"
        result.ok = False
        return result

    # 5.1 增量汉化：只翻译报告中列出的文本，其余文本沿用已有译文
    if retry_texts:
        todo_d = [u for u in uniq_d if u.what in retry_texts]
        todo_s = [u for u in uniq_s if u.text in retry_texts]
        log(f"增量汉化：跳过 {len(uniq_d) - len(todo_d)} 条对话、"
            f"{len(uniq_s) - len(todo_s)} 条字符串（已有译文），"
            f"待翻译 {len(todo_d)} 条对话、{len(todo_s)} 条字符串")
    else:
        todo_d, todo_s = uniq_d, uniq_s

    # 5.2 游戏更新检测：游戏已有本工具/标准格式的汉化文件（tl/<语言>）时，
    #     现有 tl 中已覆盖的文本全部沿用，本次只翻译「新增 / 未覆盖」文本。
    #     游戏升级（更新脚本、新增剧情）后会自动命中这里 → 旧译文 100%
    #     保留，只补译更新带来的新文本，不会误判“无需汉化”或全量重译。
    # 5.2 与 7 步组装共用一份解析结果，避免对同一目录解析两次。
    existing_d: dict[str, str] = {}
    existing_s: dict[str, str] = {}
    if info.has_chinese and not retry_texts and (todo_d or todo_s):
        parsed_d, parsed_s = _parse_existing_tl(
            info.game_dir / "tl" / language, language)
        existing_d.update(parsed_d)
        existing_s.update(parsed_s)
        # 保护：tl 目录里有 .rpy 却解析不出任何标准翻译块（如官方译文打包
        # 在 .rpa、目录内容异常）→ 无法安全沿用/增量，避免 rmtree 重建时
        # 误覆盖原目录，按旧版语义返回“游戏已自带中文翻译”。
        if not existing_d and not existing_s and any(
                (info.game_dir / "tl" / language).rglob("*.rpy")):
            removed_rpyc = remove_stale_rpyc(info.game_dir)
            extra = (f"；已删除 {removed_rpyc} 个过期 .rpyc 缓存，"
                     f"下次启动游戏将自动重新编译" if removed_rpyc else "")
            zh = next(
                (l for l in info.languages
                 if engine.is_chinese_language(l)),
                language,
            )
            result.removed_rpyc = removed_rpyc
            log(f"tl/{zh} 已有文件但无法解析为翻译块，本次跳过汉化以避免覆盖")
            result.message = f"游戏已自带中文翻译（tl/{zh}），无需汉化{extra}"
            result.ok = True
            return result
        existing_digests = {i.split("_")[-1] for i in existing_d}

        def _tl_covered(d: DialogueUnit) -> bool:
            """该对话是否已有现成译文（identifier 精确匹配 / 摘要段匹配 /
            重复后缀 …_digest_N 的摘要段匹配）。原文被修改 → 摘要段变化 →
            判定为未覆盖 → 重新翻译。"""
            if d.identifier in existing_d:
                return True
            seg = d.identifier.rsplit("_", 1)[-1]
            if seg in existing_digests:
                return True
            if "_" in d.identifier:
                prev = d.identifier.rsplit("_", 2)[-2]
                if prev in existing_digests:
                    return True
            return False

        new_d_whats = {d.what for d in dialogues if not _tl_covered(d)}
        new_s_texts = {s.text for s in strings if s.text not in existing_s}
        if new_d_whats or new_s_texts:
            todo_d = [u for u in uniq_d if u.what in new_d_whats]
            todo_s = [u for u in uniq_s if u.text in new_s_texts]
            log(f"检测到游戏更新/新增文本：本次需补译 {len(todo_d)} 条对话、"
                f"{len(todo_s)} 条字符串；其余 {len(uniq_d) - len(todo_d)} 条对话、"
                f"{len(uniq_s) - len(todo_s)} 条字符串已有译文，将全部保留")
        else:
            # 5.2.1 现有汉化已覆盖当前版本全部文本 → 无需再翻译
            log("现有汉化已覆盖当前版本全部文本，无需新增翻译")
            _drop_progress_cache(progress_cache)
            removed_rpyc = remove_stale_rpyc(info.game_dir)
            extra = (f"；已删除 {removed_rpyc} 个过期 .rpyc 缓存，"
                     f"下次启动游戏将自动重新编译" if removed_rpyc else "")
            zh = next(
                (l for l in info.languages
                 if engine.is_chinese_language(l)),
                language,
            )
            result.removed_rpyc = removed_rpyc
            # 仍可能“运行时缺翻译”（提取覆盖不到的对话，表现为部分英文），
            # 因此继续做运行时验证 / 按运行时 identifier 自动补译。
            if apply_verification:
                result.post_patches.append(
                    write_verification_patch(info.game_dir, language, log_cb=log))
            if runtime_report_path(info.game_dir, language).is_file():
                log("检测到运行时缺失报告（游戏内仍有对话缺少翻译），"
                    "正在按运行时 identifier 自动补译…")
                if client is None:
                    client = TranslationClient(config or TranslationConfig())
                result.post_patches.append(build_missing_fix(
                    info.game_dir, language, client=client, log_cb=log))
                removed = remove_stale_rpyc(info.game_dir)
                result.removed_rpyc += removed
                if removed:
                    log(f"已删除 {removed} 个过期 .rpyc 缓存，"
                        f"下次启动游戏将自动重新编译")
                result.ok = True
                lines = [p.message for p in result.post_patches
                         if p.ok and p.message]
                lines.append("提示：重新启动一次游戏，运行时校验会自动更新缺失报告；"
                             "报告只剩空对话/符号（无实际文本）即视为汉化完整")
                result.message = "\n".join(lines)
                return result
            result.message = f"汉化已是最新：tl/{zh} 已覆盖当前版本全部文本{extra}"
            result.ok = True
            return result

    # 6. 断点续译：加载上次汉化中断前实时保存的缓存（原文→译文）。
    #    文本全集指纹一致时，命中缓存的文本本次直接跳过，不再重复调用 API。
    #    （指纹变化 = 源脚本已更新，旧缓存作废，自动走全新汉化。）
    fingerprint = _progress_fingerprint(uniq_d, uniq_s)
    cached_d, cached_s = _load_progress_cache(progress_cache, fingerprint)
    d_by_text: dict[str, str] = dict(cached_d)
    s_by_text: dict[str, str] = dict(cached_s)
    if cached_d or cached_s:
        log(f"检测到上次汉化进度缓存（对话 {len(cached_d)} 条、"
            f"字符串 {len(cached_s)} 条），本次将从中断处继续…")
        pause_marker = info.game_dir / "tl" / f".{language}.暂停标记.txt"
        if pause_marker.exists():
            log("识别到上一次的暂停标记：将按标记顺序续译未完成的段落")

    # 真正需要调用 API 的文本 = 待翻译列表中「缓存缺失」或「缓存译文仍
    # 等于原文（上次失败回退，需重试）」的条目
    need_d = [u for u in todo_d
              if u.what not in d_by_text or d_by_text[u.what] == u.what]
    need_s = [u for u in todo_s
              if u.text not in s_by_text or s_by_text[u.text] == u.text]
    if len(need_d) < len(todo_d) or len(need_s) < len(todo_s):
        log(f"缓存命中：跳过 {len(todo_d) - len(need_d)} 条对话、"
            f"{len(todo_s) - len(need_s)} 条字符串，本次待翻译 "
            f"{len(need_d)} 条对话、{len(need_s)} 条字符串")

    # 6.1 翻译 + 自动补译：第一次翻译后，凡译文仍等于原文（失败回退）的
    #    文本会自动进入下一轮，只重译这些文本；重复直到全部翻译完成
    #    （或达到 MAX_RETRY_ROUNDS 上限，此时保留“未翻译报告”供手动处理）。
    if client is None:
        client = TranslationClient(config or TranslationConfig())
    if pause_event is not None:
        client.pause_event = pause_event
    target = _guess_language_name(language)

    log(f"开始 AI 翻译（目标语言：{target}）…")
    t0 = time.time()

    total = len(need_d) + len(need_s)
    def report_progress(done: int, total: int) -> None:
        pct = int(done * 100 / total) if total else 100
        if progress_cb:
            progress_cb("PROGRESS|%d" % pct)

    def report_error(msg: str) -> None:
        if progress_cb:
            progress_cb("ERR|" + msg)

    # 实时落盘：每翻译完若干条写一次缓存（中断最多丢最近一小批）
    _pt_writes = [0]
    _PT_FLUSH_EVERY = 50

    def _flush_pt_cache() -> None:
        _save_progress_cache(progress_cache, fingerprint, d_by_text, s_by_text)

    def _make_item_cb(table: dict[str, str]):
        def item_cb(text: str, result: str) -> None:
            if not text.strip():
                return
            table[text] = result
            _pt_writes[0] += 1
            if _pt_writes[0] >= _PT_FLUSH_EVERY:
                _pt_writes[0] = 0
                _flush_pt_cache()
        return item_cb

    # 第 1 轮：翻译本轮待翻译的去重文本。翻译期间用户可随时暂停：
    # PauseRequested 在请求间隙抛出 → 落盘缓存 + 写暂停标记后按“已暂停”
    # 状态返回；已译条目保存在 tl/.{语言}.汉化进度.json，重开程序再点
    # 「开始汉化」即可自动从标记处继续，无需从头开始。
    try:
        d_trans = client.translate_texts(
            [u.what for u in need_d], target=target, names=protect_terms,
            progress_cb=report_progress, offset=0, total=total,
            error_cb=report_error, item_cb=_make_item_cb(d_by_text))
        s_trans = client.translate_texts(
            [u.text for u in need_s], target=target, names=protect_terms,
            progress_cb=report_progress, offset=len(need_d), total=total,
            error_cb=report_error, item_cb=_make_item_cb(s_by_text))
        for u, tr in zip(need_d, d_trans):
            d_by_text[u.what] = tr
        for u, tr in zip(need_s, s_trans):
            s_by_text[u.text] = tr
        _flush_pt_cache()

    # 第 2+ 轮：只重译仍未翻译（译文 == 原文）的文本
    # 注意：这里使用 retry_d/retry_s，不要复用上面的 need_d/need_s，
    # 后者表示“本轮真正调用了 API 的条目”，后面组装映射还要用到 todo_d。
        retry_round = 0
        while True:
            retry_d = [u for u in uniq_d if d_by_text.get(u.what) == u.what]
            retry_s = [u for u in uniq_s if s_by_text.get(u.text) == u.text]
            if not retry_d and not retry_s:
                break
            retry_round += 1
            if retry_round > MAX_RETRY_ROUNDS:
                log(f"补译 {MAX_RETRY_ROUNDS} 轮后仍有 {len(retry_d)} 条对话、"
                    f"{len(retry_s)} 条字符串未翻译，保留未翻译报告供手动处理")
                break
            sub_total = len(retry_d) + len(retry_s)
            log(f"补译第 {retry_round}/{MAX_RETRY_ROUNDS} 轮：剩余 "
                f"{len(retry_d)} 条对话、{len(retry_s)} 条字符串，重新翻译…")
            time.sleep(2)   # 间隔片刻，缓解限流

            def sub_progress(done: int, total: int) -> None:
                pct = int(done * 100 / total) if total else 100
                if progress_cb:
                    progress_cb("PROGRESS|%d" % pct)

            d2 = client.translate_texts(
                [u.what for u in retry_d], target=target, names=protect_terms,
                progress_cb=sub_progress, offset=0, total=sub_total,
                error_cb=report_error, item_cb=_make_item_cb(d_by_text))
            s2 = client.translate_texts(
                [u.text for u in retry_s], target=target, names=protect_terms,
                progress_cb=sub_progress, offset=len(retry_d), total=sub_total,
                error_cb=report_error, item_cb=_make_item_cb(s_by_text))
            for u, tr in zip(retry_d, d2):
                d_by_text[u.what] = tr
            for u, tr in zip(retry_s, s2):
                s_by_text[u.text] = tr
            _flush_pt_cache()
    except PauseRequested:
        _flush_pt_cache()
        return _pause_and_save(
            language, info, todo_d, todo_s, d_by_text, s_by_text,
            result, log, progress_cb, cleanup_all)
    if progress_cb:
        progress_cb("PROGRESS|100")

    log(f"API 请求统计：共发出 {client.request_count} 次请求，"
        f"失败 {client.error_count} 次"
        + ("（0 次请求 = 未调用任何 API）" if client.request_count == 0 else ""))
    if client.error_messages:
        log(f"错误详情（{len(client.error_messages)} 类）："
            + " | ".join(client.error_messages[:5]))

    # 7. 组装译文映射：先加载已有 tl 中的译文（增量汉化时保留已翻译内容，
    #    避免全量重写后丢失），再写入本次翻译结果。同一文本的所有出现
    #    （identifier 不同）共享同一译文，避免按文本去重后重复出现的对话
    #    拿不到译文而被跳过。
    if not existing_d and not existing_s:
        # 全新游戏或 5.2 未执行的场景：未持有任何解析结果时再补一次解析
        existing_d, existing_s = _parse_existing_tl(
            info.game_dir / "tl" / language, language)
    dialogue_translations: dict[str, str] = dict(existing_d)
    string_translations: dict[str, str] = dict(existing_s)
    # 增量兼容（identifier 前缀修正）：命名 menu 是 Ren'Py 的隐式 label，
    # 早期提取器不处理它，导致旧 tl 中 menu 作用域对话的 identifier 前缀缺失
    # （如 day_1_xxx 而非 day1_balto_intro_feeling_xxx）。翻译 ID 的摘要段
    # （md5 前 8 位）只由文本内容决定、与 label 无关，因此完整 identifier
    # 匹配不到时，按摘要段兜底复用已有译文，避免修正前缀后旧翻译丢失、
    # 游戏重新显示英文原文。
    digest_map: dict[str, str] = {}
    for _ident, _tr in existing_d.items():
        _seg = _ident.split("_")[-1]
        if re.fullmatch(r"[0-9a-f]{8}", _seg):
            digest_map.setdefault(_seg, _tr)
    if digest_map:
        for _d in dialogues:
            if _d.identifier in dialogue_translations:
                continue
            _tr = digest_map.get(_d.identifier.split("_")[-1])
            if _tr is not None:
                dialogue_translations[_d.identifier] = _tr
    for u in todo_d:
        tr = d_by_text[u.what]
        for idx in d_groups.get(u.what, ()):
            dialogue_translations[dialogues[idx].identifier] = tr
    for u in todo_s:
        tr = s_by_text[u.text]
        for idx in s_groups.get(u.text, ()):
            string_translations[strings[idx].text] = tr

    result.translated_count = len(dialogues) + len(strings)

    # 8. 生成文件
    log("正在生成翻译文件…")
    out_dir = info.game_dir / "tl" / language
    # 清理上次生成的 tl 目录，避免旧的平铺同名文件与新生成的子目录结构
    # 同时存在（同一 translate id 定义两次）。
    if out_dir.is_dir():
        import shutil as _shutil
        _shutil.rmtree(out_dir, ignore_errors=True)
    written = write_translation_files(
        dialogues, strings, language,
        dialogue_translations, string_translations,
        game_dir=info.game_dir,
        progress_cb=lambda i, n, p: log(f"已生成 {i}/{n}: {Path(p).name}"),
    )
    result.output_files = written
    result.output_dir = out_dir

    # 8.1 防御性清理：即使旧版平铺 tl 文件与新目录结构并存（历史上曾导致
    #     同一 translate identifier 定义两次），也保证生成结果无重复块。
    result.removed_dup_blocks = dedupe_translate_blocks(out_dir)
    if result.removed_dup_blocks:
        log(f"已清理翻译文件中 {result.removed_dup_blocks} 个重复 translate 块")

    # 翻译文件已成功生成 → 断点缓存已完成使命，删除以免下次误判为“未完成”
    _drop_progress_cache(progress_cache)
    _drop_progress_cache(
        info.game_dir / "tl" / f".{language}.暂停标记.txt")

    cleanup_all()

    if not written:
        result.message = "没有可写出的翻译文件（翻译全部失败？）"
        result.ok = False
        return result

    # 9. 汉化后处理：中文字体 + 语言切换界面
    if apply_font_patch or apply_language_ui:
        log("正在执行汉化后处理（中文字体 / 语言切换界面）…")
        result.post_patches = apply_all(
            info.game_dir, language=language,
            with_font=apply_font_patch,
            with_language_ui=apply_language_ui,
        )
        for pr in result.post_patches:
            if not pr.ok:
                log(f"  ! {pr.message}")
                result.errors.append(pr.message)
            elif pr.skip:
                log(f"  - {pr.message}")
            else:
                log(f"  ✓ {pr.message}")

    # 9.1 清理过期的 .rpyc：Ren'Py 运行时优先加载 .rpyc，旧缓存里没有本次
    #     生成的翻译（identifier 不匹配），会导致游戏仍显示英文原文。
    #     删除“有对应 .rpy 源文件”的缓存后，Ren'Py 下次启动会从源文件 +
    #     tl/ 翻译文件重新编译，保证翻译生效。
    result.removed_rpyc = remove_stale_rpyc(info.game_dir)
    if result.removed_rpyc:
        log(f"已删除 {result.removed_rpyc} 个过期编译缓存（.rpyc），"
            f"Ren'Py 下次启动将自动重新编译")

    # 9.2 运行时翻译验证闭环：注入验证补丁（下次启动游戏自动导出“运行时
    #     缺失报告”）；若已有报告（上次启动生成），按运行时 identifier 自动
    #     补译提取器覆盖不到、主流程无法感知的漏译对话。
    if apply_verification:
        result.post_patches.append(
            write_verification_patch(info.game_dir, language, log_cb=log))
    if runtime_report_path(info.game_dir, language).is_file():
        result.post_patches.append(build_missing_fix(
            info.game_dir, language, client=client,
            names=protect_terms, log_cb=log))
        if result.post_patches and result.post_patches[-1].files:
            removed = remove_stale_rpyc(info.game_dir)
            result.removed_rpyc += removed
            if removed:
                log(f"已删除 {removed} 个过期编译缓存（.rpyc），"
                    f"Ren'Py 下次启动将自动重新编译")

    # 10. 统计与未翻译报告
    elapsed = time.time() - t0
    unchanged_d = [
        d for d in dialogues if dialogue_translations.get(d.identifier) == d.what]
    unchanged_s = [
        s for s in strings if string_translations.get(s.text) == s.text]
    unchanged = len(unchanged_d) + len(unchanged_s)
    result.skipped_count = unchanged

    # 若整个汉化过程没有任何翻译错误（无 HTTP/连接/格式/占位符校验失败），
    # 剩余“译文 == 原文”的文本是模型有意保持原文（专有名词、技术标识符、
    # 键盘键名、`_()` 标记但无需翻译的内容等），视为已处理，不再生成报告。
    if unchanged and not client.error_count and not client.error_messages:
        log(f"无任何翻译错误，{unchanged} 条文本为模型保持原文"
            f"（专有名词/技术标识符等），视为已处理")
        unchanged_d = []
        unchanged_s = []
        unchanged = 0
        result.skipped_count = 0

    report = out_dir.parent / f"{language}.未翻译报告.txt"
    # 配套的临时/报告文件：上次运行暂停时遗留的标记、游戏运行时生成的
    # 缺失报告（已被 build_missing_fix 在本轮消费过，本轮翻译完成即作废）。
    pause_marker = out_dir.parent / f".{language}.暂停标记.txt"
    runtime_report = out_dir.parent / f"{language}.运行时缺失报告.txt"
    if unchanged:
        try:
            with open(report, "w", encoding="utf-8-sig") as f:
                f.write("以下文本未能翻译（回退为原文），请检查翻译服务或手动补充：\n\n")
                f.write("== 对话 ==" if unchanged_d else "")
                for d in unchanged_d:
                    f.write(f"\n{d.filename}:{d.line}  {d.what}")
                f.write("\n\n== 字符串 ==" if unchanged_s else "")
                for s in unchanged_s:
                    f.write(f"\n{s.filename}:{s.line}  {s.text}")
                if client.error_messages:
                    f.write("\n\n== 翻译错误详情（去重） ==\n")
                    for em in client.error_messages[:20]:
                        f.write(f"\n- {em}")
            log(f"未翻译 {unchanged} 条（回退原文），详情见: {report}")
        except OSError:
            log(f"未翻译 {unchanged} 条（回退原文）")
        for d in unchanged_d[:10]:
            log(f"  未翻译对话: {d.filename}:{d.line} {d.what[:50]}")
        for s in unchanged_s[:10]:
            log(f"  未翻译字符串: {s.filename}:{s.line} {s.text[:50]}")
    else:
        # 全部翻译完成：删除可能残留的旧报告与暂停标记，让
        # “报告/标记是否存在”准确反映“是否还有未完成内容”，避免
        # 上次运行的噪音被下一轮误判为未完成（runtime_report 已在
        # build_missing_fix 中消费完）。
        cleaned = []
        for stale in (report, pause_marker, runtime_report):
            try:
                if stale.exists():
                    stale.unlink()
                    cleaned.append(stale.name)
            except OSError:
                pass
        if cleaned:
            log("全部翻译完成，已清理上次运行的遗留文件: " + ", ".join(cleaned))

    result.ok = True
    lines = [
        f"汉化完成！共翻译 {len(dialogue_translations)} 条对话、"
        f"{len(string_translations)} 条字符串，用时 {elapsed:.0f} 秒。",
        f"翻译文件已生成到: {out_dir}",
        f"（{len(written)} 个文件，{unchanged} 条未能翻译回退原文）",
    ]
    for pr in result.post_patches:
        if pr.ok and not pr.skip:
            lines.append("· " + pr.message)
        elif not pr.ok:
            lines.append("· 警告: " + pr.message)
    if result.removed_rpyc:
        lines.append(f"· 已删除 {result.removed_rpyc} 个过期 .rpyc 缓存，"
                     f"下次启动游戏将自动重新编译，翻译即可生效")
    if result.removed_dup_blocks:
        lines.append(f"· 已清理 {result.removed_dup_blocks} 个重复翻译块")
    if result.post_patches and any(pr.ok and pr.detail for pr in result.post_patches):
        details = "；".join(pr.detail for pr in result.post_patches if pr.detail)
        if details:
            lines.append("提示: " + details)
    result.message = "\n".join(lines)
    return result
