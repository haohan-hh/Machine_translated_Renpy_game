# -*- coding: utf-8 -*-
"""
运行时翻译验证与自动补译。

背景（Dawn Chorus 等项目实测发现的盲区）：
汉化工具按「反编译脚本」提取文本并生成翻译，但它只能验证
「提取到的文本是否都翻完了」，无法验证「游戏运行时实际显示的所有对话
是否都有翻译」。个别对话可能因为
  1. say 的属性写法特殊（如 ``Mikko strained "..."`` 的裸词属性），
  2. 反编译/提取的 identifier 与官方编译器算出的不一致，
  3. 特殊语法（NVL 段落、多行字符串、块结构）覆盖不到，
而从未被提取/翻译——工具自认为 100% 完成，游戏里却仍是英文
（表现为「部分角色中文、部分角色英文」）。

本模块提供闭环验证：
1. ``write_verification_patch()`` —— 汉化完成后向游戏注入
   ``game/zz_verify_translations.rpy``（init 999，可安全删除）。
   游戏启动时由 Ren'Py 读取自身的对话翻译注册表
   ``renpy.game.script.translator.default_translates``（全部对话，
   实测为 dict[identifier -> TranslateSay/Translate]）与
   ``language_translates``（dict[(identifier, 语言) -> 翻译]）做精确比对，
   把「运行时实际缺少指定语言翻译」的对话写入
   ``game/tl/<语言>.运行时缺失报告.txt``。
2. ``build_missing_fix()`` —— 再次运行汉化工具时若发现该报告，
   自动为其中仍有实际文本的对话补译（优先复用现有 tl 译文，
   缺失的走 AI），按运行时 identifier 生成
   ``tl/<语言>/zz_missing_fix.rpy``，保证补丁逐条命中。

补充经验固化：
- 空对话 / ``!`` / ``...`` / ``{nw}`` / 纯标签 / 纯换行段等无实际文字的
  「噪音」条目不会被误当作漏译（``is_noise_text``）。
- 多行（NVL）文本补译时只翻译去空前导空行的正文，保留原换行结构，
  显著提高 AI 保留 ``\\n`` 的成功率（此前整段翻译极易丢失占位符）。
- 带有 voice 等其他语句或多句 Say 的 ``Translate`` 保留给人工处理。
  把这类翻译单元改成孤立的一句对话会丢失游戏行为。
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .extract import encode_say_string
from .patcher import PatchResult, monitored_settings_screens
from .tl_files import (TranslationMergeError, editable_output_path, index_tl_directory,
                       unambiguous_translations, write_if_unchanged)
from .translator import (PauseRequested, TranslationError, target_language_name,
                         unprotected_text, valid_unchanged_text)

# 运行时验证补丁文件名（game 根目录；zz_ 前缀是工具自动生成的约定，
# engine 扫描 / 更新脚本时会跳过）
RUNTIME_PATCH_NAME = "zz_verify_translations.rpy"
# Language-switch probe files are separate from the dialogue-missing checker.
# The probe observes the screen action and checks the active translation.
LANGUAGE_SWITCH_PATCH_NAME = "zz_language_switch_check.rpy"
LANGUAGE_SWITCH_PROBE_FILE_NAME = "zz_language_switch_probe.rpy"
LANGUAGE_SWITCH_REPORT_TEMPLATE = "{lang}.语言切换验证报告.txt"
LANGUAGE_SWITCH_PROBE_TEXT = "__RPYTRANSLATOR_LANGUAGE_SWITCH_PROBE__"
LANGUAGE_SWITCH_PROBE_TRANSLATION = "语言切换验证成功"
# 运行时缺失报告文件名（放 tl/ 下，随汉化产物一起管理）
RUNTIME_REPORT_TEMPLATE = "{lang}.运行时缺失报告.txt"
# 自动修复补丁文件名（tl/<语言>/ 下）
FIX_FILE_NAME = "zz_missing_fix.rpy"

# 报告行：file:line \t [who] \t what \t identifier \t 类型
_TAB = "\t"
_TITLE_ROWS = 3  # 前 3 行为统计标题

# Ren'Py 文本标签 {…} / 插值 […]（与 translator 一致）
_TAG_RE = re.compile(r"\{[^{}]*\}")
_INSERT_RE = re.compile(r"\[[^\[\]]*\]")

# ---------------------------------------------------------------------------
# 无实际文字的“噪音”判定
# ---------------------------------------------------------------------------

def is_noise_text(what: str | None) -> bool:
    """判断对话文本是否没有实际可翻译内容（空 / 符号 / 纯标签 / 空行段）。

    Ren'Py 中 ``""``（空停顿）、``!``、``...``、``{nw}``、纯 ``\\n``
    （NVL 留白）等对话不显示文字，未翻译也不会造成“部分英文”，
    验证与自动修复都应跳过它们。
    """
    w = (what or "").strip()
    if not w:
        return True
    w = w.replace("{nw}", "").strip()
    if not w:
        return True
    if re.fullmatch(r"[!?….\s，。、；：\-—()（）]+", w):
        return True
    plain = _TAG_RE.sub("", _INSERT_RE.sub("", w)).strip()
    if not plain:                      # 纯标签/插值，如 {size=+2} [page]
        return True
    return False


# ---------------------------------------------------------------------------
# 运行时验证补丁
# ---------------------------------------------------------------------------

# 内嵌的 Ren'Py 脚本（init 999）。__LANG__ 在生成时替换为目标语言。
# 兼容 Ren'Py 7.4+ / 8.x：8.x 的翻译注册表位于
#   renpy.game.script.translator (ScriptTranslator)
# 7.x 早期版本位于 renpy.game.script.translations；脚本对两者做探测回退。
_RUNTIME_PATCH = r'''# -*- coding: utf-8 -*-
# 运行时翻译缺失诊断（汉化工具自动生成，可安全删除）。
# 游戏启动时比对 Ren'Py 对话翻译注册表，把运行时实际缺少 __LANG__ 翻译的
# 对话写入 tl/__LANG__.运行时缺失报告.txt；再次运行汉化工具可据此自动补译。
init 999 python:
    import io as _vv_io
    import os as _vv_os
    _vv_lang = '__LANG__'
    _vv_path = _vv_os.path.join(config.gamedir, 'tl',
                                _vv_lang + '.运行时缺失报告.txt')
    def _vv_write_report():
        # 兼容两种注册表位置（8.x: script.translator；7.x: script.translations）
        _vv_tr = None
        _vv_script = renpy.game.script
        if hasattr(_vv_script, 'translator'):
            _vv_tr = _vv_script.translator
        elif hasattr(_vv_script, 'translations'):
            _vv_tr = _vv_script.translations
        if _vv_tr is None:
            _vv_tr = renpy.translation
        _vv_def = getattr(_vv_tr, 'default_translates', None)
        if _vv_def is None:
            raise Exception('no default_translates')
        _vv_lng = getattr(_vv_tr, 'language_translates', None)
        if not hasattr(_vv_lng, '__contains__'):
            raise ValueError('no language_translates registry')
        _vv_rows = []
        _vv_miss = 0
        for _vv_code in _vv_def:
            if (_vv_code, _vv_lang) in _vv_lng:
                continue
            _vv_node = _vv_def[_vv_code]
            _vv_who = getattr(_vv_node, 'who', None)
            _vv_what = getattr(_vv_node, 'what', '')
            _vv_kind = type(_vv_node).__name__
            if _vv_kind == 'Translate':
                _vv_blk = getattr(_vv_node, 'block', None)
                if _vv_blk:
                    _vv_says = [_vv_e for _vv_e in _vv_blk if type(_vv_e).__name__ == 'Say']
                    if _vv_says:
                        _vv_who = getattr(_vv_says[0], 'who', None)
                        _vv_what = getattr(_vv_says[0], 'what', '')
                        # Preserve any other executable statements (e.g. voice).
                        _vv_kind = ('TranslateSay' if len(_vv_blk) == 1
                                    else 'TranslateComplex')
                        _vv_node_say = _vv_says[0]
            else:
                _vv_node_say = _vv_node
            if _vv_kind == 'TranslateSay' and (
                    getattr(_vv_node_say, 'attributes', None) or
                    getattr(_vv_node_say, 'temporary_attributes', None) or
                    getattr(_vv_node_say, 'with_', None) or
                    getattr(_vv_node_say, 'arguments', None) or
                    not getattr(_vv_node_say, 'interact', True)):
                _vv_kind = 'TranslateComplex'
            if _vv_what is None:
                _vv_what = ''
            _vv_what2 = str(_vv_what).replace('\n', '\\n').replace('\t', ' ')
            _vv_who2 = '[' + (str(_vv_who) if _vv_who is not None else '') + ']'
            _vv_fn = str(getattr(_vv_node, 'filename', '') or '')
            _vv_ln = getattr(_vv_node, 'linenumber', 0) or 0
            _vv_rows.append('%s:%s\t%s\t%s\t%s\t%s' % (
                _vv_fn, _vv_ln, _vv_who2, _vv_what2, _vv_code,
                _vv_kind))
            _vv_miss += 1
        _vv_dir = _vv_os.path.dirname(_vv_path)
        if not _vv_os.path.isdir(_vv_dir):
            _vv_os.makedirs(_vv_dir)
        with _vv_io.open(_vv_path, 'w', encoding='utf-8') as _vv_f:
            _vv_f.write('total dialogue ids: %d\n' % len(_vv_def))
            _vv_f.write('missing ' + _vv_lang + ' translation: %d\n' % _vv_miss)
            _vv_f.write('---- missing list (file:line [who] what id type) ----\n')
            if _vv_rows:
                _vv_f.write('\n'.join(_vv_rows))

    def _vv_check_loaded_language():
        try:
            _vv_write_report()
        except Exception as _vv_exc:
            renpy.log('Runtime translation report could not be written: %r' % (_vv_exc,))

    if getattr(config, 'defer_tl_scripts', False):
        # At init the target table may still be empty. This callback runs
        # after Ren'Py loads the selected language's scripts.
        def _vv_after_language_change():
            if _preferences.language == _vv_lang:
                _vv_check_loaded_language()
        config.change_language_callbacks.append(_vv_after_language_change)
    else:
        _vv_check_loaded_language()
'''


def runtime_report_path(game_dir: Path, language: str) -> Path:
    return game_dir / "tl" / RUNTIME_REPORT_TEMPLATE.format(lang=language)


def write_verification_patch(game_dir: Path, language: str = "schinese",
                             log_cb=None) -> PatchResult:
    """向游戏注入运行时验证补丁，返回结果（已存在且内容一致 → skip）。"""
    res = PatchResult()
    patch = game_dir / RUNTIME_PATCH_NAME
    content = _RUNTIME_PATCH.replace("__LANG__", language)
    if patch.is_file():
        try:
            if patch.read_text(encoding="utf-8-sig", errors="ignore") == content:
                res.ok = True
                res.skip = True
                res.message = "运行时翻译验证补丁已存在，跳过"
                return res
        except OSError:
            pass
    try:
        patch.write_text(content, encoding="utf-8-sig")
    except OSError as e:
        res.ok = False
        res.message = f"写运行时验证补丁失败: {e}"
        return res
    res.files.append(patch)
    res.ok = True
    res.detail = (f"启动一次游戏后，若运行时仍有对话缺失翻译，工具会生成 "
                  f"tl/{language}.运行时缺失报告.txt；再次汉化即可自动补译")
    res.message = "已注入运行时翻译验证补丁（游戏启动时自动检查漏译）"
    if log_cb:
        log_cb("  ✓ " + res.message)
        log_cb("    - " + res.detail)
    return res


# This script is intentionally generated instead of importing application
# internals. It exercises the same public language-switch API as the screen
# action and avoids depending on Ren'Py's private translation registries.
_LANGUAGE_SWITCH_PATCH = r'''# -*- coding: utf-8 -*-
# 语言切换链路验证（汉化工具自动生成，可安全删除）。
# 打开设置屏幕后被 timer 调用；不主动切换语言，只观察用户选择后
# 的偏好状态，并用 Ren'Py 官方 API 验证目标语言的探针译文。
init 999 python:
    import io as _ls_io
    import os as _ls_os
    import hashlib as _ls_hash
    _ls_lang = __LANG_REPR__
    _ls_probe = __PROBE_REPR__
    _ls_expected = __TRANSLATION_REPR__
    _ls_screen_names = __SCREEN_NAMES__
    _ls_path = _ls_os.path.join(
        config.gamedir, 'tl', _ls_lang + '.语言切换验证报告.txt')
    _ls_ui_path = _ls_os.path.join(config.gamedir, 'zz_language_ui.rpy')
    _ls_ui_signature = __UI_SIGNATURE__
    _ls_state = {'last_report': None, 'ui_action': None, 'known_languages': None,
                 'request_id': None, 'selection_ticks': 0, 'finished': None}

    def _ls_current_language():
        try:
            return getattr(_preferences, 'language', None)
        except Exception:
            try:
                return getattr(renpy.game.preferences, 'language', None)
            except Exception:
                return None

    def _ls_known_languages():
        if _ls_state['known_languages'] is not None:
            return _ls_state['known_languages']
        _ls_values = []
        _ls_api = False
        for _ls_provider in (getattr(renpy, 'translation', None), renpy):
            if _ls_provider is None:
                continue
            for _ls_name in ('known_languages', 'get_languages'):
                _ls_fn = getattr(_ls_provider, _ls_name, None)
                if _ls_fn is None:
                    continue
                try:
                    _ls_values.extend(list(_ls_fn() or []))
                    _ls_api = True
                except Exception:
                    continue
        try:
            _ls_tl = _ls_os.path.join(config.gamedir, 'tl')
            if not _ls_api and _ls_os.path.isdir(_ls_tl):
                for _ls_name in _ls_os.listdir(_ls_tl):
                    _ls_path_candidate = _ls_os.path.join(_ls_tl, _ls_name)
                    if _ls_os.path.isdir(_ls_path_candidate):
                        _ls_values.append(_ls_name)
        except Exception:
            pass
        _ls_unique = []
        for _ls_value in _ls_values:
            if _ls_value is None:
                continue
            _ls_name = str(_ls_value)
            if _ls_name and _ls_name.lower() != 'none' and _ls_name not in _ls_unique:
                _ls_unique.append(_ls_name)
        _ls_result = (sorted(_ls_unique), (_ls_api or bool(_ls_unique)))
        if _ls_lang in _ls_unique:
            _ls_state['known_languages'] = _ls_result
        return _ls_result

    def _ls_write(_ls_status, _ls_before, _ls_after, _ls_translated,
                  _ls_active_translated, _ls_known, _ls_visible_screens,
                  _ls_ui_action, _ls_requested, _ls_request_screen, _ls_error=''):
        try:
            _ls_dir = _ls_os.path.dirname(_ls_path)
            if not _ls_os.path.isdir(_ls_dir):
                _ls_os.makedirs(_ls_dir)
            with _ls_io.open(_ls_path, 'w', encoding='utf-8') as _ls_f:
                _ls_f.write('status: %s\n' % _ls_status)
                _ls_f.write('target_language: %s\n' % _ls_lang)
                _ls_f.write('known_languages: %r\n' % (_ls_known,))
                _ls_f.write('language_before: %r\n' % (_ls_before,))
                _ls_f.write('language_after_switch: %r\n' % (_ls_after,))
                _ls_f.write('probe_translation_target: %r\n' % (_ls_translated,))
                _ls_f.write('probe_translation_active: %r\n' %
                            (_ls_active_translated,))
                _ls_f.write('preferences_screen_visible: %r\n' %
                            (bool(_ls_visible_screens),))
                _ls_f.write('visible_settings_screens: %r\n' %
                            (_ls_visible_screens,))
                _ls_f.write('ui_language_action_present: %r\n' % (_ls_ui_action,))
                _ls_f.write('button_requested_language: %r\n' % (_ls_requested,))
                _ls_f.write('button_screen: %r\n' % (_ls_request_screen,))
                _ls_f.write('ui_click_simulated: False\n')
                _ls_f.write('ui_signature: %s\n' % _ls_ui_signature)
                _ls_f.write('check_scope: records a requested UI language and verifies the active preference and probe translation\n')
                if _ls_error:
                    _ls_f.write('error: %s\n' % _ls_error)
            return True
        except Exception as _ls_exc:
            try:
                renpy.log('Language-switch verification report could not be written: %r' %
                          (_ls_exc,))
            except Exception:
                pass
            return False

    def _ls_observe_language_switch():
        _ls_current = _ls_current_language()
        try:
            _ls_id, _ls_screen, _ls_before, _ls_requested = _rpytranslator_language_request
        except (NameError, ValueError, TypeError):
            _ls_id, _ls_screen, _ls_before, _ls_requested = 0, None, None, None
        if _ls_id != _ls_state['request_id']:
            _ls_state.update(request_id=_ls_id, selection_ticks=0, finished=None)
        _ls_translated = None
        _ls_active_translated = None
        _ls_known, _ls_known_api = _ls_known_languages()
        _ls_visible_screens = []
        _ls_ui_action = _ls_state['ui_action']
        _ls_error = ''
        try:
            for _ls_name in _ls_screen_names:
                try:
                    _ls_visible = renpy.get_screen(_ls_name)
                    if _ls_visible and getattr(_ls_visible, 'name', _ls_name) == _ls_name:
                        _ls_visible_screens.append(_ls_name)
                except Exception:
                    pass
            if _ls_ui_action is None:
                try:
                    with _ls_io.open(_ls_ui_path, 'rb') as _ls_ui_f:
                        _ls_ui_action = (_ls_hash.sha256(_ls_ui_f.read()).hexdigest()[:16]
                                         == _ls_ui_signature)
                except Exception:
                    _ls_ui_action = False
                _ls_state['ui_action'] = _ls_ui_action
            if not _ls_visible_screens or not _ls_ui_action:
                _ls_error = 'injected preferences screen or Language action is missing'
            elif not _ls_known_api:
                _ls_error = 'known_languages API unavailable'
            elif _ls_lang not in _ls_known:
                _ls_error = 'target language is not registered'
            elif _ls_current == _ls_lang or not getattr(config, 'defer_tl_scripts', False):
                # The synthetic strings entry checks that Ren'Py loaded a
                # real translation. Deferred scripts only load on selection.
                _ls_translated = renpy.translate_string(
                    _ls_probe, language=_ls_lang)
                if _ls_current == _ls_lang:
                    _ls_active_translated = renpy.translate_string(_ls_probe)
                if _ls_translated != _ls_expected:
                    _ls_error = 'target probe does not match the installed translation'
                elif (_ls_current == _ls_lang and
                      _ls_active_translated != _ls_translated):
                    _ls_error = 'active language does not return the target translation'
        except Exception as _ls_exc:
            _ls_error = repr(_ls_exc)
        if _ls_error:
            _ls_status = 'ERROR' if 'API unavailable' in _ls_error else 'FAIL'
        elif (_ls_requested == _ls_lang and _ls_screen in _ls_visible_screens
                and _ls_before != _ls_lang):
            if _ls_state['finished'] == 'FAIL':
                _ls_status = 'FAIL'
                _ls_error = 'language button selection did not activate the target language'
            elif _ls_state['finished'] == 'expired':
                _ls_status = 'READY'
            elif _ls_current == _ls_lang:
                _ls_status = 'PASS'
                _ls_state['finished'] = 'PASS'
            elif _ls_state['finished'] == 'PASS':
                _ls_state['finished'] = 'expired'
                _ls_status = 'READY'
            else:
                _ls_state['selection_ticks'] += 1
                if _ls_state['selection_ticks'] >= 4:
                    _ls_status = _ls_state['finished'] = 'FAIL'
                    _ls_error = 'language button selection did not activate the target language'
                else:
                    _ls_status = 'READY'
        else:
            if _ls_screen and _ls_screen not in _ls_visible_screens:
                _ls_state['finished'] = 'expired'
            _ls_status = 'READY'
        _ls_report_key = (_ls_status, _ls_id, _ls_current, _ls_visible_screens,
                          _ls_translated, _ls_active_translated, _ls_error)
        if _ls_report_key == _ls_state['last_report']:
            return
        if _ls_write(_ls_status, _ls_before, _ls_current, _ls_translated,
                     _ls_active_translated, _ls_known, _ls_visible_screens,
                     _ls_ui_action, _ls_requested, _ls_screen, _ls_error):
            _ls_state['last_report'] = _ls_report_key

    # Supersede the prior session even if the user never opens settings.
    _ls_write('WAITING', None, _ls_current_language(), None, None,
              [], [], False, None, None)
'''


def language_switch_report_path(game_dir: Path, language: str) -> Path:
    """Return the startup report path for the language-switch probe."""
    return game_dir / "tl" / LANGUAGE_SWITCH_REPORT_TEMPLATE.format(lang=language)


def _ui_signature(ui_patch: Path) -> str:
    try:
        return hashlib.sha256(ui_patch.read_bytes()).hexdigest()[:16]
    except OSError:
        return "missing"


def read_language_switch_report(path: Path, language: str | None = None,
                                ui_signature: str | None = None,
                                screen_names: list[str] | None = None) -> dict[str, str]:
    """Read a report, rejecting mismatched installations and incomplete PASS rows."""
    result: dict[str, str] = {}
    try:
        for line in Path(path).read_text(
                encoding="utf-8-sig", errors="ignore").splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip():
                result[key.strip()] = value.strip()
    except OSError:
        return {}
    if ((language is not None and result.get("target_language") != language) or
            (ui_signature is not None and result.get("ui_signature") != ui_signature)):
        return {}
    if result.get("status") == "PASS":
        try:
            target = result["target_language"]
            before = ast.literal_eval(result["language_before"])
            after = ast.literal_eval(result["language_after_switch"])
            requested = ast.literal_eval(result["button_requested_language"])
            screen = ast.literal_eval(result["button_screen"])
            visible = ast.literal_eval(result["visible_settings_screens"])
            translated = ast.literal_eval(result["probe_translation_target"])
            active = ast.literal_eval(result["probe_translation_active"])
            if not (before != target == after == requested and
                    active == translated == LANGUAGE_SWITCH_PROBE_TRANSLATION and
                    result["preferences_screen_visible"] == "True" and
                    result["ui_language_action_present"] == "True" and
                    isinstance(visible, list) and isinstance(screen, str) and
                    screen in visible and (screen_names is None or screen in screen_names)):
                return {}
        except (ValueError, TypeError, KeyError, SyntaxError):
            return {}
    return result


def install_language_switch_probe(game_dir: Path, language: str,
                                  log_cb=None) -> PatchResult:
    """Install a monitor only for settings screens that record real clicks."""
    game_dir = Path(game_dir)
    ui_patch = game_dir / "zz_language_ui.rpy"
    try:
        source = ui_patch.read_text(encoding="utf-8-sig") if ui_patch.is_file() else ""
        screens = monitored_settings_screens(source)
    except (OSError, UnicodeError) as exc:
        return PatchResult(message=f"无法检查语言设置界面: {exc}")
    if not screens:
        result = PatchResult(ok=True, skip=True,
                             message="未检测到工具生成的语言按钮，跳过其运行时验证")
        try:
            # When an update supplies native controls, an old monitor would
            # keep replacing its historical result on every subsequent boot.
            monitor = game_dir / LANGUAGE_SWITCH_PATCH_NAME
            if monitor.is_file() and monitor.read_text(encoding="utf-8-sig").startswith(
                    "# -*- coding: utf-8 -*-\n# 语言切换链路验证（汉化工具自动生成"):
                monitor.with_suffix(".rpyc").unlink(missing_ok=True)
                monitor.unlink()
        except (OSError, UnicodeError) as exc:
            return PatchResult(message=f"无法停用旧语言切换探针: {exc}")
        if log_cb:
            log_cb(result.message)
        return result
    result = write_language_switch_probe(game_dir, language, screens, log_cb)
    if result.ok and log_cb:
        report = read_language_switch_report(
            language_switch_report_path(game_dir, language), language,
            _ui_signature(ui_patch), screens)
        status = report.get("status")
        if status == "PASS":
            log_cb("上次语言切换已通过验证")
        elif status in {"FAIL", "ERROR"}:
            log_cb(f"上次语言切换验证失败: {report.get('error', status)}")
        elif status == "READY":
            log_cb(f"已打开游戏设置，请从其他语言切换到 {language} 以验证切换结果")
        else:
            log_cb("等待在游戏设置中验证语言切换")
    return result


def write_language_switch_probe(game_dir: Path, language: str = "schinese",
                                screen_names: list[str] | None = None,
                                log_cb=None) -> PatchResult:
    """Install an idempotent runtime probe for the injected language UI.

    The probe translation is deliberately a generated string. A timer in the
    injected preferences overlay records readiness, then verifies the actual
    preference change after the player uses the official Language action.
    This avoids silently switching the language during game startup.
    """
    res = PatchResult()
    game_dir = Path(game_dir)
    if not re.fullmatch(r"[A-Za-z0-9_]+", str(language)):
        res.message = "语言切换验证失败：语言代码格式无效"
        return res
    patch = game_dir / LANGUAGE_SWITCH_PATCH_NAME
    tl_dir = game_dir / "tl" / language
    ui_patch = game_dir / "zz_language_ui.rpy"
    ui_signature = _ui_signature(ui_patch)
    valid_screens = [name for name in (screen_names or ["preferences"])
                     if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name))]
    if not valid_screens:
        valid_screens = ["preferences"]
    patch_text = (_LANGUAGE_SWITCH_PATCH
                  .replace("__LANG_REPR__", repr(language))
                  .replace("__PROBE_REPR__", repr(LANGUAGE_SWITCH_PROBE_TEXT))
                  .replace("__TRANSLATION_REPR__", repr(LANGUAGE_SWITCH_PROBE_TRANSLATION))
                  .replace("__SCREEN_NAMES__", repr(valid_screens))
                  .replace("__UI_SIGNATURE__", repr(ui_signature)))
    probe_text = (
        "# 语言切换探针（汉化工具自动生成）\n"
        f"translate {language} strings:\n"
        f"    old {LANGUAGE_SWITCH_PROBE_TEXT!r}\n"
        f"    new {LANGUAGE_SWITCH_PROBE_TRANSLATION!r}\n"
    )
    try:
        patch.parent.mkdir(parents=True, exist_ok=True)
        tl_dir.mkdir(parents=True, exist_ok=True)
        patch_changed = (not patch.is_file() or
                         patch.read_text(encoding="utf-8-sig", errors="ignore")
                         != patch_text)
        docs = index_tl_directory(tl_dir, language)
        owners = [(doc, entry) for doc in docs.values() for entry in doc.entries
                  if entry.identifier is None and entry.original == LANGUAGE_SWITCH_PROBE_TEXT]
        if len(owners) > 1:
            return PatchResult(message="语言探针字符串已有多个定义，请检查目标语言的译文")
        if owners and owners[0][1].translation != LANGUAGE_SWITCH_PROBE_TRANSLATION:
            return PatchResult(message=f"语言探针字符串与已有译文冲突：{owners[0][0].path}")
        probe_changed = not owners
        if probe_changed:
            probe_file = editable_output_path(tl_dir / LANGUAGE_SWITCH_PROBE_FILE_NAME, docs)
            old = docs.get(probe_file)
            if old:
                probe_text = "".join(old.lines) + old.newline * 2 + probe_text.replace("\n", old.newline)
            write_if_unchanged(probe_file, probe_text, old)
            res.files.append(probe_file)
        if patch_changed:
            patch.write_text(patch_text, encoding="utf-8-sig")
            res.files.append(patch)
        if patch_changed or probe_changed:
            # Do not report a result from an older version of the injected
            # check as though it validated the new files.
            try:
                language_switch_report_path(game_dir, language).unlink()
            except FileNotFoundError:
                pass
    except (OSError, TranslationMergeError) as exc:
        res.ok = False
        res.message = f"写语言切换验证探针失败: {exc}"
        return res
    res.ok = True
    res.skip = not bool(res.files)
    res.message = ("语言切换验证探针已存在，跳过" if res.skip else
                   "已注入语言切换自动验证探针")
    res.detail = (f"在游戏设置中打开语言选项后检查 "
                  f"{language_switch_report_path(game_dir, language).name}；"
                  f"从其他语言切换到 {language} 后会记录结果")
    if log_cb:
        log_cb("  " + ("- " if res.skip else "✓ ") + res.message)
        log_cb("    - " + res.detail)
    return res


# ---------------------------------------------------------------------------
# 缺失报告解析
# ---------------------------------------------------------------------------

@dataclass
class MissingItem:
    filename: str
    line: int
    who: str
    what: str
    identifier: str
    node_type: str          # TranslateSay / Translate

    @property
    def is_say(self) -> bool:
        """单句对话（可安全自动补译）；Translate 为多句块，只读不写。"""
        return self.node_type == "TranslateSay"


def parse_runtime_report(path: Path, language: str | None = None) -> list[MissingItem]:
    """Parse the dialogue report and reject incomplete or misdirected snapshots."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    header = re.fullmatch(r"missing (\w+) translation: (\d+)", lines[1]) if len(lines) >= _TITLE_ROWS else None
    if (not header or not re.fullmatch(r"total dialogue ids: \d+", lines[0])
            or not lines[2].startswith("---- missing list")
            or (language is not None and header[1] != language)):
        raise ValueError("报告标题或目标语言不匹配")
    items: list[MissingItem] = []
    for ln in lines[_TITLE_ROWS:]:
        parts = ln.split(_TAB)
        if len(parts) < 5 or not re.fullmatch(r"\w+", parts[-2]):
            raise ValueError("报告中存在不完整的对话记录")
        # 结构固定为: 文件:行 / [who] / what / id / 类型；正文里的制表符
        # 已被补丁替换为空格，仍可能残留极端情况，故取前后列、正文段合并。
        where, who = parts[0], parts[1]
        identifier, node_type = parts[-2], parts[-1]
        what = _TAB.join(parts[2:-2])
        who = who.strip("[]")
        what = what.replace("\\n", "\n")
        fname, _, line_s = where.rpartition(":")
        try:
            line = int(line_s)
        except ValueError:
            line = 0
        items.append(MissingItem(
            filename=fname, line=line, who=who, what=what,
            identifier=identifier, node_type=node_type.strip() or "TranslateSay"))
    if len(items) != int(header[2]):
        raise ValueError("缺失对话总数与报告行数不符")
    return items


