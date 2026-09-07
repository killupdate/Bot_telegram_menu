import json
import urllib.error
import urllib.request


class APIError(Exception):
    def __init__(self, code, retry_after=0):
        self.code = code
        self.retry_after = retry_after
        super().__init__('Telegram API error %s' % code)

    @property
    def transient(self):
        return self.code == 429 or self.code >= 500 or self.code == 0


class Telegram:
    def __init__(self, token):
        self.url = 'https://api.telegram.org/bot' + token + '/'

    def call(self, method, **payload):
        request = urllib.request.Request(
            self.url + method, data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                body = json.load(exc)
            except (ValueError, OSError):
                raise APIError(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise APIError(0) from None
        if not body.get('ok'):
            raise APIError(body.get('error_code', 500), body.get('parameters', {}).get('retry_after', 0))
        return body['result']
