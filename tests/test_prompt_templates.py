"""An empty SYSTEM layer alone does not prove a custom chat template has no instructions."""
import unittest

from rpytranslator.connection import ModelPrompt, _model_prompt_from_show


class EmbeddedInstructionsTests(unittest.TestCase):
    def test_embedded_instructions_never_receive_the_application_fallback(self):
        for template in (
            'Reply only in German. {{ .Prompt }}',
            '{{ if .System }}{{ .System }}{{ else }}Always write in verse.{{ end }}{{ .Prompt }}',
            '{{ "<|im_start|>system\\n" + "仅用德语作答。" + "<|im_end|>" }}{{ message.content }}',
            '{% set preset = "Роль переводчика" %}{{ preset }}{{ message.content }}',
            '{% include "custom_system.jinja" %}{{ message.content }}',
            '请使用如下格式：{{ .Prompt }}',
        ):
            for payload in ({'template':template, 'modelfile':'FROM example'},
                            {'modelfile':'FROM example\nTEMPLATE """' + template + '"""'}):
                with self.subTest(payload=payload):
                    self.assertIn(_model_prompt_from_show(payload), (ModelPrompt.UNKNOWN, ModelPrompt.PRESENT))
        for key in ('tokenizer.chat_template', 'tokenizer.chat_template.default'):
            metadata = {'system': '', 'modelfile': 'FROM example\nTEMPLATE {{ .Prompt }}',
                        'model_info': {key: '请固定用德语回复。{{ message.content }}'}}
            with self.subTest(key=key):
                self.assertIn(_model_prompt_from_show(metadata), (ModelPrompt.UNKNOWN, ModelPrompt.PRESENT))

    def test_qwen_tool_protocol_and_printed_roles_are_not_translation_presets(self):
        template = ("{%- if tools %}{{ 'You may call tools for this query.' }}{%- else %}"
                    "{%- if messages[0].role == 'system' %}{{ '<|im_start|>system\\n' + messages[0].content }}"
                    "{% endif %}{% endif %}{{ message.role + '\\n' + message.content + '<|im_end|>' }}")
        payload = {'modelfile':'FROM example\nTEMPLATE {{ .Prompt }}', 'template':template}
        self.assertIs(_model_prompt_from_show(payload), ModelPrompt.ABSENT)
        self.assertIs(_model_prompt_from_show(dict(payload, system='请用德语。')), ModelPrompt.PRESENT)
        self.assertIs(_model_prompt_from_show(dict(payload, template='Unfamiliar instructions here.',
                                                   modelfile='FROM example\nSYSTEM Deployed preset.')), ModelPrompt.PRESENT)
