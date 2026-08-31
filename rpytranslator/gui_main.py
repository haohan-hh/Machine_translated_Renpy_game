# -*- coding: utf-8 -*-
"""WinUI 3 (Fluent) 图形界面：类 Windows 11 的现代化界面。

界面由 XAML 定义（XamlLoader 加载），事件与逻辑由 Python 处理。
后台翻译任务运行在独立线程，通过 queue 向 UI 线程轮询更新。
"""
from __future__ import annotations

import json
import os
import queue
import threading
from pathlib import Path

from win32more.winui3 import XamlApplication, XamlLoader
from win32more.Microsoft.UI.Xaml import Window, Thickness, DispatcherTimer
from win32more.Microsoft.UI.Xaml.Media import MicaBackdrop, SolidColorBrush
from win32more.Microsoft.UI.Xaml.Controls import ComboBoxItem
from win32more.Microsoft.UI.Xaml.Documents import Run
from win32more.Windows.Foundation import TimeSpan
from win32more.Windows.Graphics import SizeInt32
from win32more.Windows.UI import Color
from win32more import asyncui

from . import engine
from .pipeline import run_pipeline
from .translator import TranslationConfig

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 预设服务: 名称 -> (API 地址, 模型)
PRESETS: dict[str, tuple[str, str]] = {
    "OpenAI": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "DeepSeek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "Kimi (Moonshot)": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "通义千问": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "智谱 GLM": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "Ollama 本地": ("http://localhost:11434/v1", "qwen2.5:7b"),
    "自定": ("", ""),
}
SERVICES = list(PRESETS)

LANGUAGES = [
    "schinese（简体中文）",
    "tchinese（繁体中文）",
    "japanese（日语）",
    "korean（韩语）",
    "english（英语）",
]

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "rpytranslator_config.json"

# 日志配色（Windows.UI.Color: A, R, G, B）
_COLOR_DEFAULT = Color(255, 229, 229, 229)
_COLOR_ERR     = Color(255, 244, 135, 113)
_COLOR_OK      = Color(255, 139, 195, 74)
_COLOR_INFO    = Color(255, 79, 195, 255)

# ---------------------------------------------------------------------------
# XAML 界面定义
# ---------------------------------------------------------------------------

