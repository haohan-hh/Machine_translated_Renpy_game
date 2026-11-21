"""Selected-model prompt precedence on the wire, including retries and non-Ollama endpoints."""
from __future__ import annotations

import json
import threading
import unittest
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from rpytranslator.connection import ModelPrompt, _model_prompt_from_show, selected_model_prompt
from rpytranslator.translator import ResponseFormatError, TranslationClient, TranslationConfig


@contextmanager
def _local_api(metadata, respond):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append((self.path, request, self.headers.get('Authorization')))
            if self.path == '/api/show':
                status, body = metadata(request['model'])
            else:
                status, answer = respond(request)
                body = {'choices': [{'message': {'content': answer}}]} if status == 200 else {'error': answer}
            data = json.dumps(body).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with HTTPServer(('127.0.0.1', 0), Handler) as api:
        thread = threading.Thread(target=api.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{api.server_port}/v1', requests
        finally:
            api.shutdown()
            thread.join(2)


class PromptRoutingTests(unittest.TestCase):
    def test_inspects_system_history_and_model_file_without_mistaking_template_slots_for_prompts(self):
        for payload, expected in (
            ({'system': ' Custom preset '}, ModelPrompt.PRESENT),
            ({'messages': [{'role': 'system', 'content': 'custom preset'}]}, ModelPrompt.PRESENT),
            ({'modelfile': 'FROM m\nSYSTEM """\ncustom preset\n"""'}, ModelPrompt.PRESENT),
            ({'modelfile': 'FROM m\nMESSAGE system "custom preset"'}, ModelPrompt.PRESENT),
            ({'modelfile': 'FROM m\nSYSTEM "  "\nMESSAGE system ""'}, ModelPrompt.ABSENT),
            ({'modelfile': 'FROM m\nLICENSE """Read the term\nSYSTEM is an example.\n"""\n# SYSTEM ignored',
              'template': '{%- if messages[0].role == "system" %}{{ messages[0].content }}{% endif %}'},
             ModelPrompt.ABSENT),
            ({'system': '', 'template': '{{ if .System }}{{ .System }}{{ end }}\n{{ .Prompt }}'}, ModelPrompt.ABSENT),
            ({'modelfile': 'FROM m', 'template': '<|im_start|>system\nAct like a translator.<|im_end|>'},
             ModelPrompt.PRESENT),
            ({'modelfile': 'FROM m\nTEMPLATE """Fixed custom prose.\n{{ .Prompt }}"""'}, ModelPrompt.UNKNOWN),
            ({'modelfile': 'FROM m\nSYSTEM """unterminated'}, ModelPrompt.UNKNOWN),
            ({'system': ['bad']}, ModelPrompt.UNKNOWN),
            ({'messages': [{'role': 'system', 'content': 0}]}, ModelPrompt.UNKNOWN),
            ({'status': 'ok'}, ModelPrompt.UNKNOWN),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(_model_prompt_from_show(payload), expected)

    def test_ner_batch_schema_fallback_and_single_share_the_routing_decision(self):
        for status, details, system, message in (
            (200, {'system': 'model owner secret preset'}, False, '模型已有'),
            (200, {'modelfile': 'FROM m\nTEMPLATE {{ .Prompt }}'}, True, '未设置'),
            (404, {'error': 'not supported'}, False, '无法确认'),
            (200, {'status': 'ok'}, False, '无法确认'),
        ):
            first_batch = True

            def respond(req):
                nonlocal first_batch
                content = req['messages'][-1]['content']
                if 'Extract proper names' in content:
                    return 200, '["Avery"]'
                if 'Return only a JSON array' in content:
                    if first_batch:
                        first_batch = False
                        return 400, 'unsupported response_format json_schema'
                    return 200, '["我看见了@@n0@@。"]'
                return 200, '我看见了@@n0@@。'

            with self.subTest(status=status, details=details), _local_api(lambda m: (status, details), respond) as (base, seen):
                logs = []
                client = TranslationClient(TranslationConfig(base_url=base, model='qwen3:selected', api_key='test-only',
                                                            max_retries=1), log=logs.append)
                self.assertEqual(client.translate_texts(['I saw Avery.'], 'zh_hans'), ['我看见了Avery。'])
                self.assertEqual(client._translate_single('I saw Avery.', 'zh_hans', names=['Avery']), '我看见了Avery。')
                self.assertEqual(len(logs), 1)
                self.assertIn(message, logs[0])
                self.assertEqual([path for path, *_ in seen], ['/api/show'] + ['/v1/chat/completions'] * 4)
                self.assertEqual(seen[0][1], {'model': 'qwen3:selected'})
                self.assertTrue(all(auth == 'Bearer test-only' for _, _, auth in seen))
                for _, req, _ in seen[1:]:
                    self.assertEqual([m['role'] for m in req['messages']], ['system', 'user'] if system else ['user'])
                    self.assertNotIn('model owner secret preset', str(req))
                    self.assertTrue(req['messages'][-1]['content'].endswith('/no_think'))
                ner, batch, retry, single = [req for _, req, _ in seen[1:]]
                self.assertEqual(ner['response_format']['json_schema']['name'], 'proper_name_list')
                self.assertIn('do not translate', ner['messages'][-1]['content'])
                self.assertIn('Simplified Chinese', batch['messages'][-1]['content'])
                self.assertIn('@@n0@@', batch['messages'][-1]['content'])
                self.assertEqual(batch['messages'], retry['messages'])
                self.assertNotIn('response_format', retry)
                self.assertIn('Return only the translated text', single['messages'][-1]['content'])
                self.assertIn('Simplified Chinese', single['messages'][-1]['content'])

    def test_existing_model_prompt_does_not_retry_identical_system_styles_and_connection_probe_uses_same_route(self):
        answer = 'invalid'
        with _local_api(lambda m: (200, {'system': 'preset'}), lambda req: (200, answer)) as (base, seen):
            client = TranslationClient(TranslationConfig(base_url=base, model='generic'))
            with self.assertRaises(ResponseFormatError):
                client._request_json_array(['Open the door.'], 'schinese')
            self.assertEqual(len(seen), 2)
            answer = '["开门。"]'
            self.assertEqual(client.test_connection(), '开门。')
            self.assertEqual(len(seen), 3)
            self.assertEqual([m['role'] for m in seen[-1][1]['messages']], ['user'])
            answer = '["Open the door."]'
            with self.assertRaisesRegex(ResponseFormatError, '中文译文'):
                client.test_connection()

    def test_model_change_parallel_discovery_and_no_remote_metadata_probe(self):
        def metadata(model):
            return 200, {'modelfile': 'FROM m'} if model == 'plain' else {'system': 'preset'}

        with _local_api(metadata, lambda req: (200, '["测试"]')) as (base, seen):
            client = TranslationClient(TranslationConfig(base_url=base, model='plain'))
            with ThreadPoolExecutor(3) as pool:
                self.assertEqual(list(pool.map(client._request_json_array, [['s']] * 3, ['japanese'] * 3)), [['测试']] * 3)
            client.config.model = 'custom'
            client._request_json_array(['s'], 'japanese')
            self.assertEqual([req['model'] for path, req, _ in seen if path == '/api/show'], ['plain', 'custom'])
            self.assertEqual([m['role'] for m in seen[-1][1]['messages']], ['user'])
            self.assertIn('Japanese', seen[-1][1]['messages'][0]['content'])
        with patch('rpytranslator.connection._read_json') as read, patch('urllib.request.urlopen') as chat:
            remote = 'https://api.example.test/v1'
            self.assertEqual(selected_model_prompt(remote, 'm'), ModelPrompt.REMOTE)
            self.assertEqual(selected_model_prompt('http://localhost:9000/custom/v1', 'm'), ModelPrompt.UNKNOWN)
            chat.return_value = BytesIO(json.dumps({'choices': [{'message': {'content': '["开门。"]'}}]}).encode())
            client = TranslationClient(TranslationConfig(base_url=remote, model='m'))
            self.assertEqual(client.test_connection(), '开门。')
            messages = json.loads(chat.call_args.args[0].data)['messages']
            self.assertEqual([m['role'] for m in messages], ['user'])
            self.assertIn('Simplified Chinese', messages[0]['content'])
            read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
