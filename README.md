# Ren'Py 自动汉化工具

v0.3.0 可以从 Ren'Py 游戏的源码、编译脚本和 RPA 归档提取对白、菜单与界面文字，调用本机 Ollama 或兼容的聊天接口翻译，再把译文直接安装到所选游戏。版本变更见 [CHANGELOG.md](CHANGELOG.md)。

## 在 Windows 上使用

1. 双击本机的 `E:\翻译\启动翻译工具.exe`，选中包含 `game` 文件夹的游戏根目录。例如 UTFBM 选择 `D:\UTFBM`。
2. 选择翻译服务、模型和目标语言，保存设置。使用本机 Qwen 时选择「Ollama 本地」，点击「测试连接」可以看到实际答复。如果 Ollama 报告了模型显存用量，日志会显示对应数值。
3. 点击「开始汉化」。以 UTFBM 的简体中文为例，译文会直接写入 `D:\UTFBM\game\tl\schinese`。完成后重新启动游戏，在设置里从原语言切换到「简体中文」。
4. 中途暂停或游戏更新后，重新选择同一游戏与目标语言，再点「开始汉化」。工具读取已有译文和断点，只请求待补内容。「补漏查缺」可单独检查已安装的译文。

日常使用只需要上面的启动文件。本机默认模型为 Qwen3-8B 的 GGUF 量化版本，在 Ollama 中注册为 `qwen3-8b-q4_k_m:latest`，默认地址为 `http://127.0.0.1:11434/v1`。已经手动打开 Ollama 时，工具会复用它的服务。默认本机端口尚未就绪时，程序会自动调用安装目录里的 `start_ollama_fixed.cmd`。工具确认所选模型已注册后，仅在模型未加载时预热，最多等待 10 分钟，再开始聊天请求。

启动脚本优先使用 `OLLAMA_MODELS` 指定的模型库。本机使用 `E:\翻译\_dependencies\ollama_user_data_live\models`。未设置 `OLLAMA_MODELS` 时，脚本会选用安装目录中已有的随附模型库；没有随附库时，由 Ollama 使用自己的默认位置。在其他机器上使用时，请在设置里填写该机器实际安装的模型名。

Qwen 会结合角色定义和原句识别人名、地名与其他专名。需要逐字保留的别名或作品名也可以填入「保留原文」一栏。批译和单句重试都会检查 Ren'Py 插值与文字标签，并在简繁转换后还原受保护的内容。GPU 加载由 Ollama 管理，连接测试读取的是所选模型实际占用的显存。

## 游戏内的语言切换和字体

启用语言界面补丁后，工具会查找原生设置屏幕和可识别的自定义设置入口，并通过 Ren'Py 的 `Language()` 切换。对于工具注入的按钮，游戏会将验证结果写到 `game/tl/<语言>.语言切换验证报告.txt`。各状态的含义如下。

| 状态 | 在游戏里观察到的结果 |
| --- | --- |
| `WAITING` | 游戏已启动，等待打开设置。 |
| `READY` | 已打开设置，等待从其他语言点选目标语言。如果游戏已经显示中文，可以先选原语言，再选「简体中文」。 |
| `PASS` | 设置屏幕记录到这次点击，活动语言已经改变，且引擎用活动语言读到了预期的探针译文。 |
| `FAIL` / `ERROR` | 语言注册、按钮动作、译文读取或检查接口遇到问题。报告的 `error` 字段记录具体原因。 |

切换验证针对注入的按钮和一条已知译文。剧情覆盖由另一份 `game/tl/<语言>.运行时缺失报告.txt` 检查。运行过游戏后再次汉化，工具会按报告中的运行时翻译标识补全能够定位的内容。使用游戏原有语言控件或特殊自绘菜单时，请实际进入设置检查。相关接口见 Ren'Py 官方的 [Language 动作](https://www.renpy.org/doc/html/translation.html#Language)、[已知语言](https://www.renpy.org/doc/html/translation.html#renpy.known_languages) 和 [按语言读取译文](https://www.renpy.org/doc/html/translation.html#renpy.translate_string)。

工具在中文设置里只添加「系统字体」这一项。Windows 上优先使用当前用户的界面字体。该字体缺少中文字形时，工具会从可用的系统中文字体或游戏自带字体中选择，实际结果可在汉化日志里查看。

## 脚本、已有译文与适用范围

- 支持 `.rpy`、`.rpym`、`.rpyc`、`.rpymc` 以及 RPA 2.0、3.0。按游戏内相对路径处理脚本，保留不同路线的同名文件。松散脚本优先。同一路径有多个归档副本时，采用引擎默认的归档顺序。归档优先读取编译版，只有源码的分发包也允许提取。见 [Ren'Py 的归档查找顺序](https://www.renpy.org/doc/html/config.html#var-config.archives)。
- 翻译文件按对话标识或字符串原文合并。已有的手工译文和复杂翻译块会保留，新增定义写入相应文件。重复定义、无法索引的已编译译文或无法安全修改的内容会在日志中给出原因。
- 保留词、角色名以及只含符号的停顿可以保留原文。仍待补译的条目列在 `game/tl/<语言>.未翻译报告.txt`，质量检查结果按需要写入同一目录的 `<语言>.补漏查缺报告.txt`。
- 图片里的文字、运行时拼接的对话、游戏独有的脚本格式或自定义资源加载顺序可能需要人工处理。每款游戏实际可用的设置入口和已加载的译文以游戏内结果为准。

## 从源码运行

在项目根目录使用 Python 3.10 或更高版本。图形界面需要 Windows、`win32more` 及与其匹配的 Windows App Runtime。命令行模式可以单独运行。

```powershell
python -m pip install -r requirements.txt
python main.py --gui
```

GUI 和 CLI 共用已保存的服务地址与模型。优先读取启动器旁的 `rpytranslator_config.json`，其次读取 `config/rpytranslator_config.json`。没有配置时默认使用本机 Qwen，目标语言为 `schinese`。

```powershell
python main.py --cli "D:\UTFBM"
python main.py --cli "D:\UTFBM" --lang schinese --keep-terms "Alex,North Gate"
python main.py --help
```

`--api-url`、`--api-key`、`--model` 可覆盖本次调用的设置。更换服务地址时需同时指定该服务的模型，原服务的密钥不会随之传递。`--no-font-patch`、`--no-lang-ui` 和 `--no-verify` 分别跳过本次的字体配置、语言入口注入和验证补丁安装。

## 测试与构建

从项目根目录运行回归测试。归档、临时游戏、模型响应和验证报告的测试均可在本地运行。

```powershell
python -m unittest discover -s tests -v
```

要连同引擎测试一起运行，在同一 PowerShell 窗口先设置 `$env:RENPY_TEST_LAUNCHER = 'D:\UTFBM\UTFBM.exe'`，或换成自己的 Ren'Py 游戏启动器。测试会用该引擎加载临时游戏。v0.3.0 已在 Windows 上用 Python 3.10 至 3.15 的七个解释器版本运行完整测试。

在 Windows 的构建环境中安装依赖后，用仓库的 spec 打包。它会包含 WinUI 依赖、随附的 `unrpyc` 反编译器及其动态导入的模块。

```powershell
python -m pip install -r requirements.txt
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean RenPyTranslator.spec
```

默认构建产物是 `dist\RenPyTranslator.exe`。部署到上述 E 盘安装目录时，使用 `启动翻译工具.exe` 作为文件名，与现有配置、Ollama 启动脚本和模型库配套使用。模型权重和 Ollama 运行环境单独安装。

本项目仅供学习交流使用。请遵守目标游戏原作者的版权与使用条款。