XAML = r'''
<Grid xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
      xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
      RowDefinitions="Auto,*,Auto">

    <!-- 标题区 -->
    <StackPanel Grid.Row="0" Orientation="Horizontal" Spacing="14"
                Margin="28,24,28,10">
        <Border Width="42" Height="42" CornerRadius="10" Background="#26FFFFFF"
                VerticalAlignment="Center">
            <FontIcon Glyph="&#xE8F1;" FontSize="20" Foreground="#60CDFF"/>
        </Border>
        <StackPanel VerticalAlignment="Center" Spacing="1">
            <TextBlock Text="Ren'Py 自动汉化工具" FontSize="24" FontWeight="SemiBold"/>
            <TextBlock Text="识别游戏文本 → AI 翻译 → 一键生成汉化补丁"
                       FontSize="12" Opacity="0.55"/>
        </StackPanel>
    </StackPanel>

    <!-- 内容滚动区 -->
    <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
        <StackPanel Margin="28,4,28,20" Spacing="14">

            <!-- 卡片：游戏目录 -->
            <Border Background="#1FFFFFFF" BorderBrush="#14FFFFFF" BorderThickness="1"
                    CornerRadius="8" Padding="20,16">
                <StackPanel Spacing="10">
                    <TextBlock Text="游戏目录" FontSize="16" FontWeight="SemiBold"/>
                    <Grid ColumnDefinitions="*,Auto" ColumnSpacing="8">
                        <TextBox x:Name="GamePathBox" IsReadOnly="True"
                                 PlaceholderText="将 Ren'Py 游戏文件夹拖到此处，或点击“浏览…”选择"
                                 AllowDrop="True" DragOver="OnDragOver" Drop="OnDrop"/>
                        <Button Grid.Column="1" x:Name="BrowseBtn" Content="浏览…"
                                Click="OnBrowse"/>
                    </Grid>
                    <TextBlock x:Name="GameInfoText" FontSize="12" Opacity="0.65"
                               Text="尚未选择游戏目录"/>
                </StackPanel>
            </Border>

            <!-- 卡片：翻译设置 -->
            <Border Background="#1FFFFFFF" BorderBrush="#14FFFFFF" BorderThickness="1"
                    CornerRadius="8" Padding="20,16">
                <StackPanel Spacing="12">
                    <TextBlock Text="翻译设置" FontSize="16" FontWeight="SemiBold"/>
                    <Grid ColumnDefinitions="*,*" ColumnSpacing="12">
                        <StackPanel Spacing="4">
                            <TextBlock Text="翻译服务" FontSize="12" Opacity="0.6"/>
                            <ComboBox x:Name="ServiceBox" SelectionChanged="OnServiceChanged"/>
                        </StackPanel>
                        <StackPanel Grid.Column="1" Spacing="4">
                            <TextBlock Text="目标语言" FontSize="12" Opacity="0.6"/>
                            <ComboBox x:Name="LangBox"/>
                        </StackPanel>
                    </Grid>
                    <StackPanel Spacing="4">
                        <TextBlock Text="API 地址" FontSize="12" Opacity="0.6"/>
                        <TextBox x:Name="ApiUrlBox" PlaceholderText="https://api.openai.com/v1"/>
                    </StackPanel>
                    <StackPanel Spacing="4">
                        <TextBlock Text="API Key" FontSize="12" Opacity="0.6"/>
                        <PasswordBox x:Name="ApiKeyBox" PlaceholderText="sk-…"/>
                    </StackPanel>
                    <Grid ColumnDefinitions="*,Auto" ColumnSpacing="12">
                        <StackPanel Spacing="4">
                            <TextBlock Text="模型" FontSize="12" Opacity="0.6"/>
                            <TextBox x:Name="ModelBox" PlaceholderText="gpt-4o-mini"/>
                        </StackPanel>
                        <Button Grid.Column="1" x:Name="SaveBtn" Content="保存设置"
                                Click="OnSaveSettings" VerticalAlignment="Bottom"/>
                    </Grid>
                </StackPanel>
            </Border>

            <!-- 卡片：汉化后处理 -->
            <Border Background="#1FFFFFFF" BorderBrush="#14FFFFFF" BorderThickness="1"
                    CornerRadius="8" Padding="20,16">
                <StackPanel Spacing="6">
                    <TextBlock Text="汉化后处理" FontSize="16" FontWeight="SemiBold"/>
                    <ToggleSwitch x:Name="FontSwitch" Header="自动配置中文字体"
                                  OnContent="启用" OffContent="跳过"/>
                    <ToggleSwitch x:Name="LangUiSwitch" Header="注入语言切换界面"
                                  OnContent="启用" OffContent="跳过"/>
                </StackPanel>
            </Border>

            <!-- 卡片：执行 -->
            <Border Background="#1FFFFFFF" BorderBrush="#14FFFFFF" BorderThickness="1"
                    CornerRadius="8" Padding="20,16">
                <StackPanel Spacing="14">
                    <Grid ColumnDefinitions="*,Auto,Auto" ColumnSpacing="10">
                        <ProgressBar x:Name="Progress" Minimum="0" Maximum="100"
                                     Height="6" VerticalAlignment="Center"/>
                        <TextBlock Grid.Column="1" x:Name="ProgressText" Text="就绪"
                                   FontSize="12" Opacity="0.7" VerticalAlignment="Center"/>
                        <Button Grid.Column="2" x:Name="OpenOutBtn" Content="打开输出目录"
                                Click="OnOpenOutput" IsEnabled="False"/>
                    </Grid>
                    <Button x:Name="StartBtn" Content="开始汉化" Click="OnStart"
                            Height="42" FontSize="15" FontWeight="SemiBold"/>
                </StackPanel>
            </Border>

            <!-- 卡片：日志 -->
            <Border Background="#1FFFFFFF" BorderBrush="#14FFFFFF" BorderThickness="1"
                    CornerRadius="8" Padding="20,16">
                <StackPanel Spacing="10">
                    <Grid ColumnDefinitions="*,Auto">
                        <TextBlock Text="运行日志" FontSize="16" FontWeight="SemiBold"/>
                        <Button Grid.Column="1" x:Name="ClearLogBtn" Content="清空"
                                Click="OnClearLog" FontSize="12" Padding="12,5"
                                VerticalAlignment="Center"/>
                    </Grid>
                    <Border Background="#66000000" CornerRadius="6" Padding="12,10">
                        <ScrollViewer x:Name="LogScroll" VerticalScrollBarVisibility="Auto"
                                      MaxHeight="240">
                            <TextBlock x:Name="LogText" TextWrapping="Wrap"
                                       FontFamily="Consolas" FontSize="12"/>
                        </ScrollViewer>
                    </Border>
                </StackPanel>
            </Border>

        </StackPanel>
    </ScrollViewer>

    <!-- 底部状态栏 -->
    <Border Grid.Row="2" BorderThickness="0,1,0,0" BorderBrush="#14FFFFFF"
            Padding="28,7">
        <TextBlock x:Name="StatusText" Text="就绪" FontSize="12" Opacity="0.6"/>
    </Border>
</Grid>
'''


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

