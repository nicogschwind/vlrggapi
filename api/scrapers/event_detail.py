"""
Scraper for individual VLR.GG event detail pages.

Extracts event header info, prize pool breakdown, participating teams,
and group/stage standings tables. Consolidates match results when available.
"""
import asyncio
import logging

from selectolax.parser import HTMLParser

from utils.cache_manager import cache_manager
from utils.constants import VLR_BASE_URL, CACHE_TTL_EVENTS
from utils.error_handling import handle_scraper_errors, raise_for_upstream_status
from utils.html_parsers import (
    extract_text_content,
    normalize_image_url,
    parse_href_id_slug,
    build_full_url,
    parse_match_timestamp,
)
from utils.http_client import fetch_with_retries, get_http_client
from utils.id_mapper import id_mapper

logger = logging.getLogger(__name__)


def _parse_event_header(html: HTMLParser) -> dict:
    """Extract event name, series, dates, prize pool, location, and logo."""
    series = ""
    name = ""
    subtitle = ""
    dates = ""
    prize = ""
    location = ""
    logo = ""

    header = html.css_first(".event-header") or html.css_first(".wf-card")
    if not header:
        return {"name": name, "series": series, "subtitle": subtitle,
                "dates": dates, "prize": prize, "location": location, "logo": logo}

    logo_elem = header.css_first(".event-header-thumb img") or header.css_first("img")
    if logo_elem:
        logo = normalize_image_url(logo_elem.attributes.get("src", ""))

    # Series link (first anchor inside .event-desc-inner before the h1)
    desc_inner = header.css_first(".event-desc-inner")
    if desc_inner:
        series_link = desc_inner.css_first("a")
        if series_link:
            series = extract_text_content(series_link)

    # Event name
    title = header.css_first("h1.wf-title")
    if title:
        name = extract_text_content(title)

    # Subtitle
    sub = header.css_first(".event-desc-subtitle")
    if sub:
        subtitle = extract_text_content(sub)

    # Key-value items (dates, prize, location)
    for item in header.css(".event-desc-item"):
        label_elem = item.css_first(".event-desc-item-label")
        value_elem = item.css_first(".event-desc-item-value")
        if not label_elem or not value_elem:
            continue
        label = extract_text_content(label_elem).rstrip(":")
        value = extract_text_content(value_elem)
        if label.lower() == "dates":
            dates = value
        elif label.lower() == "prize":
            prize = value
        elif label.lower() in ("location", "venue"):
            location = value

    return {
        "name": name,
        "series": series,
        "subtitle": subtitle,
        "dates": dates,
        "prize": prize,
        "location": location,
        "logo": logo,
    }


def _parse_prizes(html: HTMLParser) -> list[dict]:
    """Parse the prize breakdown table from the event page."""
    prizes: list[dict] = []

    prize_card = html.css_first(".wf-card.mod-dark")
    if not prize_card:
        return prizes

    # The prize table uses div.wf-ptable with div.row elements
    ptable = prize_card.css_first(".wf-ptable")
    if not ptable:
        return prizes

    rows = ptable.css(".row")
    for row in rows[1:]:  # skip header row
        cells = row.css(".cell")
        if len(cells) < 3:
            continue

        placement = extract_text_content(cells[0])
        amount = extract_text_content(cells[1])

        # Team cell: contains an <a> link with name and optional region
        team_name = ""
        team_id = ""
        team_logo = ""
        team_region = ""

        team_link = cells[2].css_first("a")
        if team_link:
            href = team_link.attributes.get("href", "")
            team_id, _ = parse_href_id_slug(href)
            name_div = team_link.css_first(".text-of")
            if name_div:
                region_div = name_div.css_first(".ge-text-light")
                if region_div:
                    team_name = extract_text_content(name_div).replace(extract_text_content(region_div), "").strip()
                    team_region = extract_text_content(region_div)
                else:
                    team_name = extract_text_content(name_div)
            else:
                team_name = extract_text_content(team_link)
            img = team_link.css_first("img")
            if img:
                team_logo = normalize_image_url(img.attributes.get("src", ""))

        prizes.append({
            "placement": placement,
            "amount": amount,
            "team": {
                "id": team_id,
                "name": team_name,
                "logo": team_logo,
                "region": team_region,
            },
        })
        id_mapper.register_team(team_name, team_id)

    return prizes


