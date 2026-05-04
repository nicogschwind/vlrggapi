import asyncio
import logging

from selectolax.parser import HTMLParser

from utils.http_client import fetch_with_retries, get_http_client
from utils.constants import VLR_BASE_URL, VLR_EVENTS_URL, CACHE_TTL_EVENTS, CACHE_TTL_EVENT_MATCHES
from utils.cache_manager import cache_manager
from utils.error_handling import handle_scraper_errors, upstream_error_payload
from utils.html_parsers import (
    extract_text_content,
    extract_prize_value,
    extract_date_range,
    extract_region_from_flag,
    normalize_image_url,
    build_full_url,
    parse_href_id_slug,
    parse_match_timestamp,
)

logger = logging.getLogger(__name__)


def _parse_event_cards(container) -> list:
    """Parse all event cards from a section container."""
    events = []
    for event_item in container.css("a.event-item"):
        title = extract_text_content(event_item.css_first(".event-item-title"))
        event_status = extract_text_content(event_item.css_first(".event-item-desc-item-status"))
        prize = extract_prize_value(event_item.css_first(".event-item-desc-item.mod-prize"))
        dates = extract_date_range(event_item.css_first(".event-item-desc-item.mod-dates"))
        region = extract_region_from_flag(event_item.css_first(".event-item-desc-item.mod-location .flag"))
        img_elem = event_item.css_first(".event-item-thumb img")
        thumb = normalize_image_url(img_elem.attributes.get("src", "") if img_elem else "")
        full_url = build_full_url(event_item.attributes.get("href", ""))
        events.append({
            "title": title,
            "status": event_status,
            "prize": prize,
            "dates": dates,
            "region": region,
            "thumb": thumb,
            "url_path": full_url,
        })
    return events


def _parse_event_matches(html: HTMLParser) -> list:
    """Parse the match list from an event matches page."""
    matches = []
    current_date = ""

    for elem in html.css(".wf-label.mod-large, a.wf-module-item.match-item"):
        classes = elem.attributes.get("class", "")

        if "wf-label" in classes:
            current_date = elem.text(strip=True)
            continue

        href = elem.attributes.get("href", "")
        match_id, _ = parse_href_id_slug(href)
        match_url = build_full_url(href)

        team_elems = elem.css(".match-item-vs-team")
        teams = []
        for te in team_elems:
            name_el = te.css_first(".match-item-vs-team-name")
            score_el = te.css_first(".match-item-vs-team-score")
            name = name_el.text(strip=True) if name_el else "TBD"
            score = score_el.text(strip=True) if score_el else ""
            is_winner = "mod-winner" in te.attributes.get("class", "")
            teams.append({"name": name, "score": score, "is_winner": is_winner})

        while len(teams) < 2:
            teams.append({"name": "TBD", "score": "", "is_winner": False})

        series_el = elem.css_first(".match-item-event-series")
        event_series = series_el.text(strip=True) if series_el else ""

        status_el = elem.css_first(".ml-status")
        eta_el = elem.css_first(".ml-eta")
        match_status = ""
        if status_el:
            match_status = status_el.text(strip=True)
        elif eta_el:
            match_status = eta_el.text(strip=True)

        timestamp = parse_match_timestamp(elem, current_date)

        vods = []
        for vod_el in elem.css(".match-item-vod .wf-tag"):
            vod_text = vod_el.text(strip=True)
            vod_link_el = vod_el if vod_el.tag == "a" else vod_el.parent
            vod_href = vod_link_el.attributes.get("href", "") if vod_link_el else ""
            if vod_href:
                vod_href = build_full_url(vod_href)
            vods.append({"label": vod_text, "url": vod_href})

        note_el = elem.css_first(".match-item-note")
        note = note_el.text(strip=True) if note_el else ""

        matches.append({
            "match_id": match_id,
            "url": match_url,
            "date": current_date,
            "timestamp": timestamp,
            "status": match_status,
            "note": note,
            "event_series": event_series,
            "team1": teams[0],
            "team2": teams[1],
            "vods": vods,
        })
    return matches


def _parse_event_header(html: HTMLParser) -> dict:
    """Extract general event information from the header block."""
    header = html.css_first(".event-header")
    title = extract_text_content(header.css_first(".wf-title")) if header else ""
    subtitle = extract_text_content(header.css_first(".event-desc-subtitle")) if header else ""

    # Use robust helpers for common fields
    dates = extract_date_range(html.css_first(".event-desc-item.mod-dates"))
    prize = extract_prize_value(html.css_first(".event-desc-item.mod-prize"))

    # Location helper doesn't exist for general strings, so we find it by label
    location = ""
    for item in html.css(".event-desc-item"):
        label = extract_text_content(item.css_first(".ge-text-light"))
        if "Location" in label:
            location = item.text(strip=True).replace(label, "").strip()
            break

    thumb_img = html.css_first(".event-header-thumb img")
    thumb = normalize_image_url(thumb_img.attributes.get("src", "") if thumb_img else "")

    return {
        "title": title,
        "subtitle": subtitle,
        "dates": dates,
        "prize": prize,
        "location": location,
        "thumb": thumb,
    }


def _parse_event_teams(html: HTMLParser) -> list[dict]:
    """Extract participating teams and their rosters."""
    teams = []
    for team_card in html.css(".event-teams-container .event-team"):
        name_elem = team_card.css_first(".event-team-name")
        team_name = extract_text_content(name_elem)
        team_href = name_elem.attributes.get("href", "") if name_elem else ""
        tid, _ = parse_href_id_slug(team_href)

        logo_img = team_card.css_first("img.event-team-players-mask-team")
        logo = normalize_image_url(logo_img.attributes.get("src", "") if logo_img else "")

        players = []
        for p_elem in team_card.css(".event-team-players .wf-module-item"):
            p_name = extract_text_content(p_elem)
            p_href = p_elem.attributes.get("href", "")
            pid, _ = parse_href_id_slug(p_href)
            players.append({"id": pid, "name": p_name})

        teams.append({
            "id": tid,
            "name": team_name,
            "logo": logo,
            "players": players
        })
    return teams


