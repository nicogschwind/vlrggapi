"""
Shared pagination and retry logic for multi-page scrapers.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

from fastapi import HTTPException
from selectolax.parser import HTMLParser

from utils.http_client import get_http_client, fetch_with_retries
from utils.constants import (
    DEFAULT_RETRIES,
    DEFAULT_REQUEST_DELAY,
    DEFAULT_TIMEOUT,
    MAX_MATCH_PAGE_WINDOW,
    MAX_MATCH_RETRIES,
    MAX_MATCH_TIMEOUT,
)

logger = logging.getLogger(__name__)


@dataclass
class PaginationConfig:
    """Encapsulates pagination parameters and page range calculation."""
    num_pages: int = 1
    from_page: int | None = None
    to_page: int | None = None
    max_retries: int = DEFAULT_RETRIES
    request_delay: float = DEFAULT_REQUEST_DELAY
    timeout: int = DEFAULT_TIMEOUT
    theme: str | None = None

    def get_page_range(self) -> tuple[int, int, int]:
        """Calculate (start_page, end_page, total_pages) from the params."""
        if self.from_page is not None and self.to_page is not None:
            start = max(1, self.from_page)
            end = max(start, self.to_page)
            return start, end, end - start + 1

        if self.from_page is not None:
            start = max(1, self.from_page)
            end = start + self.num_pages - 1
            return start, end, self.num_pages

        if self.to_page is not None:
            end = max(1, self.to_page)
            start = max(1, end - self.num_pages + 1)
            return start, end, end - start + 1

        # Default: from page 1
        return 1, self.num_pages, self.num_pages


async def scrape_multiple_pages(
    base_url: str,
    parse_func: Callable[[HTMLParser, int], list[dict]],
    config: PaginationConfig,
    page_url_func: Callable[[str, int], str] | None = None,
) -> dict:
    """
    Generic multi-page scraper with retry and exponential backoff.

    Args:
        base_url: The base URL for page 1 (e.g. "https://www.vlr.gg/matches").
        parse_func: Callable(html: HTMLParser, page: int) -> list[dict].
        config: PaginationConfig with page range and retry settings.
        page_url_func: Optional callable(base_url, page) -> url. Defaults to
                       appending ?page=N for page > 1.

    Returns:
        dict in the standard response shape.
    """
    start_page, end_page, total_pages = config.get_page_range()
    if total_pages > MAX_MATCH_PAGE_WINDOW:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Requested page window ({total_pages}) exceeds the maximum allowed "
                f"({MAX_MATCH_PAGE_WINDOW})."
            ),
        )
    if config.max_retries > MAX_MATCH_RETRIES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Max retries ({config.max_retries}) exceeds the maximum allowed "
                f"({MAX_MATCH_RETRIES})."
            ),
        )
    if config.timeout > MAX_MATCH_TIMEOUT:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Timeout ({config.timeout}s) exceeds the maximum allowed "
                f"({MAX_MATCH_TIMEOUT}s)."
            ),
        )

    client = get_http_client()
    result: list[dict] = []
    failed_pages: list[int] = []

    if page_url_func is None:
        def page_url_func(base: str, page: int) -> str:
            return base if page == 1 else f"{base}/?page={page}"

    logger.info(
        "Scraping pages %d-%d (%d pages) with %.1fs delay and theme %s",
        start_page, end_page, total_pages, config.request_delay, config.theme,
    )

    for page in range(start_page, end_page + 1):
        try:
            url = page_url_func(base_url, page)
            logger.info(
                "Scraping page %d (%d/%d)",
                page, page - start_page + 1, total_pages,
            )

            resp = await fetch_with_retries(
                url,
                client=client,
                timeout=config.timeout,
                max_retries=config.max_retries,
                request_delay=config.request_delay,
                theme=config.theme,
            )

            if resp.status_code != 200:
                logger.warning("Page %d returned status %d", page, resp.status_code)
                failed_pages.append(page)
                continue

            html = HTMLParser(resp.text)
            page_results = parse_func(html, page)
            result.extend(page_results)
            logger.info("Page %d: %d items", page, len(page_results))

            if page < end_page:
                await asyncio.sleep(config.request_delay)

        except Exception as e:
            failed_pages.append(page)
            logger.error("Failed page %d: %s", page, e)

    successful_pages = total_pages - len(failed_pages)
    logger.info(
        "Scraping done: %d matches, %d/%d pages OK",
        len(result), successful_pages, total_pages,
    )

    if failed_pages:
        failed_pages_text = ", ".join(str(page) for page in failed_pages)
        raise HTTPException(
            status_code=502,
            detail=(
                "Failed to fetch all requested pages from VLR.GG. "
                f"Pages with exhausted retries: {failed_pages_text}."
            ),
        )

    return {
        "data": {
            "status": 200,
            "segments": result,
            "meta": {
                "page_range": f"{start_page}-{end_page}",
                "total_pages_requested": total_pages,
                "successful_pages": successful_pages,
                "failed_pages": failed_pages,
                "total_matches": len(result),
            },
        }
    }