def _parse_event_teams(html: HTMLParser) -> list[dict]:
    """Parse participating teams from event-team cards."""
    teams: list[dict] = []

    for card in html.css(".wf-card.event-team"):
        # Team name link
        name_link = card.css_first(".event-team-name")
        team_name = ""
        team_id = ""
        if name_link:
            team_name = extract_text_content(name_link)
            href = name_link.attributes.get("href", "")
            team_id, _ = parse_href_id_slug(href)

        # Players
        players: list[dict] = []
        for player_link in card.css(".event-team-players-item"):
            href = player_link.attributes.get("href", "")
            p_id, _ = parse_href_id_slug(href)
            p_name = extract_text_content(player_link)
            # Parse flag class (e.g. "flag mod-us")
            flag = ""
            flag_elem = player_link.css_first(".flag")
            if flag_elem:
                flag_class = flag_elem.attributes.get("class", "")
                flag = flag_class.replace("flag ", "").replace(" mod-", "_")
            players.append({"id": p_id, "name": p_name, "flag": flag})

        # Qualification note
        note = ""
        note_elem = card.css_first(".event-team-note")
        if note_elem:
            note_link = note_elem.css_first("a")
            if note_link:
                note = extract_text_content(note_link)

        teams.append({
            "id": team_id,
            "name": team_name,
            "players": players,
            "qualification": note,
        })
        id_mapper.register_team(team_name, team_id)
        for p in players:
            id_mapper.register_team(p["name"], "")

    return teams


def _parse_standings(html: HTMLParser) -> list[dict]:
    """Parse group/stage standings tables from the event page.

    VLR uses div.wf-ptable elements inside .wf-card containers for standings.
    Handles variable column counts (3-column groups, 5/6-column full tables).
    """
    standings: list[dict] = []

    for card in html.css(".wf-card"):
        ptable = card.css_first(".wf-ptable")
        if not ptable:
            continue
        # Skip the prize table (already handled in _parse_prizes)
        parent_classes = card.attributes.get("class", "")
        if "mod-dark" in parent_classes:
            continue

        # Check for a stage/group label before the table
        stage = ""
        label_elem = card.css_first(".wf-label") or card.css_first("h2")
        if label_elem:
            stage = extract_text_content(label_elem)

        # Parse headers
        header_row = ptable.css_first(".row")
        if not header_row:
            continue
        headers: list[str] = []
        for cell in header_row.css(".cell"):
            headers.append(extract_text_content(cell))

        if not headers:
            continue

        # Parse data rows
        rows: list[dict[str, str]] = []
        for row in ptable.css(".row")[1:]:
            cells = row.css(".cell")
            if len(cells) < 1:
                continue
            row_data: dict[str, str] = {}
            for idx, cell in enumerate(cells):
                label = headers[idx] if idx < len(headers) else str(idx)
                # Team column may have an anchor with name
                team_link = cell.css_first("a")
                if team_link and idx == 0:
                    row_data[label] = extract_text_content(team_link)
                else:
                    row_data[label] = extract_text_content(cell)
            rows.append(row_data)

        standings.append({"stage": stage, "columns": headers, "rows": rows})

    return standings


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


@handle_scraper_errors
async def vlr_event_detail(event_id: str, theme: str | None = None) -> dict:
    """Fetch full event detail: header, prizes, teams, standings, and matches.

    Args:
        event_id: Numeric VLR.GG event ID.
        theme: Optional theme preference.
    """
    async def build():
        base_url = f"{VLR_BASE_URL}/event/{event_id}"
        matches_url = f"{VLR_BASE_URL}/event/matches/{event_id}"
        client = get_http_client()

        # Fetch both the main event page and the matches page concurrently
        responses = await asyncio.gather(
            fetch_with_retries(base_url, client=client, theme=theme),
            fetch_with_retries(matches_url, client=client, theme=theme),
        )
        base_resp, matches_resp = responses
        status = base_resp.status_code
        raise_for_upstream_status(status, f"event detail {event_id}")

        base_html = HTMLParser(base_resp.text)
        matches_html = HTMLParser(matches_resp.text)

        header = _parse_event_header(base_html)
        prizes = _parse_prizes(base_html)
        teams = _parse_event_teams(base_html)
        standings = _parse_standings(base_html)
        matches = _parse_event_matches(matches_html)

        data = {
            "data": {
                "status": status,
                "segments": {
                    "event": header,
                    "prizes": prizes,
                    "teams": teams,
                    "standings": standings,
                    "matches": matches,
                },
            }
        }
        return data

    return await cache_manager.get_or_create_async(
        CACHE_TTL_EVENTS, build, "event_detail", event_id, theme
    )
