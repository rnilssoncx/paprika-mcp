"""Utility functions for paprika-mcp server."""

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
import unicodedata
from typing import Any

from paprika_recipes.cache import DirectoryCache
from paprika_recipes.exceptions import PaprikaError, RequestError
from paprika_recipes.remote import Remote

logger = logging.getLogger(__name__)

# A pattern longer than this is refused before it is compiled. Python's `re`
# holds the GIL for the whole of a match, so a pathological pattern run against
# ~400 recipes stalls every thread in the process, the event loop included —
# running tools on worker threads does not help with that. A length cap does
# not make backtracking impossible, but it bounds how much of it a pattern can
# describe, and no sensible ingredient search comes near it.
MAX_PATTERN_LENGTH = 200


class CategoriesUnavailable(Exception):
    """Paprika's category list could not be fetched; the message says why.

    Raised rather than answered with an empty list, because an empty list reads
    as "this account has no categories" — which turns a failed request into
    "No recipes found in category X" and every category into `Unknown-xxxx`.
    """


class _SharedDirectoryCache(DirectoryCache):
    """The library's recipe cache, made safe for tool calls on several threads.

    Tool handlers now run on worker threads, so two calls can each build a
    Remote over the same cache directory at once. The library writes files in
    place and saves its whole in-memory index over the file, so concurrent calls
    could leave a half-written recipe or index on disk, and the last save would
    discard every entry the other call added. A torn index reads as empty, which
    costs a GET for every recipe on the next call.

    So both writes go to a temporary file renamed into place (atomic on POSIX),
    and a save merges with the index on disk under a lock rather than replacing
    it. A merge can leave the index naming an older version of a recipe than the
    newest one fetched — two calls fetching it either side of an edit — and with
    one `{uid}.json` per recipe the index would then vouch for whichever version
    was written last: a stale recipe served as current. So each version gets
    its own file, `{uid}.{hash}.json`, and an index entry can only ever lead to
    the data it was written with. Every interleaving of two writers costs at
    most a refetch.

    The library's `{uid}.json` files are not read, so the first run after this
    change refetches every recipe once; it removes them as it replaces them.
    """

    _lock = threading.Lock()
    _SAFE = re.compile(r"[0-9A-Za-z-]+")

    def _file(self, uid: str, hash: str) -> str | None:
        # Both come from Paprika and become a file name, so they must look
        # like ids; anything else is simply not cached.
        if self._SAFE.fullmatch(uid) and self._SAFE.fullmatch(hash):
            return f"{uid}.{hash}.json"
        return None

    def _write(self, name: str, payload: Any) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._root_path, prefix=f".{name}.")
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(payload, out)
            os.replace(tmp, self._root_path / name)
        except Exception as e:
            logger.exception("Error encountered while writing to cache: %s", e)
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    def store_in_cache(self, uid: str, hash: str, recipe: dict):
        name = self._file(uid, hash)
        if name is None:
            return
        previous = self.index.get(uid)
        self._write(name, recipe)
        self.index[uid] = hash
        stale = [f"{uid}.json"] + ([self._file(uid, previous)] if previous and previous != hash else [])
        for old in stale:
            if old:
                with contextlib.suppress(OSError):
                    os.unlink(self._root_path / old)

    def read_from_cache(self, uid: str, hash: str) -> dict:
        name = self._file(uid, hash)
        if name is None or not self.is_cached(uid, hash):
            return {}
        try:
            with open(self._root_path / name) as f:
                return json.load(f)
        except Exception:
            # Missing (another call replaced this version) or unreadable: a
            # miss, which the library answers by fetching the recipe.
            return {}

    def save(self):
        with self._lock:
            self._index = {**self._load_index(), **self.index}
            self._write("index.json", self.index)


