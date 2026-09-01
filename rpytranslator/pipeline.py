# -*- coding: utf-8 -*-
"""
翻译流水线：扫描 → 提取 → AI 翻译 → 生成 tl 文件。
CLI 与 GUI 共用；progress_cb(阶段, 消息) 用于界面刷新。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import engine
from .extract import (
    DialogueUnit, ExtractionResult, extract_rpy_file, extract_rpy_files,
)
from .generator import write_translation_files
from .patcher import PatchResult, apply_all
from .rpyc_loader import cleanup, decompile_rpyc_files
from .translator import TranslationClient, TranslationConfig

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


def _decode_say(raw: str) -> str:
    """反向还原 encode_say_string：\\n→换行、\\"→"、\\ →空格、\\\\→\\。"""
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == " ":
                out.append(" ")
            elif nxt == '"':
                out.append('"')
            else:
                out.append(nxt)          # \\ → \
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


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
                        cur_old = _decode_say(mo.group(1))
                    elif mn and cur_old is not None:
                        s_map[cur_old] = _decode_say(mn.group(1))
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
                            d_map[block_id] = _decode_say(ln[q + 1:rq])
                        break
                    i += 1
                # 跳过本块其余行，直到下一个 translate 块
                while i < n and not _TRANS_RE.match(lines[i].strip()):
                    i += 1
    return d_map, s_map


def run_pipeline(
    game_path: str | Path,
    config: TranslationConfig | None = None,
    language: str = DEFAULT_LANGUAGE,
    client: TranslationClient | None = None,
    progress_cb=None,
    apply_font_patch: bool = True,
    apply_language_ui: bool = True,
    extra_terms: list[str] | None = None,
) -> PipelineResult:
    """执行完整汉化流程，返回结果统计。"""
    def log(msg: str):
        if progress_cb:
            progress_cb(msg)

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
    report_path = info.game_dir / "tl" / f"{language}.未翻译报告.txt"
    # 游戏自带中文且没有未翻译报告 → 无需汉化；
    # 若仍留有未翻译报告（上次汉化未完成），继续增量汉化补齐。
    if info.has_chinese and not report_path.exists():
        zh = next(
            (l for l in info.languages if engine.is_chinese_language(l)),
            info.languages[0],
        )
        result.message = f"游戏已自带中文翻译（tl/{zh}），无需汉化"
        result.ok = True
        return result

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

    # 2.1 提取
    log("正在提取游戏文本…")
    dialogues: list[DialogueUnit] = []
    strings: list = []
    skipped: list[str] = []

    if info.rpy_files:
        r = extract_rpy_files(info.rpy_files)
        for d in r.dialogues:
            d.filename = _norm_filename(d.filename, info.game_dir)
        for s in r.strings:
            s.filename = _norm_filename(s.filename, info.game_dir)
        dialogues.extend(r.dialogues)
        strings.extend(r.strings)
        skipped.extend(r.skipped)

    # 3. 反编译 .rpyc：Ren'Py 运行时优先加载 .rpyc，所以生成翻译文件时
    #    也以 .rpyc 为准。同名的 .rpy 会被跳过，避免 .rpy/.rpyc 内容不一致
    #    导致翻译 identifier 不匹配而翻译不生效。
    tmp_dir = None
    if info.rpyc_files:
        rpy_rel = {_norm_filename(str(p), info.game_dir): p for p in info.rpy_files}
        rpyc_rel = {_norm_filename(str(p), info.game_dir): p for p in info.rpyc_files}
        need_rpyc: list[Path] = []
        rpy_to_skip: set[str] = set()
        for rel, rpyc in rpyc_rel.items():
            need_rpyc.append(rpyc)
            rpy_equiv = rel.replace(".rpymc", ".rpy").replace(".rpyc", ".rpy")
            if rpy_equiv in rpy_rel:
                rpy_to_skip.add(rpy_equiv)
        # 只保留没有对应 .rpyc 的 .rpy
        info.rpy_files = [
            p for p in info.rpy_files
            if _norm_filename(str(p), info.game_dir) not in rpy_to_skip
        ]
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
        cleanup(tmp_dir)
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

    # 6. 翻译 + 自动补译：第一次翻译后，凡译文仍等于原文（失败回退）的
    #    文本会自动进入下一轮，只重译这些文本；重复直到全部翻译完成
    #    （或达到 MAX_RETRY_ROUNDS 上限，此时保留“未翻译报告”供手动处理）。
    if client is None:
        client = TranslationClient(config or TranslationConfig())
    target = _guess_language_name(language)

    log(f"开始 AI 翻译（目标语言：{target}）…")
    t0 = time.time()

    total = len(todo_d) + len(todo_s)
    def report_progress(done: int, total: int) -> None:
        pct = int(done * 100 / total) if total else 100
        if progress_cb:
            progress_cb("PROGRESS|%d" % pct)

    def report_error(msg: str) -> None:
        if progress_cb:
            progress_cb("ERR|" + msg)

    # 第 1 轮：翻译本轮待翻译的去重文本
    d_trans = client.translate_texts(
        [u.what for u in todo_d], target=target, names=protect_terms,
        progress_cb=report_progress, offset=0, total=total,
        error_cb=report_error)
    s_trans = client.translate_texts(
        [u.text for u in todo_s], target=target, names=protect_terms,
        progress_cb=report_progress, offset=len(todo_d), total=total,
        error_cb=report_error)

    # 按文本保存译文（多轮补译期间持续更新）
    d_by_text: dict[str, str] = {u.what: tr for u, tr in zip(todo_d, d_trans)}
    s_by_text: dict[str, str] = {u.text: tr for u, tr in zip(todo_s, s_trans)}

    # 第 2+ 轮：只重译仍未翻译（译文 == 原文）的文本
    # 注意：这里使用 retry_d/retry_s，不要复用上面的 todo_d/todo_s，
    # 后者在增量汉化里表示“本次要翻译的条目”，后面组装映射还要用到。
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
            error_cb=report_error)
        s2 = client.translate_texts(
            [u.text for u in retry_s], target=target, names=protect_terms,
            progress_cb=sub_progress, offset=len(retry_d), total=sub_total,
            error_cb=report_error)
        for u, tr in zip(retry_d, d2):
            d_by_text[u.what] = tr
        for u, tr in zip(retry_s, s2):
            s_by_text[u.text] = tr
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
    existing_d, existing_s = _parse_existing_tl(
        info.game_dir / "tl" / language, language)
    dialogue_translations: dict[str, str] = dict(existing_d)
    string_translations: dict[str, str] = dict(existing_s)
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

    cleanup(tmp_dir)

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

    # 10. 统计与未翻译报告
    elapsed = time.time() - t0
    unchanged_d = [
        d for d in dialogues if dialogue_translations.get(d.identifier) == d.what]
    unchanged_s = [
        s for s in strings if string_translations.get(s.text) == s.text]
    unchanged = len(unchanged_d) + len(unchanged_s)
    result.skipped_count = unchanged

    report = out_dir.parent / f"{language}.未翻译报告.txt"
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
        # 全部翻译完成：删除可能残留的旧报告，以“报告是否存在”判断是否已完成
        try:
            if report.exists():
                report.unlink()
                log(f"全部文本翻译完成，已删除旧的未翻译报告: {report}")
        except OSError:
            pass

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
    if result.post_patches and any(pr.ok and pr.detail for pr in result.post_patches):
        details = "；".join(pr.detail for pr in result.post_patches if pr.detail)
        if details:
            lines.append("提示: " + details)
    result.message = "\n".join(lines)
    return result
