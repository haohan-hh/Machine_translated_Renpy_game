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
import time
from pathlib import Path

from win32more.winui3 import XamlApplication, XamlLoader
from win32more.Microsoft.UI.Xaml import Window, Thickness, DispatcherTimer
from win32more.Microsoft.UI.Xaml.Media import MicaBackdrop, SolidColorBrush
from win32more.Microsoft.UI.Xaml.Controls import (
    ComboBoxItem,
    MenuFlyout,
    MenuFlyoutItem,
)
from win32more.Microsoft.UI.Xaml.Documents import Run
from win32more.Windows.Foundation import TimeSpan
from win32more.Windows.Graphics import SizeInt32
from win32more.Windows.UI import Color
from win32more import asyncui

from . import APP_NAME
from . import __version__ as PKG_VERSION
from . import engine
from .pipeline import run_pipeline
from .translator import TranslationClient, TranslationConfig, tcp_error_hint

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 应用标题：单一数据源指向 __init__.py 的 __version__，界面标题与窗口
# 标题栏都从这里取值，避免多处硬编码导致版本号显示不一致。
_APP_TITLE = f"{APP_NAME} v{PKG_VERSION}"

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
            <TextBlock x:Name="AppTitleText" Text="Ren'Py 自动汉化工具"
                       FontSize="24" FontWeight="SemiBold"/>
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
                    <Grid ColumnDefinitions="*,Auto,Auto,Auto" ColumnSpacing="12">
                        <StackPanel Spacing="4">
                            <TextBlock Text="模型" FontSize="12" Opacity="0.6"/>
                            <TextBox x:Name="ModelBox" PlaceholderText="gpt-4o-mini"/>
                        </StackPanel>
                        <Button Grid.Column="1" x:Name="ModelMenuBtn" Content="&#x25BE;"
                                VerticalAlignment="Bottom" Padding="10,4"/>
                        <Button Grid.Column="2" x:Name="TestBtn" Content="测试连接"
                                Click="OnTestConnection" VerticalAlignment="Bottom"/>
                        <Button Grid.Column="3" x:Name="SaveBtn" Content="保存设置"
                                Click="OnSaveSettings" VerticalAlignment="Bottom"/>
                    </Grid>
                    <StackPanel Spacing="4">
                        <TextBlock Text="保留原文（人名/专有名词，逗号分隔；Character 定义的人名已自动保留）"
                                   FontSize="12" Opacity="0.6"/>
                        <TextBox x:Name="KeepTermsBox"
                                 PlaceholderText="例如: Alex, Eileen, 大图书馆"/>
                    </StackPanel>
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
                    <Grid ColumnDefinitions="*,Auto,Auto,Auto" ColumnSpacing="10">
                        <ProgressBar x:Name="Progress" Minimum="0" Maximum="100"
                                     Height="6" VerticalAlignment="Center"/>
                        <TextBlock Grid.Column="1" x:Name="ProgressText" Text="就绪"
                                   FontSize="12" Opacity="0.7" VerticalAlignment="Center"/>
                        <Button Grid.Column="2" x:Name="OpenOutBtn" Content="打开输出目录"
                                Click="OnOpenOutput" IsEnabled="False"/>
                        <Button Grid.Column="3" x:Name="AuditBtn" Content="补漏查缺"
                                Click="OnAudit" ToolTipService.ToolTip="扫描已生成的翻译，检出未翻译残留 / 中英参半 / 目标语言错误，并排入重译队列"/>
                    </Grid>
                    <Grid>
                        <Grid.ColumnDefinitions>
                            <ColumnDefinition Width="*"/>
                            <ColumnDefinition Width="10"/>
                            <ColumnDefinition Width="*"/>
                        </Grid.ColumnDefinitions>
                        <Button x:Name="StartBtn" Content="开始汉化" Click="OnStart"
                                Height="42" FontSize="15" FontWeight="SemiBold"/>
                        <Button x:Name="PauseBtn" Grid.Column="2" Content="⏸ 暂停"
                                Click="OnPause" Height="42" FontSize="15"
                                FontWeight="SemiBold" IsEnabled="False"/>
                    </Grid>
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
        self._last_result: str | None = None
        self._test_mode = False
        self._paused_mode = False
        # 补漏查缺模式（仅扫描出报告，不翻译）
        self._audit_mode = False
        self._pause_event = threading.Event()
        # 已保存的模型配置（每条：{model, url, key}）；用于 ModelBox 右侧 ▼ 下拉
        self._saved_models: list[dict] = []
        # 模型下拉 Flyout 在 OnLaunched 中创建并挂到 ModelMenuBtn
        self._model_flyout: MenuFlyout | None = None
        # 下拉条目点击回调的强引用（防 GC 回收 delegate）
        self._menu_handlers: list = []

    # -- 生命周期 ----------------------------------------------------------

    def OnLaunched(self, args) -> None:
        win = Window()
        self._win = win
        win.Title = _APP_TITLE

        # Mica 背景（类 Win11 深色磨砂）
        try:
            win.SystemBackdrop = MicaBackdrop()
        except Exception:
            pass

        root = XamlLoader.Load(self, XAML)
        win.Content = root

        # 界面内的标题文本同样从单一数据源取，与窗口标题栏保持同步
        try:
            self.AppTitleText.Text = _APP_TITLE
        except Exception:
            pass

        # 模型下拉：把 MenuFlyout 挂到 ModelMenuBtn；之后 _load_settings 会
        # 根据 saved_models 填充条目。Button.Flyout 一旦设置，点击 ▼ 即自动展开。
        # 包 try：下拉列表属于锦上添花，绝不能因它让整个软件启动失败。
        try:
            self._model_flyout = MenuFlyout()
            self.ModelMenuBtn.Flyout = self._model_flyout
        except Exception as exc:
            self._model_flyout = None
            try:
                self._append_log("模型下拉初始化失败（不影响使用）: %s" % exc, "err")
            except Exception:
                pass

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
        self.ApiKeyBox.Password = data.get("key", "")
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
        self.KeepTermsBox.Text = data.get("keep_terms", "")
        # 已保存的模型列表：旧配置无此字段时退化为「当前一项」（向后兼容）
        self._saved_models = list(data.get("saved_models") or [])
        if not self._saved_models and data.get("model"):
            self._saved_models = [{
                "model": data["model"],
                "url": data.get("url", ""),
                "key": data.get("key", ""),
            }]
        self._rebuild_model_menu()
        self._append_log("已加载配置%s" % ("" if data else "（无）"), "info")

    def _save_settings(self) -> bool:
        data = {
            "service": SERVICES[self.ServiceBox.SelectedIndex],
            "lang": LANGUAGES[self.LangBox.SelectedIndex],
            "url": self.ApiUrlBox.Text,
            "key": self.ApiKeyBox.Password,
            "model": self.ModelBox.Text,
            "font": self.FontSwitch.IsOn,
            "lang_ui": self.LangUiSwitch.IsOn,
            "keep_terms": self.KeepTermsBox.Text,
        }
        # 把当前配置作为一条保存进下拉列表（按 model 名去重）
        cur_model = (data["model"] or "").strip()
        cur_entry = {"model": cur_model, "url": data["url"], "key": data["key"]}
        kept = [e for e in self._saved_models if (e.get("model") or "").strip() != cur_model]
        kept.append(cur_entry)
        # 上限 20 条：把最新的保留在末尾
        self._saved_models = kept[-20:]
        data["saved_models"] = self._saved_models
        try:
            _CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
        except Exception:
            return False
        self._rebuild_model_menu()
        return True

    # -- 模型下拉（ModelMenuBtn） -----------------------------------------

    @staticmethod
    def _vector_append(items, item) -> None:
        """向 WinRT `IVector<T>` 追加元素。

        `MenuFlyout.Items` 是 ABI 层的 `IVector<MenuFlyoutItemBase>`，不是
        .NET 风格集合：追加方法叫 **`Append`**（不是 `Add`），取长度用
        `Size`（**没有 `Count`**）——此前误用 `Count` 会在启动阶段抛
        `AttributeError` 直接导致软件起不来。这里仍做一次命名兼容，防止
        不同 win32more 版本的投影差异再次让下拉不可用。
        """
        for name in ("Append", "append", "Add"):
            fn = getattr(items, name, None)
            if callable(fn):
                fn(item)
                return
        raise AttributeError("IVector 未找到可用的追加方法（Append/append/Add）")

    def _rebuild_model_menu(self) -> None:
        """按 self._saved_models 重建模型下拉。

        实现要点（都是为了不再让启动崩掉）：
        - 每次**新建**一个 MenuFlyout 重新挂到按钮上，因此完全不需要
          `Clear()` / `RemoveAt()` / `Size`，只用到最确定的 `Append`。
        - 条目回调用**闭包**直接捕获该条配置，不用 `Tag`——`Tag` 的类型是
          `IInspectable`，把 Python dict 赋给它在 win32more 下并不可靠。
        - 闭包 delegate 存进 `self._menu_handlers` 保持引用，防止被 GC。
        - 整体包 try/except：下拉只是便利功能，任何异常都不得拖垮启动。
        """
        try:
            btn = getattr(self, "ModelMenuBtn", None)
            if btn is None:
                return
            flyout = MenuFlyout()
            items = flyout.Items
            self._menu_handlers = []          # 保持委托引用，避免被 GC
            if not self._saved_models:
                hint = MenuFlyoutItem()
                hint.Text = "（暂无已保存的模型，点击「保存设置」加入）"
                hint.IsEnabled = False
                self._vector_append(items, hint)
            else:
                title = MenuFlyoutItem()
                title.Text = "已保存的模型（点击切换）"
                title.IsEnabled = False
                self._vector_append(items, title)
                for entry in self._saved_models:
                    mi = MenuFlyoutItem()
                    model = (entry.get("model") or "").strip() or "（未命名）"
                    url = entry.get("url") or ""
                    mi.Text = f"{model}    {url}"
                    handler = self._make_model_handler(dict(entry))
                    self._menu_handlers.append(handler)
                    mi.add_Click(handler)
                    self._vector_append(items, mi)
            self._model_flyout = flyout
            btn.Flyout = flyout
        except Exception as exc:
            try:
                self._append_log("构建模型下拉列表失败（不影响使用）: %s" % exc, "err")
            except Exception:
                pass

    def _make_model_handler(self, entry: dict):
        """为一条已保存配置生成点击回调（闭包捕获 entry，避免用 Tag）。"""
        def _handler(sender, e):
            self._apply_saved_model(entry)
        return _handler

    def _apply_saved_model(self, entry: dict) -> None:
        """把一条已保存配置的 url/key/model 写回界面（不自动落盘）。"""
        try:
            self.ApiUrlBox.Text = entry.get("url") or ""
            self.ApiKeyBox.Password = entry.get("key") or ""
            self.ModelBox.Text = (entry.get("model") or "").strip()
            self._append_log(
                "已切换到已保存模型：%s" % (entry.get("model") or "（无模型名）"),
                "info")
        except Exception as exc:
            try:
                self._append_log("切换模型失败: %s" % exc, "err")
            except Exception:
                pass

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
            self._append_log("上一个任务仍在运行，请等待其完成后再试", "err")
            self.StatusText.Text = "忙：等待上一任务完成"
            return
        if not self.game_dir:
            self._append_log("请先选择游戏目录", "err")
            self._msgbox("请先选择游戏目录", "缺少游戏目录")
            return
        url = self.ApiUrlBox.Text.strip()
        key = self.ApiKeyBox.Password.strip()
        model = self.ModelBox.Text.strip()
        if not url or not key or not model:
            self._append_log("请填写完整的 API 地址 / Key / 模型", "err")
            self._msgbox("请填写完整的 API 地址、API Key 与模型名称。", "配置不完整")
            return
        lang = LANGUAGES[self.LangBox.SelectedIndex].split("（")[0]
        font = self.FontSwitch.IsOn
        lang_ui = self.LangUiSwitch.IsOn
        game_dir = self.game_dir
        keep_terms = [
            t.strip() for t in self.KeepTermsBox.Text.replace("，", ",").split(",")
            if t.strip()
        ]

        config = TranslationConfig(
            base_url=url,
            api_key=key,
            model=model,
        )
        self._save_settings()

        self._test_mode = False
        self._pause_event = threading.Event()
        self.worker = threading.Thread(
            target=self._run_translation,
            args=(config, lang, game_dir, font, lang_ui, keep_terms,
                  self._pause_event),
            daemon=True,
        )
        self.worker.start()
        self._ensure_timer()
        self._set_busy(True)
        # ETA 倒计时起点：任务启动时刻（含解包/反编译阶段），并清空旧任务状态
        self._eta_start = time.monotonic()
        self._eta_smooth = None
        self._eta_max_pct = 0
        self.ProgressText.Text = "准备中…"
        self.StatusText.Text = "正在翻译…"
        self._append_log("开始汉化: %s → %s" % (self.game_dir, lang), "info")
        self._append_log("目标语言: %s | 模型: %s" % (lang, model), "info")

    # -- 事件：测试连接 ----------------------------------------------------

    def OnTestConnection(self, sender, e) -> None:
        """向 AI 服务商发一个最小请求，验证地址 / Key / 模型是否可用。"""
        if self._busy():
            self._append_log("上一个任务仍在运行，请等待其完成后再试", "err")
            self.StatusText.Text = "忙：等待上一任务完成"
            return
        url = self.ApiUrlBox.Text.strip()
        key = self.ApiKeyBox.Password.strip()
        model = self.ModelBox.Text.strip()
        if not url or not key or not model:
            self._append_log("请填写完整的 API 地址 / Key / 模型", "err")
            return
        self._test_mode = True
        self.worker = threading.Thread(
            target=self._run_test_connection,
            args=(TranslationConfig(base_url=url, api_key=key, model=model),),
            daemon=True,
        )
        self.worker.start()
        self._ensure_timer()
        self._set_busy(True)
        self.ProgressText.Text = "测试中…"
        self.StatusText.Text = "正在测试连接（DNS→TCP→HTTP）…"
        self._append_log("正在测试连接: %s（模型 %s）…" % (url, model), "info")

    def _run_test_connection(self, config: TranslationConfig) -> None:
        """分步测试连接：DNS 解析 → TCP 连接 → HTTP 请求，任一步失败立即反馈。

        说明：urlopen 的 timeout 无法限制 DNS 解析耗时，因此 DNS 解析放在
        独立守护线程中运行，用 join(timeout) 兜底，避免无限等待。
        """
        import socket
        from urllib.parse import urlparse

        try:
            u = urlparse(config.base_url)
            if u.scheme not in ("http", "https") or not u.hostname:
                self.msg_q.put("TEST_ERR|API 地址格式无效: %s" % config.base_url)
                return
            host = u.hostname
            port = u.port or (443 if u.scheme == "https" else 80)

            # 1) DNS 解析（getaddrinfo 无法设超时 → 线程 + join 兜底）
            resolved: list = []

            def _resolve() -> None:
                try:
                    resolved.append(
                        socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM))
                except Exception as e:  # noqa: BLE001
                    resolved.append(e)

            rt = threading.Thread(target=_resolve, daemon=True)
            rt.start()
            rt.join(timeout=15)
            if not resolved:
                self.msg_q.put(
                    "TEST_ERR|DNS 解析超时（>15 秒）: %s\n请检查网络连接，或确认该地址在当前网络环境可访问。"
                    % host)
                return
            if isinstance(resolved[0], Exception):
                self.msg_q.put("TEST_ERR|DNS 解析失败: %s" % resolved[0])
                return

            # 2) TCP 连接（直连，不走 HTTP 代理）
            #    localhost 在 Windows 上常同时解析出 IPv6(::1) 与 IPv4(127.0.0.1)，
            #    而本地模型服务（Ollama / LM Studio）通常只监听 IPv4；只尝试首个
            #    地址会误报“目标计算机积极拒绝”(WinError 10061)。逐个地址尝试，
            #    并在全部失败后短暂等待重试一轮（服务可能刚启动）。
            last_err: Exception | None = None
            connected = False
            for round_no in range(2):
                for family, stype, proto, _, sockaddr in resolved[0]:
                    sock = socket.socket(family, stype, proto)
                    sock.settimeout(15)
                    try:
                        sock.connect(sockaddr)
                        connected = True
                    except Exception as e:  # noqa: BLE001
                        last_err = e
                    finally:
                        sock.close()
                    if connected:
                        break
                if connected or round_no == 1:
                    break
                time.sleep(0.8)
            if not connected:
                self.msg_q.put(
                    "TEST_ERR|TCP 连接失败: %s:%s - %s\n%s"
                    % (host, port, last_err, tcp_error_hint(last_err, host)))
                return

            # 3) HTTP 请求（含鉴权，验证地址 / Key / 模型）。
            #    本地模型首次请求需把权重加载进显存，可能耗时 10~60 秒，
            #    故测试超时放宽到 120 秒，避免“服务正常但被误判失败”。
            self.msg_q.put("TCP 已连通，正在发送测试请求（本地模型首次加载可能较慢）…")
            test_cfg = TranslationConfig(
                base_url=config.base_url,
                api_key=config.api_key,
                model=config.model,
                timeout=120,
                max_retries=1,
            )
            client = TranslationClient(test_cfg)
            t0 = time.time()
            resp = client.chat([{"role": "user", "content": "你好"}]).strip()
            elapsed = int((time.time() - t0) * 1000)
            snippet = (resp or "")[:60]
            self.msg_q.put("TEST_OK|连接成功（%d ms）：%s" % (elapsed, snippet))
        except Exception as exc:
            self.msg_q.put("TEST_ERR|%s" % exc)
        finally:
            self.msg_q.put("__done__")

    def OnAudit(self, sender, e) -> None:
        """补漏查缺：对已翻译的游戏单独执行扫描（不翻译，只出报告+排队）。"""
        if self._busy():
            self._append_log("上一个任务仍在运行，请等待其完成后再试", "err")
            self.StatusText.Text = "忙：等待上一任务完成"
            return
        game_dir = (getattr(self, "game_dir", None)
                    or self.GamePathBox.Text).strip()
        if not game_dir or not Path(game_dir).exists():
            self._append_log("请先选择已翻译的游戏目录，再执行补漏查缺", "err")
            return
        lang = LANGUAGES[self.LangBox.SelectedIndex].split("（")[0].strip()
        self._audit_mode = True
        self.worker = threading.Thread(
            target=self._run_audit,
            args=(game_dir, lang),
            daemon=True,
        )
        self.worker.start()
        self._ensure_timer()
        self._set_busy(True)
        self.ProgressText.Text = "扫描中…"
        self.StatusText.Text = "补漏查缺扫描中…"
        self._append_log("补漏查缺：正在扫描 tl/%s 下的翻译文件…" % lang, "info")

    def _run_audit(self, game_dir: str, lang: str) -> None:
        """后台线程：跑 audit.run_audit，日志经 msg_q 回 UI 线程。"""
        try:
            from rpytranslator.audit import run_audit
            run_audit(game_dir, lang, log=lambda t: self.msg_q.put(t))
        except Exception as exc:  # noqa: BLE001
            import traceback
            self.msg_q.put("ERR|补漏查缺失败: %s" % exc)
            try:
                with open("_gui_worker_err.txt", "a", encoding="utf-8") as f:
                    traceback.print_exc(file=f)
            except Exception:
                pass
        finally:
            self.msg_q.put("__done__")

    def _run_translation(
        self,
        config: TranslationConfig,
        lang: str,
        game_dir: str,
        font: bool,
        lang_ui: bool,
        keep_terms: list[str] | None = None,
        pause_event=None,
    ) -> None:
        try:
            result = run_pipeline(
                game_path=game_dir or "",
                config=config,
                language=lang,
                progress_cb=lambda text: self.msg_q.put(text),
                apply_font_patch=font,
                apply_language_ui=lang_ui,
                extra_terms=keep_terms,
                pause_event=pause_event,
            )
            prefix = "PAUSED|" if result.paused else "RESULT|"
            self.msg_q.put(prefix + result.message)
        except Exception as exc:
            import traceback
            self.msg_q.put("ERR|%s" % exc)
            try:
                with open("_gui_worker_err.txt", "a", encoding="utf-8") as f:
                    traceback.print_exc(file=f)
            except Exception:
                pass
        finally:
            self.msg_q.put("__done__")

    # -- 队列轮询（UI 线程） -------------------------------------------------

    def _busy(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def _ensure_timer(self) -> None:
        """确保 UI 轮询定时器正在运行（每次启动后台任务时调用）。

        DispatcherTimer 空闲时会被 on_tick 停掉以节省资源；
        若不在此重新 Start，队列中的消息将永远不会被 UI 消费，
        界面会卡在“准备中 / 测试中”不刷新。
        """
        try:
            if self._timer is not None:
                self._timer.Start()
        except Exception as exc:
            self._append_log("定时器重启失败: %s" % exc, "err")

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self.msg_q.get_nowait()
                if msg == "__done__":
                    self._on_done()
                    continue
                if msg.startswith("TEST_OK|"):
                    self._append_log(msg[8:], "ok")
                    self.StatusText.Text = "连接成功"
                    continue
                if msg.startswith("TEST_ERR|"):
                    self._append_log(msg[9:], "err")
                    self.StatusText.Text = "连接失败"
                    continue
                if msg.startswith("RESULT|"):
                    self._last_result = msg[7:]
                    continue
                if msg.startswith("PAUSED|"):
                    self._last_result = msg[7:]
                    self._paused_mode = True
                    continue
                if msg.startswith("ERR|"):
                    self._append_log(msg[4:], "err")
                    continue
                if msg.startswith("PROGRESS|"):
                    try:
                        self._update_progress(int(msg.split("|")[1]))
                    except (ValueError, IndexError):
                        pass
                    continue
                self._append_log(msg)
        except queue.Empty:
            pass

    def _update_progress(self, pct: int) -> None:
        """对接成功后：把“就绪”替换为实时百分比进度 + 预计剩余时间。"""
        pct = max(0, min(100, pct))
        self.Progress.IsIndeterminate = False
        self.Progress.Value = pct
        # 进度只增不减（流水线分阶段上报时可能回退），保证 ETA 单调收敛
        if pct > getattr(self, "_eta_max_pct", 0):
            self._eta_max_pct = pct
        eta = self._estimate_remaining(pct)
        self.ProgressText.Text = "%d%%%s" % (
            pct, " · 剩余约 %s" % eta if eta else "")

    def _estimate_remaining(self, pct: int) -> str | None:
        """根据已耗时与当前进度估算剩余时间，指数平滑抑制抖动。

        - 起点取任务启动时刻（含解包/反编译等前期阶段），避免整体低估；
        - 进度过早（<3%）或刚起步（<5 秒）时估算极不稳定，暂不显示；
        - 用历史值的指数加权平均（0.7 旧 + 0.3 新）平滑吞吐波动。
        """
        start = getattr(self, "_eta_start", None)
        if start is None or pct >= 100:
            return None
        elapsed = time.monotonic() - start
        if elapsed < 5 or pct < 3:
            return None
        raw = elapsed * (100 - pct) / pct
        prev = getattr(self, "_eta_smooth", None)
        smooth = raw if prev is None else prev * 0.7 + raw * 0.3
        self._eta_smooth = smooth
        return self._format_duration(smooth)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """把秒数格式化为人类友好的时长，如 ``1时02分`` / ``3分20秒`` / ``45秒``。"""
        total = max(1, int(round(seconds)))
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h:
            return "%d时%02d分" % (h, m)
        if m:
            return "%d分%02d秒" % (m, s)
        return "%d秒" % s

    def OnPause(self, sender, e) -> None:
        """暂停：立即置位信号，翻译线程在当前请求完成的间隙停止。"""
        if self._test_mode or self.worker is None:
            return
        self._pause_event.set()
        self.PauseBtn.IsEnabled = False
        self.StatusText.Text = "正在暂停…"
        self._append_log(
            "已请求暂停：等待当前请求完成即停止，已译内容自动保存，"
            "未完成段落将写入暂停标记。", "info")

    def _on_done(self) -> None:
        self.worker = None
        self._set_busy(False)
        # 清空 ETA 状态，避免下次任务继承上次的起点/平滑值
        self._eta_start = None
        self._eta_smooth = None
        self._eta_max_pct = 0
        if self._test_mode:
            self._test_mode = False
            self.ProgressText.Text = "就绪"
            self.Progress.Value = 0
            return
        if self._audit_mode:
            self._audit_mode = False
            self.ProgressText.Text = "就绪"
            self.Progress.Value = 0
            return
        if getattr(self, "_last_result", None):
            self._append_log(self._last_result, "ok")
            self._last_result = None
        if getattr(self, "_paused_mode", False):
            self._paused_mode = False
            self._append_log(
                "已暂停。关闭程序后重新打开，点击「开始汉化」会自动从"
                "暂停标记处继续，无需从头开始。", "info")
            self.ProgressText.Text = "已暂停"
            self.StatusText.Text = "已暂停（可从断点继续）"
            return
        self.ProgressText.Text = "完成"
        self.StatusText.Text = "完成"

    def _set_busy(self, busy: bool) -> None:
        self.StartBtn.IsEnabled = not busy
        self.TestBtn.IsEnabled = not busy
        # 暂停按钮只在翻译任务运行中可用（连接测试/补漏查缺不适用）
        self.PauseBtn.IsEnabled = busy and not self._test_mode and not self._audit_mode
        if not busy:
            self._pause_event = threading.Event()
        self.BrowseBtn.IsEnabled = not busy
        self.SaveBtn.IsEnabled = not busy
        self.AuditBtn.IsEnabled = not busy
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
