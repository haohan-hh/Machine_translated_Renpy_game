# Ren'Py 自动汉化工具 (rpytranslator)

将 Ren'Py 游戏自动汉化为简体/繁体中文：识别游戏文本 → AI 翻译 → 一键生成汉化补丁。

## 功能特性

- **自动识别**：解析 Ren'Py 游戏目录，读取 `.rpy` / `.rpyc` 脚本并提取全部对白文本（同文件同时存在 `.rpy` 与 `.rpyc` 时按 Ren'Py 优先级只处理 `.rpy`，避免重复翻译块）
- **命名菜单识别**：`menu 名字:` 是 Ren'Py 的隐式 label，其作用域内（含菜单之后到下一个 `label` 前）的对话 identifier 前缀为菜单名，提取器与 Ren'Py 实际编译结果逐字节一致，翻译必定命中
- **增量翻译复用**：再次汉化时自动加载已有 `tl/` 译文；即使提取逻辑升级导致 identifier 前缀变化，也能按翻译 ID 的摘要段（md5 前 8 位，由文本内容决定、与 label 无关）兜底匹配，旧译文不会丢失
- **编译残留清理**：解析已有译文时自动剥离 Ren'Py 编译产物（如 unrpyc 反编译）中残留的 `@@pN@@p` 块位置标记，保留 `@@nN@@` / `@@pN@@` 插值占位符，保证译文干净且插值完整
- **完整覆盖**：按源文件相对路径生成翻译文件（保留子目录结构，`days/route_aelfric/day_6.rpy` 与 `days/route_ulrich/day_6.rpy` 不会互相覆盖）；同文本重复出现也全部写入译文
- **角色名保留**：角色名（`Character("...")`、`xxxVars.name = "..."`）自动保留原文不翻译；AI 提示词要求人名、角色名一律不译
- **AI 翻译**：支持任意 OpenAI 兼容接口（OpenAI / DeepSeek / Kimi / 通义千问 / 智谱 GLM / Ollama 本地等），批量翻译、智能重试，失败回退原文并生成「未翻译报告.txt」定位遗漏
- **自动补译**：首轮翻译后自动检测仍未翻译的文本并循环重译，直到全部汉化完成；只有多次补译后仍失败时才保留「未翻译报告.txt」（此时其内容即为需要手动补充/排查的部分）
- **报告驱动增量汉化**：再次拖入已汉化过的游戏时，若发现仍存在「未翻译报告.txt」，将自动只翻译报告中列出的文本（跳过已有译文的条目），并保留上次已翻译成功的内容；每次汉化结束后自动更新报告，直到全部完成、报告消失
- **智能判定未翻译**：代码性文本（样式属性、资源路径、字典键、颜色等）不会被误提取为对话；若整个汉化过程无任何翻译错误、剩余文本只是模型有意保持原文（专有名词、键盘键名等），则视为已处理、不生成报告——报告仅在存在真实翻译错误时保留，方便定位问题
- **一键补丁**：自动生成 `game/tl/<语言>/` 汉化补丁目录，可直接放入游戏生效
- **中文适配**：可选自动配置中文字体、注入语言切换界面
- **现代化界面**：WinUI 3 (Fluent Design) 界面，支持拖放选择游戏目录、Mica 磨砂背景

## 界面预览

WinUI 3 原生界面（类 Windows 11 风格）：
- 卡片式布局，深色主题 + Mica 背景
- 游戏目录拖放 / 浏览选择
- 7 个预设翻译服务一键切换
- 实时彩色运行日志，自动滚动

## 快速开始

### 方式一：直接使用可执行文件

下载 [Releases](https://github.com/haohan-hh/Machine_translated_Renpy_game/releases) 中的 `RenPyTranslator.exe`，双击运行即可（Windows 10 1809+ 或 Windows 11）。

### 方式二：源码运行

需要 Python 3.10+，并安装 Windows App Runtime 1.5：

```bash
pip install -r requirements.txt
python main.py --gui          # 图形界面
python main.py --cli 游戏目录 --api-key sk-xxx   # 命令行模式
```

## 使用方法

1. 启动程序后，将 Ren'Py 游戏文件夹拖入窗口（或点击“浏览…”选择）
2. 选择翻译服务，填写 API 地址 / Key / 模型（配置会自动保存）
3. 选择目标语言，点击「开始汉化」
4. 完成后打开输出目录，将补丁放入游戏根目录即可生效

## 命令行模式

```bash
python main.py --cli <游戏目录> [选项]
```

| 选项 | 说明 | 默认值 |
| --- | --- | --- |
| `--api-url` | 翻译服务地址 | `https://api.openai.com/v1` |
| `--api-key` | API Key | 无 |
| `--model` | 模型名 | `gpt-4o-mini` |
| `--lang` | 目标语言 | `schinese` |
| `--no-font-patch` | 跳过中文字体配置 | - |
| `--no-lang-ui` | 跳过语言切换界面 | - |

## 从源码构建 exe

```bash
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name RenPyTranslator --collect-all win32more --paths . main.py
```

## 项目结构

```
main.py                    # 入口（GUI / CLI）
rpytranslator/
  engine.py                # Ren'Py 游戏扫描与文本提取
  translator.py            # OpenAI 兼容翻译客户端
  pipeline.py              # 汉化流程编排
  patcher.py               # 字体与语言界面注入
  generator.py             # 翻译文件生成
  housekeeping.py          # 汉化后自动化：清理过期 .rpyc 缓存、去重 translate 块
  gui_main.py              # WinUI 3 图形界面
```

## 许可证

本项目仅供学习交流使用。请遵守目标游戏的原作者版权与使用条款。
