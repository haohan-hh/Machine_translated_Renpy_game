# -*- coding: utf-8 -*-
"""
Ren'Py 游戏自动汉化工具 - 主入口

用法:
    python main.py --gui                     # 启动图形界面（默认）
    python main.py --cli <游戏目录> [选项]    # 命令行模式

CLI 选项:
    --api-url   翻译服务地址（默认 https://api.openai.com/v1）
    --api-key   API Key
    --model     模型名（默认 gpt-4o-mini）
    --lang      目标语言标识（默认 schinese）
    --no-font-patch  跳过自动配置中文字体
    --no-lang-ui     跳过注入语言切换界面
"""
from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="rpytranslator",
        description="Ren'Py 游戏自动汉化工具：识别文本 → AI 翻译 → 生成汉化文件",
    )
    parser.add_argument("--gui", action="store_true", help="启动图形界面")
    parser.add_argument("--cli", metavar="游戏目录", nargs="?",
                        const=None, help="命令行模式，指定游戏目录")
    parser.add_argument("--api-url", default="https://api.openai.com/v1",
                        help="翻译 API 地址（OpenAI 兼容）")
    parser.add_argument("--api-key", default="", help="API Key")
    parser.add_argument("--model", default="gpt-4o-mini", help="模型名")
    parser.add_argument("--lang", default="schinese",
                        help="目标语言（schinese / tchinese / zh_cn …）")
    parser.add_argument("--no-font-patch", action="store_true",
                        help="跳过自动配置中文字体")
    parser.add_argument("--no-lang-ui", action="store_true",
                        help="跳过注入语言切换界面")

    args = parser.parse_args(argv)

    # 默认启动 GUI
    if args.gui or args.cli is None:
        return _launch_gui()

    # CLI 模式
    return _launch_cli(args)


def _launch_gui() -> int:
    try:
        from rpytranslator.gui_main import run_gui
    except ImportError as exc:
        print("[错误] WinUI 3 框架未安装，请先执行:\n"
              "       pip install win32more\n"
              f"       详细原因: {exc}")
        return 1
    return run_gui()


def _launch_cli(args) -> int:
    from pathlib import Path

    from rpytranslator.pipeline import run_pipeline
    from rpytranslator.translator import TranslationConfig

    game = Path(args.cli).expanduser().resolve()
    if not game.exists():
        print(f"错误: 路径不存在 - {game}")
        return 1

    cfg = TranslationConfig(
        base_url=args.api_url, api_key=args.api_key, model=args.model)
    print(f"游戏目录: {game}")
    print(f"API: {args.api_url} | 模型: {args.model} | 语言: {args.lang}")

    def _progress(m: str) -> None:
        if m.startswith("PROGRESS|"):
            print("  进度: %s%%" % m.split("|")[1])
        else:
            print("  ", m)

    result = run_pipeline(game, config=cfg, language=args.lang,
                          progress_cb=_progress,
                          apply_font_patch=not args.no_font_patch,
                          apply_language_ui=not args.no_lang_ui)
    print("\n" + "=" * 50)
    print(result.message)
    if not result.ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