def get_credentials() -> tuple[str, str]:
    """Get Paprika credentials from environment variables or config file.

    Priority:
    1. PAPRIKA_EMAIL and PAPRIKA_PASSWORD environment variables
    2. ~/.paprika-mcp/config.json file

    Raises:
        ValueError: If credentials are not configured
    """
    # Check environment variables first
    email = os.environ.get("PAPRIKA_EMAIL")
    password = os.environ.get("PAPRIKA_PASSWORD")

    if email and password:
        return email, password

    # Check config file
    config_path = os.path.expanduser("~/.paprika-mcp/config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
                email = config.get("email")
                password = config.get("password")
                if email and password:
                    return email, password
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to read config file: {e}")

    raise ValueError(
        "Paprika credentials not found. Set PAPRIKA_EMAIL and PAPRIKA_PASSWORD "
        "environment variables, or create ~/.paprika-mcp/config.json with "
        '{"email": "your@email.com", "password": "yourpassword"}'
    )


def get_user_agent() -> str | None:
    """Get User-Agent string from environment or config file.

    Priority:
    1. PAPRIKA_USER_AGENT environment variable
    2. user_agent field in ~/.paprika-mcp/config.json

    Note: If not configured here, the Remote class will auto-detect from
    the installed Paprika app.

    Returns:
        User-Agent string or None (which triggers auto-detection in Remote)
    """
    # Check environment variable first
    user_agent = os.environ.get("PAPRIKA_USER_AGENT")
    if user_agent:
        return user_agent

    # Check config file
    config_path = os.path.expanduser("~/.paprika-mcp/config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
                user_agent = config.get("user_agent")
                if user_agent:
                    return user_agent
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to read user_agent from config file: {e}")

    return None


def get_remote() -> Remote:
    """Get authenticated Remote instance using stored credentials.

    The Remote class uses a DirectoryCache to store recipe data locally:
    - Recipe metadata (list of UIDs/hashes) is always fetched fresh from the API
    - Individual recipe details are cached in ~/.paprika-mcp/cache/
    - Cached recipes are keyed by UID and validated by hash
    - If a recipe's hash matches the cache, the cached version is used
    - If hash differs or not cached, recipe is fetched from API and cached

    Note: Remote.recipes is a generator that makes API calls. If you need to
    iterate multiple times or access by index, convert to list first.

    Raises:
        ValueError: If credentials are not configured
        PaprikaError: If authentication fails (check credentials)
        RequestError: If API request fails (network/server issue)
    """
    email, password = get_credentials()
    user_agent = get_user_agent()

    # Use cache to avoid re-downloading recipes
    cache_dir = os.path.expanduser("~/.paprika-mcp/cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache = _SharedDirectoryCache(cache_dir)

    try:
        # Use 30 second timeout to prevent hanging on network issues
        remote = Remote(email, password, cache=cache, user_agent=user_agent, timeout=30)
        # Test authentication by accessing bearer_token
        _ = remote.bearer_token
        return remote
    except (PaprikaError, RequestError) as e:
        api_msg = str(e) or "unknown error"
        raise type(e)(
            f"Paprika authentication failed: {api_msg}. "
            "Verify email and password in ~/.paprika-mcp/config.json "
            "or PAPRIKA_EMAIL/PAPRIKA_PASSWORD environment variables."
        ) from e
    except Exception as e:
        logger.error(f"Failed to connect to Paprika API: {e}", exc_info=True)
        raise


def get_categories(remote: Remote) -> dict[str, Any]:
    """Fetch Paprika's categories, fresh on every call.

    Returns a dict with:
    - 'uid_to_name': mapping of UUID to category name
    - 'name_to_uid': mapping of lowercase name to UUID
    - 'all': list of all category dicts
    - 'by_uid': mapping of UUID to full category dict

    This used to be cached for the life of the process, which on a server that
    runs for weeks meant a category added in the app did not exist until the
    next deploy: search answered "No recipes found" for it and update_recipe
    rendered it as `Unknown-xxxx`. It is one small GET, so each tool call makes
    it once and passes the result down rather than caching it.

    It goes through the Remote's own request method, as the meal planner's
    collections do, so it carries the app's User-Agent and the library's
    retries and error parsing.

    Raises:
        CategoriesUnavailable: the request failed, so the categories are unknown
            — never an empty result standing in for one.
    """
    try:
        categories = remote._request("get", "/api/v2/sync/categories/").json().get("result", [])
    except Exception as e:
        logger.warning(f"Failed to fetch categories: {e}")
        raise CategoriesUnavailable(f"Could not fetch categories from Paprika ({type(e).__name__}: {e})") from e

    uid_to_name = {}
    name_to_uid = {}
    by_uid = {}
    for cat in categories:
        uid = cat["uid"]
        name = cat.get("name", "")
        if name:
            uid_to_name[uid] = name
            name_to_uid[name.lower()] = uid
            by_uid[uid] = cat

    return {
        "uid_to_name": uid_to_name,
        "name_to_uid": name_to_uid,
        "all": categories,
        "by_uid": by_uid,
    }


def translate_category_uids(uids: list[str], uid_to_name: dict[str, str]) -> str:
    """Translate a list of category UUIDs to comma-separated names.

    Takes the map rather than fetching it, so a search over every recipe makes
    one categories request, not one per recipe.

    Args:
        uids: List of category UUIDs
        uid_to_name: the 'uid_to_name' map from get_categories

    Returns:
        Comma-separated string of category names
    """
    if not uids:
        return ""

    names = [uid_to_name.get(uid, f"Unknown-{uid[:8]}") for uid in uids]
    return ", ".join(names)


def notify_apps(remote: Remote) -> str:
    """Ask Paprika to tell the apps something changed; say so if it could not.

    Called only after a write has succeeded, so a failure here must not be
    reported as a failed write — the change is in Paprika and the apps will show
    it at their next sync. Returns "" when the notify went through, otherwise a
    sentence to append to the tool's answer.
    """
    try:
        remote.notify()
    except Exception as e:
        logger.warning(f"Notify failed after a successful write: {e}")
        return (
            f"\n\nThe change is saved in Paprika, but the apps could not be told to refresh "
            f"({type(e).__name__}: {e}); they will show it at their next sync."
        )
    return ""


def compile_pattern(query: str) -> re.Pattern:
    """Compile a user's search pattern, or raise ValueError saying why not.

    Raised as ValueError with the answer as its message, so an invalid pattern
    reaches the member as "invalid pattern" — the old behaviour of returning no
    matches read as "No recipes found matching ...", a confident no for a
    question that was never asked.
    """
    if len(query) > MAX_PATTERN_LENGTH:
        raise ValueError(
            f"Error: Regex pattern is {len(query)} characters; the limit is {MAX_PATTERN_LENGTH}."
        )
    try:
        return re.compile(query, re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"Error: Invalid regex pattern '{query}': {e}") from None


def fold(text: str) -> str:
    """Text as a search compares it: without accents, case-folded.

    Recipes are clipped from the web with their accents ("Gruyère", "crème
    fraîche") and nobody types them, Claude included, so an exact comparison
    answered "No recipes found" for gruyere while the French Onion Soup called
    for it. Decomposing (NFKD) and dropping the combining marks folds é to e
    and leaves letters without one alone; folding is per character, so line
    numbers and context are unaffected.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def normalize_string(text: str) -> str:
    """Normalize a title for comparison, ignoring accents and case (see fold).

    Two live recipes whose titles fold to the same string are refused as
    ambiguous by read_recipe, so ignoring accents cannot pick one silently.
    """
    return fold(text)


def search_in_text(
    text: str, query: str | re.Pattern, context_lines: int = 2, regex: bool = False
) -> list[dict[str, Any]]:
    """Search for query in text and return matches with context.

    Args:
        text: Text to search in
        query: Search query (plain text, or a pattern from compile_pattern)
        context_lines: Number of lines of context around matches
        regex: If True, query is a compiled pattern. The caller compiles it
            once, through compile_pattern, so an invalid one is refused before
            the search rather than swallowed here as "no matches".

    Returns list of dicts with 'line', 'match', and 'context' keys.
    """
    if not text:
        return []

    matches = []
    lines = text.split("\n")

    if regex:
        pattern = query if isinstance(query, re.Pattern) else compile_pattern(query)
        for i, line in enumerate(lines):
            # Tried on the folded line as well, so 'gruyere|emmental' finds
            # "Gruyère" without the pattern having to spell the accent.
            if pattern.search(line) or pattern.search(fold(line)):
                # Get context lines before and after
                start = max(0, i - context_lines)
                end = min(len(lines), i + context_lines + 1)
                context = "\n".join(lines[start:end])

                matches.append(
                    {"line": i + 1, "match": line.strip(), "context": context}
                )
    else:
        query_folded = fold(query)
        for i, line in enumerate(lines):
            if query_folded in fold(line):
                # Get context lines before and after
                start = max(0, i - context_lines)
                end = min(len(lines), i + context_lines + 1)
                context = "\n".join(lines[start:end])

                matches.append(
                    {"line": i + 1, "match": line.strip(), "context": context}
                )

    return matches
