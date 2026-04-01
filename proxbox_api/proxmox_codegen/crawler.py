"""Async Playwright crawler for Proxmox API Viewer recursive raw capture."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

from proxbox_api.proxmox_codegen.apidoc_parser import PROXMOX_API_VIEWER_URL

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

HTTP_METHODS = ("GET", "POST", "PUT", "DELETE")


@dataclass(slots=True)
class CrawlConfig:
    """Crawler configuration values."""

    url: str = PROXMOX_API_VIEWER_URL
    timeout_ms: int = 120000
    worker_count: int = 10
    retry_count: int = 2
    retry_backoff_seconds: float = 0.35
    checkpoint_every: int = 50


def _normalize_doc_section_text(value: object) -> str | None:
    """Normalize rendered doc section text while preserving paragraph breaks."""

    if not isinstance(value, str):
        return None

    lines = [line.strip() for line in value.splitlines()]
    normalized_lines: list[str] = []
    pending_blank = False

    for line in lines:
        if not line:
            pending_blank = True
            continue
        if pending_blank and normalized_lines:
            normalized_lines.append("")
        normalized_lines.append(line)
        pending_blank = False

    normalized = "\n".join(normalized_lines).strip()
    return normalized or None


def _normalize_doc_sections(sections: list[dict[str, object]] | None) -> dict[str, str]:
    """Reduce rendered doc sections into a heading-keyed text map."""

    normalized: dict[str, str] = {}
    if not isinstance(sections, list):
        return normalized

    for section in sections:
        if not isinstance(section, dict):
            continue
        heading = section.get("heading")
        body = _normalize_doc_section_text(section.get("body"))
        if not isinstance(heading, str) or not body:
            continue
        normalized[heading.strip()] = body

    return normalized


async def _setup_page(page: Page, config: CrawlConfig) -> list[dict[str, object]]:
    await page.goto(config.url, wait_until="networkidle", timeout=config.timeout_ms)
    await page.wait_for_timeout(900)
    await page.evaluate(
        """
        () => {
          const tree = Ext.ComponentQuery.query('treepanel')[0];
          if (tree) {
            tree.expandAll();
          }
        }
        """
    )
    await page.wait_for_timeout(1200)
    tree_paths = await page.evaluate(
        """
        () => {
          if (typeof Ext === 'undefined') {
            return [];
          }
          const tree = Ext.ComponentQuery.query('treepanel')[0];
          if (!tree) {
            return [];
          }
          const root = tree.getStore().getRoot();
          const out = [];
          const walk = (node, depth) => {
            if (node && node.data && node.data.path) {
              out.push({
                path: node.data.path,
                text: node.data.text,
                depth,
                has_info: !!node.data.info,
                methods: node.data.info ? Object.keys(node.data.info).filter((k) => ['GET','POST','PUT','DELETE'].includes(k)) : [],
              });
            }
            (node.childNodes || []).forEach((child) => walk(child, depth + 1));
          };
          (root.childNodes || []).forEach((child) => walk(child, 0));
          return out;
        }
        """
    )
    return tree_paths


async def _select_path(page: Page, path: str) -> bool:
    return bool(
        await page.evaluate(
            """
            ({path}) => {
              const tree = Ext.ComponentQuery.query('treepanel')[0];
              if (!tree) {
                return false;
              }
              const rec = tree.getStore().findRecord('path', path, 0, false, false, true);
              if (!rec || !rec.data || !rec.data.info) {
                return false;
              }
              try {
                tree.getSelectionModel().select(rec);
                tree.expandPath(rec.getPath());
                return true;
              } catch (_error) {
                return false;
              }
            }
            """,
            {"path": path},
        )
    )


async def _extract_method_data(page: Page, path: str, method: str) -> dict[str, object] | None:
    return await page.evaluate(
        """
        ({method, path}) => {
          const tree = Ext.ComponentQuery.query('treepanel')[0];
          if (!tree) {
            return null;
          }
          const rec = tree.getStore().findRecord('path', path, 0, false, false, true);
          if (!rec || !rec.data || !rec.data.info) {
            return null;
          }
          const info = rec.data.info[method];
          if (!info) {
            return null;
          }
          return {
            method,
            path,
            method_name: info.name || null,
            description: info.description || null,
            parameters: info.parameters || null,
            returns: info.returns || null,
            permissions: info.permissions || null,
            allowtoken: info.allowtoken,
            protected: info.protected,
            unstable: info.unstable,
          };
        }
        """,
        {"path": path, "method": method},
    )


async def _extract_rendered_doc_sections(page: Page) -> dict[str, str]:
    """Read rendered section content from the active method tab in the viewer doc panel."""

    sections = await page.evaluate(
        """
        () => {
          const docview = document.querySelector('#docview');
          if (!docview) {
            return [];
          }

          const activePanel =
            docview.querySelector('.x-tabpanel-child.x-panel-active') ||
            docview.querySelector('.x-panel-active') ||
            docview;

          const contentRoot =
            activePanel.querySelector('.x-panel-body') ||
            activePanel.querySelector('.x-component-default') ||
            activePanel;

          const blocks = [];
          const headings = contentRoot.querySelectorAll('h1, h2, h3, h4, .x-title-text, .x-fieldset-header-text');

          headings.forEach((headingNode) => {
            const heading = (headingNode.textContent || '').trim();
            if (!heading || !['Description', 'Usage'].includes(heading)) {
              return;
            }

            const bodyParts = [];
            let cursor = headingNode.nextElementSibling;
            while (cursor) {
              const text = (cursor.innerText || cursor.textContent || '').trim();
              const nextHeading = (cursor.textContent || '').trim();
              if (cursor.matches('h1, h2, h3, h4, .x-title-text, .x-fieldset-header-text') && nextHeading) {
                break;
              }
              if (text) {
                bodyParts.push(text);
              }
              cursor = cursor.nextElementSibling;
            }

            if (bodyParts.length) {
              blocks.push({
                heading,
                body: bodyParts.join('\\n\\n'),
              });
            }
          });

          return blocks;
        }
        """
    )
    return _normalize_doc_sections(sections)


def _synthesize_raw_sections(method_data: dict[str, object]) -> list[str]:
    returns = method_data.get("returns")
    if not isinstance(returns, dict):
        return []

    sections: list[str] = []
    if "items" in returns:
        sections.append(f"items: {json.dumps(returns['items'], indent=4, sort_keys=True)}")
    if "properties" in returns:
        sections.append(f"properties:{json.dumps(returns['properties'], indent=4, sort_keys=True)}")
    return sections


async def _capture_endpoint(  # noqa: C901
    page: Page, item: dict[str, object], timeout_ms: int
) -> tuple[str, dict[str, object]] | None:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    path = str(item.get("path") or "")
    if not path:
        return None

    methods = [m for m in item.get("methods", []) if m in HTTP_METHODS]
    if not methods:
        return None

    selected = await _select_path(page, path)
    if selected:
        await page.wait_for_timeout(80)

    endpoint = {
        "path": path,
        "text": item.get("text"),
        "depth": item.get("depth", 0),
        "methods": {},
    }

    for method in methods:
        raw_sections: list[str] = []
        rendered_sections: dict[str, str] = {}
        if selected:
            tab = page.locator("#docview .x-tab-inner", has_text=method).first
            try:
                await tab.click(timeout=timeout_ms)
                await page.wait_for_timeout(60)
            except PlaywrightTimeoutError:
                pass

            buttons = page.locator("#docview .x-btn-inner")
            button_count = await buttons.count()
            for idx in range(button_count):
                label = (await buttons.nth(idx).inner_text()).strip()
                if label == "Show RAW":
                    try:
                        await buttons.nth(idx).click(timeout=timeout_ms)
                        await page.wait_for_timeout(40)
                    except PlaywrightTimeoutError:
                        continue

            pre_texts = await page.locator("#docview pre").all_text_contents()
            raw_sections = [text.strip() for text in pre_texts if text.strip()]
            rendered_sections = await _extract_rendered_doc_sections(page)

        method_data = await _extract_method_data(page, path, method)
        if not method_data:
            continue

        if not raw_sections:
            raw_sections = _synthesize_raw_sections(method_data)

        method_data["raw_sections"] = raw_sections
        method_data["viewer_description"] = rendered_sections.get("Description")
        method_data["viewer_usage"] = rendered_sections.get("Usage")
        method_data["source"] = "viewer"
        endpoint["methods"][method] = method_data

    return (path, endpoint)


async def _write_checkpoint(checkpoint_path: Path, payload: dict[str, object]) -> None:
    """Persist crawler checkpoint without blocking the event loop."""

    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    await asyncio.to_thread(checkpoint_path.write_text, serialized, "utf-8")


async def _worker(  # noqa: C901
    context: BrowserContext,
    queue: asyncio.Queue,
    output: dict[str, dict[str, object]],
    failures: dict[str, str],
    state: dict[str, int],
    lock: asyncio.Lock,
    checkpoint_path: Path | None,
    checkpoint_every: int,
    url: str,
    timeout_ms: int,
    retry_count: int,
    retry_backoff_seconds: float,
) -> None:
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        await page.wait_for_timeout(600)
        await page.evaluate(
            """
            () => {
              const tree = Ext.ComponentQuery.query('treepanel')[0];
              if (tree) {
                tree.expandAll();
              }
            }
            """
        )
        await page.wait_for_timeout(600)

        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                break

            try:
                path = str(item.get("path") or "")
                captured: tuple[str, dict[str, object]] | None = None
                last_error: Exception | None = None

                for attempt in range(retry_count + 1):
                    try:
                        captured = await _capture_endpoint(
                            page=page,
                            item=item,
                            timeout_ms=timeout_ms,
                        )
                        if captured is not None:
                            break
                    except Exception as error:  # noqa: BLE001
                        last_error = error

                    if attempt < retry_count:
                        await asyncio.sleep(retry_backoff_seconds * (2**attempt))

                checkpoint_payload: dict[str, object] | None = None

                async with lock:
                    state["processed"] += 1

                    if captured is not None:
                        endpoint_path, endpoint = captured
                        output[endpoint_path] = endpoint
                    elif path:
                        failures[path] = (
                            str(last_error)
                            if last_error is not None
                            else "capture returned no data"
                        )

                    if (
                        checkpoint_path is not None
                        and checkpoint_every > 0
                        and (state["processed"] - state["last_checkpoint"]) >= checkpoint_every
                    ):
                        state["last_checkpoint"] = state["processed"]
                        checkpoint_payload = {
                            "processed": state["processed"],
                            "captured_endpoints": len(output),
                            "failed_endpoints": len(failures),
                            "endpoints": dict(sorted(output.items())),
                            "failures": dict(sorted(failures.items())),
                        }

                if checkpoint_payload is not None and checkpoint_path is not None:
                    await _write_checkpoint(checkpoint_path, checkpoint_payload)
            finally:
                queue.task_done()
    finally:
        await page.close()


async def crawl_proxmox_api_viewer_async(
    url: str = PROXMOX_API_VIEWER_URL,
    timeout_ms: int = 120000,
    worker_count: int = 10,
    retry_count: int = 2,
    retry_backoff_seconds: float = 0.35,
    checkpoint_path: str | Path | None = None,
    checkpoint_every: int = 50,
) -> dict[str, object]:
    """Recursively traverse all navigation items and capture endpoint raw data in parallel."""

    from playwright.async_api import async_playwright

    started_at = perf_counter()
    config = CrawlConfig(
        url=url,
        timeout_ms=timeout_ms,
        worker_count=max(1, worker_count),
        retry_count=max(0, retry_count),
        retry_backoff_seconds=max(0.0, retry_backoff_seconds),
        checkpoint_every=max(1, checkpoint_every),
    )
    checkpoint_file = Path(checkpoint_path) if checkpoint_path else None

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(ignore_https_errors=True)

        seed_page = await context.new_page()
        tree_paths = await _setup_page(seed_page, config)
        await seed_page.close()

        all_items = [item for item in tree_paths if item.get("has_info")]
        queue: asyncio.Queue = asyncio.Queue()
        for item in all_items:
            queue.put_nowait(item)

        results: dict[str, dict[str, object]] = {}
        failures: dict[str, str] = {}
        state = {"processed": 0, "last_checkpoint": 0}
        lock = asyncio.Lock()

        workers = [
            asyncio.create_task(
                _worker(
                    context=context,
                    queue=queue,
                    output=results,
                    failures=failures,
                    state=state,
                    lock=lock,
                    checkpoint_path=checkpoint_file,
                    checkpoint_every=config.checkpoint_every,
                    url=config.url,
                    timeout_ms=config.timeout_ms,
                    retry_count=config.retry_count,
                    retry_backoff_seconds=config.retry_backoff_seconds,
                )
            )
            for _ in range(config.worker_count)
        ]
        for _ in workers:
            queue.put_nowait(None)

        await queue.join()
        await asyncio.gather(*workers)

        if checkpoint_file is not None:
            await _write_checkpoint(
                checkpoint_file,
                {
                    "processed": state["processed"],
                    "captured_endpoints": len(results),
                    "failed_endpoints": len(failures),
                    "endpoints": dict(sorted(results.items())),
                    "failures": dict(sorted(failures.items())),
                },
            )

        await context.close()
        await browser.close()

    elapsed = perf_counter() - started_at
    total_methods = sum(len(ep.get("methods", {})) for ep in results.values())

    return {
        "source": "playwright-async",
        "url": url,
        "worker_count": config.worker_count,
        "retry_count": config.retry_count,
        "retry_backoff_seconds": config.retry_backoff_seconds,
        "checkpoint_every": config.checkpoint_every,
        "checkpoint_path": str(checkpoint_file) if checkpoint_file else None,
        "endpoint_count": len(results),
        "discovered_navigation_items": len(all_items),
        "method_count": total_methods,
        "failed_endpoint_count": len(failures),
        "failures": dict(sorted(failures.items())),
        "duration_seconds": round(elapsed, 3),
        "endpoints": dict(sorted(results.items())),
    }


def crawl_proxmox_api_viewer(
    url: str = PROXMOX_API_VIEWER_URL,
    timeout_ms: int = 120000,
    worker_count: int = 10,
    retry_count: int = 2,
    retry_backoff_seconds: float = 0.35,
    checkpoint_path: str | Path | None = None,
    checkpoint_every: int = 50,
) -> dict[str, object]:
    """Synchronous wrapper for async crawler, preserving existing call sites."""

    return asyncio.run(
        crawl_proxmox_api_viewer_async(
            url=url,
            timeout_ms=timeout_ms,
            worker_count=worker_count,
            retry_count=retry_count,
            retry_backoff_seconds=retry_backoff_seconds,
            checkpoint_path=checkpoint_path,
            checkpoint_every=checkpoint_every,
        )
    )
