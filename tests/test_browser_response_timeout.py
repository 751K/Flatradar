"""A response whose body stalls must still be covered by the fetch deadline."""
import json
import shutil
import subprocess

import pytest

from browser_fetcher import BrowserFetcher


NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node is needed to run browser JavaScript")


class _CapturePage:
    def evaluate(self, script, arg=None):
        self.script = script
        return {}


def test_stalled_body_is_aborted_after_response_headers():
    fetcher = BrowserFetcher()
    page = _CapturePage()
    fetcher._page = page
    fetcher._raw_fetch("/test", method="GET", timeout_ms=20)
    script = page.script
    runner = r"""
const vm = require('vm');
const data = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const context = {
  AbortController, setTimeout: (fn, ms) => setTimeout(fn, Math.min(ms, 20)),
  clearTimeout,
  window: { location: { href: 'https://example.com/' } },
  fetch: async (url, init) => ({
    text: () => new Promise((resolve, reject) => {
      if (init.signal.aborted) return reject(new Error('aborted'));
      init.signal.addEventListener('abort', () => reject(new Error('aborted')));
    }),
  }),
};
const fn = vm.runInNewContext('(' + data.script + ')', context);
const watchdog = setTimeout(() => { console.error('request did not time out'); process.exit(1); }, 500);
fn().then(result => {
  clearTimeout(watchdog);
  if (result.error !== 'aborted') throw new Error(JSON.stringify(result));
}).catch(err => { console.error(err); process.exitCode = 1; });
"""
    result = subprocess.run(
        [NODE, "-e", runner], input=json.dumps({"script": script}), text=True,
        capture_output=True, timeout=3, check=False,
    )
    assert result.returncode == 0, result.stderr