class GuiApp(XamlApplication):
    """Ren'Py 自动汉化工具 - WinUI 3 界面。"""

    def __init__(self) -> None:
        super().__init__()
        self._win: Window | None = None
        self.msg_q: "queue.Queue[str]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.game_dir: str | None = None
        self.game_info: object | None = None
        self._timer: DispatcherTimer | None = None
        self._log_lines = 0

    # -- 生命周期 ----------------------------------------------------------

    def OnLaunched(self, args) -> None:
        win = Window()
        self._win = win
        win.Title = "Ren'Py 自动汉化工具"

        # Mica 背景（类 Win11 深色磨砂）
        try:
            win.SystemBackdrop = MicaBackdrop()
        except Exception:
            pass

        root = XamlLoader.Load(self, XAML)
        win.Content = root

        try:
            win.AppWindow.Resize(SizeInt32(920, 880))
        except Exception:
            pass

        self._init_controls()
        self._load_settings()

        win.Activate()

        # UI 轮询定时器：从工作线程队列取消息更新界面
        try:
            timer = DispatcherTimer()
            timer.Interval = TimeSpan(1000000)  # 100 ms
            self._timer = timer

            def on_tick(sender, e):
                self._poll_queue()
                if not self._busy():
                    timer.Stop()

            timer.add_Tick(on_tick)
            timer.Start()
        except Exception as exc:
            self._append_log("定时器启动失败: %s" % exc, "err")

    # -- 初始化 ------------------------------------------------------------

    def _init_controls(self) -> None:
        for name in SERVICES:
            item = ComboBoxItem()
            item.Content = name
            self.ServiceBox.Items.Append(item)
        for lang in LANGUAGES:
            item = ComboBoxItem()
            item.Content = lang
            self.LangBox.Items.Append(item)
        self.ServiceBox.SelectedIndex = 0
        self.LangBox.SelectedIndex = 0

    # -- 配置持久化 --------------------------------------------------------

    def _load_settings(self) -> None:
        data: dict = {}
        try:
            if _CONFIG_PATH.exists():
                data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        self.ApiUrlBox.Text = data.get("url", PRESETS[SERVICES[0]][0])
        self.ApiKeyBox.Text = data.get("key", "")
        self.ModelBox.Text = data.get("model", PRESETS[SERVICES[0]][1])
        try:
            si = SERVICES.index(data.get("service", SERVICES[0]))
            self.ServiceBox.SelectedIndex = si
        except Exception:
            self.ServiceBox.SelectedIndex = 0
        try:
            li = LANGUAGES.index(data.get("lang", LANGUAGES[0]))
            self.LangBox.SelectedIndex = li
        except Exception:
            self.LangBox.SelectedIndex = 0
        self.FontSwitch.IsOn = bool(data.get("font", True))
        self.LangUiSwitch.IsOn = bool(data.get("lang_ui", True))
        self._append_log("已加载配置%s" % ("" if data else "（无）"), "info")

    def _save_settings(self) -> None:
        data = {
            "service": SERVICES[self.ServiceBox.SelectedIndex],
            "lang": LANGUAGES[self.LangBox.SelectedIndex],
            "url": self.ApiUrlBox.Text,
            "key": self.ApiKeyBox.Text,
            "model": self.ModelBox.Text,
            "font": self.FontSwitch.IsOn,
            "lang_ui": self.LangUiSwitch.IsOn,
        }
        try:
            _CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
            return True
        except Exception:
            return False

    # -- 事件：浏览 / 拖放 --------------------------------------------------

    def OnBrowse(self, sender, e) -> None:
        path = self._pick_folder_dialog()
        if path:
            self._set_game(path)

    def OnDragOver(self, sender, e) -> None:
        from win32more.Windows.ApplicationModel.DataTransfer import DataPackageOperation
        e.AcceptedOperation = DataPackageOperation.Copy

    def OnDrop(self, sender, e) -> None:
        try:
            op = e.DataView.GetStorageItemsAsync()
            asyncui.create_task(self._handle_drop(op))
        except Exception as exc:
            self._append_log("解析拖放内容失败: %s" % exc, "err")

    async def _handle_drop(self, op) -> None:
        try:
            items = await op
            if items.Size > 0:
                item = items.GetAt(0)
                self._set_game(str(item.Path))
        except Exception as exc:
            self._append_log("拖放解析失败: %s" % exc, "err")

    def _pick_folder_dialog(self) -> str | None:
        """Win11 文件夹选择对话框（SHBrowseForFolderW，不依赖 COM 注册表激活）。"""
        from ctypes import byref, create_unicode_buffer, cast, c_wchar_p
        from win32more.Windows.Win32.UI.Shell import (
            BROWSEINFOW, SHBrowseForFolderW, SHGetPathFromIDListW,
        )
        from win32more.Windows.Win32.Foundation import HWND
        try:
            bi = BROWSEINFOW()
            bi.hwndOwner = HWND(self._hwnd)
            bi.lpszTitle = "选择 Ren'Py 游戏目录"
            display = create_unicode_buffer(260)
            bi.pszDisplayName = cast(display, c_wchar_p)
            bi.ulFlags = 0x0001 | 0x0040  # BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
            pidl = SHBrowseForFolderW(byref(bi))
            if not pidl:
                return None
            buf = create_unicode_buffer(260)
            if SHGetPathFromIDListW(pidl, buf):
                return buf.value
            return None
        except Exception as exc:
            self._append_log("目录选择失败: %s" % exc, "err")
            return None

    def _set_game(self, path: str) -> None:
        path = path.strip().strip('"')
        if not path or not os.path.isdir(path):
            self._append_log("目录不存在: %s" % path, "err")
            return
        self.GamePathBox.Text = path
        self.game_dir = path
        self._append_log("已选择游戏目录: %s" % path, "info")
        try:
            self.game_info = engine.scan_game(path)
        except Exception as exc:
            self._append_log("解析游戏目录失败: %s" % exc, "err")
            self.game_info = None
        self._update_game_info()

    def _update_game_info(self) -> None:
        info = self.game_info
        if info is None:
            self.GameInfoText.Text = "未能识别该目录中的 Ren'Py 游戏"
            self.OpenOutBtn.IsEnabled = False
            return
        parts = []
        if getattr(info, "version_hint", None):
            parts.append("Ren'Py %s" % info.version_hint)
        if getattr(info, "has_chinese", False):
            parts.append("已含中文")
        if getattr(info, "languages", None):
            parts.append("语言: %s" % ", ".join(info.languages))
        if getattr(info, "text_count", None):
            parts.append("文本量: %s" % info.text_count)
        for note in getattr(info, "notes", []) or []:
            self._append_log("提示: %s" % note, "info")
        self.GameInfoText.Text = " · ".join(parts) if parts else "已识别 Ren'Py 游戏"
        self.OpenOutBtn.IsEnabled = True

    # -- 事件：服务 / 语言 --------------------------------------------------

    def OnServiceChanged(self, sender, e) -> None:
        idx = self.ServiceBox.SelectedIndex
        if idx < 0 or idx >= len(SERVICES):
            return
        url, model = PRESETS[SERVICES[idx]]
        if url:
            self.ApiUrlBox.Text = url
        if model:
            self.ModelBox.Text = model

    # -- 事件：保存 / 清空日志 / 打开输出目录 -------------------------------

    def OnSaveSettings(self, sender, e) -> None:
        if self._save_settings():
            self.StatusText.Text = "设置已保存"
            self._append_log("设置已保存到 %s" % _CONFIG_PATH, "ok")
        else:
            self.StatusText.Text = "设置保存失败"
            self._append_log("设置保存失败", "err")

    def OnClearLog(self, sender, e) -> None:
        self.LogText.Inlines.Clear()
        self._log_lines = 0
        self.StatusText.Text = "日志已清空"

    def OnOpenOutput(self, sender, e) -> None:
        if self.game_dir:
            out = os.path.join(self.game_dir, "汉化补丁")
            try:
                os.startfile(out)  # type: ignore[attr-defined]
            except OSError:
                os.startfile(self.game_dir)  # type: ignore[attr-defined]

    # -- 事件：开始汉化 -----------------------------------------------------

    def OnStart(self, sender, e) -> None:
        if self._busy():
            return
        if not self.game_dir:
            self._append_log("请先选择游戏目录", "err")
            self._msgbox("请先选择游戏目录", "缺少游戏目录")
            return
        url = self.ApiUrlBox.Text.strip()
        key = self.ApiKeyBox.Text.strip()
        model = self.ModelBox.Text.strip()
        if not url or not key or not model:
            self._append_log("请填写完整的 API 地址 / Key / 模型", "err")
            self._msgbox("请填写完整的 API 地址、API Key 与模型名称。", "配置不完整")
            return
        lang = LANGUAGES[self.LangBox.SelectedIndex].split("（")[0]

        config = TranslationConfig(
            base_url=url,
            api_key=key,
            model=model,
        )
        self._save_settings()

        self.worker = threading.Thread(
            target=self._run_translation,
            args=(config,),
            daemon=True,
        )
        self.worker.start()
        self._set_busy(True)
        self.StatusText.Text = "正在翻译…"
        self._append_log("开始汉化: %s → %s" % (self.game_dir, lang), "info")
        self._append_log("目标语言: %s | 模型: %s" % (lang, model), "info")

    def _run_translation(self, config: TranslationConfig) -> None:
        lang = LANGUAGES[self.LangBox.SelectedIndex].split("（")[0]
        try:
            run_pipeline(
                game_path=self.game_dir or "",
                config=config,
                language=lang,
                progress_cb=lambda text: self.msg_q.put(text),
                apply_font_patch=self.FontSwitch.IsOn,
                apply_language_ui=self.LangUiSwitch.IsOn,
            )
        except Exception as exc:
            self.msg_q.put("ERR|%s" % exc)
        finally:
            self.msg_q.put("__done__")

    # -- 队列轮询（UI 线程） -------------------------------------------------

    def _busy(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self.msg_q.get_nowait()
                if msg == "__done__":
                    self._on_done()
                    continue
                if msg.startswith("ERR|"):
                    self._append_log(msg[4:], "err")
                    continue
                self._append_log(msg)
        except queue.Empty:
            pass

    def _on_done(self) -> None:
        self.worker = None
        self._set_busy(False)
        self.StatusText.Text = "完成"
        self._append_log("全部完成，汉化补丁已生成。", "ok")

    def _set_busy(self, busy: bool) -> None:
        self.StartBtn.IsEnabled = not busy
        self.BrowseBtn.IsEnabled = not busy
        self.SaveBtn.IsEnabled = not busy
        self.Progress.IsIndeterminate = busy
        if not busy:
            self.Progress.Value = 100

    # -- 日志 ---------------------------------------------------------------

    def _append_log(self, text: str, level: str = "info") -> None:
        try:
            run = Run()
            run.Text = text + "\n"
            run.Foreground = SolidColorBrush({
                "err": _COLOR_ERR,
                "ok": _COLOR_OK,
                "info": _COLOR_INFO,
            }.get(level, _COLOR_DEFAULT))
            self.LogText.Inlines.Append(run)
            self._log_lines += 1
            if self._log_lines > 400:
                self.LogText.Inlines.Clear()
                self._log_lines = 0
            try:
                self.LogScroll.ChangeView(None, 1e18, None)
            except Exception:
                pass
        except Exception as exc:
            import traceback
            try:
                with open("_append_log_err.txt", "a", encoding="utf-8") as f:
                    f.write("LOG-FAIL: %r\n" % (exc,))
                    traceback.print_exc(file=f)
            except Exception:
                pass

    # -- 消息框 -------------------------------------------------------------

    def _msgbox(self, text: str, caption: str) -> None:
        try:
            from win32more.Windows.Win32.UI.WindowsAndMessaging import (
                MessageBoxW, MESSAGEBOX_STYLE,
            )
            MessageBoxW(None, text, caption,
                        MESSAGEBOX_STYLE(0x00000010))  # MB_ICONERROR | MB_OK
        except Exception as exc:
            self._append_log("消息框显示失败: %s" % exc, "err")

    # -- 窗口句柄 -----------------------------------------------------------

    @property
    def _hwnd(self) -> int:
        try:
            return int(self._win.AppWindow.Id.value)
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def run_gui() -> int:
    """启动 WinUI 3 应用（阻塞至窗口关闭）。"""
    XamlApplication.Start(GuiApp)
    return 0


# 兼容旧入口（main.py 旧版引用）
def launch_gui(root=None) -> None:  # pragma: no cover - 兼容桩
    raise RuntimeError(
        "旧版 tkinter 入口已移除，请通过 run_gui() 启动 WinUI 3 界面"
    )


if __name__ == "__main__":
    run_gui()