def _parse_event_standings(html: HTMLParser) -> list[dict]:
    """Extract standings tables (groups, leagues)."""
    standings = []
    for group in html.css(".event-groups-container .event-group"):
        table = group.css_first("table.mod-group")
        if not table:
            continue

        group_name = extract_text_content(table.css_first("th.mod-title"))
        group_standings = []

        for row in table.css("tbody tr"):
            team_link = row.css_first("a.event-group-team")
            if not team_link:
                continue

            name_container = team_link.css_first(".event-group-team-name")
            t_country = extract_text_content(name_container.css_first(".ge-text-light"))
            t_name = extract_text_content(name_container).replace(t_country, "").strip()
            t_href = team_link.attributes.get("href", "")
            tid, _ = parse_href_id_slug(t_href)

            t_logo_img = row.css_first("img.event-group-team-logo")
            t_logo = normalize_image_url(t_logo_img.attributes.get("src", "") if t_logo_img else "")

            # Extract stats from subsequent td cells
            cells = row.css("td")
            stats = {
                "record": extract_text_content(cells[2]) if len(cells) > 2 else "",
                "maps": extract_text_content(cells[3]) if len(cells) > 3 else "",
            }

            group_standings.append({
                "id": tid,
                "team": t_name,
                "country": t_country,
                "logo": t_logo,
                "stats": stats
            })

        standings.append({
            "group": group_name,
            "table": group_standings
        })
    return standings


@handle_scraper_errors
async def vlr_events(upcoming=True, completed=True, page=1, theme: str | None = None):
    """
    Get Valorant events from VLR.GG

    Args:
        upcoming (bool): If True, include upcoming events
        completed (bool): If True, include completed events
        page (int): Page number for pagination (only applies to completed events)
        theme (str): Optional theme preference.

    Returns:
        dict: Response with status code and events data
    """
    cache_key = ("events", upcoming, completed, page, theme)

    async def build():
        if not upcoming and not completed:
            show_upcoming = show_completed = True
        else:
            show_upcoming, show_completed = upcoming, completed

        url = f"{VLR_EVENTS_URL}/?page={page}" if show_completed and page > 1 else VLR_EVENTS_URL

        client = get_http_client()
        resp = await fetch_with_retries(url, client=client, theme=theme)
        status = resp.status_code
        if status >= 400:
            return upstream_error_payload(status, "events")

        html = HTMLParser(resp.text)

        events = []

        if show_upcoming:
            for section in html.css("div.wf-label.mod-large.mod-upcoming"):
                parent = section.parent
                if parent and parent.css("a.event-item"):
                    events.extend(_parse_event_cards(parent))

        if show_completed:
            for section in html.css("div.wf-label.mod-large.mod-completed"):
                parent = section.parent
                if parent and parent.css("a.event-item"):
                    events.extend(_parse_event_cards(parent))

        return {"data": {"status": status, "segments": events}}

    return await cache_manager.get_or_create_async(CACHE_TTL_EVENTS, build, *cache_key)


@handle_scraper_errors
async def vlr_event_matches(event_id: str, theme: str | None = None):
    """Get match list for a specific event from VLR.GG.

    Args:
        event_id: The numeric event ID from vlr.gg
        theme: Optional theme preference.

    Returns:
        dict: Response with status code and match list data
    """
    cache_key = ("event_matches", event_id, theme)

    async def build():
        url = f"{VLR_BASE_URL}/event/matches/{event_id}"
        client = get_http_client()
        resp = await fetch_with_retries(url, client=client, theme=theme)
        status = resp.status_code
        if status >= 400:
            return upstream_error_payload(status, f"event matches {event_id}")

        html = HTMLParser(resp.text)

        matches = _parse_event_matches(html)
        return {"data": {"status": status, "segments": matches}}

    return await cache_manager.get_or_create_async(
        CACHE_TTL_EVENT_MATCHES, build, *cache_key
    )


@handle_scraper_errors
async def vlr_event_details(event_id: str, theme: str | None = None):
    """
    Scrape comprehensive event details from VLR.GG.
    Consolidates header info, teams, standings, and matches.

    Args:
        event_id: Numeric VLR.GG event identifier.
        theme: Optional theme preference.
    """
    # Use shorter TTL because it includes live matches
    cache_key = ("event_details", event_id, theme)

    async def build():
        base_url = f"{VLR_BASE_URL}/event/{event_id}"
        matches_url = f"{VLR_BASE_URL}/event/matches/{event_id}"
        client = get_http_client()

        # Fetch both the main event page and the matches page
        responses = await asyncio.gather(
            fetch_with_retries(base_url, client=client, theme=theme),
            fetch_with_retries(matches_url, client=client, theme=theme),
        )
        base_resp, matches_resp = responses
        status = base_resp.status_code

        if status >= 400:
            return {"data": {"status": status, "segments": []}}

        base_html = HTMLParser(base_resp.text)
        matches_html = HTMLParser(matches_resp.text)

        header_info = _parse_event_header(base_html)
        teams = _parse_event_teams(base_html)
        standings = _parse_event_standings(base_html)
        matches = _parse_event_matches(matches_html)

        segment = {
            "id": event_id,
            **header_info,
            "teams": teams,
            "standings": standings,
            "matches": matches
        }

        return {"data": {"status": status, "segments": [segment]}}

    return await cache_manager.get_or_create_async(
        CACHE_TTL_EVENT_MATCHES, build, *cache_key
    )
