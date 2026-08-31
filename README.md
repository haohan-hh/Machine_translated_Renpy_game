# Ren'Py 自动汉化工具 (rpytranslator)

将 Ren'Py 游戏自动汉化为简体/繁体中文：识别游戏文本 → AI 翻译 → 一键生成汉化补丁。

## 功能特性

- **自动识别**：解析 Ren'Py 游戏目录，读取 `.rpy` / `.rpyc` 脚本并提取全部对白文本
- **AI 翻译**：支持任意 OpenAI 兼容接口（OpenAI / DeepSeek / Kimi / 通义千问 / 智谱 GLM / Ollama 本地等），批量翻译、智能重试
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

下载 [Releases](https://github.com/rpytranslator/rpytranslator/releases) 中的 `RenPyTranslator.exe`，双击运行即可（Windows 10 1809+ 或 Windows 11）。

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
  postprocessor.py         # 字体与语言界面注入
  gui_main.py              # WinUI 3 图形界面
```

## 许可证

本项目仅供学习交流使用。请遵守目标游戏的原作者版权与使用条款。