# ---------------------------------------------------------------------------
# 已有译文反查（原文 → 译文）
# ---------------------------------------------------------------------------

def load_tl_dialogue_map(tl_lang_dir: Path, language: str) -> dict[str, str]:
    """加载 tl/<语言> 中全部对话翻译：{原文(去首尾空白): 译文}。

    原文取自翻译块的注释行（# who "原文"），译文取自块内首个非注释行；
    与生成器输出格式一致，也兼容官方/手工格式。多行文本以还原后的
    真实换行存储。
    """
    return _reusable_dialogue(index_tl_directory(tl_lang_dir, language).values())


def _reusable_dialogue(docs) -> dict[str, str]:
    mapping = unambiguous_translations(
        (entry.original.strip(), entry.translation.strip())
        for doc in docs for entry in doc.entries
        if entry.identifier and entry.original and entry.translation)
    return {src: dst for src, dst in mapping.items() if dst and dst != src}


# ---------------------------------------------------------------------------
# 自动补译修复补丁
# ---------------------------------------------------------------------------

def build_missing_fix(
    game_dir: Path,
    language: str = "schinese",
    client=None,
    names: list[str] | None = None,
    log_cb=None,
) -> PatchResult:
    """根据运行时缺失报告生成 tl/<语言>/zz_missing_fix.rpy。

    规则（实测经验固化，避免踩坏已生效翻译）：
    - 单句只有人名、插值或符号时保留原文，也为其运行时标识注册对应条目。
    - 简单单句对话 → 优先复用现有 tl 译文；
      无译文则 AI 翻译；多行文本先拆出前导空白再翻正文。
    - 无 Say 的 Translate 块（纯 UserStatement 段落）→ 无文字，跳过。
    幂等：已生成的 zz_missing_fix.rpy 会先被解析合并，重复运行不丢旧修复。
    """
    res = PatchResult()
    report = runtime_report_path(game_dir, language)
    if not report.is_file():
        res.ok = True
        res.skip = True
        res.message = "未发现运行时缺失报告，跳过自动补译"
        if log_cb:
            log_cb("  - " + res.message)
        return res

    tl_dir = game_dir / "tl" / language
    try:
        items = parse_runtime_report(report, language)
    except (OSError, ValueError) as exc:
        return PatchResult(message=f"无法读取运行时缺失报告 {report.name}: {exc}")
    if not items and not any(tl_dir.glob(Path(FIX_FILE_NAME).stem + "*.rpy")):
        return PatchResult(ok=True, skip=True, message=f"运行时缺失报告无条目：{report.name}")
    if log_cb:
        log_cb(f"  - 运行时缺失报告：共 {len(items)} 条")

    try:
        docs = index_tl_directory(tl_dir, language)
    except (OSError, TranslationMergeError) as exc:
        return PatchResult(message=f"无法核对已有补译: {exc}")
    fix_file = editable_output_path(tl_dir / FIX_FILE_NAME, docs)
    old_fix = docs.get(fix_file)
    installed = {entry.identifier for path, doc in docs.items() if path != fix_file
                 for entry in doc.entries if entry.identifier}
    reusable = _reusable_dialogue(docs.values())
    lines = list(old_fix.lines) if old_fix else []
    claimed = set(installed)
    if old_fix:
        for entry in reversed(old_fix.entries):
            if entry.identifier is None:
                continue
            if entry.identifier in installed:
                del lines[entry.block_start:entry.block_end]
            claimed.add(entry.identifier)

    target = target_language_name(language)
    protected = set(names or ()) | set(getattr(client, "detected_names", ()))
    pending: dict[str, list[MissingItem]] = {}
    resolved: list[tuple[MissingItem, str]] = []
    reused = kept = unsupported = 0
    for item in items:
        if item.identifier in claimed:
            continue
        claimed.add(item.identifier)
        # Old reports label all compound blocks "Translate". Their other
        # statements are not present in those reports and cannot be recreated.
        if not item.is_say:
            if not is_noise_text(item.what):
                unsupported += 1
            continue
        key = item.what.strip()
        if not any(c.isalpha() for c in unprotected_text(key, protected)):
            resolved.append((item, key))
            kept += 1
        elif key in reusable:
            resolved.append((item, reusable[key]))
            reused += 1
        else:
            pending.setdefault(key, []).append(item)

    error = ""
    if pending and client is not None:
        try:
            translated = client.translate_texts(list(pending), target=target, names=names or [])
        except PauseRequested:
            raise
        except (TranslationError, OSError, RuntimeError, ValueError) as exc:
            translated = []
            error = str(exc)
        protected.update(getattr(client, "detected_names", ()))
        failed = getattr(client, "failed_texts", ())
        for key, value in zip(list(pending), translated):
            if key in failed or not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            if value == key:
                if not valid_unchanged_text(key, target, protected):
                    continue
                kept += len(pending[key])
            resolved.extend((item, value) for item in pending.pop(key))
    remaining = sum(len(group) for group in pending.values()) + unsupported
    if resolved:
        newline = old_fix.newline if old_fix else "\n"
        if not old_fix:
            lines.append("# 运行时缺失对话自动补译（汉化工具生成）" + newline)
        if lines and not lines[-1].endswith(("\r", "\n")):
            lines.append(newline)
        for item, translated_body in resolved:
            # Preserve both margins, including the blank lines used in NVL text.
            body = item.what.strip()
            rendered = item.what.replace(body, translated_body, 1) if body else item.what
            who = item.who + " " if item.who else ""
            lines.append(newline.join((
                "", f"translate {language} {item.identifier}:", "",
                "    # " + who + encode_say_string(item.what),
                "    " + who + encode_say_string(rendered), "")))
    changed = lines != (old_fix.lines if old_fix else [])
    if changed:
        try:
            write_if_unchanged(fix_file, "".join(lines), old_fix)
        except (OSError, TranslationMergeError) as exc:
            return PatchResult(message=f"保存运行时补译失败: {exc}")
    detail = f"复用 {reused} 条，原样保留 {kept} 条，新翻译 {len(resolved) - reused - kept} 条"
    if remaining:
        detail += f"，仍有 {remaining} 条待补译"
    if unsupported:
        detail += f"，其中 {unsupported} 个复杂对话块需保留原结构手动补译"
    if error:
        detail += f"（{error}）"
    if log_cb:
        log_cb("运行时补译：" + detail)
    return PatchResult(ok=not remaining, skip=not changed and not remaining,
                       files=[fix_file] if changed else [], message="运行时补译：" + detail)
