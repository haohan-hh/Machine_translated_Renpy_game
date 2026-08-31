# -*- coding: utf-8 -*-
"""
翻译流水线：扫描 → 提取 → AI 翻译 → 生成 tl 文件。
CLI 与 GUI 共用；progress_cb(阶段, 消息) 用于界面刷新。
"""
from __future__ import annotations

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
    """按文本去重，返回 (去重后单元列表, {文本: 原索引列表})。"""
    seen: dict[str, int] = {}
    unique: list = []
    groups: dict[str, list[int]] = {}
    for u in units:
        key = u.what if isinstance(u, DialogueUnit) else u.text
        if key not in seen:
            seen[key] = len(unique)
            groups[key] = []
            unique.append(u)
        groups[key].append(seen[key])
    return unique, groups


def run_pipeline(
    game_path: str | Path,
    config: TranslationConfig | None = None,
    language: str = DEFAULT_LANGUAGE,
    client: TranslationClient | None = None,
    progress_cb=None,
    apply_font_patch: bool = True,
    apply_language_ui: bool = True,
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
    if info.has_chinese:
        zh = next(
            (l for l in info.languages if engine.is_chinese_language(l)),
            info.languages[0],
        )
        result.message = f"游戏已自带中文翻译（tl/{zh}），无需汉化"
        result.ok = True
        return result

    # 2. 提取
    log("正在提取游戏文本…")
    dialogues: list[DialogueUnit] = []
    strings: list = []
    skipped: list[str] = []

    if info.rpy_files:
        r = extract_rpy_files(info.rpy_files)
        dialogues.extend(r.dialogues)
        strings.extend(r.strings)
        skipped.extend(r.skipped)

    # 3. 反编译 .rpyc
    tmp_dir = None
    decompiled: list[Path] = []
    if info.rpyc_files:
        log(f"发现 {len(info.rpyc_files)} 个 .rpyc 文件，正在反编译…")
        tmp_dir, mapping = decompile_rpyc_files(info.rpyc_files, info.game_dir)
        if mapping:
            decompiled = list(mapping.values())
            r = extract_rpy_files(decompiled)
            dialogues.extend(r.dialogues)
            strings.extend(r.strings)
            skipped.extend(r.skipped)

    # 4. 按文本去重（省 API 调用）
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

    # 5. 翻译
    if client is None:
        client = TranslationClient(config or TranslationConfig())
    target = _guess_language_name(language)

    log(f"开始 AI 翻译（目标语言：{target}）…")
    t0 = time.time()

    d_trans = client.translate_texts([u.what for u in uniq_d], target=target)
    s_trans = client.translate_texts([u.text for u in uniq_s], target=target)

    log(f"API 请求统计：共发出 {client.request_count} 次请求，"
        f"失败 {client.error_count} 次"
        + ("（0 次请求 = 未调用任何 API）" if client.request_count == 0 else ""))

    # 6. 组装译文映射
    dialogue_translations: dict[str, str] = {}
    for u, tr in zip(uniq_d, d_trans):
        dialogue_translations[u.identifier] = tr
    string_translations: dict[str, str] = {}
    for u, tr in zip(uniq_s, s_trans):
        string_translations[u.text] = tr

    result.translated_count = len(dialogue_translations) + len(string_translations)

    # 7. 生成文件
    log("正在生成翻译文件…")
    out_dir = info.game_dir / "tl" / language
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

    # 8. 汉化后处理：中文字体 + 语言切换界面
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

    # 9. 统计
    elapsed = time.time() - t0
    unchanged = sum(
        1 for u, tr in zip(uniq_d, d_trans) if tr == u.what)
    unchanged += sum(
        1 for u, tr in zip(uniq_s, s_trans) if tr == u.text)
    result.skipped_count = unchanged

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
