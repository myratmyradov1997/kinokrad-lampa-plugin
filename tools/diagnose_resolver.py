"""Measure the minimal trusted player-iframe resolve.

Run on the production host with the project virtual environment. The output
contains timings and IDs only; the dynamic Borth signature is never printed.
"""

import argparse
import json
import os
import sys
import time
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import server


def resolve_direct(embed_url, file_id, page_url):
    captured = {}
    responses = {}
    started = time.monotonic()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent=server.UA, locale="ru-RU")
        page = context.new_page()
        page.add_init_script(
            """
            (() => {
              const wanted = %d;
              const original = JSON.parse;
              JSON.parse = function(text, reviver) {
                const value = original.call(JSON, text, reviver);
                if (value && value.active && value.all) {
                  let found = null;
                  const walk = node => {
                    if (!node || found) return;
                    if (typeof node === 'object' && Number(node.id) === wanted) { found = node; return; }
                    if (typeof node === 'object') Object.values(node).forEach(walk);
                  };
                  walk(value.all);
                  if (found) value.active = found;
                }
                return value;
              };
            })();
            """
            % int(file_id)
        )

        def on_request(req):
            if "/bnsi/movies/" in req.url:
                captured.update(
                    method=req.method,
                    url=req.url,
                    headers=req.headers,
                    post=req.post_data or "",
                    elapsed=round(time.monotonic() - started, 3),
                )

        def on_response(resp):
            if "/bnsi/movies/" in resp.url and resp.ok:
                responses[int(resp.url.rstrip("/").split("/")[-1])] = resp

        page.on("request", on_request)
        page.on("response", on_response)
        context.route(
            page_url,
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body='<iframe src="%s"></iframe>' % server.html_lib.escape(embed_url, quote=True),
            ),
        )
        page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        parent_loaded = round(time.monotonic() - started, 3)
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline and int(file_id) not in responses:
            page.wait_for_timeout(100)
        result = responses[int(file_id)].json()
        total = round(time.monotonic() - started, 3)
        browser.close()

    return captured, result, total, parent_loaded


def replay(captured, file_id):
    headers = {
        key: value
        for key, value in captured["headers"].items()
        if key.lower() not in {"content-length", "host"}
    }
    url = captured["url"].rsplit("/", 1)[0] + "/" + str(file_id)
    started = time.monotonic()
    response = requests.request(
        captured["method"],
        url,
        headers=headers,
        data=captured["post"] or None,
        timeout=20,
    )
    elapsed = round(time.monotonic() - started, 3)
    data = response.json() if response.ok else None
    return response.status_code, data, elapsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("embed_url")
    parser.add_argument("page_url")
    parser.add_argument("file_id", type=int)
    parser.add_argument("replay_file_id", type=int)
    args = parser.parse_args()

    captured, result, total, parent_loaded = resolve_direct(
        args.embed_url, args.file_id, args.page_url
    )
    status, replay_result, replay_time = replay(captured, args.replay_file_id)
    print(
        json.dumps(
            {
                "browser_seconds": total,
                "parent_loaded_seconds": parent_loaded,
                "first_request_seconds": captured["elapsed"],
                "requested_id": int(urlparse(captured["url"]).path.rsplit("/", 1)[-1]),
                "qualities": [sorted(x.get("quality", {}).keys()) for x in result.get("hlsSource", [])],
                "replay_status": status,
                "replay_seconds": replay_time,
                "replay_qualities": [
                    sorted(x.get("quality", {}).keys()) for x in (replay_result or {}).get("hlsSource", [])
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
